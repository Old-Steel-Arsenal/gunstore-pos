"""reserved_web_serials — serials held by a Pending-Fulfillment Woo Online Order,
both dealer-assigned (confirmed_serial) and placeholder-held (stock_hold_serial)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestReservedWebSerials(FrappeTestCase):
	def test_returns_confirmed_and_held_with_kinds_and_url(self):
		from ffl_woo_sync.woocommerce.fulfillment import reserved_web_serials
		sql_rows = [
			{"confirmed_serial": "SN-CONF", "stock_hold_serial": "", "parent": "WOO-ORD-1"},
			{"confirmed_serial": "", "stock_hold_serial": "SN-HELD", "parent": "WOO-ORD-1"},
		]
		# get_roles must be patched too: the db.sql mock would otherwise corrupt the
		# role lookup the gate performs, masking the real behaviour under test.
		with patch("frappe.get_roles", return_value=["Sales Manager"]), \
			 patch("frappe.db.sql", return_value=sql_rows):
			out = reserved_web_serials("GLOCK-19")
		self.assertEqual(out["SN-CONF"]["kind"], "woo_assigned")
		self.assertEqual(out["SN-CONF"]["source"], "woo")
		self.assertEqual(out["SN-CONF"]["ref"], "WOO-ORD-1")
		self.assertEqual(out["SN-CONF"]["url"], "/app/woo-online-order/WOO-ORD-1")
		self.assertEqual(out["SN-HELD"]["kind"], "woo_held")
		self.assertIn("WOO-ORD-1", out["SN-HELD"]["tooltip"])

	def test_blank_serials_are_skipped(self):
		from ffl_woo_sync.woocommerce.fulfillment import reserved_web_serials
		with patch("frappe.get_roles", return_value=["Sales Manager"]), \
			 patch("frappe.db.sql", return_value=[]):
			out = reserved_web_serials("GLOCK-19")
		self.assertEqual(out, {})

	def test_non_sales_user_gets_empty_no_leak(self):
		"""A user without a shipping role must not enumerate reserved serials —
		and the gate short-circuits before the DB query runs."""
		from ffl_woo_sync.woocommerce.fulfillment import reserved_web_serials
		sql = MagicMock(return_value=[
			{"confirmed_serial": "SN-CONF", "stock_hold_serial": "", "parent": "WOO-ORD-1"}])
		with patch("frappe.get_roles", return_value=["Website User"]), \
			 patch("frappe.db.sql", sql):
			out = reserved_web_serials("GLOCK-19")
		self.assertEqual(out, {})
		sql.assert_not_called()
