"""Tests for the backfill_consignment_serials patch.

Guns taken in as consignment before the consignment-profit feature landed
have no is_consignment flag on their Serial No. This patch flags them from
their FFL Acquisition (transaction_type="Consignment"), filling the
store-default commission % and a consignor Supplier sourced from the
acquisition's seller. A serial whose acquisition is NOT a consignment must
stay untouched.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.acquisition import record_firearm_acquisition
from ffl_core.patches import backfill_consignment_serials as bf


class BackfillConsignmentSerials(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)

	def _firearm_serial(self, suffix: str) -> tuple[str, str]:
		"""Firearm Item + Serial No, mirroring test_serial_firearm_type.py."""
		if not frappe.db.exists("Item Group", "Used Firearm"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
				"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
		code = f"CON-BF-ITEM-{suffix}"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Used Firearm", "is_firearm": 1, "is_stock_item": 1,
				"has_serial_no": 1}).insert(ignore_permissions=True)
		serial = f"CON-BF-{suffix}"
		if not frappe.db.exists("Serial No", serial):
			frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
				"item_code": code}).insert(ignore_permissions=True)
		return code, serial

	def test_backfill_flags_consignment_serial_from_acquisition(self):
		suffix = self._testMethodName
		code, serial = self._firearm_serial(suffix)
		record_firearm_acquisition(
			item_code=code, serial=serial, source_type="Individual",
			source_name="Test Consignor", source_id_type="Driver License",
			source_id_number=f"DL-{suffix}", cost=0, company=self.company,
			transaction_type="Consignment",
		)
		bf.execute()
		self.assertEqual(frappe.db.get_value("Serial No", serial, "is_consignment"), 1)
		self.assertEqual(
			float(frappe.db.get_value("Serial No", serial, "consignment_commission_pct")), 20.0)
		c = frappe.db.get_value("Serial No", serial, "consignor")
		self.assertTrue(c and frappe.db.exists("Supplier", c))

	def test_backfill_leaves_non_consignment_serial_untouched(self):
		suffix = self._testMethodName
		code, serial = self._firearm_serial(suffix)
		record_firearm_acquisition(
			item_code=code, serial=serial, source_type="Individual",
			source_name="Test Purchase Seller", source_id_type="Driver License",
			source_id_number=f"DL-{suffix}", cost=100, company=self.company,
			# transaction_type left unset -> not a consignment acquisition
		)
		bf.execute()
		self.assertFalse(frappe.db.get_value("Serial No", serial, "is_consignment"))
