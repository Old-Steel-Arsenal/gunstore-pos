"""Every firearm-group Item is serial-tracked — enforced by the Item hook.

2026-09-16: an NFA suppressor promoted from RSR with the dialog's "Has Serial
No" box left off booked fine in FastBound (the FFL Acquisition carries the
serial) but had no Serial No record, so the POS picker / 4473 path could not
sell it. The fix lives in ``firearm.auto_set_is_firearm`` (before_save) so all
Item creation paths and the manual form get it; these tests pin that hook.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import item_groups


def _item(group: str, **extra):
	code = f"SERGUARD-{frappe.generate_hash(length=8)}"
	doc = frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": group, "stock_uom": "Nos", "is_stock_item": 1,
		"has_serial_no": 0, **extra,
	})
	doc.insert(ignore_permissions=True)
	return doc


class FirearmSerialGuard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		item_groups.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

	def test_nfa_item_is_forced_serial_tracked(self):
		# The exact shape of the incident: NFA group, has_serial_no left 0.
		doc = _item("NFA")
		self.assertEqual(int(doc.has_serial_no), 1)
		self.assertEqual(int(doc.is_firearm), 1)
		self.assertEqual(doc.serial_no_series, f"{doc.name}-.####")

	def test_firearm_leaf_is_forced_serial_tracked(self):
		doc = _item("Rifle")
		self.assertEqual(int(doc.has_serial_no), 1)

	def test_non_firearm_item_untouched(self):
		doc = _item("Accessory")
		self.assertEqual(int(doc.has_serial_no), 0)
		self.assertFalse(doc.serial_no_series)

	def test_non_stock_item_in_a_firearm_group_is_left_alone(self):
		# ERPNext refuses has_serial_no on a non-stock Item (validate_item_type),
		# and that check runs before this hook — forcing the flag here would make
		# every LATER save of a fee / service line filed under a firearm group
		# fail with no way to untick it. is_firearm still stamps.
		doc = _item("NFA", is_stock_item=0)
		self.assertEqual(int(doc.has_serial_no), 0)
		self.assertEqual(int(doc.is_firearm), 1)
		doc.description = "saved again"
		doc.save(ignore_permissions=True)  # the regression: this used to throw
		self.assertEqual(int(doc.has_serial_no), 0)

	def test_regroup_into_firearm_forces_flag_when_no_ledger(self):
		doc = _item("Accessory")
		doc.item_group = "Pistol"
		doc.save(ignore_permissions=True)
		self.assertEqual(int(doc.has_serial_no), 1)

	def test_legacy_item_with_stock_ledger_is_left_alone(self):
		# ERPNext refuses the flip once uncancelled SLEs exist; the hook must
		# not trap every later save of such an Item — it stays has_serial_no=0
		# (and is_firearm=1) for the operator to fix via cancel + re-receive.
		doc = _item("Accessory")
		warehouse = frappe.db.get_value(
			"Warehouse", {"company": self.company, "is_group": 0}, "name")
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [{"item_code": doc.name, "qty": 1, "t_warehouse": warehouse,
				"basic_rate": 10}],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		# Fresh instance, as a form save would be: ERPNext caches
		# stock_ledger_created() per doc object, and the pre-receipt instance
		# still remembers "no ledger".
		doc = frappe.get_doc("Item", doc.name)
		doc.item_group = "Pistol"
		doc.save(ignore_permissions=True)
		self.assertEqual(int(doc.has_serial_no), 0)
		self.assertEqual(int(doc.is_firearm), 1)
