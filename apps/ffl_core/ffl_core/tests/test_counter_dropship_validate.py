"""``manual_order._validate_items`` drop-ship branch.

A counter order line tagged ``source="dropship"`` is fulfilled by RSR, not from
our shelf: it has NO local serial and NO warehouse, so the firearm one-serial
guard must not fire on it. A drop-ship firearm still keeps ``has_regulated`` (FDS
ships to the buyer's transfer FFL — that is exactly the destination-FFL + canonical
FFL validation create_order already enforces).

Pure/harness-safe: patches only the firearm classifier, the item-existence probe,
and the new ``_dropship_eligibility`` seam — never a blanket ``frappe.db`` stub.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_core.api import manual_order


def _exists_known(doctype, name=None, *a, **k):
	# Answer only the Item-existence question these lines ask; delegate nothing.
	if doctype == "Item":
		return name in {"RSR-ACC", "RSR-GUN", "LOCAL-GUN", "BARE-ACC"}
	return None


# Which item codes classify as (regulated) firearms — answered by question, not
# a positional script, so extra reads never shift the answer.
_GUNS = {"RSR-GUN", "LOCAL-GUN"}


_ELIG_CALLS = []


def _elig(item_code, distributor=None, sku=None, *, is_firearm=False):
	"""Stand-in for the provider-backed eligibility seam: RSR-* are eligible,
	BARE-ACC is not a drop-ship item. A line that names its house gets it back
	(with the house's SKU), like the real seam."""
	_ELIG_CALLS.append((item_code, distributor, sku))
	del is_firearm  # the real seam judges the lane with it; this stub does not
	if item_code == "BARE-ACC":
		manual_order.frappe.throw("Item BARE-ACC is not set up for drop-ship.")
	return {"distributor": distributor or "RSR", "sku": sku or f"{item_code}-SKU",
		"restricted_states": "NY,NJ"}


class CounterDropshipValidateTest(unittest.TestCase):
	def setUp(self):
		self.p = [
			mock.patch.object(manual_order.frappe.db, "exists", _exists_known),
			mock.patch.object(manual_order, "is_firearm_item",
				lambda code: code in _GUNS),
			mock.patch.object(manual_order, "is_regulated_firearm_item",
				lambda code: code in _GUNS),
			mock.patch.object(manual_order, "_dropship_eligibility", _elig,
				create=True),
		]
		for p in self.p:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)

	def test_dropship_row_orders_from_the_house_the_line_names(self):
		"""distributor-settings-layout E3: the Fulfillment plan's pick rides the line
		(`dropship_distributor` + that house's `sku`) and must reach eligibility —
		not be re-derived from the Item's web claim."""
		del _ELIG_CALLS[:]
		rows, _serials, _reg = manual_order._validate_items([
			{"item_code": "RSR-ACC", "qty": 1, "rate": 30, "source": "dropship",
				"dropship_distributor": "Sports South", "distributor": "Sports South",
				"sku": "SS-77"}])
		self.assertEqual(_ELIG_CALLS, [("RSR-ACC", "Sports South", "SS-77")])
		self.assertEqual(rows[0]["dropship_distributor"], "Sports South")
		self.assertEqual(rows[0]["sku"], "SS-77")

	def test_a_line_naming_only_distributor_still_carries_its_house(self):
		# The planner payload sends both keys; a resumed/legacy line may send one.
		del _ELIG_CALLS[:]
		manual_order._validate_items([{"item_code": "RSR-ACC", "qty": 1, "rate": 30,
			"source": "dropship", "distributor": "Sports South"}])
		self.assertEqual(_ELIG_CALLS, [("RSR-ACC", "Sports South", None)])

	def test_dropship_accessory_row_has_no_serial_or_warehouse(self):
		rows, serials, has_regulated = manual_order._validate_items(
			[{"item_code": "RSR-ACC", "qty": 2, "rate": 30, "source": "dropship"}])
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["source"], "dropship")
		self.assertEqual(row["qty"], 2)
		self.assertEqual(row["serials"], [])
		self.assertEqual(row["warehouse"], "")
		self.assertEqual(row["dropship_distributor"], "RSR")
		self.assertFalse(has_regulated)
		self.assertEqual(serials, set())

	def test_dropship_firearm_needs_no_local_serial_but_keeps_regulated(self):
		rows, _serials, has_regulated = manual_order._validate_items(
			[{"item_code": "RSR-GUN", "qty": 1, "rate": 500, "source": "dropship"}])
		# The one-serial firearm guard must NOT fire — RSR owns the serial.
		self.assertEqual(rows[0]["source"], "dropship")
		self.assertEqual(rows[0]["serials"], [])
		self.assertEqual(rows[0]["warehouse"], "")
		# FDS: a drop-ship gun still forces destination_ffl + FFL validation.
		self.assertTrue(has_regulated)

	def test_dropship_firearm_must_be_one_unit_per_line(self):
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._validate_items(
				[{"item_code": "RSR-GUN", "qty": 2, "rate": 500, "source": "dropship"}])

	def test_ineligible_item_is_rejected(self):
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._validate_items(
				[{"item_code": "BARE-ACC", "qty": 1, "rate": 30, "source": "dropship"}])

	def test_mixed_local_and_dropship_lines_coexist(self):
		rows, serials, has_regulated = manual_order._validate_items([
			{"item_code": "RSR-ACC", "qty": 1, "rate": 30, "source": "dropship"},
			{"item_code": "RSR-GUN", "qty": 1, "rate": 500, "source": "dropship"},
		])
		self.assertEqual([r["source"] for r in rows], ["dropship", "dropship"])
		self.assertTrue(has_regulated)

	def test_local_line_unchanged_by_the_new_branch(self):
		# A plain accessory (no source key) still validates the old way.
		rows, serials, has_regulated = manual_order._validate_items(
			[{"item_code": "RSR-ACC", "qty": 3, "rate": 10}])
		self.assertEqual(rows[0]["qty"], 3)
		self.assertNotIn("dropship", rows[0].get("source", ""))
		self.assertFalse(has_regulated)

	def test_dropship_line_without_item_code_throws_instead_of_vanishing(self):
		# The pre-ensure gap: quote(sku)-only flows produced item_code=None
		# lines that the blank-row skip silently DROPPED from the invoice while
		# the operator collected the on-screen total. Loud beats lossy.
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._validate_items(
				[{"item_code": "", "qty": 1, "rate": 30, "source": "dropship"}])

	def test_blank_local_filler_row_is_still_skipped(self):
		rows, _serials, _reg = manual_order._validate_items([
			{"item_code": "", "qty": 0, "rate": 0},
			{"item_code": "RSR-ACC", "qty": 2, "rate": 10, "source": "dropship"},
		])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["item_code"], "RSR-ACC")


class DropshipBlockReasonTest(unittest.TestCase):
	"""The provider-backed half of eligibility: a live distributor block or a
	zero catalog qty makes a line ineligible. Judged on the quote — a pure read of
	the dict, so no frappe stub at all."""

	def _reason(self, quote):
		return manual_order._dropship_block_reason("RSR-GUN", quote)

	def test_a_to_store_resolution_is_a_reason(self):
		"""The judgement key is the quote's RESOLVED mode."""
		self.assertTrue(self._reason({"fulfillment_mode": "To Store",
			"dropship_blocked": True, "qty": 5}))

	def test_a_direct_ok_override_passes_despite_the_raw_flag(self):
		"""The Sig case at the counter: the feed still flags the SKU but
		the brand authorization resolves it Direct — the line must be sellable
		as a drop-ship line."""
		self.assertIsNone(self._reason({"fulfillment_mode": "Direct",
			"dropship_blocked": True, "qty": 5}))

	def test_a_quote_missing_the_mode_falls_back_to_the_raw_flag(self):
		"""An older integrations build without the resolved mode must degrade to the OLD
		judgement, never to a laxer one."""
		self.assertTrue(self._reason({"dropship_blocked": True, "qty": 5}))

	def test_out_of_stock_is_a_reason(self):
		self.assertTrue(self._reason({"fulfillment_mode": "Direct",
			"dropship_blocked": False, "qty": 0}))

	def test_available_sku_is_not_blocked(self):
		self.assertIsNone(self._reason({"fulfillment_mode": "Direct",
			"dropship_blocked": False, "qty": 3}))

	def test_no_quote_is_not_blocked(self):
		# Provider trouble degraded to None: never sinks the counter sale
		# (FillOrKill=Y kills a truly unavailable line at the distributor).
		self.assertIsNone(self._reason(None))


class DropshipQuoteTest(unittest.TestCase):
	"""One quote per line, for the house AND the SKU the line carries."""

	def test_quotes_the_line_house_and_sku(self):
		import ffl_integrations.distributor.api as dist_api
		seen = []
		with mock.patch.object(dist_api, "quote",
				lambda *a, **k: seen.append((a, k)) or {"found": True}):
			q = manual_order._dropship_quote("RSR-GUN", "Sports South", "SS-9")
		self.assertEqual(q, {"found": True})
		self.assertEqual(seen, [((), {"item_code": "RSR-GUN", "sku": "SS-9",
			"distributor": "Sports South"})])

	def test_provider_trouble_degrades_to_none_and_is_logged(self):
		import ffl_integrations.distributor.api as dist_api

		def _boom(*a, **k):
			raise RuntimeError("catalog service down")

		with mock.patch.object(dist_api, "quote", _boom), \
				mock.patch.object(manual_order.frappe, "log_error") as log:
			self.assertIsNone(manual_order._dropship_quote("RSR-GUN", "RSR", None))
		log.assert_called_once()


class DropshipEligibilityTest(unittest.TestCase):
	"""distributor-settings-layout E3 — `_dropship_eligibility(item_code, distributor,
	sku)`. Stubs answer only the reads under test: the Item's claim and restricted
	list, the named Distributor's two switches, and the quote seam."""

	def setUp(self):
		self.claim = "Sports South"
		self.item_restricted = "NY"
		self.houses = {
			"RSR": {"enabled": 1, "dropship_enabled": 1},
			"Sports South": {"enabled": 1, "dropship_enabled": 1},
		}
		self.quote = {"found": True, "sku": "RSR-SKU", "fulfillment_mode": "Direct",
			"qty": 4, "restricted_states": ["CA"]}
		self.quotes = []

		def _get_value(doctype, name=None, field=None, *a, **k):
			if doctype == "Item" and field == "dropship_distributor":
				return self.claim
			if doctype == "Item" and field == "restricted_states":
				return self.item_restricted
			if doctype == "Distributor" and isinstance(field, (list, tuple)):
				row = self.houses.get(name)
				return manual_order.frappe._dict(row) if row else None
			raise AssertionError(f"unexpected read {doctype}.{field}")

		for p in [
			mock.patch.object(manual_order.frappe.db, "get_value", _get_value),
			mock.patch.object(manual_order, "_dropship_quote",
				lambda item_code, distributor, sku: self.quotes.append(
					(item_code, distributor, sku)) or self.quote),
		]:
			p.start()
			self.addCleanup(p.stop)

	def test_the_line_house_wins_over_the_item_claim(self):
		elig = manual_order._dropship_eligibility("GEI-SPR", "RSR", "RSR-SKU")
		self.assertEqual(elig["distributor"], "RSR")
		self.assertEqual(elig["sku"], "RSR-SKU")
		self.assertEqual(self.quotes, [("GEI-SPR", "RSR", "RSR-SKU")],
			"availability is judged at the planned house with its SKU")

	def test_the_quote_supplies_the_house_sku_when_the_line_has_none(self):
		elig = manual_order._dropship_eligibility("GEI-SPR", "RSR", None)
		self.assertEqual(elig["sku"], "RSR-SKU")

	def test_a_line_without_a_house_follows_the_item_claim(self):
		elig = manual_order._dropship_eligibility("GEI-SPR")
		self.assertEqual(elig["distributor"], "Sports South")
		self.assertEqual(self.quotes, [("GEI-SPR", "Sports South", None)])

	def test_no_house_and_no_claim_is_refused(self):
		self.claim = None
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._dropship_eligibility("GEI-SPR")

	def test_an_unknown_house_is_refused_back_to_the_plan(self):
		with self.assertRaises(manual_order.frappe.ValidationError) as ctx:
			manual_order._dropship_eligibility("GEI-SPR", "NOPE-DC", None)
		self.assertIn("fulfillment plan", str(ctx.exception))
		self.assertEqual(self.quotes, [])

	def test_a_disabled_house_is_refused_back_to_the_plan(self):
		self.houses["RSR"]["enabled"] = 0
		with self.assertRaises(manual_order.frappe.ValidationError) as ctx:
			manual_order._dropship_eligibility("GEI-SPR", "RSR", "RSR-SKU")
		self.assertIn("fulfillment plan", str(ctx.exception))

	def test_a_house_that_does_not_drop_ship_is_refused_back_to_the_plan(self):
		self.houses["RSR"]["dropship_enabled"] = 0
		with self.assertRaises(manual_order.frappe.ValidationError) as ctx:
			manual_order._dropship_eligibility("GEI-SPR", "RSR", "RSR-SKU")
		self.assertIn("fulfillment plan", str(ctx.exception))

	def test_eligibility_throws_when_availability_blocks(self):
		self.quote = dict(self.quote, qty=0)
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._dropship_eligibility("GEI-SPR", "RSR", "RSR-SKU")

	def test_restricted_states_are_the_item_list_plus_the_house_block(self):
		"""Compliance, strictest rule: Item.restricted_states ∪ the planned
		house's blocked_states(sku)."""
		restricted = manual_order._dropship_eligibility(
			"GEI-SPR", "RSR", "RSR-SKU")["restricted_states"]
		self.assertTrue(manual_order._state_restricted(restricted, "CA"))
		self.assertTrue(manual_order._state_restricted(restricted, "NY"))
		self.assertFalse(manual_order._state_restricted(restricted, "TX"))

	def test_a_house_without_a_restriction_feed_keeps_the_item_list(self):
		# quote() answers [] for a provider with no blocked_states method.
		self.quote = dict(self.quote, restricted_states=[])
		restricted = manual_order._dropship_eligibility(
			"GEI-SPR", "Sports South", "SS-1")["restricted_states"]
		self.assertTrue(manual_order._state_restricted(restricted, "NY"))
		self.assertFalse(manual_order._state_restricted(restricted, "CA"))

	def test_quote_trouble_keeps_the_item_list_and_the_line_sku(self):
		self.quote = None
		elig = manual_order._dropship_eligibility("GEI-SPR", "RSR", "RSR-SKU")
		self.assertEqual(elig["sku"], "RSR-SKU")
		self.assertEqual(elig["restricted_states"], "NY")


class DropshipContextTest(unittest.TestCase):
	def test_context_carries_the_line_house_and_sku(self):
		with mock.patch.object(manual_order, "is_firearm_item", lambda c: False), \
				mock.patch.object(manual_order, "_counter_ffl_block", lambda ffl: None):
			ctx = manual_order._counter_distributor_context(
				payload={}, destination_ffl=None, is_business=False,
				rows=[{"item_code": "GEI-SPR", "qty": 1, "rate": 5, "source": "dropship",
					"dropship_distributor": "RSR", "sku": "RSR-SKU",
					"restricted_states": "NY,CA"}])
		line = ctx["lines"][0]
		self.assertEqual(line["lane"], "dropship")
		self.assertEqual(line["distributor"], "RSR")
		self.assertEqual(line["sku"], "RSR-SKU")


if __name__ == "__main__":
	unittest.main()
