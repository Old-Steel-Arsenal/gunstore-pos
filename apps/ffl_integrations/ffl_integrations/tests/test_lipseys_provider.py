"""Lipsey's provider — pure: lanes by login, order bodies, the placement answer,
the status worked out from mirrored invoices, the shipping-day cursor; on a site:
placement through the real placement worker for all three order types (and the
unknown-outcome path that must never re-send), the OneDay mirror feeding the
poll, and receiving a store order by its acknowledgement."""
from __future__ import annotations

import types
import unittest
from datetime import date, datetime
from unittest import mock

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor import lipseys_provider as lp
from ffl_integrations.distributor.base import LaneNotSupported, PlanningError
from ffl_integrations.distributor.tasks import compute_do_updates
from ffl_integrations.distributor.util import FFL_FLAG_IS_LEGAL, legal_firearm
from ffl_integrations.lipseys.api_client import (
	LipseysClient, LipseysConfig, LipseysConfigError, LipseysError, LipseysPlacementUnknown,
)
from ffl_integrations.tests import lipseys_synth as syn


class _Settings(dict):
	"""What the provider reads off Lipseys Settings (``.get``), without a site."""


def _settings(**over):
	base = {"ordering_enabled": 1, "customer_number": "", "dealer_email": "dealer@example.com",
		"dealer_password": "*****", "dropship_email": "", "dropship_password": "",
		"disable_email": 0, "dropship_overnight": 0}
	base.update(over)
	return _Settings(base)


def _with_dropship(**over):
	return _settings(dropship_email="dropship@example.com", dropship_password="*****", **over)


def _ln(sku, qty=1, *, firearm=False, cost=10.0):
	return {"sku": sku, "item_code": sku, "qty": qty, "ffl_required": firearm,
		"is_firearm": firearm, "department": "", "unit_cost": cost}


def _do(order_type="Stocking", **over):
	base = dict(name="DO-TEST-0001", order_type=order_type, web_ref="", consumer_name="Jane Buyer",
		contact_num="(972) 555-1212", ship_address_line1="1 Main St", ship_address_line2="",
		ship_address_city="Springfield", ship_address_state="Texas", ship_address_zip="70001-1234",
		transfer_ffl_license="9-99-999-99-9A-99999", transfer_ffl_premise_state="TX",
		lines=[{"sku": syn.sku(1), "item_code": "X", "wish_qty": 2, "unit_cost": 100.0}])
	base.update(over)
	return types.SimpleNamespace(**base)


def _lines(*pairs):
	return [{"sku": s, "item_code": s, "description": "", "wish_qty": q, "unit_cost": 100.0}
		for s, q in pairs]


class TestLanes(unittest.TestCase):
	def test_dealer_login_alone_runs_the_store_lanes_only(self):
		caps = lp.LipseysProvider(settings=_settings()).capabilities()
		self.assertIn("to_store", caps)
		self.assertNotIn("dropship", caps)
		self.assertNotIn("fds", caps)

	def test_the_dropship_login_adds_both_dropship_lanes(self):
		caps = lp.LipseysProvider(settings=_with_dropship()).capabilities()
		self.assertTrue({"to_store", "dropship", "fds"} <= caps)

	def test_a_dropship_plan_without_the_dropship_login_is_refused(self):
		p = lp.LipseysProvider(settings=_settings())
		with self.assertRaises(LaneNotSupported):
			p.plan_fulfillment([_ln(syn.sku(1), firearm=True)], {"mode": "fds"})
		with self.assertRaises(LaneNotSupported):
			p.plan_fulfillment([_ln(syn.sku(2))], {"mode": "direct", "ship_to": {}})

	def test_store_plan_splits_firearms_from_accessories_on_the_dealer_account(self):
		plans = lp.LipseysProvider(settings=_settings()).plan_fulfillment(
			[_ln(syn.sku(1), firearm=True), _ln(syn.sku(2))], {"mode": "stocking"})
		self.assertEqual(len(plans), 2)
		self.assertEqual({p["account"] for p in plans}, {"dealer"})
		self.assertEqual(plans[0]["lines"][0]["sku"], syn.sku(1))

	def test_classification_divergence_still_holds_the_order(self):
		line = {**_ln(syn.sku(1), firearm=False), "legal_firearm": True}
		with self.assertRaises(PlanningError):
			lp.LipseysProvider(settings=_settings()).plan_fulfillment([line], {"mode": "stocking"})

	def test_big_carts_chunk_at_the_line_cap(self):
		lines = [_ln(syn.sku(n)) for n in range(1, lp.MAX_ORDER_LINES + 2)]
		plans = lp.LipseysProvider(settings=_settings()).plan_fulfillment(lines, {"mode": "stocking"})
		self.assertEqual([len(p["lines"]) for p in plans], [lp.MAX_ORDER_LINES, 1])

	def test_the_ffl_flag_is_the_legal_answer_for_lipseys(self):
		self.assertIn("lipseys", FFL_FLAG_IS_LEGAL)
		row = {"sku": syn.sku(1), "ffl_required": True, "product_class": "Gun Parts", "legal": None}
		self.assertTrue(legal_firearm(row, "lipseys"))
		self.assertFalse(legal_firearm({**row, "ffl_required": False}, "lipseys"))

	def test_firearms_cannot_drop_ship_to_california(self):
		class P(lp.LipseysProvider):
			def __init__(self, row):
				super().__init__(settings=_with_dropship())
				self.row = row

			def catalog_row(self, sku):
				return self.row

		gun = {"sku": syn.sku(1), "ffl_required": True, "legal": None}
		self.assertEqual(P(gun).blocked_states(syn.sku(1)), ["CA"])
		self.assertEqual(P({**gun, "ffl_required": False}).blocked_states(syn.sku(1)), [])
		self.assertEqual(P(None).blocked_states(syn.sku(1)), [])


class TestOrderBodies(unittest.TestCase):
	def test_store_order_is_an_api_order_with_no_address(self):
		endpoint, body = lp.order_request(_do(), lanes.STOCKING, _lines((syn.sku(1), 2)),
			_settings(disable_email=1))
		self.assertEqual(endpoint, "Order/APIOrder")
		self.assertEqual(body, {"PONumber": "DO-TEST-0001", "DisableEmail": True,
			"Items": [{"ItemNo": syn.sku(1), "Quantity": 2}]})

	def test_accessory_dropship_carries_the_buyer_as_billing_and_shipping(self):
		endpoint, body = lp.order_request(_do("Dropship Accessory"), lanes.DIRECT,
			_lines((syn.sku(2), 1)), _settings(dropship_overnight=1))
		self.assertEqual(endpoint, "Order/DropShip")
		self.assertEqual(body["PoNumber"], "DO-TEST-0001")
		for prefix in ("Billing", "Shipping"):
			self.assertEqual(body[f"{prefix}Name"], "Jane Buyer")
			self.assertEqual(body[f"{prefix}AddressState"], "TX")
			self.assertEqual(body[f"{prefix}AddressZip"], "70001")
		self.assertTrue(body["Overnight"])
		self.assertNotIn("Ffl", body)

	def test_incomplete_buyer_address_is_refused_before_lipseys(self):
		for over in ({"ship_address_zip": "7000"}, {"ship_address_state": "Nowhere"},
				{"ship_address_line1": ""}, {"consumer_name": ""}):
			with self.subTest(over), self.assertRaises(LipseysError):
				lp.order_request(_do("Dropship Accessory", **over), lanes.DIRECT,
					_lines((syn.sku(2), 1)), _settings())

	def test_firearm_dropship_sends_the_15_character_ffl_and_never_an_address(self):
		endpoint, body = lp.order_request(_do("Dropship Firearm"), lanes.FDS,
			_lines((syn.sku(1), 1)), _settings())
		self.assertEqual(endpoint, "Order/DropShipFirearm")
		self.assertEqual(body["Ffl"], "999999999A99999")
		self.assertEqual(body["Po"], "DO-TEST-0001")
		self.assertEqual(body["Phone"], "9725551212")
		self.assertFalse(body["DelayShipping"])
		self.assertFalse(any("Address" in k for k in body))

	def test_firearm_dropship_refusals(self):
		for over in ({"transfer_ffl_license": "9-99-999"}, {"transfer_ffl_premise_state": "California"},
				{"transfer_ffl_premise_state": "CA"}, {"contact_num": ""}, {"consumer_name": ""}):
			with self.subTest(over), self.assertRaises(LipseysError):
				lp.order_request(_do("Dropship Firearm", **over), lanes.FDS,
					_lines((syn.sku(1), 1)), _settings())


class TestPlacementAnswer(unittest.TestCase):
	def test_api_order_fill_short_refused_and_two_open_orders(self):
		lines = _lines((syn.sku(1), 2), (syn.sku(2), 3), (syn.sku(3), 1), (syn.sku(4), 1))
		env = syn.api_order(
			syn.order_line(syn.sku(1), requested=2, price=98.0, order="7000001"),
			syn.order_line(syn.sku(2), requested=3, fulfilled=1, price=10.0, order="7000002"),
			syn.order_line(syn.sku(3), requested=1, fulfilled=0, order="7000001",
				errors=["Item blocked"], order_error=True))
		res = lp.normalize_placement(env, lines, consumer_order=False)
		self.assertEqual(res["web_ref"], "7000001,7000002")
		by = {a["sku"]: a for a in res["ack_lines"]}
		self.assertEqual((by[syn.sku(1)]["ack_qty"], by[syn.sku(1)]["unit_cost"], by[syn.sku(1)]["item_status"]),
			(2, 98.0, ""))
		self.assertEqual((by[syn.sku(2)]["ack_qty"], by[syn.sku(2)]["item_status"]), (1, "SHORT"))
		self.assertEqual((by[syn.sku(3)]["ack_qty"], by[syn.sku(3)]["item_status"]), (0, "CANCELLED"))
		self.assertEqual((by[syn.sku(4)]["ack_qty"], by[syn.sku(4)]["item_status"]), (0, "CANCELLED"))
		self.assertEqual(res["freight"], 0.0)
		self.assertEqual(res["hold_status"], "")
		self.assertEqual(res["totals"]["subtotal"], 206.0)
		self.assertTrue(any("Item blocked" in n for n in res["notes"]))

	def test_dropship_answer_carries_hold_and_freight(self):
		env = syn.dropship_answer(syn.order_line(syn.sku(2), requested=1, price=20.0, order="7000101"),
			hold=True, freight=12.5, changes=["Price changed"])
		res = lp.normalize_placement(env, _lines((syn.sku(2), 1)), consumer_order=True)
		self.assertEqual(res["web_ref"], "7000101")
		self.assertEqual(res["hold_status"], lp.HOLD_ACCOUNT)
		self.assertEqual(res["freight"], 12.5)
		self.assertEqual(res["totals"]["total"], 32.5)
		self.assertTrue(any("Price changed" in n for n in res["notes"]))

	def test_an_outright_refusal_raises_and_places_nothing(self):
		env = syn.api_order(success=False, errors=["Account on credit hold"])
		with self.assertRaises(LipseysError) as ctx:
			lp.normalize_placement(env, _lines((syn.sku(1), 1)), consumer_order=False)
		self.assertNotIsInstance(ctx.exception, LipseysPlacementUnknown)
		self.assertIn("credit hold", str(ctx.exception))

	def test_every_line_refused_without_an_order_is_a_refusal(self):
		env = syn.api_order(syn.order_line(syn.sku(1), fulfilled=0, order="", order_error=True,
			errors=["Not valid for ship-to"]), success=False)
		with self.assertRaises(LipseysError):
			lp.normalize_placement(env, _lines((syn.sku(1), 1)), consumer_order=False)

	def test_success_without_any_line_is_an_unknown_outcome(self):
		with self.assertRaises(LipseysPlacementUnknown):
			lp.normalize_placement(syn.api_order(), _lines((syn.sku(1), 1)), consumer_order=False)

	def test_an_answer_that_cannot_be_read_is_an_unknown_outcome_sent_once(self):
		odd = {**syn.order_line(syn.sku(1)), "fulfilledQuantity": "lots"}
		session = syn.FakeSession({"Authentication/Login": syn.login_ok(),
			"Order/APIOrder": syn.api_order(odd)})
		p = lp.LipseysProvider(settings=_settings(), client_factory=lambda a: LipseysClient(
			LipseysConfig(email="d@example.com", password="pw", account=a), session=session))
		with self.assertRaises(LipseysPlacementUnknown) as ctx:
			p._place(_do())
		self.assertIn("may or may not have been placed", str(ctx.exception))
		self.assertEqual(session.paths().count("Order/APIOrder"), 1)


class TestDerivedStatus(unittest.TestCase):
	def _lines(self):
		return [{"sku": syn.sku(1), "ack_qty": 2, "item_status": ""},
			{"sku": syn.sku(2), "ack_qty": 0, "item_status": "CANCELLED"}]

	def test_before_any_invoice_the_order_is_acknowledged(self):
		st = lp.derive_status(self._lines(), "7000001", "", [])
		do_lines = [{"sku": syn.sku(1), "wish_qty": 2, "line_status": "Pending"},
			{"sku": syn.sku(2), "wish_qty": 1, "line_status": "Pending"}]
		res = compute_do_updates(do_lines, st)
		self.assertEqual(res["status"], "Acknowledged")
		self.assertEqual(res["lines"][syn.sku(1)]["line_status"], "Acked")
		self.assertEqual(res["lines"][syn.sku(2)]["line_status"], "Killed")

	def test_an_invoice_ships_every_acked_line_with_its_tracking(self):
		inv = [{"invoice_number": "8000001", "tracking_numbers": "1ZA,1ZB"},
			{"invoice_number": "8000002", "tracking_numbers": "1ZB"}]
		st = lp.derive_status(self._lines(), "7000001", lp.HOLD_ACCOUNT, inv)
		self.assertEqual(st["hold_status"], "")
		res = compute_do_updates([{"sku": syn.sku(1), "wish_qty": 2}, {"sku": syn.sku(2), "wish_qty": 1}], st)
		self.assertEqual(res["status"], "Complete")
		line = res["lines"][syn.sku(1)]
		self.assertEqual((line["invoiced_qty"], line["invoices"], line["tracking_numbers"]),
			(2, "8000001,8000002", "1ZA,1ZB"))
		self.assertEqual(res["lines"][syn.sku(2)]["invoiced_qty"], 0)

	def test_a_hold_stays_until_the_first_invoice(self):
		st = lp.derive_status(self._lines(), "7000101", lp.HOLD_ACCOUNT, [])
		self.assertEqual(compute_do_updates([{"sku": syn.sku(1), "wish_qty": 2}], st)["status"], "Hold")


class TestShippingDays(unittest.TestCase):
	def test_first_run_reads_only_the_last_settled_day(self):
		self.assertEqual(lp.due_days(None, datetime(2026, 10, 14, 9)), [date(2026, 10, 13)])

	def test_before_the_settle_hour_yesterday_is_not_over_yet(self):
		self.assertEqual(lp.due_days(None, datetime(2026, 10, 14, 2)), [date(2026, 10, 12)])

	def test_catch_up_from_the_cursor_is_capped_at_seven_days(self):
		self.assertEqual(lp.due_days(date(2026, 10, 11), datetime(2026, 10, 14, 9)),
			[date(2026, 10, 12), date(2026, 10, 13)])
		days = lp.due_days(date(2026, 9, 1), datetime(2026, 10, 14, 9))
		self.assertEqual((len(days), days[0], days[-1]), (7, date(2026, 10, 7), date(2026, 10, 13)))
		self.assertEqual(lp.due_days(date(2026, 10, 13), datetime(2026, 10, 14, 9)), [])

	def test_rows_group_into_one_invoice_each(self):
		rows = [("dealer", syn.shipment("8000001", po="DO-1", tracking="1ZA")),
			("dealer", syn.shipment("8000001", po="DO-1", tracking="1ZB")),
			("fulfillment", syn.shipment("8000002", po="DO-2", order="7000101", tracking="")),
			("dealer", syn.shipment("", po="DO-3"))]
		inv = {i["invoice_number"]: i for i in lp.group_shipments(rows, date(2026, 10, 13))}
		self.assertEqual(sorted(inv), ["8000001", "8000002"])
		self.assertEqual(inv["8000001"]["tracking_numbers"], ["1ZA", "1ZB"])
		self.assertEqual(inv["8000001"]["invoice_date"], "2026-10-13")
		self.assertEqual((inv["8000002"]["account"], inv["8000002"]["order_ref"]),
			("fulfillment", "7000101"))


class TestAvailability(unittest.TestCase):
	def test_validate_item_answer_shapes(self):
		row = lp.availability_row(syn.sku(1), 2, syn.validate(1, qty=5)["data"])
		self.assertEqual((row["qty"], row["cost"], row["available"], row["orderable"]),
			(5, 101.5, True, True))
		self.assertFalse(lp.availability_row(syn.sku(1), 2, syn.validate(1, allocated=True)["data"])["available"])
		blocked = lp.availability_row(syn.sku(1), 1, syn.validate(1, blocked=True)["data"])
		self.assertEqual((blocked["available"], blocked["orderable"]), (False, False))
		self.assertFalse(lp.availability_row("NOPE", 1, None)["orderable"])

	def test_live_check_on_the_dealer_login(self):
		session = syn.FakeSession({"Authentication/Login": syn.login_ok(),
			"Items/ValidateItem": lambda body: syn.validate(int(body[-5:]), qty=1)})
		p = lp.LipseysProvider(settings=_settings(), client_factory=lambda account: LipseysClient(
			LipseysConfig(email="d@example.com", password="pw", account=account), session=session))
		rows = p.check_availability([{"sku": syn.sku(1), "qty": 1}, {"sku": syn.sku(2), "qty": 3}])
		self.assertEqual([r["available"] for r in rows], [True, False])

	def test_ordering_off_reads_the_catalog_and_never_calls_lipseys(self):
		class P(lp.LipseysProvider):
			def catalog_row(self, sku):
				return {"sku": sku, "qty": 4, "cost": 50.0, "discontinued": False}

		calls = []
		p = P(settings=_settings(ordering_enabled=0), client_factory=lambda a: calls.append(a))
		rows = p.check_availability([{"sku": syn.sku(1), "qty": 2}])
		self.assertEqual((rows[0]["qty"], rows[0]["available"]), (4, True))
		self.assertEqual(calls, [])


try:
	import frappe
	from frappe.tests.utils import FrappeTestCase
except Exception:  # pragma: no cover — pure runs without bench
	FrappeTestCase = None


if FrappeTestCase:
	ITEM = "LIP-TEST-ACC"

	class TestLipseysOnSite(FrappeTestCase):
		@classmethod
		def setUpClass(cls):
			super().setUpClass()
			from ffl_integrations.lipseys import seed
			seed.ensure_distributor()
			cls.dist = frappe.db.get_value("Distributor", {"provider": "lipseys"}, "name")
			cls.seeded_enabled = frappe.db.get_value("Distributor", cls.dist, "enabled")
			if not frappe.db.exists("Item", ITEM):
				group = "Products" if frappe.db.exists("Item Group", "Products") else "All Item Groups"
				frappe.get_doc({"doctype": "Item", "item_code": ITEM,
					"item_name": "Lipsey's test accessory", "item_group": group, "stock_uom": "Nos",
					"is_stock_item": 1}).insert(ignore_permissions=True)

		def setUp(self):
			s = frappe.get_single("Lipseys Settings")
			s.update({"ordering_enabled": 1, "dealer_email": "dealer@example.com",
				"dealer_password": "pw-dealer", "dropship_email": "dropship@example.com",
				"dropship_password": "pw-dropship", "customer_number": "",
				"shipping_polled_through": None})
			s.save(ignore_permissions=True)
			frappe.clear_document_cache("Lipseys Settings", "Lipseys Settings")
			self.session = syn.FakeSession({"Authentication/Login": syn.login_ok()})
			client = lambda account="dealer": LipseysClient(  # noqa: E731
				LipseysConfig(email=f"{account}@example.com", password="pw", account=account,
					base_url="https://api.example.invalid/api/Integration"), session=self.session)
			patcher = mock.patch.object(LipseysClient, "from_settings", staticmethod(client))
			patcher.start()
			self.addCleanup(patcher.stop)

		def _do(self, order_type="Stocking", qty=2, **over):
			doc = {"doctype": "Distributor Order", "distributor": self.dist,
				"order_type": order_type,
				"account": "fulfillment" if order_type.startswith("Dropship") else "dealer",
				"lines": [{"sku": syn.sku(1), "item_code": ITEM, "wish_qty": qty,
					"unit_cost": 90.0}]}
			if order_type == "Dropship Accessory":
				doc.update(consumer_name="Jane Buyer", contact_num="9725551212",
					ship_address_line1="1 Main St", ship_address_city="Springfield",
					ship_address_state="TX", ship_address_zip="70001")
			if order_type == "Dropship Firearm":
				doc.update(consumer_name="Jane Buyer", contact_num="9725551212",
					transfer_ffl_license="9-99-999-99-9A-99999", transfer_ffl_premise_state="TX")
			doc.update(over)
			do = frappe.get_doc(doc).insert(ignore_permissions=True)
			do.db_set("status", "Queued")
			return do

		def _place(self, do):
			from ffl_integrations.distributor import tasks
			tasks.place_order_job(do.name)
			return frappe.get_doc("Distributor Order", do.name)

		def test_registry_has_lipseys_and_seed_is_idempotent_and_disabled(self):
			from ffl_integrations.distributor import api as dist_api
			from ffl_integrations.lipseys import seed
			self.assertEqual(dist_api.get_provider("lipseys").code, "lipseys")
			self.assertFalse(seed.ensure_distributor())
			self.assertEqual(self.seeded_enabled, 0)
			self.assertEqual(self.dist, "Lipsey's")

		def test_store_order_places_through_the_worker_and_records_the_ack(self):
			self.session.routes["Order/APIOrder"] = syn.api_order(
				syn.order_line(syn.sku(1), requested=2, fulfilled=2, price=88.5, order="7000001"))
			do = self._place(self._do())
			self.assertEqual(do.status, "Placed")
			self.assertEqual(do.web_ref, "7000001")
			self.assertEqual((do.lines[0].ack_qty, do.lines[0].unit_cost), (2, 88.5))
			sent = next(c for c in self.session.calls if c["path"] == "Order/APIOrder")
			self.assertEqual(sent["body"]["PONumber"], do.name)
			self.assertNotIn("pw", do.raw_response)

		def test_accessory_dropship_goes_on_the_dropship_login(self):
			self.session.routes["Order/DropShip"] = syn.dropship_answer(
				syn.order_line(syn.sku(1), requested=1, price=20.0, order="7000101"), freight=9.5)
			do = self._place(self._do("Dropship Accessory", qty=1))
			self.assertEqual((do.status, do.web_ref, do.freight_cost), ("Placed", "7000101", 9.5))
			logins = [c["body"]["Email"] for c in self.session.calls
				if c["path"] == "Authentication/Login"]
			self.assertEqual(logins, ["fulfillment@example.com"])

		def test_firearm_dropship_held_on_account_hold(self):
			self.session.routes["Order/DropShipFirearm"] = syn.dropship_answer(
				syn.order_line(syn.sku(1), requested=1, price=400.0, order="7000102"), hold=True)
			do = self._place(self._do("Dropship Firearm", qty=1))
			self.assertEqual((do.status, do.hold_status), ("Hold", lp.HOLD_ACCOUNT))
			sent = next(c for c in self.session.calls if c["path"] == "Order/DropShipFirearm")
			self.assertEqual(sent["body"]["Ffl"], "999999999A99999")

		def test_unknown_outcome_goes_to_error_and_is_sent_exactly_once(self):
			self.session.routes["Order/APIOrder"] = syn.TIMEOUT
			do = self._place(self._do())
			self.assertEqual(do.status, "Error")
			self.assertIn("may or may not have been placed", do.error)
			self.assertEqual(self.session.paths().count("Order/APIOrder"), 1)
			self.assertEqual(do.web_ref or "", "")

		def test_a_placed_order_is_never_sent_again(self):
			do = self._do()
			do.db_set("web_ref", "7000001")
			do = self._place(do)
			self.assertEqual(do.status, "Error")
			self.assertNotIn("Order/APIOrder", self.session.paths())

		def test_live_orders_off_sends_nothing(self):
			frappe.db.set_single_value("Lipseys Settings", "ordering_enabled", 0)
			frappe.clear_document_cache("Lipseys Settings", "Lipseys Settings")
			do = self._place(self._do())
			self.assertEqual(do.status, "Error")
			self.assertEqual(self.session.calls, [])

		def test_dropship_without_the_dropship_login_sends_nothing(self):
			frappe.db.set_single_value("Lipseys Settings", "dropship_email", "")
			frappe.clear_document_cache("Lipseys Settings", "Lipseys Settings")
			do = self._place(self._do("Dropship Accessory", qty=1))
			self.assertEqual(do.status, "Error")
			self.assertEqual(self.session.calls, [])

		def test_store_order_is_receivable_by_its_ack_before_any_invoice(self):
			from ffl_integrations.distributor import receive_bridge
			self.session.routes["Order/APIOrder"] = syn.api_order(
				syn.order_line(syn.sku(1), requested=2, fulfilled=1, price=88.5))
			do = self._place(self._do())
			cands = [c for c in receive_bridge.match_incoming(ITEM)["candidates"]
				if c["do_name"] == do.name]
			self.assertEqual(len(cands), 1)
			self.assertEqual((cands[0]["outstanding"], cands[0]["unit_cost"]), (1, 88.5))

		def _sync_day(self, rows, now=datetime(2026, 10, 14, 9), *, from_cursor="2026-10-12"):
			"""One invoice-sync run at ``now``; OneDay has ``rows`` for 2026-10-13."""
			from ffl_integrations.distributor import api as dist_api
			from ffl_integrations.distributor import invoices
			frappe.db.set_value("Distributor", self.dist, "enabled", 1)
			if from_cursor:
				frappe.db.set_single_value("Lipseys Settings", "shipping_polled_through", from_cursor)
				frappe.clear_document_cache("Lipseys Settings", "Lipseys Settings")
			self.session.routes["Shipping/OneDay"] = lambda day: syn.one_day(*rows) \
				if day == "10/13/2026" else syn.one_day()
			only_lipseys = {"lipseys": dist_api.get_providers()["lipseys"]}
			with mock.patch.object(invoices, "get_providers", lambda: only_lipseys), \
					mock.patch("frappe.utils.now_datetime", lambda: now):
				return invoices.sync_invoices()

		def test_oneday_mirror_feeds_the_poll_and_completes_the_store_order(self):
			from ffl_integrations.distributor import tasks
			self.session.routes["Order/APIOrder"] = syn.api_order(
				syn.order_line(syn.sku(1), requested=2, fulfilled=2, price=88.5, order="7000001"))
			do = self._place(self._do())
			out = self._sync_day([syn.shipment("8000001", po=do.name, order="7000001",
				tracking="1ZTEST0000000001")])
			self.assertEqual(out[self.dist], 1)
			inv = frappe.get_doc("Distributor Invoice", {"distributor": self.dist,
				"invoice_number": "8000001"})
			self.assertEqual((inv.distributor_order, inv.outside_pos, inv.destination),
				(do.name, 0, "Store"))
			self.assertEqual([(l.sku, l.qty, l.unit_cost) for l in inv.lines], [(syn.sku(1), 2, 88.5)])
			self.assertEqual(str(frappe.db.get_single_value("Lipseys Settings",
				"shipping_polled_through")), "2026-10-13")
			tasks.poll_order(do.name)
			do.reload()
			self.assertEqual(do.status, "Complete")
			self.assertEqual((do.lines[0].invoiced_qty, do.lines[0].invoices,
				do.lines[0].tracking_numbers), (2, "8000001", "1ZTEST0000000001"))
			# the next run asks for no day it already read
			calls = len(self.session.calls)
			self._sync_day([], from_cursor=None)
			self.assertEqual(self.session.paths()[calls:].count("Shipping/OneDay"), 0)

		def test_an_invoice_naming_a_shared_lipseys_order_ships_every_order_on_it(self):
			"""APIOrder adds a later store order to the open Lipsey's order: the
			invoice names the first PO, the second order matches by order number."""
			from ffl_integrations.distributor import tasks
			self.session.routes["Order/APIOrder"] = [
				syn.api_order(syn.order_line(syn.sku(1), requested=2, order="7000005")),
				syn.api_order(syn.order_line(syn.sku(1), requested=1, order="7000005"))]
			first = self._place(self._do())
			second = self._place(self._do(qty=1))
			for d in (first, second):
				d.db_set("placed_at", "2026-10-12 10:00:00")
			late = self._do(qty=1)
			self.session.routes["Order/APIOrder"] = syn.api_order(
				syn.order_line(syn.sku(1), requested=1, order="7000005"))
			late = self._place(late)   # placed after the invoice day: not on it
			self._sync_day([syn.shipment("8000009", po=first.name, order="7000005")])
			for d in (first, second, late):
				tasks.poll_order(d.name)
			self.assertEqual(frappe.db.get_value("Distributor Order", first.name, "status"), "Complete")
			self.assertEqual(frappe.db.get_value("Distributor Order", second.name, "status"), "Complete")
			self.assertNotEqual(frappe.db.get_value("Distributor Order", late.name, "status"), "Complete")

		def test_an_invoice_no_order_matches_is_outside_pos_without_lines(self):
			self._sync_day([syn.shipment("8000077", po="WEB-ORDER-1", order="7999999")])
			inv = frappe.get_doc("Distributor Invoice", {"distributor": self.dist,
				"invoice_number": "8000077"})
			self.assertEqual((inv.outside_pos, inv.destination, len(inv.lines)), (1, "Store", 0))

		def test_a_failed_day_keeps_the_cursor(self):
			from ffl_integrations.distributor import api as dist_api
			from ffl_integrations.distributor import invoices
			frappe.db.set_value("Distributor", self.dist, "enabled", 1)
			frappe.db.set_single_value("Lipseys Settings", "shipping_polled_through", "2026-10-12")
			frappe.clear_document_cache("Lipseys Settings", "Lipseys Settings")
			self.session.routes["Shipping/OneDay"] = syn.Resp({"message": "down"}, status=503)
			only_lipseys = {"lipseys": dist_api.get_providers()["lipseys"]}
			with mock.patch.object(invoices, "get_providers", lambda: only_lipseys), \
					mock.patch("frappe.utils.now_datetime", lambda: datetime(2026, 10, 14, 9)):
				invoices.sync_invoices()
			self.assertEqual(str(frappe.db.get_single_value("Lipseys Settings",
				"shipping_polled_through")), "2026-10-12")
			self.assertIn("2026-10-13", frappe.db.get_single_value("Lipseys Settings", "last_error"))

		def test_connection_test_signs_in_with_both_logins(self):
			self.session.routes["Items/ValidateItem"] = syn.envelope(None, success=False)
			from ffl_integrations.ffl_integrations.doctype.lipseys_settings.lipseys_settings import (
				test_connection,
			)
			checks = test_connection()["checks"]
			labels = {c["label"]: c["ok"] for c in checks}
			self.assertTrue(labels["Ordering API (dealer login)"])
			self.assertTrue(labels["Ordering API (drop-ship login)"])
			self.assertNotIn("Order/APIOrder", self.session.paths())


if __name__ == "__main__":
	unittest.main()
