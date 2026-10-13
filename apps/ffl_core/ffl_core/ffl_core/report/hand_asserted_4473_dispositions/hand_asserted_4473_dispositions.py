"""Hand-Asserted 4473 Dispositions — the review queue for every bound-book
entry a human vouched for instead of FastBound.

Two doors write these (both stamp ``manual_override=1``): a manager forcing a
stuck electronic 4473 through (``manager_override_4473``), and the counter
recording a paper 4473 in Paper 4473 Mode (``attest_paper_4473``). Both are
legitimate — the point of this report is that they are legitimate ONLY while
someone periodically reads them back: who asserted, why, for which gun, and
whether FastBound ever caught up (``fastbound_id`` stays empty for paper-mode
entries until a reconcile runs with the integration back on).

Requested by an accountant audit of the integration switches: a
control that lets non-managers satisfy the 4473 gate needs a standing review
surface, or "audited" just means "written down where nobody looks".

The Invoice column reads ``linked_pos_invoice`` — the field both writers
actually stamp. It asked for ``sales_invoice`` until 2026-09-03, which no
hand-assertion has ever carried, so the column was blank on every row in
production for the whole life of the report. A reviewer reads a blank as "no
invoice attached", not "the report is asking the wrong question", which is
what made it worth a test rather than a one-line edit.
"""
from __future__ import annotations

import frappe
from frappe import _


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return _columns(), _rows(filters)


def _columns():
	return [
		{"fieldname": "creation", "label": _("Asserted On"), "fieldtype": "Datetime", "width": 165},
		{"fieldname": "name", "label": _("Disposition"), "fieldtype": "Link",
			"options": "FFL Disposition", "width": 160},
		{"fieldname": "serial", "label": _("Serial"), "fieldtype": "Link",
			"options": "Serial No", "width": 130},
		{"fieldname": "disposition_type", "label": _("Type"), "fieldtype": "Data", "width": 110},
		{"fieldname": "invoice", "label": _("Invoice"), "fieldtype": "Data", "width": 170},
		{"fieldname": "override_by", "label": _("Asserted By"), "fieldtype": "Link",
			"options": "User", "width": 160},
		{"fieldname": "override_reason", "label": _("Reason"), "fieldtype": "Data", "width": 320},
		{"fieldname": "fastbound_id", "label": _("FastBound ID"), "fieldtype": "Data", "width": 130},
	]


def _rows(filters):
	conditions = {"manual_override": 1, "docstatus": 1}
	if filters.get("from_date"):
		conditions["creation"] = [">=", filters.from_date]
	rows = frappe.get_all(
		"FFL Disposition",
		filters=conditions,
		fields=["creation", "name", "serial", "disposition_type",
			"linked_pos_invoice", "sales_invoice",
			"override_by", "override_reason", "fastbound_id"],
		order_by="creation desc",
		limit=500,
	)
	for row in rows:
		# A hand-asserted 4473 is always rung at the counter, so in practice it
		# is `linked_pos_invoice` that is set — `_book_hand_asserted_4473` (the
		# single booking path behind BOTH manager_override_4473 and
		# attest_paper_4473) writes only that field, and asking for
		# `sales_invoice` is what made this column blank on every row in
		# production. `sales_invoice` is kept as a fallback rather than dropped:
		# it is the field the ordinary (non-asserted) disposition writers in
		# api/manual_order.py use, so a future hand-assertion booked off a Sales
		# Invoice still names its sale here instead of silently going blank
		# again.
		pos_invoice = row.pop("linked_pos_invoice", None)
		sales_invoice = row.pop("sales_invoice", None)
		row["invoice"] = pos_invoice or sales_invoice
	return rows
