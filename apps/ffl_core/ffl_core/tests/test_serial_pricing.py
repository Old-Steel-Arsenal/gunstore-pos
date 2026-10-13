"""available_serials_with_prices: an un-priced firearm (sell_price 0, the
NOT-NULL Currency default) must surface as None and sort LAST, so the POS
serial picker shows it as un-priced and auto-picks a real-priced unit first
instead of treating it as the cheapest $0 gun.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.firearm import available_serials_with_prices, serial_dealer_price


class SerialPricing(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		code = "SP-GUN-1"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "All Item Groups", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)
		cls.code = code
		cls._ensure_serial("SP-PRICED", code, 500)
		cls._ensure_serial("SP-UNPRICED", code, 0)

	@staticmethod
	def _ensure_serial(serial, item_code, sell_price):
		frappe.delete_doc_if_exists("Serial No", serial)
		frappe.get_doc({
			"doctype": "Serial No", "serial_no": serial, "item_code": item_code,
		}).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Serial No", serial,
			{"status": "Active", "sell_price": sell_price}, update_modified=False,
		)

	def test_unpriced_serial_is_none_and_sorts_last(self):
		rows = available_serials_with_prices([self.code])[self.code]
		by_serial = {r["serial"]: r for r in rows}
		self.assertEqual(by_serial["SP-PRICED"]["sell_price"], 500)
		self.assertIsNone(
			by_serial["SP-UNPRICED"]["sell_price"],
			"a $0 (un-priced) serial must report None, not 0",
		)
		# priced unit first (auto-picked), un-priced last
		self.assertEqual(rows[0]["serial"], "SP-PRICED")
		self.assertEqual(rows[-1]["serial"], "SP-UNPRICED")


class SerialDealerPriceRule(FrappeTestCase):
	def test_uniform_uses_item_dealer_price(self):
		self.assertEqual(serial_dealer_price(1, 700, 999), 700)

	def test_per_serial_uses_serial_dealer_price(self):
		self.assertEqual(serial_dealer_price(0, 700, 850), 850)

	def test_zero_or_none_is_unpriced(self):
		self.assertIsNone(serial_dealer_price(0, 700, 0))
		self.assertIsNone(serial_dealer_price(1, 0, 850))
		self.assertIsNone(serial_dealer_price(0, 700, None))
