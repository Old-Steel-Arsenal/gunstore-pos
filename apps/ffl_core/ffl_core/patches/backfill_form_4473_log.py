"""Backfill the Form 4473 Log from the POS Invoices that already carry a
completed 4473 — the record the webhook and the paper / override path write
from now on (ffl_core Form 4473 Log).

One row per 4473: a FastBound 4473 is keyed by the form id in the invoice's
fastbound_4473_pdf_url, so an amendment or a Duplicate that carried the same
link lands on the same row (its invoice is added to the row's list, oldest
first). A 4473 without a link is paper / override when the invoice (or the
invoice it amends) has a hand-asserted Retail Sale disposition, else FastBound.
A cancelled original that was amended is skipped: its 4473 went with the
amendment. Local data only — the PDFs are copied by the hourly sweep
(ffl_integrations.fastbound.pdf_archive), never from inside migrate.
Idempotent.
"""
from __future__ import annotations

import frappe

from ffl_core.compliance.webhook_fields import form_4473_id
from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
from ffl_core.serials import serials_for_lines


def execute():
	lines = frappe.db.sql("""
		SELECT pi.name, pi.posting_date, pi.posting_time, pi.customer, pi.docstatus,
		       pi.amended_from, COALESCE(pi.nics_status, '') AS nics_status,
		       COALESCE(pi.fastbound_4473_pdf_url, '') AS pdf_url,
		       COALESCE(pi.fastbound_disposition_id, '') AS disposition_id,
		       pii.serial_no, pii.serial_and_batch_bundle
		FROM `tabPOS Invoice` pi
		INNER JOIN `tabPOS Invoice Item` pii ON pii.parent = pi.name
		INNER JOIN `tabItem` i ON i.name = pii.item_code AND i.is_firearm = 1 AND COALESCE(i.is_antique, 0) = 0
		WHERE COALESCE(pi.is_return, 0) = 0 AND pi.form_4473_status = 'Completed'
		ORDER BY pi.creation, pi.name, pii.idx""", as_dict=True)
	if not lines:
		return
	orders: dict[str, dict] = {}
	for line, serials in zip(lines, serials_for_lines(lines)):
		order = orders.setdefault(line.name, {**line, "serials": []})
		order["serials"] += [s for s in serials if s not in order["serials"]]

	names = list(orders)
	amended = set(frappe.get_all("POS Invoice", filters={"amended_from": ("in", names)},
		pluck="amended_from"))
	hand_asserted = set(frappe.get_all("FFL Disposition",
		filters={"manual_override": 1, "docstatus": 1, "linked_pos_invoice": ("is", "set")},
		pluck="linked_pos_invoice"))

	for name, order in orders.items():
		fid = form_4473_id(order["pdf_url"])
		if not fid and (
				(order["docstatus"] == 2 and name in amended)
				or frappe.db.exists("Form 4473 Log", {"pos_invoice": name})):
			continue
		hand = name in hand_asserted or (order["amended_from"] or "") in hand_asserted
		# One bad row (odd legacy data) is logged and skipped — never the migrate.
		frappe.db.savepoint("backfill_4473")
		try:
			_record(name, order, fid, hand)
		except Exception:  # noqa: BLE001 — see above
			frappe.db.rollback(save_point="backfill_4473")
			frappe.log_error(title=f"Form 4473 Log backfill skipped {name}",
				message=frappe.get_traceback())


def _record(name: str, order: dict, fid: str | None, hand: bool) -> None:
	record_4473(
		form4473_id=fid,
		pos_invoices=[name],
		customer=order["customer"],
		serials=order["serials"],
		nics_status=order["nics_status"],
		source="Paper / override" if hand and not fid else "FastBound",
		fastbound_disposition_id=order["disposition_id"],
		pdf_url=order["pdf_url"],
		completed_on=f"{order['posting_date']} {order['posting_time'] or '00:00:00'}",
	)
