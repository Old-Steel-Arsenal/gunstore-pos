"""Scan-first receiving: which open Distributor Order a scanned unit belongs to,
the Incoming panel, and receiving by line. Site tests (make test)."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.distributor import receive_bridge as rb
from ffl_integrations.tests.test_distributor_integration import _Base


class _IncomingBase(_Base):
	def setUp(self):
		super().setUp()
		from unittest import mock
		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError
		p = mock.patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("disabled"))
		p.start(); self.addCleanup(p.stop)

	@property
	def _warehouse(self):
		company = frappe.new_doc("Purchase Receipt").company \
			or frappe.db.get_single_value("Global Defaults", "default_company")
		from ffl_core.warehouses import default_stock_warehouse
		return default_stock_warehouse(company)


class TestMatchIncoming(_IncomingBase):

	def _placed(self, lines, placed_at, status="Placed", source=None):
		do = self._do(lines, status=status)
		values = {"placed_at": placed_at}
		if source:
			values.update({"source_doctype": source[0], "source_name": source[1]})
		frappe.db.set_value("Distributor Order", do.name, values, update_modified=False)
		return do

	def test_earliest_open_order_comes_first(self):
		late = self._placed([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1,
			"ack_qty": 1, "unit_cost": 1.0}], "2026-09-15 10:00:00")
		early = self._placed([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1,
			"ack_qty": 1, "unit_cost": 1.0}], "2026-09-14 10:00:00")
		# Index, not a [:2] slice: IntegrationTestCase only rolls back at class
		# teardown, so an earlier test method's own DC-ACC order (same fixture
		# item) can still be an open candidate here — the FIFO property under
		# test is the relative order of these two, not the exact candidate count.
		names = [c["do_name"] for c in rb.match_incoming("DC-ACC")["candidates"]]
		self.assertLess(names.index(early.name), names.index(late.name))

	def test_draft_and_fully_received_orders_are_not_candidates(self):
		self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1}])
		done = self._placed([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1,
			"ack_qty": 1, "invoiced_qty": 1, "received_qty": 1}], "2026-09-14 10:00:00")
		names = [c["do_name"] for c in rb.match_incoming("DC-ACC")["candidates"]]
		self.assertNotIn(done.name, names)

	def test_no_open_order_means_plain_restock(self):
		self.assertEqual(rb.match_incoming("DC-NOPE")["candidates"], [])

	def test_candidate_carries_supplier_and_cost(self):
		do = self._placed([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 2,
			"ack_qty": 2, "unit_cost": 9.5}], "2026-09-14 10:00:00")
		c = next(c for c in rb.match_incoming("DC-ACC")["candidates"] if c["do_name"] == do.name)
		self.assertEqual(c["outstanding"], 2)
		self.assertEqual(c["unit_cost"], 9.5)
		self.assertEqual(c["supplier"], frappe.db.get_value("Distributor", "TEST-DC", "supplier"))

	def test_package_index_counts_every_placed_parcel_including_received(self):
		# Same source, two parcels: one already fully received (Complete), one
		# still open. package_index's "m" must still be 2 — the receiver reading
		# "2/2" needs to know this is the last piece of the WHOLE order, not just
		# the still-outstanding ones.
		old = self._placed([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1,
			"ack_qty": 1, "invoiced_qty": 1, "received_qty": 1, "unit_cost": 1.0}],
			"2026-09-10 10:00:00", status="Complete", source=("Sales Invoice", "SI-PKG-1"))
		new = self._placed([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1,
			"ack_qty": 1, "unit_cost": 1.0}],
			"2026-09-11 10:00:00", status="Placed", source=("Sales Invoice", "SI-PKG-1"))
		names = [c["do_name"] for c in rb.match_incoming("DC-ACC")["candidates"]]
		self.assertNotIn(old.name, names)  # fully received — not itself a candidate
		c = next(c for c in rb.match_incoming("DC-ACC")["candidates"] if c["do_name"] == new.name)
		self.assertEqual(c["package_index"], "2/2")

	def test_non_staff_is_refused(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				rb.match_incoming("DC-ACC")
		finally:
			frappe.set_user("Administrator")


class TestListIncoming(_IncomingBase):
	def _customer(self):
		name = "Incoming Tess"
		if not frappe.db.exists("Customer", {"customer_name": name}):
			frappe.get_doc({"doctype": "Customer", "customer_name": name}).insert(
				ignore_permissions=True)
		return frappe.db.get_value("Customer", {"customer_name": name}, "name")

	def _sourced(self, lines, source):
		do = self._do(lines, status="Placed")
		frappe.db.set_value("Distributor Order", do.name, {
			"source_doctype": source[0], "source_name": source[1],
			"order_type": "Procure To Store", "placed_at": "2026-09-14 10:00:00"},
			update_modified=False)
		return do

	def test_orders_group_by_customer_order_and_restock(self):
		si = frappe.get_doc({"doctype": "Sales Invoice", "customer": self._customer(),
			"items": [{"item_code": "DC-ACC", "qty": 1, "rate": 5}]})
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		mine = self._sourced([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1}],
			("Sales Invoice", si.name))
		restock = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 2, "ack_qty": 2}],
			status="Placed")
		groups = rb.list_incoming()["groups"]
		by_label = {g["label"]: g for g in groups}
		mine_row = next(o for o in by_label[si.name]["orders"] if o["do_name"] == mine.name)
		self.assertEqual(by_label[si.name]["customer_name"], "Incoming Tess")
		self.assertEqual(mine_row["customer_name"], "Incoming Tess")
		self.assertEqual(mine_row["supplier"], "DC-TEST-SUPPLIER")
		self.assertEqual(mine_row["lines"][0]["item_code"], "DC-ACC")
		restock_row = next(o for o in by_label["Restock"]["orders"] if o["do_name"] == restock.name)
		self.assertEqual(restock_row["outstanding"], 2)
		self.assertEqual(groups[-1]["label"], "Restock")

	def test_fully_received_orders_are_absent(self):
		done = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"invoiced_qty": 1, "received_qty": 1}], status="Placed")
		names = [o["do_name"] for g in rb.list_incoming()["groups"] for o in g["orders"]]
		self.assertNotIn(done.name, names)


class TestPollOrderNow(_IncomingBase):
	def test_runs_one_poll_and_reports(self):
		from unittest import mock
		from ffl_integrations.distributor import tasks
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1}],
			status="Placed")
		frappe.db.set_value("Distributor Order", do.name, "placed_at", "2026-09-14 10:00:00",
			update_modified=False)
		status = {"po_num": do.name, "web_ref": "1", "hold_status": "", "lines": [
			{"sku": "DC-ACC", "invoiced_qty": 1, "item_status": "", "invoices": ["INV1"],
				"tracking_numbers": ["1Z1"], "serials": []}]}
		with mock.patch.object(tasks, "_provider_for",
				return_value=mock.Mock(get_order_status=lambda name: status)):
			out = tasks.poll_order_now(do.name)
		self.assertEqual(out["status"], "Complete")
		self.assertEqual(frappe.db.get_value("Distributor Order Line", do.lines[0].name,
			"tracking_numbers"), "1Z1")

	def test_non_staff_is_refused(self):
		from ffl_integrations.distributor import tasks
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				tasks.poll_order_now("DO-NONE")
		finally:
			frappe.set_user("Administrator")


class TestReceiveLines(_IncomingBase):
	def test_only_the_scanned_units_are_booked(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 5, "ack_qty": 5,
			"invoiced_qty": 5, "unit_cost": 1.0}], status="Placed")
		out = rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 2}],
			warehouse=self._warehouse)
		self.assertTrue(out.get("name"))
		self.assertEqual(out["do_name"], do.name)
		self.assertEqual(frappe.db.get_value("Distributor Order Line", do.lines[0].name,
			"received_qty"), 2)
		self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "receive_status"), "Partial")

	def test_over_receipt_is_refused(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}], status="Placed")
		with self.assertRaises(frappe.ValidationError):
			rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 2}])

	def test_gun_takes_the_receiver_serial_not_the_order_echo(self):
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 400.0, "serials": "ECHO-1"}], status="Placed")
		out = rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 1,
			"serials": ["TYPED-1"]}], warehouse=self._warehouse)
		booked = rb._booked_firearm_serials(out["name"])
		self.assertEqual(booked.get("DC-GUN"), ["TYPED-1"])

	def test_gun_without_a_serial_is_refused(self):
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 400.0}], status="Placed")
		with self.assertRaises(frappe.ValidationError):
			rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 1}])

	def test_cost_prefers_the_invoiced_figure(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0, "invoiced_unit_cost": 1.25}], status="Placed")
		out = rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 1}],
			warehouse=self._warehouse)
		rate = frappe.db.get_value("Purchase Receipt Item", {"parent": out["name"]}, "rate")
		self.assertAlmostEqual(float(rate), 1.25, places=2)

	def test_acquisition_is_stamped_with_the_order(self):
		do = self._do([{"sku": "DC-GUN", "item_code": "DC-GUN", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 400.0}], status="Placed")
		out = rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 1,
			"serials": ["TYPED-2"]}], warehouse=self._warehouse)
		self.assertEqual(frappe.db.get_value("FFL Acquisition",
			{"purchase_receipt": out["name"]}, "distributor_order"), do.name)

	def test_house_without_a_supplier_is_named_in_the_refusal(self):
		# prod 2026-09-18: Sports South had no Supplier link, so the order bucket
		# died at create_receive's door with "Supplier is required." — about the
		# page's Supplier box, which this path never reads. Name the record.
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}], status="Placed")
		frappe.db.set_value("Distributor", "TEST-DC", "supplier", None, update_modified=False)
		try:
			with self.assertRaises(frappe.ValidationError) as caught:
				rb.receive_lines(do.name, [{"line_name": do.lines[0].name, "qty": 1}],
					warehouse=self._warehouse)
			self.assertIn("TEST-DC", str(caught.exception))
		finally:
			frappe.db.set_value("Distributor", "TEST-DC", "supplier", "DC-TEST-SUPPLIER",
				update_modified=False)

	def test_dead_or_drop_ship_orders_are_refused(self):
		draft = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}])
		with self.assertRaises(frappe.ValidationError):
			rb.receive_lines(draft.name, [{"line_name": draft.lines[0].name, "qty": 1}])
		drop = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}], status="Placed")
		frappe.db.set_value("Distributor Order", drop.name, "order_type", "Dropship Accessory",
			update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			rb.receive_lines(drop.name, [{"line_name": drop.lines[0].name, "qty": 1}])
