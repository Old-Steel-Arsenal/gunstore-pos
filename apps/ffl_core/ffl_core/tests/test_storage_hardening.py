"""Storage locations: UOM conversion, the one preselection rule, locks,
swallowed errors that must not show, cancel replay per line, a cancelled Stock Entry,
counts without N+1, and the trade-in endpoint with credit."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt

from ffl_core import storage
from ffl_core.api import storage as api
from ffl_core.tests.test_storage_counter import CounterStorageFixtures


class UomConversion(CounterStorageFixtures):
	def boxed_item(self):
		"""Sold by the Box of 50 (stock UOM Nos)."""
		code = self.item()
		doc = frappe.get_doc("Item", code)
		for u in ("Box",):
			if not frappe.db.exists("UOM", u):
				frappe.get_doc({"doctype": "UOM", "uom_name": u}).insert(ignore_permissions=True)
		doc.append("uoms", {"uom": "Box", "conversion_factor": 50})
		doc.save(ignore_permissions=True)
		return code

	def box_sale(self, item, boxes, loc):
		si = frappe.get_doc({"doctype": "Sales Invoice", "customer": self.buyer, "company": self.company,
			"update_stock": 1, "set_warehouse": self.warehouse,
			"items": [{"item_code": item, "qty": boxes, "uom": "Box", "conversion_factor": 50, "rate": 100,
				"warehouse": self.warehouse, "ffl_storage_location": loc}]})
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		return si

	def test_a_sale_by_the_box_takes_stock_units_out_of_the_location(self):
		r1, = self.open_zone(1)
		item = self.boxed_item()
		self.stock(item, 500)
		storage.put_qty(item, r1, 500)
		si = self.box_sale(item, 2, r1)
		self.assertEqual(si.items[0].stock_qty, 100)
		self.assertEqual(storage.qty_at(r1, item), 400)
		self.assertEqual(self.moves(item_code=item, voucher_no=si.name)[0].qty, 100)
		self.assertEqual(storage.unassigned_qty(self.company, item), 0)

	def test_a_return_by_the_box_puts_stock_units_back(self):
		r1, = self.open_zone(1)
		item = self.boxed_item()
		self.stock(item, 500)
		storage.put_qty(item, r1, 500)
		si = self.box_sale(item, 2, r1)
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		ret = make_return_doc("Sales Invoice", si.name)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		self.assertEqual(storage.qty_at(r1, item), 500)
		ret.cancel()
		self.assertEqual(storage.qty_at(r1, item), 400)


class OnePreselectionRule(CounterStorageFixtures):
	def test_sole_location_needs_exactly_one_place_and_nothing_unassigned(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, r1, 4)
		info = storage.item_locations(self.company, [ammo])[ammo]
		self.assertIsNone(info["sole"])             # 6 are still unassigned
		storage.put_qty(ammo, r1, 6)
		self.assertEqual(storage.item_locations(self.company, [ammo])[ammo]["sole"], r1)
		self.assertEqual(api.stock_locations([ammo])[ammo]["sole"], r1)
		storage.move(reason="move", item_code=ammo, qty=1, from_location=r1, to_location=r2)
		self.assertIsNone(storage.item_locations(self.company, [ammo])[ammo]["sole"])  # two places

	def test_dispose_lines_preselect_by_the_same_rule(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, r1, 4)
		line = {"line": "7", "item_code": ammo, "qty": 2}
		self.assertIsNone(storage.dispose_pick_lines(self.company, [line])[0]["chosen"])
		storage.put_qty(ammo, r1, 6)
		out = storage.dispose_pick_lines(self.company, [line])[0]
		self.assertEqual((out["chosen"], out["line"], out["item_name"]), (r1, "7", f"Item {ammo}"))
		# a location already recorded on the line wins
		self.assertEqual(storage.dispose_pick_lines(self.company, [{**line, "chosen": "X"}])[0]["chosen"], "X")

	def test_picks_are_parsed_once_for_everyone(self):
		self.assertEqual(storage.parse_picks('{"1": " FT-A ", "2": ""}'), {"1": "FT-A"})
		self.assertEqual(storage.parse_picks({3: "B", 4: None}), {"3": "B"})
		self.assertEqual(storage.parse_picks(""), {})
		self.assertEqual(storage.parse_picks(None), {})


class LocksAndSwallowedErrors(CounterStorageFixtures):
	def test_a_quantity_move_locks_the_item_and_reads_unassigned_fresh(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		locked_items, fresh = [], []
		real_get_value, real_unassigned = frappe.db.get_value, storage.unassigned_qty

		def spy_get_value(doctype, *a, **kw):
			if doctype == "Item" and kw.get("for_update"):
				locked_items.append(a[0])
			return real_get_value(doctype, *a, **kw)

		def spy_unassigned(company, item, **kw):
			fresh.append(kw.get("lock"))
			return real_unassigned(company, item, **kw)

		with patch.object(frappe.db, "get_value", side_effect=spy_get_value), \
				patch.object(storage, "unassigned_qty", side_effect=spy_unassigned):
			storage.move(reason="move", item_code=ammo, qty=2, to_location=r1, check_unassigned=True)
		self.assertIn(ammo, locked_items)
		self.assertEqual(fresh, [True])

	def test_a_gun_move_locks_the_serial_before_reading_it(self):
		a, = self.slots(1)
		g = self.gun()
		locked = []
		real = frappe.db.get_value

		def spy(doctype, *args, **kw):
			if doctype == "Serial No" and kw.get("for_update"):
				locked.append(args[0])
			return real(doctype, *args, **kw)

		with patch.object(frappe.db, "get_value", side_effect=spy):
			storage.assign_serial(g, a)
		self.assertIn(g, locked)

	def test_a_swallowed_failure_leaves_no_red_error_behind(self):
		def boom():
			frappe.throw("a swallowed error")
		frappe.local.message_log = []
		frappe.msgprint("queued by the real request")
		before = list(frappe.local.message_log)
		with patch("frappe.log_error"):
			self.assertFalse(storage.never_block(boom))
		self.assertEqual(frappe.local.message_log, before)  # the request's own message stays, ours is gone

	def test_a_placement_that_no_longer_fits_is_reported_not_shown_in_red(self):
		a, = self.slots(1)
		storage.assign_serial(self.gun(), a)
		mine = self.gun()
		frappe.local.message_log = []
		problems = storage.apply_placements([{"serial": mine, "location": a}], strict=False)
		self.assertEqual(len(problems), 1)
		self.assertIn("already holds", problems[0])
		self.assertEqual(frappe.local.message_log, [])
		self.assertIsNone(self.where(mine))


class CancelReplay(CounterStorageFixtures):
	def two_line_sale(self):
		r1, r2 = self.open_zone(2)
		a, b = self.item(), self.item()
		for item, r in ((a, r1), (b, r2)):
			self.stock(item, 5)
			storage.put_qty(item, r, 5)
		return r1, r2, a, b, self.sale([(a, 2, r1), (b, 3, r2)])

	def test_units_may_return_into_a_location_disabled_since(self):
		r1, r2, a, b, si = self.two_line_sale()
		storage.take_qty(a, r1, 3, reason="adjust")      # empty it so it can be disabled
		storage.set_disabled(None, r1, 1)
		si.cancel()
		self.assertEqual(storage.qty_at(r1, a), 2)       # went back, disabled or not
		self.assertEqual(storage.qty_at(r2, b), 5)

	def test_a_line_that_cannot_replay_never_stops_the_others(self):
		r1, r2, a, b, si = self.two_line_sale()
		real_move = storage.move

		def flaky(**kw):
			if kw.get("item_code") == a:
				frappe.throw("this line cannot go back")
			return real_move(**kw)

		with patch.object(storage, "move", side_effect=flaky), patch("frappe.log_error") as logged:
			done = storage.reverse_voucher("Sales Invoice", si.name)
		self.assertEqual(done, 1)
		self.assertTrue(logged.called)
		self.assertEqual(storage.qty_at(r2, b), 5)       # the healthy line replayed
		self.assertEqual(storage.qty_at(r1, a), 3)       # the failed one stays out: unassigned later


class StockEntryCancel(CounterStorageFixtures):
	def test_cancelling_a_material_issue_puts_its_units_back(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		from ffl_core.firearm import issue_stock_qty
		name = issue_stock_qty(ammo, 2, self.warehouse)
		storage.move(reason="ship", item_code=ammo, qty=2, from_location=r1,
			voucher_type="Stock Entry", voucher_no=name)
		self.assertEqual(storage.qty_at(r1, ammo), 3)
		frappe.get_doc("Stock Entry", name).cancel()
		self.assertEqual(storage.qty_at(r1, ammo), 5)


class CountsWithoutNPlusOne(CounterStorageFixtures):
	def test_the_header_counts_agree_with_the_full_lists(self):
		a, = self.slots(1)
		self.gun()
		over, loose = self.item(), self.item()
		r1, = self.open_zone(1)
		self.stock(over, 4)
		storage.put_qty(over, r1, 4)
		self._entry({"stock_entry_type": "Material Issue", "items": [
			{"item_code": over, "qty": 1, "s_warehouse": self.warehouse}]})
		self.stock(loose, 3)
		full = storage.unassigned_summary(self.company)
		counts = storage.unassigned_counts(self.company)
		self.assertEqual(counts, {"guns": len(full["guns"]), "items": len(full["items"]),
			"to_confirm": len(full["to_confirm"])})
		self.assertEqual(api.get_map(self.company)["unassigned"], counts)

	def test_item_names_come_in_one_query_not_one_per_item(self):
		r1, = self.open_zone(1)
		items = [self.item() for _ in range(6)]
		for i in items:
			self.stock(i, 1)
		calls = []
		real = frappe.db.get_value

		def spy(doctype, *a, **k):
			calls.append(doctype)
			return real(doctype, *a, **k)

		with patch.object(frappe.db, "get_value", side_effect=spy):
			out = storage.unassigned_summary(self.company)
		self.assertTrue({i for i in items} <= {r["item_code"] for r in out["items"]})
		self.assertLessEqual(calls.count("Item"), 1)


class TradeInWithCredit(CounterStorageFixtures):
	def setUp(self) -> None:
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; these tests are not about FastBound.
		# A site where another module saved credentials (they persist) would otherwise reach the live client.
		from unittest.mock import patch

		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in storage trade-in tests"))
		p.start()
		self.addCleanup(p.stop)

	def test_a_trade_in_for_credit_parks_the_location_and_a_cash_one_places_it_now(self):
		import json
		from ffl_core.api import firearm_intake, trade_in
		a, b = self.slots(2)
		cust = f"_Test STG Seller2 {self.tag}"
		frappe.get_doc({"doctype": "Customer", "customer_name": cust, "customer_group": "Individual",
			"territory": "All Territories"}).insert(ignore_permissions=True)
		base = {"source_name": cust, "source_first_name": "Jo", "source_last_name": "Seller",
			"source_address_line1": "1 Main St", "source_city": "Springfield", "source_state": "TX",
			"source_zip": "75024", "id_type": "Driver License", "id_number": "D1",
			"manufacturer": "Colt", "model_name": "1911", "caliber": ".45 ACP", "condition_grade": "Good",
			"acquisition_cost": 300, "firearm_type": "Pistol"}
		credit = trade_in.create_trade_in_intake({**base, "serial_no": f"TC-{self.tag}", "apply_credit": 1,
			"storage_location": a})
		parked = json.loads(frappe.db.get_value("FFL Acquisition", credit["acquisition"], "pending_intake_payload"))
		self.assertEqual(parked["storage_location"], a)
		self.assertIsNone(self.where(f"TC-{self.tag}"))  # books (and is placed) at checkout
		firearm_intake.complete_intake(credit["acquisition"])
		self.assertEqual(self.where(f"TC-{self.tag}"), a)
		trade_in.create_trade_in_intake({**base, "serial_no": f"TD-{self.tag}", "payout_method": "Cash",
			"storage_location": b})
		self.assertEqual(self.where(f"TD-{self.tag}"), b)
