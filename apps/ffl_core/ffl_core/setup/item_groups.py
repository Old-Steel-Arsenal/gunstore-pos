"""Canonical Item Group taxonomy — RSR-department-aligned, product TYPE only.

Condition (new vs used) is the Item-level `is_used` checkbox, NOT a separate
group. Promotion files an Item under the catalog master's category
(ffl_integrations.catalog_master); the category mirror links these groups to the
master's tree nodes and, once linked, owns a non-firearm group's place in the tree
(a node moved in the catalog workbench re-parents its group) — so this seed only
heals the topology of the groups the mirror does not own.

Legacy leaves (LEGACY_LEAVES) are only kept in place where a site still has
them — never created, so an unused one retired by
patches/retire_legacy_item_groups stays gone.

Idempotent: creates the root + tree if missing, fixes topology if it drifted,
and rebuilds the nested-set pointers (lft/rgt) so the tree view + filters work.
Wired into ffl_core after_migrate, and reused by the dev/CI bootstrap.

  All Item Groups
  ├─ Firearm                (group)
  │  ├─ Pistol / Revolver / Rifle / Shotgun / Other Firearm / Receiver
  │  └─ NFA (group) → Suppressor / SBR / SBS / DD / MG / AOW
  ├─ Ammunition (group)     → Pistol Ammo / Rifle Ammo / Shotgun Ammo / Rimfire Ammo
  ├─ Accessory  (group)     → Optic / Magazine / Holster / Cleaning / Light / Knife / Safe / Misc Accessory
  │                           / Slings & Mounts / Sights / Optic Mounts
  └─ Parts      (group)     → Uppers / Barrels / Bolt Carrier Groups & Charging Handles / Triggers
                              / Lower Parts & Kits / Muzzle Devices / Handguards & Rails / Stocks & Grips
"""
from __future__ import annotations

import frappe
from frappe.utils.nestedset import rebuild_tree

from ffl_core.utils import commit

ROOT = "All Item Groups"

# (name, parent, is_group); parent "" => nested-set root. Order matters:
# every parent appears before its children so inserts resolve cleanly.
TREE: list[tuple[str, str, int]] = [
	(ROOT, "", 1),
	("Firearm", ROOT, 1),
	# Handgun split into Pistol / Revolver (2026-07-21) — both carry live stock
	# and stay distinct. "Handgun" is a legacy leaf (LEGACY_LEAVES).
	("Pistol", "Firearm", 0),
	("Revolver", "Firearm", 0),
	("Handgun", "Firearm", 0),
	("Rifle", "Firearm", 0),
	("Shotgun", "Firearm", 0),
	("Other Firearm", "Firearm", 0),
	("Receiver", "Firearm", 0),
	("NFA", "Firearm", 1),
	("Suppressor", "NFA", 0),
	("SBR", "NFA", 0),
	("SBS", "NFA", 0),
	("DD", "NFA", 0),
	("MG", "NFA", 0),
	("AOW", "NFA", 0),
	("Ammunition", ROOT, 1),
	("Pistol Ammo", "Ammunition", 0),
	("Rifle Ammo", "Ammunition", 0),
	("Shotgun Ammo", "Ammunition", 0),
	("Rimfire Ammo", "Ammunition", 0),
	("Accessory", ROOT, 1),
	("Optic", "Accessory", 0),
	("Magazine", "Accessory", 0),
	("Holster", "Accessory", 0),
	("Cleaning", "Accessory", 0),
	("Light", "Accessory", 0),
	("Knife", "Accessory", 0),
	("Safe", "Accessory", 0),
	("Misc Accessory", "Accessory", 0),
	# Website accessory sub-menus.
	("Slings & Mounts", "Accessory", 0),
	("Sights", "Accessory", 0),
	("Optic Mounts", "Accessory", 0),
	# Gun parts: the website's Parts menu. Not firearms —
	# a serialized receiver is a firearm and stays in Firearm (Rifle / Other
	# Firearm); handguards and stocks moved here from Accessory.
	("Parts", ROOT, 1),
	("Uppers", "Parts", 0),
	("Barrels", "Parts", 0),
	("Bolt Carrier Groups & Charging Handles", "Parts", 0),
	("Triggers", "Parts", 0),
	("Lower Parts & Kits", "Parts", 0),
	("Muzzle Devices", "Parts", 0),
	("Handguards & Rails", "Parts", 0),
	("Stocks & Grips", "Parts", 0),
	# Non-taxable services (FFL transfer fee, gunsmithing labor, …). Kept OUT of
	# ALL_FIREARM_GROUPS so service items never gate as firearms, and given a
	# default 0% Item Tax Template by setup/tax.py so service lines aren't taxed.
	("Service", ROOT, 0),
	# Legacy leaves — healed where a site still has them, never created
	# (LEGACY_LEAVES; retired by patches/retire_legacy_item_groups and
	# retire_new_used_firearm_item_groups when nothing links to them).
	("New Firearm", ROOT, 0),
	("Used Firearm", ROOT, 0),
	("Long Gun", ROOT, 0),  # legacy — split into Rifle + Shotgun; demoted to root leaf
]

# Dead leaves: kept (topology healed) where Items or history still link to them,
# never created — a fresh site has nothing keyed to them, and the storefront
# category filter mirrors Item Group names.
LEGACY_LEAVES = frozenset({"Handgun", "Long Gun", "New Firearm", "Used Firearm"})


def _mirror_owned() -> set:
	"""Non-firearm groups the category mirror links (Catalog Category.item_group)."""
	from ffl_core.firearm import ALL_FIREARM_GROUPS

	if not frappe.db.table_exists("Catalog Category"):
		return set()
	linked = frappe.get_all("Catalog Category", filters={"item_group": ("is", "set")}, pluck="item_group")
	return {g for g in linked if g not in ALL_FIREARM_GROUPS}


def ensure() -> dict:
	created = 0
	owned = _mirror_owned()
	for name, parent, is_group in TREE:
		if frappe.db.exists("Item Group", name):
			cur = frappe.db.get_value(
				"Item Group", name, ["parent_item_group", "is_group"], as_dict=True
			)
			if name in owned:
				continue
			if (cur.parent_item_group or "") != (parent or "") or int(cur.is_group or 0) != is_group:
				frappe.db.set_value(
					"Item Group", name,
					{"parent_item_group": parent or None, "is_group": is_group},
					update_modified=False,
				)
		elif name not in LEGACY_LEAVES:
			doc = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": name,
				"is_group": is_group,
			})
			if parent:
				doc.parent_item_group = parent
			doc.insert(ignore_permissions=True)
			created += 1
	rebuild_tree("Item Group")
	commit()
	return {"created": created, "total": len(TREE)}
