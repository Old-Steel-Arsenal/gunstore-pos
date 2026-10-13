"""Fixed price or auction, decided at Send.

The Send dialog offers what a listing would carry and records what was
chosen; these pin the two helpers behind it. The payload itself is covered in
test_gunbroker_mapping, the guards in test_gunbroker_guards.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import listings, mapping
from ffl_integrations.tests import gunbroker_fixtures as fx


class RecordingTheChoice(FrappeTestCase):
	def setUp(self):
		patcher = patch("frappe.db.set_value")
		self.set_value = patcher.start()
		self.addCleanup(patcher.stop)

	def _written(self) -> dict:
		self.set_value.assert_called_once()
		args, kwargs = self.set_value.call_args
		self.assertEqual(args[:2], ("Serial No", "SN-001"))
		self.assertFalse(kwargs.get("update_modified", True))
		return args[2]

	def test_an_auction_records_all_of_its_terms(self):
		listings.set_listing_spec("SN-001", {
			"type": "Auction", "starting_bid": 900, "reserve_price": 1000,
			"buy_now_price": 1400, "duration": 7, "auto_relist": 2})
		written = self._written()
		self.assertEqual(written["gb_listing_type"], "Auction")
		self.assertEqual(written["gb_starting_bid"], 900.0)
		self.assertEqual(written["gb_reserve_price"], 1000.0)
		self.assertEqual(written["gb_buy_now_price"], 1400.0)
		self.assertEqual(written["gb_auction_duration"], 7)
		self.assertEqual(written["gb_auction_auto_relist"], 2)
		self.assertNotIn("gb_listing_title", written)
		self.assertNotIn("gb_list_price", written)

	def test_a_fixed_price_clears_the_auction_figures(self):
		"""A gun listed as an auction last time and at a fixed price this time
		must not carry the old opening bid into the next auction."""
		listings.set_listing_spec("SN-001", {"type": "Fixed Price", "price": 1500})
		written = self._written()
		self.assertEqual(written["gb_listing_type"], "Fixed Price")
		self.assertEqual(written["gb_starting_bid"], 0.0)
		self.assertEqual(written["gb_auction_duration"], 0)
		self.assertEqual(written["gb_list_price"], 1500.0)

	def test_an_empty_title_and_a_zero_price_clear_the_overrides(self):
		listings.set_listing_spec("SN-001", {"type": "Fixed Price", "title": "", "price": 0})
		written = self._written()
		self.assertEqual(written["gb_listing_title"], "")
		self.assertEqual(written["gb_list_price"], 0.0)

	def test_the_paid_promotions_are_all_written_when_the_spec_has_them(self):
		"""All four, so a box left unticked clears what an earlier listing bought."""
		listings.set_listing_spec("SN-001", {"type": "Fixed Price",
			"premium": {"featured": 1, "view_counter": 1}})
		written = self._written()
		self.assertEqual(written["gb_featured"], 1)
		self.assertEqual(written["gb_showcase"], 0)
		self.assertEqual(written["gb_sponsored_onsite"], 0)
		self.assertEqual(written["gb_view_counter"], 1)

	def test_an_empty_premium_object_clears_all_four(self):
		listings.set_listing_spec("SN-001", {"type": "Fixed Price", "premium": {}})
		written = self._written()
		for field in ("gb_featured", "gb_showcase", "gb_sponsored_onsite", "gb_view_counter"):
			self.assertEqual(written[field], 0)

	def test_the_recorded_promotions_stand_when_the_spec_says_nothing(self):
		for spec in ({"type": "Fixed Price", "price": 1500}, {"type": "Fixed Price", "premium": None}):
			self.set_value.reset_mock()
			listings.set_listing_spec("SN-001", spec)
			written = self._written()
			for field in ("gb_featured", "gb_showcase", "gb_sponsored_onsite", "gb_view_counter"):
				self.assertNotIn(field, written)

	def test_a_condition_chosen_for_the_gun_is_recorded_as_code_and_label(self):
		listings.set_listing_spec("SN-001", {"type": "Fixed Price", "condition": 2})
		self.assertEqual(self._written()["gb_condition"], "2 New Old Stock")

	def test_a_condition_equal_to_the_item_clears_the_override(self):
		listings.set_listing_spec("SN-001", {"type": "Fixed Price", "condition": 0})
		self.assertEqual(self._written()["gb_condition"], "")

	def test_the_recorded_condition_stands_when_the_spec_says_nothing(self):
		listings.set_listing_spec("SN-001", {"type": "Fixed Price", "price": 1500})
		self.assertNotIn("gb_condition", self._written())

	def test_a_refused_condition_records_nothing(self):
		with self.assertRaises(frappe.ValidationError):
			listings.set_listing_spec("SN-001", {"condition": 7})
		self.set_value.assert_not_called()

	def test_a_refused_spec_records_no_promotion(self):
		with self.assertRaises(frappe.ValidationError):
			listings.set_listing_spec("SN-001", {"premium": {"nope": 1}})
		self.set_value.assert_not_called()

	def test_terms_gunbroker_would_refuse_are_refused_here(self):
		with self.assertRaises(frappe.ValidationError):
			listings.set_listing_spec("SN-001", {"type": "Auction", "starting_bid": 0})
		self.set_value.assert_not_called()


class WhatTheDialogIsOffered(FrappeTestCase):
	def _offer(self, **serial):
		row = frappe._dict({"name": "SN-001", "item_code": "COLT-PYTHON",
			"item_name": "Colt Python 357", "firearm_type": "Revolver"})
		row.update(serial)
		context = {"serial": row, "item": frappe._dict({"firearm_type": "Revolver"})}
		with patch.object(listings, "_settings", return_value=fx.settings_stub()), \
				patch.object(mapping, "listing_context", return_value=context), \
				patch.object(listings, "effective_serial_price", return_value=1200.0):
			return listings.listing_offer("SN-001")

	def test_a_gun_never_listed_is_offered_the_defaults(self):
		offer = self._offer()
		self.assertEqual(offer["title"], "Colt Python 357")
		self.assertEqual(offer["derived_title"], "Colt Python 357")
		self.assertEqual(offer["price"], 1200.0)
		self.assertEqual(offer["pos_price"], 1200.0)
		self.assertEqual(offer["type"], "Fixed Price")
		self.assertEqual(offer["starting_bid"], 1200.0, "the opening bid starts at the POS price")
		self.assertEqual(offer["duration"], 7)
		self.assertEqual(offer["auto_relist"], 2)
		self.assertEqual(offer["durations"], [1, 3, 5, 7, 10, 14])
		self.assertEqual(offer["shipping"]["effective"], 34.99)

	def test_a_gun_never_promoted_is_offered_every_promotion_off(self):
		self.assertEqual(self._offer()["premium"], {"featured": False, "showcase": False,
			"sponsored_onsite": False, "view_counter": False})

	def test_a_gun_with_no_override_is_offered_what_the_item_gives(self):
		offer = self._offer()
		self.assertEqual(offer["condition"], 0)
		self.assertEqual(offer["derived_condition"], 1)
		self.assertEqual(offer["condition_options"], [1, 2, 3])

	def test_the_recorded_condition_is_offered_again(self):
		offer = self._offer(gb_condition="2 New Old Stock")
		self.assertEqual(offer["condition"], 2)
		self.assertEqual(offer["derived_condition"], 1)

	def test_an_antique_is_offered_used_and_nothing_else(self):
		context_item = {"firearm_type": "Revolver", "is_antique": 1, "is_used": 0}
		row = frappe._dict({"name": "SN-001", "item_code": "COLT-PYTHON",
			"item_name": "Colt Python 357", "firearm_type": "Revolver", "gb_condition": "2 New Old Stock"})
		context = {"serial": row, "item": frappe._dict(context_item)}
		with patch.object(listings, "_settings", return_value=fx.settings_stub()), \
				patch.object(mapping, "listing_context", return_value=context), \
				patch.object(listings, "effective_serial_price", return_value=1200.0):
			offer = listings.listing_offer("SN-001")
		self.assertEqual(offer["derived_condition"], 3)
		self.assertEqual(offer["condition_options"], [3])

	def test_the_recorded_promotions_are_offered_again(self):
		offer = self._offer(gb_featured=1, gb_sponsored_onsite=1)
		self.assertEqual(offer["premium"], {"featured": True, "showcase": False,
			"sponsored_onsite": True, "view_counter": False})

	def test_what_was_chosen_last_time_is_offered_again(self):
		offer = self._offer(gb_listing_title="Python, boxed", gb_list_price=1500,
			gb_listing_type="Auction", gb_starting_bid=900, gb_reserve_price=1000,
			gb_auction_duration=10, gb_auction_auto_relist=1)
		self.assertEqual(offer["title"], "Python, boxed")
		self.assertEqual(offer["price"], 1500.0)
		self.assertEqual(offer["type"], "Auction")
		self.assertEqual(offer["starting_bid"], 900.0)
		self.assertEqual(offer["reserve_price"], 1000.0)
		self.assertEqual(offer["duration"], 10)
		self.assertEqual(offer["auto_relist"], 1)
