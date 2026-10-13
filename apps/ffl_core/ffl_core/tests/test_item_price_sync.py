"""sync_standard_selling_price (Item on_update) + the backfill patch.

Contract: a sales item with standard_rate > 0 always ends up with a selling
Item Price (create-if-missing — the ERPNext gap where a rate set AFTER item
creation never reaches the price list); uniform_sell_price=1 items hard-sync
the record to standard_rate; an existing record on a non-uniform item is
never touched (manager-curated card price wins).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.item_hooks import _default_selling_price_list


def _item_price(code: str):
	return frappe.db.get_value(
		"Item Price",
		{"item_code": code, "price_list": _default_selling_price_list(), "selling": 1},
		["name", "price_list_rate"], as_dict=True)


def _wipe_prices(code: str):
	for name in frappe.get_all("Item Price", filters={"item_code": code}, pluck="name"):
		frappe.delete_doc("Item Price", name, force=1, ignore_permissions=True)


def _ensure_item(code: str, **overrides):
	frappe.delete_doc_if_exists("Item", code, force=1)
	doc = {
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


class ItemPriceSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()

	def test_rate_set_after_creation_creates_price(self):
		# The Test1 bug: item created without a rate, priced later.
		item = _ensure_item("IPS-LATE")
		_wipe_prices("IPS-LATE")
		item.reload()
		item.standard_rate = 20
		item.save(ignore_permissions=True)
		ip = _item_price("IPS-LATE")
		self.assertIsNotNone(ip, "saving a later-priced item must create the Item Price")
		self.assertEqual(ip.price_list_rate, 20)

	def test_uniform_item_hard_syncs(self):
		item = _ensure_item("IPS-UNI", uniform_sell_price=1, standard_rate=750)
		self.assertEqual(_item_price("IPS-UNI").price_list_rate, 750)  # native at insert
		item.reload()
		item.standard_rate = 800
		item.save(ignore_permissions=True)
		self.assertEqual(_item_price("IPS-UNI").price_list_rate, 800)  # followed

	def test_uniform_item_recreates_deleted_price(self):
		item = _ensure_item("IPS-UNI2", uniform_sell_price=1, standard_rate=600)
		_wipe_prices("IPS-UNI2")
		item.reload()
		item.save(ignore_permissions=True)
		self.assertEqual(_item_price("IPS-UNI2").price_list_rate, 600)

	def test_non_uniform_existing_price_never_touched(self):
		item = _ensure_item("IPS-CURATED", standard_rate=500)
		ip = _item_price("IPS-CURATED")
		frappe.db.set_value("Item Price", ip.name, "price_list_rate", 999)
		item.reload()
		item.standard_rate = 500.01
		item.save(ignore_permissions=True)
		self.assertEqual(_item_price("IPS-CURATED").price_list_rate, 999,
			"a manager-curated price must never be overwritten for non-uniform items")

	def test_save_is_idempotent_single_record(self):
		item = _ensure_item("IPS-ONCE", standard_rate=40)
		item.reload(); item.save(ignore_permissions=True)
		item.reload(); item.save(ignore_permissions=True)
		rows = frappe.get_all("Item Price", filters={
			"item_code": "IPS-ONCE",
			"price_list": _default_selling_price_list(), "selling": 1})
		self.assertEqual(len(rows), 1)

	def test_zero_rate_creates_nothing(self):
		item = _ensure_item("IPS-ZERO")
		_wipe_prices("IPS-ZERO")
		item.reload()
		item.save(ignore_permissions=True)
		self.assertIsNone(_item_price("IPS-ZERO"))

	def test_per_gun_item_never_gets_item_price(self):
		# Serialized + non-uniform = priced per serial; an Item Price would
		# pre-rate POS lines and bypass the serial-sets-the-price flow.
		item = _ensure_item("IPS-PERGUN", is_stock_item=1, has_serial_no=1,
			is_firearm=1, uniform_sell_price=0, standard_rate=850)
		_wipe_prices("IPS-PERGUN")
		item.reload()
		item.save(ignore_permissions=True)
		self.assertIsNone(_item_price("IPS-PERGUN"))
		from ffl_core.patches.backfill_standard_selling_prices import execute
		execute()   # the backfill must skip it too
		self.assertIsNone(_item_price("IPS-PERGUN"))

	def test_backfill_patch_idempotent(self):
		from ffl_core.patches.backfill_standard_selling_prices import execute
		item = _ensure_item("IPS-PATCH", uniform_sell_price=1, standard_rate=300)
		_wipe_prices("IPS-PATCH")
		execute()
		self.assertEqual(_item_price("IPS-PATCH").price_list_rate, 300)
		execute()   # second run: no dupes, value stable
		rows = frappe.get_all("Item Price", filters={"item_code": "IPS-PATCH"})
		self.assertEqual(len(rows), 1)
