"""One-shot patch — split the legacy "Long Gun" Item Group into Rifle + Shotgun.

The taxonomy moved shotguns out of "Long Gun" into their own group (see
ffl_core.setup.item_groups). This reassigns every Item still pinned to the
legacy "Long Gun" group: firearm_type == "Shotgun" → "Shotgun", everything
else (Rifle / Receiver / blank) → "Rifle".

Idempotent: re-running finds no Items left in "Long Gun". Calls
item_groups.ensure() first because patches run during migrate, before the
after_migrate hook that seeds the tree — so Rifle/Shotgun may not exist yet
on a fresh migrate.
"""
from __future__ import annotations

import frappe

from ffl_core.setup import item_groups


def execute():
	# Guarantee the target leaf groups exist (idempotent). Patches run before
	# the after_migrate item-group seed, so don't assume Rifle/Shotgun are there.
	item_groups.ensure()

	rows = frappe.get_all(
		"Item",
		filters={"item_group": "Long Gun"},
		fields=["name", "firearm_type"],
	)
	if not rows:
		return
	print(f"split_long_gun_item_group: reassigning {len(rows)} item(s) out of 'Long Gun'")
	for r in rows:
		target = "Shotgun" if (r.firearm_type or "").strip() == "Shotgun" else "Rifle"
		frappe.db.set_value("Item", r.name, "item_group", target, update_modified=False)
