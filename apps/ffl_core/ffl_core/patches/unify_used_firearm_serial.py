"""One-shot patch — unify Used Firearm to has_serial_no=1 + Serial No records.

ERPNext refuses to create a Serial No directly with a warehouse; it must
come via a stock movement. For each legacy Item (has_serial_no=0 +
firearm_serial set) we cancel its Material Receipts, flip the flag, and
re-receive with the serial so ERPNext registers a Serial No record.

Idempotent — re-runs skip items already at has_serial_no=1.
"""
from __future__ import annotations

import frappe

from ffl_core.firearm import serialized_receipt_row


def execute():
	items = frappe.get_all(
		"Item",
		filters={"has_serial_no": 0, "firearm_serial": ("is", "set")},
		fields=["name", "firearm_serial"],
	)
	if not items:
		return

	print(f"unify_used_firearm_serial: {len(items)} item(s) to migrate")
	for item in items:
		try:
			_migrate_one(item.name, item.firearm_serial)
		except Exception as exc:  # noqa: BLE001 — keep going on per-item failure
			frappe.log_error(
				title=f"unify_used_firearm_serial failed for {item.name}",
				message=str(exc),
			)


def _migrate_one(item_code: str, serial: str) -> None:
	serial = (serial or "").strip()
	if not serial:
		frappe.db.set_value("Item", item_code, "has_serial_no", 1,
			update_modified=False)
		return

	# Same serial already bound to a different item is a data error (firearm
	# serials are globally unique by ATF rule); log so the operator can
	# reconcile, then flip the flag and bail.
	existing_sn_item = frappe.db.get_value("Serial No", serial, "item_code")
	if existing_sn_item:
		if existing_sn_item != item_code:
			frappe.log_error(
				title="unify_used_firearm_serial: serial collision",
				message=(f"Item {item_code} has firearm_serial={serial!r}, but a "
					f"Serial No record with that value already exists bound to "
					f"item {existing_sn_item!r}. Skipping — please reconcile."),
			)
		frappe.db.set_value("Item", item_code, "has_serial_no", 1,
			update_modified=False)
		return

	receipts = frappe.get_all(
		"Stock Entry Detail",
		filters={"item_code": item_code, "docstatus": 1},
		fields=["parent", "qty", "t_warehouse", "basic_rate"],
	)
	for r in receipts:
		se = frappe.get_doc("Stock Entry", r.parent)
		if se.docstatus == 1 and se.stock_entry_type == "Material Receipt":
			se.flags.ignore_permissions = True
			se.cancel()

	frappe.db.set_value("Item", item_code, "has_serial_no", 1,
		update_modified=False)

	if not receipts:
		return
	r = receipts[0]
	new_se = frappe.get_doc({
		"doctype": "Stock Entry",
		"stock_entry_type": "Material Receipt",
		"company": frappe.db.get_value("Warehouse", r.t_warehouse, "company"),
		"items": [serialized_receipt_row(
			item_code=item_code, qty=r.qty, warehouse=r.t_warehouse,
			basic_rate=r.basic_rate or 0.01, serial=serial,
		)],
		"remarks": "Backfill: unify_used_firearm_serial migration",
	})
	new_se.flags.ignore_permissions = True
	new_se.insert()
	new_se.submit()
