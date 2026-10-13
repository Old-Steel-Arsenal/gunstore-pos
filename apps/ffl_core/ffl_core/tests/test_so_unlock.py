"""Sales Order unlock — firearm pre-orders without serials.

A Sales Order is a pre-order carrier (Create Order, B2B): the gun may not
even be in the building yet, and ERPNext's SO Item has no serial_no field at
all — so the serial gate historically made every firearm SO unsaveable. The
gates that actually guard the door stay on Delivery Note / Sales Invoice /
POS Invoice.

Also pins the listing-status cancel fix: cancelling an SO must only revert
Reserved → Listed, never promote a never-published (Draft) used gun to Listed.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.firearm import require_serial_for_firearm
from ffl_core.listing import status_sync


def _doc(doctype: str, *item_codes: str) -> SimpleNamespace:
	# docstatus=1: the serial gate fires at submit (drafts may hold
	# serial-less firearm rows, e.g. a pickup before the gun is picked).
	return SimpleNamespace(
		doctype=doctype, docstatus=1,
		items=[SimpleNamespace(idx=i + 1, item_code=c, serial_no="")
			for i, c in enumerate(item_codes)],
	)


class SerialGateByDoctype(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if not frappe.db.exists("Item Group", "Firearm"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Firearm",
				"is_group": 0, "parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
		if not frappe.db.exists("UOM", "Nos"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "Nos"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "SOU-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "SOU-GUN", "item_name": "SOU-GUN",
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
				"serial_no_series": "SOU-GUN-.####",
			}).insert(ignore_permissions=True)

	def test_sales_order_firearm_row_without_serial_passes(self):
		require_serial_for_firearm(_doc("Sales Order", "SOU-GUN"))  # no raise

	def test_sales_invoice_firearm_row_without_serial_still_blocked(self):
		with self.assertRaises(frappe.ValidationError):
			require_serial_for_firearm(_doc("Sales Invoice", "SOU-GUN"))

	def test_delivery_note_firearm_row_without_serial_still_blocked(self):
		with self.assertRaises(frappe.ValidationError):
			require_serial_for_firearm(_doc("Delivery Note", "SOU-GUN"))

	def test_pos_invoice_firearm_row_without_serial_still_blocked(self):
		with self.assertRaises(frappe.ValidationError):
			require_serial_for_firearm(_doc("POS Invoice", "SOU-GUN"))


class ListingCancelRevert(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		if not frappe.db.exists("Item Group", "Used Firearm"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
				"is_group": 0, "parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
		if not frappe.db.exists("UOM", "Nos"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "Nos"}).insert(ignore_permissions=True)

	def _used_gun(self, code: str, listing_status: str) -> str:
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Used Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", code, "listing_status", listing_status,
			update_modified=False)
		return code

	def test_cancel_reverts_reserved_to_listed(self):
		code = self._used_gun("SOU-USED-R", "Reserved")
		status_sync.on_sales_order_cancel(_doc("Sales Order", code))
		self.assertEqual(frappe.db.get_value("Item", code, "listing_status"), "Listed")

	def test_cancel_leaves_never_published_draft_alone(self):
		code = self._used_gun("SOU-USED-D", "Draft")
		status_sync.on_sales_order_cancel(_doc("Sales Order", code))
		self.assertEqual(frappe.db.get_value("Item", code, "listing_status"), "Draft")


if __name__ == "__main__":
	unittest.main()
