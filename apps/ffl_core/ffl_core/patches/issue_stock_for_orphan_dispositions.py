"""One-shot — issue ERPNext stock for already-submitted FFL Dispositions whose
firearm is still Active in ERPNext.

Before this change, FFL Disposition.on_submit only enqueued the FastBound push
and never touched ERPNext stock. The 'Sync Bound Book' reconcile sweep was the
only path that cleared the divergence, run manually. This patch catches up the
historical orphans so the new auto-issue hook starts from a clean state.

Scope:
  - docstatus=1 (Submitted only — drafts and cancellations have no stock impact)
  - stock_entry IS NOT SET (idempotent: skip dispositions already linked)
  - disposition_type in the stock-issuing set (Retail Sale skipped — POS / SI
    already moved stock; the disposition is a downstream record)
  - Serial No.status='Active' AND warehouse is set (only firearms still in
    stock from ERPNext's point of view — anything else is already gone)
"""
from __future__ import annotations

import frappe


_STOCK_ISSUING_TYPES = (
	"FFL Transfer", "Dealer Transfer", "Return",
	"Scrap", "Destroyed",
	"Gunsmithing", "NFA",
)


def execute():
	# `FFL Disposition.stock_entry` is a Custom Field installed via fixtures,
	# which Frappe applies AFTER post_model_sync patches. Make the
	# stock_entry-IS-NULL predicate optional so this patch can run on either
	# side of that timing — the Serial-No-still-Active filter is the real gate.
	has_stock_entry_col = "stock_entry" in frappe.db.get_table_columns("FFL Disposition")
	stock_entry_clause = (
		"AND (d.stock_entry IS NULL OR d.stock_entry = '')"
		if has_stock_entry_col else ""
	)
	rows = frappe.db.sql(
		f"""
		SELECT d.name AS disp_name, d.disposition_type, d.serial,
		       sn.item_code, sn.warehouse
		FROM `tabFFL Disposition` d
		INNER JOIN `tabSerial No` sn ON sn.name = d.serial
		WHERE d.docstatus = 1
		  {stock_entry_clause}
		  AND d.disposition_type IN %(types)s
		  AND sn.status = 'Active'
		  AND sn.warehouse IS NOT NULL AND sn.warehouse <> ''
		""",
		{"types": _STOCK_ISSUING_TYPES},
		as_dict=True,
	)
	if not rows:
		print("issue_stock_for_orphan_dispositions: nothing to do")
		return

	# Inline the Material Issue here rather than importing the runtime helper,
	# so the patch is self-contained and a future refactor of
	# disposition_stock.py doesn't silently change the backfill semantics.
	from frappe import _  # noqa: F401

	issued = 0
	errors: list[dict] = []
	for r in rows:
		try:
			company = frappe.db.get_value("Warehouse", r.warehouse, "company")
			if not company:
				errors.append({"disposition": r.disp_name, "error": "warehouse has no company"})
				continue
			se = frappe.get_doc({
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Issue",
				"company": company,
				"remarks": (
					f"Backfilled by issue_stock_for_orphan_dispositions for "
					f"FFL Disposition {r.disp_name} ({r.disposition_type})."
				),
				"items": [{
					"item_code": r.item_code,
					"qty": 1,
					"s_warehouse": r.warehouse,
					"use_serial_batch_fields": 1,
					"serial_no": r.serial,
				}],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()
			if has_stock_entry_col:
				frappe.db.set_value(
					"FFL Disposition", r.disp_name, "stock_entry", se.name,
					update_modified=False,
				)
			frappe.db.commit()
			issued += 1
		except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
			frappe.db.rollback()
			errors.append({"disposition": r.disp_name, "error": str(exc)[:300]})

	print(
		f"issue_stock_for_orphan_dispositions: issued {issued} Stock Entry "
		f"(of {len(rows)} candidate orphan dispositions); {len(errors)} error(s)."
	)
	for e in errors:
		print(f"  - {e['disposition']}: {e['error']}")
