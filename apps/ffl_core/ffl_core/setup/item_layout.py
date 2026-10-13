"""Drop the Item ``field_order`` Property Setter so ``insert_after`` rules the layout.

Saving Item in Customize Form (2026-06-25 on prod) stored a ``field_order``
Property Setter that pins every field then present to its position at that
moment. Meta.sort_fields honours it ahead of ``insert_after``, so the Item form
reorganisation in ffl_core's custom_field fixtures only moved fields the
setter had never seen (Channels tab rendered empty and was hidden; Used /
Quantity stayed ahead of Firearm). Deleting the setter hands ordering back to
the fixtures. Idempotent; wired into after_migrate (not patches.txt, which
runs once) so a later Customize Form save cannot silently re-pin the layout
past the next deploy.
"""
from __future__ import annotations

import frappe


def ensure():
	name = frappe.db.get_value(
		"Property Setter", {"doc_type": "Item", "property": "field_order"}, "name")
	if not name:
		return
	frappe.delete_doc("Property Setter", name, ignore_permissions=True, force=True)
	frappe.clear_cache(doctype="Item")
	print(f"item_layout.ensure: removed Item field_order setter {name}")
