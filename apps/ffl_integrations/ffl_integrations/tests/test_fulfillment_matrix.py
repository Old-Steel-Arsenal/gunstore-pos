"""`distributor.api.fulfillment_matrix` — one-shot batch quote for the order-builder planner.

Each row x (local + every enabled distributor): the local cell gives on-hand qty
and feasibility, a channel cell gives cost/stock/mode (direct / to_store),
column-level degradation (one failing distributor greys out only its column),
and a row-level preselect (enough stock -> local; otherwise the drop-ship-capable
distributor with the lowest cost, ties going to the first by modified asc).

Fixtures follow test_counter_ensure_item.py: build their own Distributor + stub
catalog and clean up via addClassCleanup (enabled Distributors are live routing
config on the shared dev site).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import api as dist_api
from ffl_integrations.tests import catalog_stub

DIST_A = "FMX-DC-A"
DIST_B = "FMX-DC-B"

SKU_BOTH = "FMX-ACC-1"        # both distributors carry it, A is cheaper
SKU_BLOCKED = "FMX-BLK-1"     # dropship_blocked -> to_store only
SKU_ONLY_A = "FMX-ONLY-A"
SKU_PART = "FMX-BBL-1"        # RSR ships it only to a dealer; not a firearm
LOCAL_ITEM = "FMX-LOCAL-1"    # local stock item (not in the catalog)


def _mk_distributor(code):
	if frappe.db.exists("Distributor", code):
		return
	frappe.get_doc({
		"doctype": "Distributor", "code": code, "distributor_name": f"{code} DC",
		"provider": "rsr", "enabled": 1, "dropship_enabled": 1, "stocking_enabled": 1,
		"poll_enabled": 0, "safety_buffer": 3, "display_cap": 5,
		"default_markup_pct": 35, "zero_stock_when_feed_stale": 0,
		"override_dropship_defaults": 1,
	}).insert(ignore_permissions=True)


def _cleanup():
	if frappe.db.exists("Item", LOCAL_ITEM):
		frappe.delete_doc("Item", LOCAL_ITEM, ignore_permissions=True, force=True)
	for dist in (DIST_A, DIST_B):
		if frappe.db.exists("Distributor", dist):
			frappe.delete_doc("Distributor", dist, ignore_permissions=True, force=True)
	frappe.db.commit()


class FulfillmentMatrixTest(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		_mk_distributor(DIST_A)
		_mk_distributor(DIST_B)
		cls.addClassCleanup(_cleanup)
		if not frappe.db.exists("Item", LOCAL_ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": LOCAL_ITEM, "item_name": LOCAL_ITEM,
				"item_group": "Products" if frappe.db.exists("Item Group", "Products")
					else "All Item Groups",
				"stock_uom": "Nos", "is_stock_item": 1,
			}).insert(ignore_permissions=True)

	def setUp(self):
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": SKU_BOTH, "dealer_price": 10.0, "quantity": 25,
				"department_number": "20", "short_description": "FMX BOTH"},
			{"rsr_stock_number": SKU_BLOCKED, "dealer_price": 50.0, "quantity": 9,
				"dropship_blocked": 1, "department_number": "20",
				"short_description": "FMX BLOCKED"},
			{"rsr_stock_number": SKU_ONLY_A, "dealer_price": 30.0, "quantity": 4,
				"department_number": "20", "short_description": "FMX ONLY A"},
			{"rsr_stock_number": SKU_PART, "dealer_price": 150.0, "quantity": 5,
				"ffl_required": 1, "product_class": "Gun Parts", "department_number": "42",
				"short_description": "FMX BBL 5.56 16"},
		])

	def tearDown(self):
		catalog_stub.uninstall()

	def _row(self, matrix, idx=0):
		return matrix["rows"][idx]

	# Note: both Distributors share the rsr provider (the stub keys its catalog by
	# provider code), so the two columns hold identical content. Column structure
	# and degradation are what is under test; the price gap between them is mocked.

	def test_matrix_shape_columns_and_local_cell(self):
		m = dist_api.fulfillment_matrix([{"item_code": LOCAL_ITEM, "qty": 3}])
		self.assertIn(DIST_A, m["columns"])
		self.assertIn(DIST_B, m["columns"])
		row = self._row(m)
		self.assertEqual(row["local"]["in_stock"], 0)
		self.assertFalse(row["local"]["feasible"])
		self.assertEqual(len(row["channels"]), len(m["columns"]))

	def test_channel_cell_carries_cost_qty_mode(self):
		m = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		cell = next(c for c in self._row(m)["channels"] if c["distributor"] == DIST_A)
		self.assertEqual(cell["mode"], "direct")
		self.assertTrue(cell["feasible"])
		self.assertEqual(cell["cost"], 10.0)
		self.assertEqual(cell["qty"], 25)
		self.assertEqual(cell["sku"], SKU_BOTH)

	def test_blocked_brand_offers_ship_to_store_only(self):
		m = dist_api.fulfillment_matrix([{"sku": SKU_BLOCKED, "qty": 1}])
		cell = self._row(m)["channels"][0]
		self.assertEqual(cell["mode"], "to_store")
		self.assertTrue(cell["feasible"])        # the to_store channel is selectable
		self.assertFalse(cell["modes"]["direct"]["feasible"])
		self.assertTrue(cell["modes"]["to_store"]["feasible"])
		# the cell says WHY direct is off, in words the clerk can act on
		self.assertIn("ship to store only", cell["modes"]["direct"]["reason"])
		self.assertIn("ship to store only", cell.get("reason", ""))

	def test_dealer_only_part_is_ordered_to_the_store(self):
		m = dist_api.fulfillment_matrix([{"sku": SKU_PART, "qty": 1}])
		cell = self._row(m)["channels"][0]
		self.assertFalse(cell["is_firearm"])
		self.assertTrue(cell["ffl_required"])
		self.assertFalse(cell["modes"]["direct"]["feasible"])
		self.assertIn("only to a dealer", cell["modes"]["direct"]["reason"])
		self.assertTrue(cell["modes"]["to_store"]["feasible"])

	def test_row_carries_the_items_own_firearm_flag(self):
		m = dist_api.fulfillment_matrix([{"item_code": LOCAL_ITEM, "qty": 1},
			{"sku": SKU_PART, "qty": 1}])
		self.assertIs(self._row(m, 0)["is_firearm"], False)
		self.assertIsNone(self._row(m, 1)["is_firearm"])   # no Item: the cell decides

	def test_a_direct_house_offers_both_modes(self):
		m = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		cell = self._row(m)["channels"][0]
		self.assertTrue(cell["modes"]["direct"]["feasible"])
		self.assertTrue(cell["modes"]["to_store"]["feasible"])
		self.assertEqual(cell["department"], "20")
		self.assertEqual(self._row(m)["preselect_mode"], "direct")

	def test_a_house_with_drop_ship_switched_off_offers_ship_to_store_only(self):
		"""distributor-settings-layout E3: create_order refuses a drop-ship line whose
		house has drop-ship switched off and sends the clerk back to this plan — so
		the plan must not offer that house's Dropship button in the first place."""
		frappe.db.set_value("Distributor", DIST_B, "dropship_enabled", 0,
			update_modified=False)
		self.addCleanup(frappe.db.set_value, "Distributor", DIST_B, "dropship_enabled", 1,
			update_modified=False)
		m = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		cells = {c["distributor"]: c for c in self._row(m)["channels"]}
		self.assertFalse(cells[DIST_B]["modes"]["direct"]["feasible"])
		self.assertIn("drop-ship", cells[DIST_B]["modes"]["direct"]["reason"])
		self.assertTrue(cells[DIST_B]["modes"]["to_store"]["feasible"])
		self.assertEqual(cells[DIST_B]["mode"], "to_store")
		self.assertTrue(cells[DIST_A]["modes"]["direct"]["feasible"])

	def test_a_house_with_ship_to_store_switched_off_offers_drop_ship_only(self):
		"""The other half of the switch pair: create_order refuses a ship-to-store line
		whose house has it switched off, so the plan must not offer the button."""
		frappe.db.set_value("Distributor", DIST_B, "stocking_enabled", 0,
			update_modified=False)
		self.addCleanup(frappe.db.set_value, "Distributor", DIST_B, "stocking_enabled", 1,
			update_modified=False)
		m = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		cells = {c["distributor"]: c for c in self._row(m)["channels"]}
		self.assertFalse(cells[DIST_B]["modes"]["to_store"]["feasible"])
		self.assertIn("ship to store", cells[DIST_B]["modes"]["to_store"]["reason"])
		self.assertTrue(cells[DIST_B]["modes"]["direct"]["feasible"])
		self.assertTrue(cells[DIST_A]["modes"]["to_store"]["feasible"])

	def test_a_house_that_cannot_run_a_lane_offers_no_button_for_it(self):
		"""A greyed-out button says "not now"; a missing one says "never". A house
		whose integration has no store lane must show the second."""
		from unittest import mock

		provider = dist_api.get_provider("rsr")
		store_less = {c for c in provider.capabilities() if c != "to_store"}
		with mock.patch.object(type(provider), "capabilities", lambda _self: set(store_less)):
			m = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		cell = self._row(m)["channels"][0]
		self.assertEqual(sorted(cell["modes"]), ["direct"])
		self.assertEqual(cell["mode"], "direct")

	def test_short_distributor_stock_blocks_both_modes(self):
		m = dist_api.fulfillment_matrix([{"sku": SKU_ONLY_A, "qty": 99}])
		cell = self._row(m)["channels"][0]
		self.assertFalse(cell["feasible"])
		self.assertEqual(cell["modes"]["to_store"]["reason"], "insufficient distributor stock")

	def test_unknown_sku_is_infeasible_not_error(self):
		m = dist_api.fulfillment_matrix([{"sku": "FMX-NOPE-404", "qty": 1}])
		cell = self._row(m)["channels"][0]
		self.assertFalse(cell["feasible"])
		self.assertFalse(cell.get("error"))
		self.assertEqual(cell.get("reason"), "not carried")

	def test_provider_outage_degrades_that_column_only(self):
		from unittest import mock

		# make B column cell computation raise: column-level degradation, A column unaffected
		orig_cell = dist_api._matrix_cell

		def cell_wrap(provider, dist_name, line, qty, **kw):
			if dist_name == DIST_B:
				raise RuntimeError("house down")
			return orig_cell(provider, dist_name, line, qty, **kw)

		with mock.patch.object(dist_api, "_matrix_cell", side_effect=cell_wrap):
			m = dist_api.fulfillment_matrix([{"sku": SKU_ONLY_A, "qty": 1}])
		cells = {c["distributor"]: c for c in self._row(m)["channels"]}
		self.assertTrue(cells[DIST_B].get("error"))
		self.assertTrue(cells[DIST_A]["feasible"])

	def test_preselect_ties_break_by_column_order_and_cheapest_wins(self):
		from unittest import mock
		# The shared site may have other enabled distributors (same provider, same
		# stub data = same price, a tie); the tie goes to the earlier column
		# (modified asc), which matches the first feasible column.
		m = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		first_feasible = next(c["distributor"]
			for c in self._row(m)["channels"] if c.get("feasible"))
		self.assertEqual(self._row(m)["preselect"], first_feasible)
		# artificially cut B's price: the lowest cost wins, column order yields
		orig = dist_api._matrix_cell

		def cheaper_b(provider, dist_name, line, qty, **kw):
			cell = orig(provider, dist_name, line, qty, **kw)
			if dist_name == DIST_B and cell.get("feasible"):
				cell["cost"] = 1.0
			return cell

		with mock.patch.object(dist_api, "_matrix_cell", side_effect=cheaper_b):
			m_b = dist_api.fulfillment_matrix([{"sku": SKU_BOTH, "qty": 2}])
		self.assertEqual(self._row(m_b)["preselect"], DIST_B)

	def test_preselect_local_when_stocked(self):
		# enough stock on hand -> local (5 units received into stock)
		company = frappe.db.get_single_value("Global Defaults", "default_company")
		wh = frappe.db.get_value("Warehouse",
			{"company": company, "is_group": 0, "warehouse_name": "Stores"}, "name") \
			or frappe.db.get_value("Warehouse",
				{"company": company, "is_group": 0}, "name")
		frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": company,
			"items": [{"item_code": LOCAL_ITEM, "qty": 5,
				"t_warehouse": wh, "basic_rate": 2}],
		}).submit()
		m2 = dist_api.fulfillment_matrix([{"item_code": LOCAL_ITEM, "qty": 3}])
		self.assertTrue(self._row(m2)["local"]["feasible"])
		self.assertEqual(self._row(m2)["preselect"], "local")

	def test_blocked_everywhere_preselects_ship_to_store(self):
		m = dist_api.fulfillment_matrix([{"sku": SKU_BLOCKED, "qty": 1}])
		self.assertIsNotNone(self._row(m)["preselect"])
		self.assertEqual(self._row(m)["preselect_mode"], "to_store")

	def test_nothing_carried_preselects_none(self):
		m = dist_api.fulfillment_matrix([{"sku": "FMX-NOPE-404", "qty": 1}])
		self.assertIsNone(self._row(m)["preselect"])
		self.assertIsNone(self._row(m)["preselect_mode"])
