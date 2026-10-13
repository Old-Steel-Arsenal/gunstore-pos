"""create_order counter drop-ship wiring.

These are the decisions create_order makes for a drop-ship counter order, factored
into pure/patchable helpers (the whole create_order flow — SO+SI build, submit — is
site-tested in test_counter_dropship_integration):

* the update_stock rule (a drop-ship line forces the order to
  defer, because ERPNext hard-throws on update_stock=1 + delivered_by_supplier);
* Business buyers are excluded (individual retail only);
* the ORIGINAL payload ship-to is carried to the DO, never the FFL premise;
* the ffl_counter_order_distributor hook fires with the right context.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_core.api import manual_order


class UpdateStockRuleTest(unittest.TestCase):
	"""Pinned: a drop-ship order can never dispose-now."""

	def test_local_dispose_now_updates_stock(self):
		self.assertEqual(manual_order._counter_update_stock(
			dispose_now=True, has_dropship=False), 1)

	def test_dropship_order_forced_to_defer(self):
		self.assertEqual(manual_order._counter_update_stock(
			dispose_now=True, has_dropship=True), 0)

	def test_deferred_order_never_updates_stock(self):
		self.assertEqual(manual_order._counter_update_stock(
			dispose_now=False, has_dropship=True), 0)
		self.assertEqual(manual_order._counter_update_stock(
			dispose_now=False, has_dropship=False), 0)


class BuyerExclusionTest(unittest.TestCase):
	def test_business_buyer_with_dropship_is_rejected(self):
		rows = [{"item_code": "X", "source": "dropship"}]
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._assert_dropship_buyer(rows, is_business=True)

	def test_individual_buyer_with_dropship_is_fine(self):
		rows = [{"item_code": "X", "source": "dropship"}]
		manual_order._assert_dropship_buyer(rows, is_business=False)

	def test_business_buyer_without_dropship_is_fine(self):
		rows = [{"item_code": "X", "source": "local"}]
		manual_order._assert_dropship_buyer(rows, is_business=True)


# MixedOrderBanTest retired with the ban itself: counter-fulfillment-legs
# lifts the mixed-order restriction; the successor coverage (mixed rows validate
# together, assertion-is-gone) lives in test_counter_mixed_order.py; the
# ship-to-store channel in test_counter_to_store.py.


class ShipToBlockTest(unittest.TestCase):
	def test_maps_payload_shape_to_orderplan_shape(self):
		block = manual_order._counter_ship_to_block(
			{"ship_to": {"address_line1": "1 Main", "address_line2": "Apt 2",
				"city": "Austin", "state": "TX", "pincode": "78701"}})
		self.assertEqual(block, {"line1": "1 Main", "line2": "Apt 2",
			"city": "Austin", "state": "TX", "zip": "78701"})

	def test_empty_ship_to_is_none(self):
		self.assertIsNone(manual_order._counter_ship_to_block({}))

	def test_state_is_normalised_at_the_entry_point(self):
		"""Normalise here so the Distributor Order's ship_address_state
		lands as a 2-letter code (its field format), not whatever the operator typed."""
		block = manual_order._counter_ship_to_block(
			{"ship_to": {"address_line1": "1 Main", "city": "Austin",
				"state": "Texas", "pincode": "78701"}})
		self.assertEqual(block["state"], "TX")

	def test_unrecognised_state_is_passed_through(self):
		block = manual_order._counter_ship_to_block(
			{"ship_to": {"address_line1": "1 Rue", "city": "Montreal",
				"state": "Ontario", "pincode": "H0H"}})
		self.assertEqual(block["state"], "Ontario")


class FflBlockTest(unittest.TestCase):
	def test_builds_block_from_atf_record(self):
		# The doctype's licence column is `ffl_number` (its autoname); `license` is the
		# OrderPlan contract key this block EMITS. This stub previously used the wrong
		# source name and so encoded the very bug the bench caught.
		rec = {"ffl_number": "1-23-456", "street": "9 Gun Rd", "city": "Dallas",
			"state": "TX", "zip": "75201"}
		with mock.patch.object(manual_order.frappe.db, "get_value",
				lambda dt, n, f, *a, **k: rec):
			block = manual_order._counter_ffl_block("ATF-REC-1")
		self.assertEqual(block["license"], "1-23-456")
		self.assertEqual(block["premise_state"], "TX")
		self.assertEqual(block["ffl_record"], "ATF-REC-1")

	def test_none_ffl_is_none(self):
		self.assertIsNone(manual_order._counter_ffl_block(None))


class ContextTest(unittest.TestCase):
	def test_context_carries_only_dropship_lines_and_original_ship_to(self):
		with mock.patch.object(manual_order, "is_firearm_item",
				lambda code: code == "RSR-GUN"), \
				mock.patch.object(manual_order, "_counter_ffl_block",
				lambda ffl: {"license": "1-23", "ffl_record": ffl}):
			ctx = manual_order._counter_distributor_context(
				payload={"ship_to": {"address_line1": "1 Main", "city": "Austin",
					"state": "TX", "pincode": "78701"}},
				rows=[
					{"item_code": "LOCAL-GUN", "qty": 1, "rate": 500, "serials": ["S1"]},
					{"item_code": "RSR-GUN", "qty": 1, "rate": 600, "source": "dropship",
						"dropship_distributor": "RSR", "restricted_states": "NY"},
				],
				destination_ffl="ATF-REC-1", is_business=False)
		self.assertEqual(len(ctx["lines"]), 1)              # local gun excluded
		self.assertEqual(ctx["lines"][0]["item_code"], "RSR-GUN")
		self.assertTrue(ctx["lines"][0]["is_firearm"])
		self.assertEqual(ctx["ship_to"]["state"], "TX")             # original ship-to
		self.assertEqual(ctx["destination_ffl"]["ffl_record"], "ATF-REC-1")
		self.assertEqual(ctx["buyer_type"], manual_order._BUYER_INDIVIDUAL)


class HookFireTest(unittest.TestCase):
	def test_fires_registered_hook_with_si_and_context(self):
		captured = []
		with mock.patch.object(manual_order.frappe, "get_hooks",
				lambda name, *a, **k: ["some.dotted.path"] if name == "ffl_counter_order_distributor" else []), \
				mock.patch.object(manual_order.frappe, "get_attr",
				lambda path: (lambda si, ctx: captured.append((si, ctx)))):
			manual_order._fire_counter_distributor_hook("SINV-1", {"lines": [1]})
		self.assertEqual(captured, [("SINV-1", {"lines": [1]})])

	def test_raise_propagates_to_roll_back_the_sale(self):
		def _boom(si, ctx):
			raise manual_order.frappe.ValidationError("RSR blocked")
		with mock.patch.object(manual_order.frappe, "get_hooks",
				lambda name, *a, **k: ["p"] if name == "ffl_counter_order_distributor" else []), \
				mock.patch.object(manual_order.frappe, "get_attr", lambda path: _boom):
			with self.assertRaises(manual_order.frappe.ValidationError):
				manual_order._fire_counter_distributor_hook("SINV-1", {})


if __name__ == "__main__":
	unittest.main()
