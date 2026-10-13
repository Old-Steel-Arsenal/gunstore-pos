"""The per-listing shipping rate: saved on the record, offered with the default.

The Settings rates became defaults on 2026-08-29; the person pressing Send
decides the figure a listing actually carries. These pin the two helpers the
Send dialog and the push endpoints share.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import listings, mapping
from ffl_integrations.tests import gunbroker_fixtures as fx


class SavingARate(FrappeTestCase):
	def setUp(self):
		patcher = patch("frappe.db.set_value")
		self.set_value = patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_rate_is_written_without_touching_modified(self):
		"""House rule for every gb_* write: a listing detail is not an edit."""
		self.assertEqual(listings.set_ship_cost("Serial No", "SN-001", "60"), 60.0)
		self.set_value.assert_called_once_with(
			"Serial No", "SN-001", "gb_ship_cost", 60.0, update_modified=False)

	def test_free_shipping_is_its_own_flag_not_a_zero_rate(self):
		"""0 still means "follow the default"; free shipping is asked for by name."""
		self.assertIs(listings.set_free_shipping("SN-001", "1"), True)
		self.set_value.assert_called_once_with(
			"Serial No", "SN-001", "gb_free_shipping", 1, update_modified=False)
		self.set_value.reset_mock()
		self.assertIs(listings.set_free_shipping("SN-001", 0), False)
		self.set_value.assert_called_once_with(
			"Serial No", "SN-001", "gb_free_shipping", 0, update_modified=False)

	def test_zero_clears_it(self):
		"""So the default applies again — and keeps applying when it changes."""
		self.assertEqual(listings.set_ship_cost("Item", "AMMO-9MM", 0), 0.0)
		self.set_value.assert_called_once_with(
			"Item", "AMMO-9MM", "gb_ship_cost", 0.0, update_modified=False)

	def test_a_negative_rate_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			listings.set_ship_cost("Serial No", "SN-001", -5)
		self.set_value.assert_not_called()

	def test_only_the_two_listing_doctypes_carry_one(self):
		with self.assertRaises(frappe.ValidationError):
			listings.set_ship_cost("Sales Invoice", "SINV-1", 5)
		self.set_value.assert_not_called()


class TheFigureTheDialogShows(FrappeTestCase):
	def _serial(self, *, own, firearm_type="Revolver", free=0):
		context = {
			"serial": frappe._dict({"name": "SN-001", "item_code": "COLT-PYTHON",
				"firearm_type": firearm_type, "gb_ship_cost": own, "gb_free_shipping": free}),
			"item": frappe._dict({"firearm_type": firearm_type}),
		}
		with patch.object(listings, "_settings", return_value=fx.settings_stub()), \
				patch.object(mapping, "listing_context", return_value=context):
			return listings.ship_cost_summary("Serial No", "SN-001")

	def test_a_gun_without_a_rate_is_offered_its_types_default(self):
		self.assertEqual(self._serial(own=None),
			{"own": 0.0, "default": 34.99, "free": False, "effective": 34.99})
		self.assertEqual(self._serial(own=0, firearm_type="Rifle")["effective"], 45.0)

	def test_a_gun_with_a_rate_shows_both(self):
		self.assertEqual(self._serial(own=60),
			{"own": 60.0, "default": 34.99, "free": False, "effective": 60.0})

	def test_a_free_shipping_gun_is_offered_zero(self):
		self.assertEqual(self._serial(own=0, free=1),
			{"own": 0.0, "default": 34.99, "free": True, "effective": 0.0})

	def test_a_model_is_offered_the_accessory_default(self):
		with patch.object(listings, "_settings", return_value=fx.settings_stub()), \
				patch("frappe.db.get_value", return_value=None):
			summary = listings.ship_cost_summary("Item", "AMMO-9MM")
		self.assertEqual(summary,
			{"own": 0.0, "default": 19.99, "free": False, "effective": 19.99})

	def test_anything_else_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			listings.ship_cost_summary("Sales Invoice", "SINV-1")
