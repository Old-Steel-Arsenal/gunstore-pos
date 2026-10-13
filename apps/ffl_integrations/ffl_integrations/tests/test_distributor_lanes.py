"""The lane contract every provider is planned through (``distributor/lanes.py``).

A house states the lanes it runs in ``capabilities()``. The base template turns a
cart plus a ship context into orders of that lane — order type, account, destination
and split — so a new house implements its own rules only (which lanes, how a cart
splits, how a line ships). Pure: no site.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.base import DistributorProvider, LaneNotSupported, PlanningError


class _Provider(DistributorProvider):
	"""The smallest provider that can be planned: lanes, a cap, a ship method."""

	code = "fake"
	display_name = "Fake House"
	max_order_lines = 2

	def __init__(self, capabilities):
		self._capabilities = set(capabilities)

	def capabilities(self):
		return set(self._capabilities)

	def search(self, q, limit=10):
		return []

	def catalog_row(self, sku):
		return None

	def check_availability(self, lines):
		return []

	def place_order(self, do_name):
		return {}

	def get_order_status(self, do_name):
		return {}


_GUN = {"sku": "GUN", "item_code": "GUN", "qty": 1, "is_firearm": True, "ffl_required": True}
_ACC = {"sku": "ACC", "item_code": "ACC", "qty": 2, "is_firearm": False, "ffl_required": False}
_CTX = {"ship_to": {"line1": "5 Elm", "city": "Springfield", "state": "TX", "zip": "75024"},
	"destination_ffl": {"license": "1-99-888-01-2D-09999"},
	"consumer_name": "Ann", "contact_num": "555", "fill_or_kill": True}


class LaneTableTest(unittest.TestCase):
	def test_each_lane_knows_its_order_type_account_and_family(self):
		self.assertEqual(
			[(l.mode, l.order_type, l.account, l.family) for l in lanes.LANES.values()],
			[("stocking", "Stocking", "dealer", "store"),
				("to_store", "Procure To Store", "dealer", "store"),
				("direct", "Dropship Accessory", "fulfillment", "dropship"),
				("fds", "Dropship Firearm", "fulfillment", "dropship")])

	def test_the_doctype_offers_exactly_these_order_types(self):
		# The Select is the fourth place the names are written down; a lane added here
		# without adding it there fails at insert time, on a real order.
		doctype = json.loads((Path(__file__).parents[1] / "ffl_integrations" / "doctype"
			/ "distributor_order" / "distributor_order.json").read_text())
		field = next(f for f in doctype["fields"] if f["fieldname"] == "order_type")
		# The Select's own order is its display order; what must match is the set.
		self.assertEqual(set(field["options"].split("\n")), set(lanes.ORDER_TYPES))

	def test_an_order_type_reads_back_as_its_mode(self):
		for mode, lane in lanes.LANES.items():
			self.assertEqual(lanes.mode_of(lane.order_type), mode)
		self.assertIsNone(lanes.mode_of("Something Else"))

	def test_store_lanes_stock_goods_and_drop_ship_lanes_do_not(self):
		self.assertTrue(lanes.stocks_goods("Stocking"))
		self.assertTrue(lanes.stocks_goods("Procure To Store"))
		self.assertFalse(lanes.stocks_goods("Dropship Firearm"))
		self.assertFalse(lanes.stocks_goods(""))

	def test_an_unknown_mode_is_a_restock(self):
		self.assertEqual(lanes.lane("").mode, lanes.STOCKING)
		self.assertEqual(lanes.lane("nonsense").mode, lanes.STOCKING)
		self.assertFalse(lanes.ships_to_consumer(None))

	def test_only_the_four_modes_name_a_lane(self):
		self.assertEqual({m for m in lanes.LANES if lanes.is_lane(m)},
			{"stocking", "to_store", "direct", "fds"})
		self.assertFalse(lanes.is_lane("dropship"))
		self.assertFalse(lanes.is_lane(""))


class PlanTemplateTest(unittest.TestCase):
	def test_a_store_only_house_plans_to_store_and_refuses_drop_ship(self):
		house = _Provider({"to_store"})
		plans = house.plan_fulfillment([_GUN], {**_CTX, "mode": "to_store"})
		self.assertEqual(len(plans), 1)
		self.assertEqual(plans[0]["order_type"], "Procure To Store")
		self.assertEqual(plans[0]["account"], "dealer")
		for mode, cart in (("direct", [_ACC]), ("fds", [_GUN])):
			with self.subTest(mode=mode):
				with self.assertRaises(LaneNotSupported) as ctx:
					house.plan_fulfillment(cart, {**_CTX, "mode": mode})
				self.assertIn("Fake House", str(ctx.exception))

	def test_a_drop_ship_only_house_refuses_the_store_lanes(self):
		house = _Provider({"dropship", "fds"})
		self.assertTrue(house.plan_fulfillment([_ACC], {**_CTX, "mode": "direct"}))
		for mode in ("to_store", "stocking"):
			with self.subTest(mode=mode):
				with self.assertRaises(LaneNotSupported):
					house.plan_fulfillment([_ACC], {**_CTX, "mode": mode})

	def test_each_lane_carries_only_its_own_destination(self):
		house = _Provider({"dropship", "fds", "to_store"})
		direct = house.plan_fulfillment([_ACC], {**_CTX, "mode": "direct"})[0]
		self.assertEqual(direct["ship_to"]["city"], "Springfield")
		self.assertIsNone(direct["ship_ffl"])
		fds = house.plan_fulfillment([_GUN], {**_CTX, "mode": "fds"})[0]
		self.assertIsNone(fds["ship_to"], "a firearm order never carries the buyer's address")
		self.assertEqual(fds["ship_ffl"], "1-99-888-01-2D-09999")
		self.assertEqual(fds["transfer_dealer"]["license"], "1-99-888-01-2D-09999")
		to_store = house.plan_fulfillment([_GUN], {**_CTX, "mode": "to_store"})[0]
		self.assertEqual(
			[to_store["ship_to"], to_store["ship_ffl"], to_store["transfer_dealer"],
				to_store["consumer_name"], to_store["contact_num"]],
			[None, None, None, "", ""])

	def test_firearms_and_accessories_never_share_an_order(self):
		plans = _Provider({"to_store"}).plan_fulfillment(
			[_GUN, _ACC], {**_CTX, "mode": "to_store"})
		self.assertEqual([[ln["sku"] for ln in p["lines"]] for p in plans], [["GUN"], ["ACC"]])

	def test_a_cart_over_the_line_cap_becomes_several_orders(self):
		lines = [{**_ACC, "sku": f"ACC-{i}", "item_code": f"ACC-{i}"} for i in range(5)]
		plans = _Provider({"to_store"}).plan_fulfillment(lines, {**_CTX, "mode": "stocking"})
		self.assertEqual([len(p["lines"]) for p in plans], [2, 2, 1])

	def test_a_classification_disagreement_stops_the_order(self):
		with self.assertRaises(PlanningError) as ctx:
			_Provider({"to_store"}).plan_fulfillment(
				[{**_GUN, "ffl_required": False}], {**_CTX, "mode": "to_store"})
		self.assertIn("Fake House", str(ctx.exception))

	def test_a_firearm_is_never_planned_to_the_buyers_address(self):
		house = _Provider({"dropship", "fds", "to_store"})
		with self.assertRaises(PlanningError) as ctx:
			house.plan_fulfillment([_GUN, _ACC], {**_CTX, "mode": "direct"})
		self.assertIn("transfer dealer", str(ctx.exception))

	def test_an_accessory_is_never_planned_to_the_transfer_dealer(self):
		house = _Provider({"dropship", "fds", "to_store"})
		with self.assertRaises(PlanningError):
			house.plan_fulfillment([_GUN, _ACC], {**_CTX, "mode": "fds"})

	def test_the_store_lanes_take_firearms_and_accessories_alike(self):
		house = _Provider({"to_store"})
		for mode in ("to_store", "stocking"):
			with self.subTest(mode=mode):
				self.assertEqual(
					len(house.plan_fulfillment([_GUN, _ACC], {**_CTX, "mode": mode})), 2)

	def test_a_mode_that_names_no_lane_is_refused(self):
		# It used to fall back to stocking — an order for a buyer, sent to the shop.
		with self.assertRaises(PlanningError) as ctx:
			_Provider({"to_store"}).plan_fulfillment([_ACC], {**_CTX, "mode": "dropship"})
		self.assertIn("dropship", str(ctx.exception))

	def test_an_empty_cart_plans_nothing(self):
		self.assertEqual(_Provider({"to_store"}).plan_fulfillment([], {"mode": "to_store"}), [])


if __name__ == "__main__":
	unittest.main()
