# apps/ffl_core/ffl_core/tests/test_serial_firearm_type.py
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import firearm


class SerialFirearmType(FrappeTestCase):
    def _firearm_item(self, code: str, ftype: str) -> str:
        if not frappe.db.exists("Item Group", "Used Firearm"):
            frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
                "parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
        if not frappe.db.exists("Item", code):
            frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
                "item_group": "Used Firearm", "is_firearm": 1, "is_stock_item": 1,
                "has_serial_no": 1, "firearm_type": ftype}).insert(ignore_permissions=True)
        return code

    def test_blank_serial_inherits_type_from_item(self):
        code = self._firearm_item("FT-INH-RIFLE", "Rifle")
        doc = frappe.get_doc({"doctype": "Serial No", "serial_no": "FT-INH-1",
            "item_code": code}).insert(ignore_permissions=True)
        self.assertEqual(doc.firearm_type, "Rifle")

    def test_explicit_serial_type_not_overwritten(self):
        code = self._firearm_item("FT-INH-PISTOL", "Pistol")
        doc = frappe.get_doc({"doctype": "Serial No", "serial_no": "FT-INH-2",
            "item_code": code, "firearm_type": "Revolver"}).insert(ignore_permissions=True)
        self.assertEqual(doc.firearm_type, "Revolver")

    def test_backfill_copies_item_type_to_blank_serial(self):
        from ffl_core.patches import backfill_serial_firearm_type
        # Unique names every run: the patch's create_custom_fields COMMITS
        # (framework-side frappe.db.commit), so rows created before the call
        # outlive the test rollback on a persistent site — a fixed serial here
        # bricked its own re-runs with Duplicate entry.
        sfx = frappe.generate_hash(length=10)
        code = self._firearm_item(f"FT-BF-{sfx}", "Shotgun")
        serial = f"FT-BF-S-{sfx}"
        # Materialize a serial then blank its type directly (simulate a pre-field gun).
        frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
            "item_code": code}).insert(ignore_permissions=True)
        frappe.db.set_value("Serial No", serial, "firearm_type", "", update_modified=False)
        backfill_serial_firearm_type.execute()
        self.assertEqual(frappe.db.get_value("Serial No", serial, "firearm_type"), "Shotgun")
