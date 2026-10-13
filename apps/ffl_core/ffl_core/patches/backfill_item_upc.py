"""One-shot patch — populate the new Item.upc field from existing UPC rows
in the Item Barcode child table, so previously-imported/received items show
their UPC in the single field immediately (without waiting for a re-save).

Idempotent: only fills items whose upc is empty.
"""
from __future__ import annotations

import frappe

from ffl_core.patches._helpers import ensure_field_column


def execute():
	if not ensure_field_column("Item", "upc", {
		"label": "UPC",
		"fieldtype": "Data",
		"insert_after": "item_name",
		"in_standard_filter": 1,
	}):
		print("backfill_item_upc: Item.upc column not present, skipping")
		return

	rows = frappe.db.sql(
		"""
		SELECT b.parent AS item, b.barcode AS upc
		FROM `tabItem Barcode` b
		JOIN `tabItem` i ON i.name = b.parent
		WHERE UPPER(b.barcode_type) = 'UPC'
		  AND b.barcode IS NOT NULL AND b.barcode != ''
		  AND (i.upc IS NULL OR i.upc = '')
		GROUP BY b.parent
		""",
		as_dict=True,
	)
	if not rows:
		return
	print(f"backfill_item_upc: filling {len(rows)} item(s)")
	for r in rows:
		frappe.db.set_value("Item", r.item, "upc", r.upc, update_modified=False)
