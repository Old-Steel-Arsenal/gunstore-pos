"""Retire the unused legacy "Handgun" / "Long Gun" Item Groups.

Handgun was split into Pistol / Revolver (2026-07-21) and Long Gun into Rifle /
Shotgun (split_long_gun_item_group); both are dead leaves. ``item_groups.ensure()``
no longer creates them on a site that lacks them, so deleting an unused one
sticks — the storefront category filter mirrors Item Group names, and a leftover
leaf reads as a live "Handgun" / "Long Gun" choice.

A group anything still links to (an Item, a submitted invoice line, an RSR
department default) is kept: history must not be orphaned. Idempotent.
"""
from __future__ import annotations

import frappe
from frappe.database.database import savepoint

from ffl_core.firearm import HANDGUN, LONG_GUN


def execute():
	retired = retire((HANDGUN, LONG_GUN))
	if retired:
		print(f"retire_legacy_item_groups: deleted {', '.join(retired)}")


def retire(names) -> list[str]:
	"""Delete each named Item Group that nothing links to; return the deleted names."""
	retired = []
	for name in names:
		if not frappe.db.exists("Item Group", name):
			continue
		# delete_doc runs the nested-set on_trash BEFORE its link check, so a
		# refused delete must roll back to here or the tree pointers stay edited.
		with savepoint(catch=frappe.ValidationError):  # LinkExistsError / child groups
			frappe.delete_doc("Item Group", name, ignore_permissions=True)
		frappe.clear_messages()
		if not frappe.db.exists("Item Group", name):
			retired.append(name)
	return retired
