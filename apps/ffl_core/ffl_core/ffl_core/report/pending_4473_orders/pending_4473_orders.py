"""Pending 4473 Orders — the parked-firearm-order queue.

Every Draft POS Invoice with a firearm line, with its 4473/NICS state, how
long it has been parked, and the Brady date (3rd business day after the NICS
check started — if NICS is still Delayed past it, federal law lets the dealer
transfer at their discretion). This is the queue the operator works every
morning: ring Delayed customers whose Brady date arrived, clean up Denied
orders, chase 4473s nobody finished.

Posting date stands in for the NICS-check date: the 4473 is kicked off from
the same cart session that creates the draft, so they coincide in practice.

The "FB Pending Disp." column is the other half of DISP-2026-02000: an
abandoned 4473 leaves an UNCOMMITTED disposition holding the item inside
FastBound, invisible from here, and the next real disposition for that gun 400s
against it. It is behind a filter because filling it costs one FastBound round
trip per gun and the morning queue is read far more often than it is
investigated.
"""
from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, nowdate

from ffl_core.serials import serials_for_lines

# ffl_integrations registers the FastBound side: fn(serials) -> {serial: {...}}.
# A hook rather than an import because ffl_core has to run on a site with no
# integration app at all (see ffl_core.alerts on why the dependency only points
# one way).
PENDING_DISPOSITION_PROBE_HOOK = "ffl_pending_disposition_probe"


def add_business_days(start: date, days: int) -> date:
	"""`days` business days after `start` (Mon–Fri; a weekend start counts
	from the next business day, matching the Brady-clock convention)."""
	current = getdate(start)
	remaining = days
	while remaining > 0:
		current = current + timedelta(days=1)
		if current.weekday() < 5:  # Mon=0 … Fri=4
			remaining -= 1
	return current


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return _columns(), _rows(filters)


def _columns():
	return [
		{"label": _("Invoice"), "fieldname": "pos_invoice", "fieldtype": "Link",
			"options": "POS Invoice", "width": 170},
		{"label": _("Parked On"), "fieldname": "parked_on", "fieldtype": "Date", "width": 100},
		{"label": _("Days Pending"), "fieldname": "days_pending", "fieldtype": "Int", "width": 100},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link",
			"options": "Customer", "width": 170},
		{"label": _("Firearms"), "fieldname": "serials", "fieldtype": "Data", "width": 200},
		{"label": _("4473"), "fieldname": "form_4473_status", "fieldtype": "Data", "width": 90},
		{"label": _("NICS"), "fieldname": "nics_status", "fieldtype": "Data", "width": 90},
		{"label": _("Brady Day 3"), "fieldname": "brady_date", "fieldtype": "Date", "width": 100},
		{"label": _("Total"), "fieldname": "grand_total", "fieldtype": "Currency", "width": 110},
		{"label": _("Transfer?"), "fieldname": "is_ffl_transfer", "fieldtype": "Check", "width": 80},
		{"label": _("FB Pending Disp."), "fieldname": "fb_pending_disposition",
			"fieldtype": "Data", "width": 220},
	]


def fb_pending_dispositions(serials: list[str]) -> dict[str, str]:
	"""``{serial: "<disposition id> (item #<n>)"}`` for guns FastBound has parked
	on an UNCOMMITTED disposition.

	That is what an abandoned 4473 leaves behind, and it is invisible from POS:
	the gun still reads Available in the bound book, so nothing here objects,
	while the next real disposition for it fails with
	``has already been added to another disposition``.

	Never raises: this decorates a report an operator is reading, and FastBound
	being unreachable must cost them a blank column, not the queue.
	"""
	if not serials:
		return {}
	out: dict[str, str] = {}
	for path in frappe.get_hooks(PENDING_DISPOSITION_PROBE_HOOK):
		try:
			found = frappe.get_attr(path)(serials) or {}
		except Exception:  # noqa: BLE001 — FastBound down / unconfigured / absent
			frappe.log_error(
				title="Pending 4473 Orders: FastBound pending-disposition probe failed",
				message=frappe.get_traceback())
			continue
		for serial, info in found.items():
			disp_id = (info or {}).get("disposition_id") or ""
			if not disp_id:
				continue
			# The probe's contract is {disposition_id, item_number}. This used to
			# read a "status" key no provider ever sent — dead code — while
			# dropping the item number, which is what the operator types into
			# FastBound's search box to reach the hold.
			item_number = (info or {}).get("item_number") or ""
			out[serial] = f"{disp_id} (item #{item_number})" if item_number else disp_id
	return out


def _rows(filters):
	conditions = ["pi.docstatus = 0", "COALESCE(pi.is_return, 0) = 0"]
	params = {}
	if filters.get("nics_status"):
		conditions.append("COALESCE(pi.nics_status, '') = %(nics_status)s")
		params["nics_status"] = filters.nics_status

	# One row per firearm LINE, folded to one row per invoice below. The serials
	# are resolved in Python by ffl_core.serials.serials_for_lines rather than
	# GROUP_CONCAT'ed in SQL, because a line stores them in either of two places:
	# newline-joined in serial_no, or in a Serial and Batch Bundle (the v16
	# default, and the shape a GROUP_CONCAT(pii.serial_no) reads as NULL — the
	# queue then showed a blank Firearms column and the FastBound probe had
	# nothing to ask about). Same two-column + shared-helper shape as
	# ffl_core.reservations._draft_pos_serials.
	lines = frappe.db.sql(
		f"""
		SELECT pi.name AS pos_invoice, pi.posting_date, pi.customer,
		       COALESCE(pi.form_4473_status, '') AS form_4473_status,
		       COALESCE(pi.nics_status, '') AS nics_status,
		       COALESCE(pi.is_ffl_transfer, 0) AS is_ffl_transfer,
		       pi.grand_total,
		       pii.serial_no, pii.serial_and_batch_bundle
		FROM `tabPOS Invoice` pi
		INNER JOIN `tabPOS Invoice Item` pii ON pii.parent = pi.name
		INNER JOIN `tabItem` i ON i.name = pii.item_code AND i.is_firearm = 1 AND COALESCE(i.is_antique, 0) = 0
		WHERE {" AND ".join(conditions)}
		ORDER BY pi.posting_date ASC, pi.name, pii.idx
		""",
		params,
		as_dict=True,
	)
	today = getdate(nowdate())
	rows: list[dict] = []
	serials_by_invoice: dict[str, list[str]] = {}
	for line, line_serials in zip(lines, serials_for_lines(lines)):
		name = line["pos_invoice"]
		if name not in serials_by_invoice:
			serials_by_invoice[name] = []
			parked = getdate(line["posting_date"])
			rows.append({
				"pos_invoice": name,
				"customer": line["customer"],
				"form_4473_status": line["form_4473_status"],
				"nics_status": line["nics_status"],
				"is_ffl_transfer": line["is_ffl_transfer"],
				"grand_total": line["grand_total"],
				"parked_on": parked,
				"days_pending": date_diff(today, parked),
				"brady_date": add_business_days(parked, 3),
				"fb_pending_disposition": "",
			})
		for serial in line_serials:
			if serial not in serials_by_invoice[name]:
				serials_by_invoice[name].append(serial)
	for r in rows:
		r["serials"] = ", ".join(serials_by_invoice[r["pos_invoice"]])
	if filters.get("check_fastbound"):
		held = fb_pending_dispositions(sorted({
			s for serials in serials_by_invoice.values() for s in serials}))
		for r in rows:
			r["fb_pending_disposition"] = "; ".join(
				f"{s}: {held[s]}" for s in serials_by_invoice[r["pos_invoice"]]
				if s in held)
	return rows
