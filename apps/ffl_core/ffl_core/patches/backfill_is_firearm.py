"""One-shot patch — set is_firearm=1 on every Item currently in a firearm
Item Group. After this, `is_firearm` is the single source of truth.

Idempotent: items already at is_firearm=1 are skipped. If the Custom Field
column hasn't been added yet (fresh-clone migrate before fixtures install),
the patch sync-installs it first.
"""
from __future__ import annotations

import frappe

from ffl_core.firearm import ALL_FIREARM_GROUPS
from ffl_core.patches._helpers import ensure_field_column


def execute():
	if not ensure_field_column("Item", "is_firearm", {
		"label": "Firearm",
		"fieldtype": "Check",
		"default": "0",
		"insert_after": "item_group",
		"in_list_view": 1,
		"in_standard_filter": 1,
	}):
		print("backfill_is_firearm: Item.is_firearm column not present, skipping")
		return

	groups = list(ALL_FIREARM_GROUPS)
	rows = frappe.get_all(
		"Item",
		filters={"item_group": ("in", groups), "is_firearm": 0},
		pluck="name",
	)
	if not rows:
		return
	print(f"backfill_is_firearm: stamping {len(rows)} item(s)")
	for name in rows:
		frappe.db.set_value("Item", name, "is_firearm", 1, update_modified=False)
