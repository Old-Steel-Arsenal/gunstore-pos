"""The local catalog end to end on a fresh site in Local mode, both shipped drivers fed
synthetic feeds (``tests/rsr_synth.py`` and ``tests/ss_synth.py``) through fake
transports under their real codes: search, quote, a drop-ship order created but
never placed, and the product change feed (what the listing sync follows) picking
up a change from each house.

The check refuses to run on a site that already holds a local RSR or Sports South
catalog, and deletes every row it wrote: the lanes commit, so nothing it writes is
rolled back.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import test_bootstrap
from ffl_integrations.catalog_local import sync
from ffl_integrations.catalog_local.drivers import rsr, ss
from ffl_integrations.data_service import clear_capabilities
from ffl_integrations.catalog_master import feed_head
from ffl_integrations.distributor import api, catalog, remote_catalog
from ffl_integrations.tests import catalog_stub, rsr_synth, ss_synth
from ffl_integrations.tests.rsr_synth import (
	FakeTransport,
	catalog_text,
	deleted_text,
	quantity_text,
	restrictions_text,
)

LOCAL = "ffl_integrations.catalog_local.is_local"
E2E_CODES = ("E2E-RSR", "E2E-SS")
E2E_SUPPLIER = "E2E Test Supplier"
SHIP_CONTEXT = {
	"mode": "direct",
	"ship_to": {"line1": "1 Example St", "line2": "", "city": "Example", "state": "GA",
		"zip": "30301"},
	"consumer_name": "Test Buyer",
	"contact_num": "555-555-0100",
}


def _delete_e2e_docs() -> None:
	"""The lanes and the order commit, so these rows are deleted explicitly: orders first
	(they link the Distributors), then the Distributors."""
	for name in frappe.get_all("Distributor Order", filters={"distributor": ["in", list(E2E_CODES)]},
			pluck="name"):
		frappe.delete_doc("Distributor Order", name, ignore_permissions=True, force=True)
	for code in E2E_CODES:
		if frappe.db.exists("Distributor", code):
			frappe.delete_doc("Distributor", code, ignore_permissions=True, force=True)
	frappe.db.commit()


def _clear_memo() -> None:
	frappe.local._remote_catalog_memo = {}


def _ensure_item(code: str) -> None:
	"""A plain stock Item named after the SKU, as promotion would make one: the planner
	writes the SKU as the order line's item_code when no other is given, and the order
	links to an Item."""
	frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": code, "item_group": "Ammunition",
		"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 0, "is_firearm": 0,
	}).insert(ignore_permissions=True)


def _ensure_supplier_group(name: str) -> None:
	if not frappe.db.exists("Supplier Group", name):
		frappe.get_doc({
			"doctype": "Supplier Group", "supplier_group_name": name,
			"parent_supplier_group": "All Supplier Groups", "is_group": 0,
		}).insert(ignore_permissions=True)


class TestLocalCatalogEndToEnd(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		test_bootstrap.run()

	def setUp(self):
		catalog_stub.uninstall()
		if frappe.db.sql("SELECT 1 FROM dc_offer WHERE distributor IN ('rsr', 'ss') LIMIT 1") or \
				frappe.db.sql("SELECT 1 FROM dc_restriction WHERE distributor = 'rsr' LIMIT 1"):
			self.skipTest("this site already holds a local rsr/ss catalog: the end-to-end check "
				"needs a fresh site")
		_delete_e2e_docs()  # a crashed earlier run's leftovers
		self.last_run_id = frappe.db.sql("SELECT COALESCE(MAX(id), 0) FROM dc_sync_run")[0][0]
		self.created_supplier = False
		self.created_item = False
		self.addCleanup(self._cleanup)

		local = patch(LOCAL, return_value=True)
		local.start()
		self.addCleanup(local.stop)
		clear_capabilities()

		self.created_item = not frappe.db.exists("Item", ss_synth.sku(2))
		if self.created_item:
			_ensure_item(ss_synth.sku(2))
		self.created_supplier = not frappe.db.exists("Supplier", E2E_SUPPLIER)
		if self.created_supplier:
			_ensure_supplier_group("Distributor")
			frappe.get_doc({
				"doctype": "Supplier", "supplier_name": E2E_SUPPLIER, "supplier_group": "Distributor",
			}).insert(ignore_permissions=True)
		for code, name, provider in (("E2E-RSR", "E2E RSR", "rsr"), ("E2E-SS", "E2E SS", "ss")):
			frappe.get_doc({
				"doctype": "Distributor", "code": code, "distributor_name": name,
				"provider": provider, "enabled": 1, "dropship_enabled": 1, "stocking_enabled": 1,
				"poll_enabled": 0, "supplier": E2E_SUPPLIER,
			}).insert(ignore_permissions=True)

		self.rsr_t = FakeTransport({
			"catalog": catalog_text(range(1, 7)),
			"quantity": quantity_text([(rsr_synth.sku(n), n % 7) for n in range(1, 7)]),
			"deleted": deleted_text(["ZZR-999999"]),
			"restrictions": restrictions_text([(rsr_synth.sku(1), "ALL", "NJ", "S", "F")]),
		})
		sync.register(rsr.RsrDriver(code="rsr", transport=self.rsr_t))
		self.addCleanup(sync.register, rsr.DRIVER)

		self.ss_t = ss_synth.FakeSSTransport(ss_synth.standard_feed(range(1, 7)))
		sync.register(ss.SsDriver(code="ss", transport=self.ss_t))
		self.addCleanup(sync.register, ss.DRIVER)

		snapshot = patch.object(ss, "SNAPSHOT_MIN_ROWS", 0)
		snapshot.start()
		self.addCleanup(snapshot.stop)

	def _cleanup(self):
		_delete_e2e_docs()
		if self.created_item and frappe.db.exists("Item", ss_synth.sku(2)):
			frappe.delete_doc("Item", ss_synth.sku(2), ignore_permissions=True, force=True)
		if self.created_supplier and frappe.db.exists("Supplier", E2E_SUPPLIER):
			frappe.delete_doc("Supplier", E2E_SUPPLIER, ignore_permissions=True, force=True)
		for table in ("dc_offer", "dc_product_move", "dc_restriction", "dc_meta"):
			frappe.db.sql(f"DELETE FROM {table} WHERE distributor IN ('rsr', 'ss')")
		frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("9999%",))
		frappe.db.sql("DELETE FROM dc_sync_run WHERE id > %s AND (source LIKE %s OR source LIKE %s)",
			(self.last_run_id, "rsr\\_%", "ss\\_%"))
		frappe.db.commit()
		clear_capabilities()

	def test_search_quote_dropship_dry_run_and_the_change_feed(self):
		# 1. Both houses sync.
		rsr_out = sync.run_house("rsr")
		ss_out = sync.run_house("ss")
		self.assertEqual(rsr_out["_failed"], [])
		self.assertEqual(ss_out["_failed"], [])

		# 2. Search: one typeahead over both houses, then one house by name.
		_clear_memo()
		hits = remote_catalog.search(None, "ACME TEST", 50)
		self.assertTrue({o["distributor"] for h in hits for o in h["offers"]} >= {"rsr", "ss"})
		found = api.search("ACME TEST HOLSTER", 10, "E2E-SS")
		self.assertIn(ss_synth.sku(2), [r.get("sku") for r in found])

		# 3. Quote, both houses.
		_clear_memo()
		q = api.quote(sku=ss_synth.sku(2), distributor="E2E-SS")
		self.assertTrue(q["found"])
		self.assertEqual(q["cost"], 102.5)
		self.assertFalse(q["ffl_required"])
		q2 = api.quote(sku=rsr_synth.sku(1), distributor="E2E-RSR")
		self.assertTrue(q2["found"])
		self.assertIn("NJ", q2["restricted_states"])

		# 4. Drop-ship order: created as a Draft, never placed.
		enq = []
		with patch("frappe.enqueue", side_effect=lambda *a, **k: enq.append((a, k))), \
				patch("ffl_integrations.distributor.api.enqueue_distributor",
					side_effect=lambda *a, **k: enq.append((a, k))), \
				patch("ffl_integrations.distributor.tasks.place_order_job",
					side_effect=AssertionError("an order was placed")):
			created = api._create_orders("E2E-SS", [{"sku": ss_synth.sku(2), "qty": 1}], SHIP_CONTEXT)
		self.assertEqual(len(created), 1)
		self.assertFalse([c for c in enq if "place_order" in repr(c)])
		do = frappe.get_doc("Distributor Order", created[0]["name"])
		self.assertEqual(do.status, "Draft")
		self.assertEqual(do.order_type, "Dropship Accessory")
		self.assertFalse(do.placed_at)
		self.assertFalse(do.raw_request)
		self.assertEqual(do.ship_address_state, "GA")
		self.assertEqual(do.consumer_name, "Test Buyer")
		self.assertEqual(len(do.lines), 1)
		self.assertEqual(do.lines[0].sku, ss_synth.sku(2))
		self.assertEqual(do.lines[0].item_code, ss_synth.sku(2))
		self.assertEqual(do.lines[0].wish_qty, 1)
		self.assertEqual(do.lines[0].unit_cost, 102.5)

		# 5. Change feed: the head, then a change from each house.
		head = feed_head()
		self.assertIn("rsr:", head)
		self.assertIn("ss:", head)

		self.ss_t.files["IncrementalOnhandUpdate"] = ss_synth.onhand_incremental(
			ss_synth.servertime(1), [(ss_synth.sku(2), 9, 99.75)])
		sync.run_lane("ss", "quantity")
		page = catalog.changes(head)
		self.assertIn(ss_synth.upc(2), page["keys"])
		_clear_memo()
		ss_row = remote_catalog.rows("ss", [ss_synth.sku(2)])[ss_synth.sku(2)]
		self.assertEqual(ss_row["qty"], 9)
		self.assertEqual(ss_row["cost"], 99.75)

		self.rsr_t.files["catalog"] = catalog_text(range(1, 7), over={3: {"cost": "321.50"}})
		out = sync.run_lane("rsr", "catalog", skip_schedule=True)
		self.assertEqual(out["updated"], 1)
		page2 = catalog.changes(page["cursor"])
		self.assertIn(rsr_synth.upc(3), page2["keys"])
		self.assertNotIn(ss_synth.upc(2), page2["keys"])
		_clear_memo()
		rsr_row = remote_catalog.rows("rsr", [rsr_synth.sku(3)])[rsr_synth.sku(3)]
		self.assertEqual(rsr_row["cost"], 321.5)

	def test_nothing_reaches_the_data_service_client(self):
		sync.run_house("rsr")
		sync.run_house("ss")
		_clear_memo()
		with patch("ffl_integrations.data_service.client.get",
					side_effect=AssertionError("Local mode reached the Data Service")), \
				patch("ffl_integrations.data_service.client.post",
					side_effect=AssertionError("Local mode reached the Data Service")), \
				patch("ffl_integrations.data_service.client._request",
					side_effect=AssertionError("Local mode reached the Data Service")):
			self.assertTrue(remote_catalog.search(None, "ACME TEST", 50))
			self.assertTrue(api.quote(sku=ss_synth.sku(2), distributor="E2E-SS")["found"])
			self.assertTrue(api.quote(sku=rsr_synth.sku(1), distributor="E2E-RSR")["found"])
			catalog.changes("")


if __name__ == "__main__":
	unittest.main()
