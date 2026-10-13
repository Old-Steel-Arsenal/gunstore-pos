"""Promotional (sale) price: the POS side of the Item.sale_price /
Serial No.sale_sell_price + sale_from/sale_to feature.

Contracts pinned here:

* ``_sale_active`` / ``_apply_sale`` — window semantics (blank dates are
  open-ended; the window is inclusive on both calendar days) and the
  must-undercut-regular rule (WooCommerce silently drops a sale >= regular,
  so the counter must ignore one too, never charge it).
* ``serial_sale_fields`` — same two-level resolution as the regular price:
  uniform parent → Item triple, per-serial → the serial's own tag.
* ``available_serials_with_prices`` — an ACTIVE sale replaces sell_price in
  the one API every POS pricing surface (picker / card / gate) reads, and
  the sorted-ascending contract holds for the discounted value.
* validators — a sale at/above the regular price refuses to save; an
  inverted window refuses to save.
* Item Price hard-sync — a uniform item's card price is the price the
  register charges TODAY (sale overlay), and ``refresh_sale_item_prices``
  flips it when a window opens/closes with no Item save in between.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.firearm import (
	_apply_sale,
	_sale_active,
	available_serials_with_prices,
	current_price,
	item_current_price,
	serial_sale_fields,
)
from ffl_core.item_hooks import _default_selling_price_list, refresh_sale_item_prices


def _item_price_rate(code: str):
	return frappe.db.get_value(
		"Item Price",
		{"item_code": code, "price_list": _default_selling_price_list(), "selling": 1},
		"price_list_rate")


class SaleWindowRules(FrappeTestCase):
	def test_no_price_is_never_active(self):
		self.assertFalse(_sale_active(0, None, None, on="2026-08-21"))
		self.assertFalse(_sale_active(None, "2026-08-01", "2026-12-31", on="2026-08-21"))

	def test_open_ended_dates(self):
		self.assertTrue(_sale_active(10, None, None, on="2026-08-21"))
		self.assertTrue(_sale_active(10, "2026-08-21", None, on="2026-08-21"))
		self.assertTrue(_sale_active(10, None, "2026-08-21", on="2026-08-21"))

	def test_window_is_inclusive_both_ends(self):
		self.assertTrue(_sale_active(10, "2026-08-21", "2026-08-30", on="2026-08-21"))
		self.assertTrue(_sale_active(10, "2026-08-21", "2026-08-30", on="2026-08-30"))
		self.assertFalse(_sale_active(10, "2026-08-22", "2026-08-30", on="2026-08-21"))
		self.assertFalse(_sale_active(10, "2026-08-01", "2026-08-20", on="2026-08-21"))

	def test_apply_sale_overlays_only_when_it_undercuts(self):
		self.assertEqual(_apply_sale(100, 80, None, None, on="2026-08-21"), 80)
		# WC drops a sale >= regular on save — the counter must match.
		self.assertEqual(_apply_sale(100, 100, None, None, on="2026-08-21"), 100)
		self.assertEqual(_apply_sale(100, 120, None, None, on="2026-08-21"), 100)

	def test_apply_sale_outside_window_charges_regular(self):
		self.assertEqual(
			_apply_sale(100, 80, "2026-09-01", None, on="2026-08-21"), 100)
		self.assertEqual(
			_apply_sale(100, 80, None, "2026-08-20", on="2026-08-21"), 100)

	def test_current_price_is_the_register_rule_with_the_regular_price_as_the_floor(self):
		"""What a uniform-priced Item charges today — the one rule the register's Item
		Price and GunBroker's model listing share. Falls back to the regular price, and
		to 0.0 for an Item with none."""
		self.assertEqual(current_price(100, 80, None, None, on="2026-08-21"), 80.0)
		self.assertEqual(current_price(100, 80, "2026-09-01", None, on="2026-08-21"), 100.0)
		self.assertEqual(current_price(100, 120, None, None, on="2026-08-21"), 100.0)
		self.assertEqual(current_price(None, 80, None, None, on="2026-08-21"), 0.0)

	def test_item_current_price_reads_the_item_or_a_row_handed_in(self):
		row = {"standard_rate": 100, "sale_price": 80, "sale_from": "2026-08-01",
			"sale_to": "2026-08-31"}
		self.assertEqual(item_current_price("NO-SUCH-ITEM", on="2026-08-21", row=row), 80.0)
		self.assertEqual(item_current_price("NO-SUCH-ITEM", on="2026-09-01", row=row), 100.0)
		self.assertEqual(item_current_price("NO-SUCH-ITEM"), 0.0)

	def test_unpriced_gun_never_gets_a_sale(self):
		self.assertIsNone(_apply_sale(None, 80, None, None, on="2026-08-21"))

	def test_two_level_resolution_mirrors_the_regular_rule(self):
		item = (80, "2026-08-01", "2026-08-31")
		serial = (60, "2026-09-01", "2026-09-30")
		self.assertEqual(serial_sale_fields(1, *item, *serial), item)
		self.assertEqual(serial_sale_fields(0, *item, *serial), serial)


class SaleValidators(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()

	def _item(self, code, **overrides):
		frappe.delete_doc_if_exists("Item", code, force=1)
		doc = {
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0,
		}
		doc.update(overrides)
		return frappe.get_doc(doc)

	def test_sale_at_or_above_regular_refuses_to_save(self):
		for bad in (100, 120):
			with self.assertRaises(frappe.ValidationError):
				self._item("SALE-VAL-1", standard_rate=100, sale_price=bad,
					uniform_sell_price=1).insert(ignore_permissions=True)

	def test_inverted_window_refuses_to_save(self):
		with self.assertRaises(frappe.ValidationError):
			self._item("SALE-VAL-2", standard_rate=100, sale_price=80,
				uniform_sell_price=1, sale_from="2026-08-30",
				sale_to="2026-08-01").insert(ignore_permissions=True)

	def test_valid_sale_saves(self):
		self._item("SALE-VAL-3", standard_rate=100, sale_price=80,
			uniform_sell_price=1).insert(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value("Item", "SALE-VAL-3", "sale_price"), 80)

	def test_serial_sale_at_or_above_tag_refuses_to_save(self):
		self._item("SALE-VAL-GUN", is_stock_item=1, has_serial_no=1,
			is_firearm=1).insert(ignore_permissions=True)
		frappe.delete_doc_if_exists("Serial No", "SALE-VAL-SN1")
		sn = frappe.get_doc({
			"doctype": "Serial No", "serial_no": "SALE-VAL-SN1",
			"item_code": "SALE-VAL-GUN",
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", sn.name, "sell_price", 500,
			update_modified=False)
		sn.reload()
		sn.sale_sell_price = 500
		with self.assertRaises(frappe.ValidationError):
			sn.save(ignore_permissions=True)
		sn.reload()
		sn.sale_sell_price = 450
		sn.save(ignore_permissions=True)  # undercutting sale is fine


class PickerAppliesActiveSale(FrappeTestCase):
	"""available_serials_with_prices is the one API every POS pricing surface
	reads — the overlay must land there, and only for ACTIVE windows."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		code = "SALE-PICK-GUN"
		frappe.delete_doc_if_exists("Item", code, force=1)
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "All Item Groups", "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
		}).insert(ignore_permissions=True)
		cls.code = code
		for serial, fields in {
			"SALE-PICK-ON":  {"sell_price": 500, "sale_sell_price": 300},
			"SALE-PICK-OFF": {"sell_price": 400},
			"SALE-PICK-FUT": {"sell_price": 450, "sale_sell_price": 100,
				"sale_from": "2126-01-01"},
		}.items():
			frappe.delete_doc_if_exists("Serial No", serial)
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": serial, "item_code": code,
			}).insert(ignore_permissions=True)
			frappe.db.set_value("Serial No", serial,
				{"status": "Active", **fields}, update_modified=False)

	def test_active_sale_replaces_sell_price_and_resorts(self):
		rows = available_serials_with_prices([self.code])[self.code]
		by_serial = {r["serial"]: r for r in rows}
		self.assertEqual(by_serial["SALE-PICK-ON"]["sell_price"], 300)
		self.assertEqual(by_serial["SALE-PICK-OFF"]["sell_price"], 400)
		# A future window must NOT discount yet.
		self.assertEqual(by_serial["SALE-PICK-FUT"]["sell_price"], 450)
		# Sorted ascending over the EFFECTIVE price: the $300 sale wins.
		self.assertEqual(rows[0]["serial"], "SALE-PICK-ON")

	def test_raw_sale_fields_do_not_leak_into_the_response(self):
		rows = available_serials_with_prices([self.code])[self.code]
		self.assertEqual(
			set(rows[0]), {"serial", "sell_price", "purchase_rate"},
			"the picker response shape is a JS contract — overlay internals must be popped")


class UniformCardFollowsTheWindow(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()

	def test_hard_sync_and_daily_refresh_flip_with_the_window(self):
		code = "SALE-CARD-UNI"
		frappe.delete_doc_if_exists("Item", code, force=1)
		item = frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "All Item Groups", "stock_uom": "Nos",
			"is_stock_item": 0, "uniform_sell_price": 1,
			"standard_rate": 750, "sale_price": 600,
		}).insert(ignore_permissions=True)
		self.assertEqual(_item_price_rate(code), 600,
			"active sale must land on the card at save time")

		# The window closes overnight — no Item save fires. Only the daily
		# refresh can flip the card back; simulate the rollover with a raw
		# write (validators and on_update both bypassed, as in real life).
		frappe.db.set_value("Item", code, "sale_to", "2020-01-01",
			update_modified=False)
		refresh_sale_item_prices()
		self.assertEqual(_item_price_rate(code), 750,
			"expired window must fall back to the regular price on refresh")

		# ...and re-opens (e.g. staff pre-scheduled the next promo).
		frappe.db.set_value("Item", code, "sale_to", None, update_modified=False)
		refresh_sale_item_prices()
		self.assertEqual(_item_price_rate(code), 600,
			"open window must re-apply on refresh")

		# Clearing the sale on a normal save restores the regular price
		# through the ordinary hard-sync path.
		item.reload()
		item.sale_price = 0
		item.save(ignore_permissions=True)
		self.assertEqual(_item_price_rate(code), 750)
