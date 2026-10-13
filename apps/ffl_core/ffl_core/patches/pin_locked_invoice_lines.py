"""Retire the auto-inserted Item Prices behind a past incident
and list the drafts carrying a locked line that has drifted.

1. setup/locked_lines.ensure(): the ffl_locked_rate row field, live Item Prices
   for the locked items deleted (prod: TRADE-IN-CREDIT / Standard Selling =
   -869 since 2026-06-27), expired tombstones in their place.
2. Draft POS / Sales Invoices whose locked line no longer matches its amount
   are LISTED, not written: compliance/locked_lines pins them on their next
   save (before_validate), through ERPNext's own save — a write from here would
   leave its per-item tax detail stale. Prod had no such draft on 2026-10-03.
Idempotent.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt


def execute():
	from ffl_core.compliance.locked_lines import expected_rate, rate_precision
	from ffl_core.setup import locked_lines as setup

	print(f"pin_locked_invoice_lines: item prices {setup.ensure()}")
	drifted = []
	for doctype in ("POS Invoice", "Sales Invoice"):
		names = frappe.get_all(
			f"{doctype} Item",
			filters={"docstatus": 0, "parenttype": doctype, "item_code": ["in", setup.locked_items()]},
			pluck="parent", distinct=True,
		)
		p = rate_precision(doctype)
		for name in names:
			try:
				doc = frappe.get_doc(doctype, name)
				for row in doc.items:
					want = expected_rate(doc, row)
					if want and flt(row.rate, p) != flt(want, p):
						drifted.append(f"{name} row {row.idx}: {flt(row.rate, p)} -> {flt(want, p)}")
			except Exception:
				frappe.log_error(title=f"pin_locked_invoice_lines: {doctype} {name} unreadable",
					message=frappe.get_traceback())
	print(f"pin_locked_invoice_lines: drafts to re-save ({len(drifted)}): {drifted}")
