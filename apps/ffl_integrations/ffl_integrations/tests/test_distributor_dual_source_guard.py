"""Regression tests for the dual-source guard's UPC-keyed item_code fix.

`_guard_dual_source` (now `items`) used to hard-code `item_code = sku` — safe under
the OLD invariant where `promote_to_item` always named the Item after the RSR
stock number. Since rsr/promote.py's UPC-keying change, a real Item
can be named after the row's UPC instead, so a guard that only ever checks
`sku` silently no-ops on an Item that is actually in stock under its UPC —
exactly the collision the guard exists to catch:

  received serialized firearm (item_code=<UPC>) → later Flow-B bulk list of
  the same distributor sku → old guard checks a nonexistent `Item(sku)`,
  finds nothing, passes → promote hits the existing Item idempotently →
  create_listing force-sets uniform_sell_price=1 on a real in-stock gun.

These tests call `items._guard_dual_source` directly (not the full
`create_listing`) with a real `catalog_row`-shaped row built via the stub —
this avoids the currently-red `test_distributor_listing_integration.py`
machinery (a real Distributor doc, push-hook registration, pricing) that is
entirely orthogonal to the candidate-code logic under test here.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import items
from ffl_integrations.distributor.rsr_provider import RSRProvider
from ffl_integrations.tests import catalog_stub


class DualSourceGuardUpcKeying(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()

	def setUp(self):
		for code in ("888123400002", "GUARD-ORPHAN-1"):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True)
		frappe.db.commit()
		self.stub = catalog_stub.install()
		self.provider = RSRProvider()

	def tearDown(self):
		catalog_stub.uninstall()

	def _serialized_firearm(self, item_code):
		"""Published per-serial (has_serial_no=1, uniform_sell_price=0) — the
		3rd branch of `dual_source_conflict` trips on this shape alone, no
		Serial No / on-hand qty needed (mirrors the retired-for-now
		integration test's `_local_firearm` idiom)."""
		frappe.get_doc({
			"doctype": "Item", "item_code": item_code, "item_name": item_code,
			"item_group": "Rifle", "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 1, "serial_no_series": f"{item_code}-.####",
			"uniform_sell_price": 0,
		}).insert(ignore_permissions=True)

	def test_guard_blocks_on_the_upc_keyed_item_even_though_sku_differs(self):
		"""A firearm received/promoted earlier now lives under its UPC, not the
		distributor sku — the guard must still catch it when Flow B later
		tries to bulk-list the SAME sku as drop-ship."""
		sku, upc = "GLK19", "888123400002"
		catalog_stub.seed_catalog([{"rsr_stock_number": sku, "upc": upc,
			"department_number": "01", "ffl_required": 1,
			"short_description": "GLOCK 19 GEN5", "manufacturer": "GLOCK"}])
		self._serialized_firearm(upc)  # the real Item lives under the UPC now
		row = self.provider.catalog_row(sku)
		with self.assertRaises(frappe.ValidationError) as ctx:
			items._guard_dual_source(sku, {}, "RSR", row)
		self.assertIn("published per-serial", str(ctx.exception))

	def test_guard_still_blocks_the_orphan_case_keyed_by_stock_number(self):
		"""No UPC on the row (orphan) → item_code == sku, the OLD invariant —
		must still block exactly as before the UPC-keying change."""
		sku = "GUARD-ORPHAN-1"
		catalog_stub.seed_catalog([{"rsr_stock_number": sku, "upc": "",
			"department_number": "01", "ffl_required": 1,
			"short_description": "Orphan Test Gun", "manufacturer": "GLOCK"}])
		self._serialized_firearm(sku)
		row = self.provider.catalog_row(sku)
		with self.assertRaises(frappe.ValidationError) as ctx:
			items._guard_dual_source(sku, {}, "RSR", row)
		self.assertIn("published per-serial", str(ctx.exception))

	def test_a_brand_new_sku_with_no_local_item_under_either_candidate_passes(self):
		"""Sanity: neither candidate identity exists locally → the guard is a
		no-op (the normal, non-conflicting Flow-B path)."""
		sku, upc = "GLK26", "999999999999"  # neither candidate is ever inserted as an Item
		catalog_stub.seed_catalog([{"rsr_stock_number": sku, "upc": upc,
			"department_number": "01", "ffl_required": 1,
			"short_description": "GLOCK 26 GEN5", "manufacturer": "GLOCK"}])
		row = self.provider.catalog_row(sku)
		items._guard_dual_source(sku, {}, "RSR", row)  # must not raise
