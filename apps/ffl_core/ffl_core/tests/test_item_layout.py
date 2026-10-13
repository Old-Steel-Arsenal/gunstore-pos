"""setup/item_layout.ensure — the Item field_order Property Setter must not
survive a migrate, or the custom_field fixtures' insert_after is ignored for
every field the setter saw (2026-09-02 prod: Channels tab rendered empty)."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import item_layout


class ItemFieldOrderSetterIsDropped(FrappeTestCase):
	def test_a_saved_field_order_setter_is_removed_and_ensure_is_idempotent(self):
		if not frappe.db.exists("Property Setter", {"doc_type": "Item", "property": "field_order"}):
			frappe.get_doc({
				"doctype": "Property Setter", "doctype_or_field": "DocType",
				"doc_type": "Item", "property": "field_order", "property_type": "Data",
				"value": frappe.as_json(["item_code", "item_name"]),
			}).insert(ignore_permissions=True)
		item_layout.ensure()
		self.assertFalse(frappe.db.exists("Property Setter", {"doc_type": "Item", "property": "field_order"}))
		item_layout.ensure()  # nothing left: must not raise
		self.assertIn("ffl_core.setup.item_layout.ensure", frappe.get_hooks("after_migrate"))
