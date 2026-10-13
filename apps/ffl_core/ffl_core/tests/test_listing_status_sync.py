"""Listing-status state machine tests.

Calls the sync helpers directly — same pattern as Spike 1/2 to avoid the
full ERPNext SI lifecycle.
"""
from __future__ import annotations

from types import SimpleNamespace
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.listing.status_sync import (
	on_sales_invoice_cancel,
	on_sales_invoice_submit,
	on_sales_order_cancel,
	on_sales_order_submit,
)


class ListingStatusSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls._ensure_item_group("All Item Groups", is_group=1, parent=None)
		cls._ensure_item_group("Used Firearm", is_group=0, parent="All Item Groups")
		cls._ensure_item_group("Accessory", is_group=0, parent="All Item Groups")
		cls._ensure_uom("Nos")
		cls._ensure_item("FFL-LS-USED-1", item_group="Used Firearm", listing_status="Listed")
		cls._ensure_item("FFL-LS-USED-2", item_group="Used Firearm", listing_status="Listed")
		cls._ensure_item("FFL-LS-ACC", item_group="Accessory", listing_status=None)
		# Phase-2 used gun: is_used=1 but filed under a TYPE group (not "Used
		# Firearm"). The status machine must still recognise it via is_used.
		cls._ensure_typed_used_item("FFL-LS-NEWUSED", item_group="Accessory")

	@staticmethod
	def _ensure_item_group(name, is_group, parent):
		if frappe.db.exists("Item Group", name):
			return
		doc = frappe.get_doc({"doctype": "Item Group", "item_group_name": name, "is_group": is_group})
		if parent:
			doc.parent_item_group = parent
		doc.insert(ignore_permissions=True)

	@staticmethod
	def _ensure_uom(name):
		if not frappe.db.exists("UOM", name):
			frappe.get_doc({"doctype": "UOM", "uom_name": name, "must_be_whole_number": 1}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_item(code, item_group, listing_status):
		if frappe.db.exists("Item", code):
			# reset listing_status for clean test runs
			if listing_status is not None:
				frappe.db.set_value("Item", code, "listing_status", listing_status, update_modified=False)
			return
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1 if item_group == "Used Firearm" else 0,
			"serial_no_series": f"{code}-.####" if item_group == "Used Firearm" else "",
		})
		doc.insert(ignore_permissions=True)
		if listing_status is not None:
			frappe.db.set_value("Item", code, "listing_status", listing_status, update_modified=False)

	@staticmethod
	def _ensure_typed_used_item(code, item_group):
		"""A used firearm filed under a non-'Used Firearm' type group (Phase 2):
		is_used=1 + is_firearm=1 is what marks it used, not the group name."""
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": item_group, "stock_uom": "Nos", "is_stock_item": 1,
				"is_used": 1, "is_firearm": 1, "has_serial_no": 1,
				"serial_no_series": f"{code}-.####",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", code, "listing_status", "Listed", update_modified=False)

	def setUp(self):
		# Reset every test
		frappe.db.set_value("Item", "FFL-LS-USED-1", "listing_status", "Listed", update_modified=False)
		frappe.db.set_value("Item", "FFL-LS-USED-2", "listing_status", "Listed", update_modified=False)
		frappe.db.set_value("Item", "FFL-LS-NEWUSED", "listing_status", "Listed", update_modified=False)

	@staticmethod
	def _doc(*items, update_stock=0):
		return SimpleNamespace(
			items=[SimpleNamespace(item_code=c) for c in items],
			update_stock=update_stock,
		)

	# ----------------------------------------------------------------- tests

	def test_so_submit_marks_used_firearm_reserved(self):
		on_sales_order_submit(self._doc("FFL-LS-USED-1"))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Reserved")

	def test_so_submit_ignores_accessory(self):
		# accessory has no listing_status — must not raise
		on_sales_order_submit(self._doc("FFL-LS-ACC"))

	def test_phase2_used_firearm_in_type_group_flips(self):
		# A used gun under a TYPE group (is_used=1, not "Used Firearm" group)
		# must still flip listing_status on sale — else it stays Listed forever.
		on_sales_invoice_submit(self._doc("FFL-LS-NEWUSED", update_stock=1))
		self.assertEqual(
			frappe.db.get_value("Item", "FFL-LS-NEWUSED", "listing_status"), "Sold")

	def test_so_cancel_returns_used_firearm_to_listed(self):
		on_sales_order_submit(self._doc("FFL-LS-USED-1"))
		on_sales_order_cancel(self._doc("FFL-LS-USED-1"))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Listed")

	def test_si_submit_with_update_stock_marks_sold(self):
		on_sales_invoice_submit(self._doc("FFL-LS-USED-1", update_stock=1))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Sold")

	def test_si_submit_without_update_stock_marks_reserved(self):
		on_sales_invoice_submit(self._doc("FFL-LS-USED-1", update_stock=0))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Reserved")

	def test_si_cancel_returns_used_firearm_to_listed(self):
		on_sales_invoice_submit(self._doc("FFL-LS-USED-1", update_stock=1))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Sold")
		on_sales_invoice_cancel(self._doc("FFL-LS-USED-1"))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Listed")

	def test_multiple_used_firearms_in_one_so(self):
		on_sales_order_submit(self._doc("FFL-LS-USED-1", "FFL-LS-USED-2"))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Reserved")
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-2", "listing_status"), "Reserved")

	def test_idempotent_no_op_when_already_target(self):
		# Forcing already-Reserved → Reserved should not error
		frappe.db.set_value("Item", "FFL-LS-USED-1", "listing_status", "Reserved", update_modified=False)
		on_sales_order_submit(self._doc("FFL-LS-USED-1"))
		self.assertEqual(frappe.db.get_value("Item", "FFL-LS-USED-1", "listing_status"), "Reserved")


if __name__ == "__main__":
	unittest.main()
