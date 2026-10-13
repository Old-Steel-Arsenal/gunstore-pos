"""Schema test for the Woo Online Order Serial child table."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestWooOnlineOrderSerialSchema(FrappeTestCase):
	def test_parent_accepts_serial_rows_with_expected_fields(self):
		order = frappe.new_doc("Woo Online Order")
		row = order.append("serials", {
			"item_code": "GLOCK-19",
			"woo_line_id": "7",
			"unit_index": 1,
			"confirmed_serial": "SN-CONF",
			"stock_hold_serial": "SN-HOLD",
			"reservation_error": "",
		})
		self.assertEqual(row.doctype, "Woo Online Order Serial")
		self.assertEqual(row.item_code, "GLOCK-19")
		self.assertEqual(row.unit_index, 1)
		self.assertEqual(row.confirmed_serial, "SN-CONF")
		self.assertEqual(row.stock_hold_serial, "SN-HOLD")
