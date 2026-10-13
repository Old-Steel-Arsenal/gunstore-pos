"""The GunBroker category picked in the Send dialog.

The map in GunBroker Settings files every rifle under "Other Rifles"; the
person pressing Send can move a model to one of that category's siblings on
GunBroker, and the choice is saved on the Item.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import listings, mapping
from ffl_integrations.tests import gunbroker_fixtures as fx

RIFLES = {
	"categoryID": 3025, "categoryName": "Other Rifles",
	"categoryPath": {"id": 0, "name": "All", "child": {"id": 851, "name": "Guns & Firearms",
		"child": {"id": 979, "name": "Rifles",
			"child": {"id": 3025, "name": "Other Rifles", "child": None}}}},
}
RIFLE_LEAVES = {"results": [
	{"categoryID": 3024, "categoryName": "Semi Auto Rifles", "canContainItems": True},
	{"categoryID": 3022, "categoryName": "Bolt Action Rifles", "canContainItems": True},
	{"categoryID": 3025, "categoryName": "Other Rifles", "canContainItems": True},
	{"categoryID": 9999, "categoryName": "A branch", "canContainItems": False},
]}
CATEGORY_MAP = [frappe._dict(r) for r in (
	{"firearm_type": "Rifle", "category_id": 3025, "category_name": "Other Rifles"},
	{"firearm_type": "Rifle", "is_antique": 1, "category_id": 3022, "category_name": "Antique"},
)]


class _Cache:
	def __init__(self):
		self.store = {}

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value


class _Base(FrappeTestCase):
	def setUp(self):
		self.cache = _Cache()
		self.client = MagicMock()
		self.client.get_category.return_value = RIFLES
		self.client.get_categories.return_value = RIFLE_LEAVES
		for target, kw in (
			("frappe.cache", {"return_value": self.cache}),
			("ffl_integrations.gunbroker.listings.GunBrokerClient.from_settings_fast",
				{"return_value": self.client}),
			# Reads Settings, which goes through the cache patched above.
			("ffl_integrations.gunbroker.listings.sandbox_mode", {"return_value": False}),
		):
			patcher = patch(target, **kw)
			patcher.start()
			self.addCleanup(patcher.stop)


class Choices(_Base):
	def test_the_siblings_on_gunbroker_are_offered(self):
		choices = listings.category_choices(3025)
		self.assertEqual([c["id"] for c in choices], [3024, 3022, 3025])
		self.client.get_categories.assert_called_once_with(979)

	def test_they_are_read_once_a_day_not_once_a_dialog(self):
		listings.category_choices(3025)
		listings.category_choices(3025)
		self.client.get_category.assert_called_once()

	def test_gunbroker_down_offers_the_category_alone_and_waits_before_asking_again(self):
		"""An outage costs one timeout and one Error Log per retry window, not one
		per dialog opened."""
		self.client.get_category.side_effect = Exception("timed out")
		with patch.object(listings, "safe_log_error") as log:
			self.assertEqual(listings.category_choices(3025), [{"id": 3025, "name": ""}])
			self.assertEqual(listings.category_choices(3025), [{"id": 3025, "name": ""}])
		self.client.get_category.assert_called_once()
		log.assert_called_once()

	def test_a_category_gunbroker_does_not_know_is_not_asked_about_again(self):
		self.client.get_category.return_value = {}
		listings.category_choices(4242)
		listings.category_choices(4242)
		self.client.get_category.assert_called_once()

	def test_sandbox_and_production_are_cached_apart(self):
		with patch.object(listings, "sandbox_mode", return_value=True):
			listings.category_choices(3025)
		with patch.object(listings, "sandbox_mode", return_value=False):
			listings.category_choices(3025)
		self.assertEqual(self.client.get_category.call_count, 2)


class Offer(_Base):
	def _offer(self, firearm_type="Rifle", **item):
		serial = frappe._dict({"item_code": "SG551", "firearm_type": firearm_type, "year_made": 0})
		item = frappe._dict({"firearm_type": firearm_type, "gb_category_id": 0, "is_antique": 0, **item})
		return listings.category_offer(serial, item, fx.settings_stub(category_map=CATEGORY_MAP))

	def test_a_modern_rifle_is_not_offered_the_antique_category(self):
		offer = self._offer()
		self.assertEqual(offer["id"], 3025)
		self.assertEqual([c["id"] for c in offer["options"]], [3024, 3025])

	def test_an_antique_gets_no_choice(self):
		offer = self._offer(is_antique=1)
		self.assertEqual(offer, {"id": 3022, "options": [{"id": 3022, "name": "Antique"}]})
		self.client.get_category.assert_not_called()

	def test_no_category_no_options(self):
		self.assertEqual(self._offer(firearm_type="Receiver")["options"], [])

	def test_a_sibling_named_antique_is_never_offered_to_a_modern_gun(self):
		"""Mapped or not: GunBroker files Antique next to Curios & Relics."""
		self.client.get_categories.return_value = {"results": [
			*RIFLE_LEAVES["results"],
			{"categoryID": 2322, "categoryName": "Antique Guns", "canContainItems": True}]}
		self.assertNotIn(2322, [c["id"] for c in self._offer()["options"]])

	def test_a_modern_gun_of_a_type_routed_by_age_gets_no_choice(self):
		"""Its pick would land on the Item and take the model's old guns out of C&R."""
		cr_map = [*CATEGORY_MAP, frappe._dict(
			{"firearm_type": "Rifle", "is_cr_eligible": 1, "category_id": 2323})]
		serial = frappe._dict({"item_code": "M1", "firearm_type": "Rifle", "year_made": 1985})
		item = frappe._dict({"firearm_type": "Rifle", "gb_category_id": 0, "is_antique": 0})
		with patch.object(mapping, "is_cr_eligible", return_value=False):
			offer = listings.category_offer(serial, item, fx.settings_stub(category_map=cr_map))
		self.assertEqual([c["id"] for c in offer["options"]], [3025])

	def test_another_types_c_and_r_category_is_not_offered(self):
		cr_map = [*CATEGORY_MAP, frappe._dict(
			{"firearm_type": "Pistol", "is_cr_eligible": 1, "category_id": 3024})]
		offer = listings.category_offer(
			frappe._dict({"item_code": "SG551", "firearm_type": "Rifle", "year_made": 0}),
			frappe._dict({"firearm_type": "Rifle", "gb_category_id": 0, "is_antique": 0}),
			fx.settings_stub(category_map=cr_map))
		self.assertNotIn(3024, [c["id"] for c in offer["options"]])

	def test_a_gun_the_map_files_as_curios_and_relics_gets_no_choice(self):
		"""The choice lands on the Item and would outrank the per-gun C&R rule
		for every other gun of the model."""
		serial = frappe._dict({"item_code": "M1", "firearm_type": "Rifle", "year_made": 1943})
		item = frappe._dict({"firearm_type": "Rifle", "gb_category_id": 0, "is_antique": 0})
		cr_map = [*CATEGORY_MAP, frappe._dict(
			{"firearm_type": "Rifle", "is_cr_eligible": 1, "category_id": 2323,
				"category_name": "Curios & Relics"})]
		with patch.object(mapping, "is_cr_eligible", return_value=True):
			offer = listings.category_offer(serial, item, fx.settings_stub(category_map=cr_map))
		self.assertEqual(offer, {"id": 2323, "options": [{"id": 2323, "name": "Curios & Relics"}]})
		self.client.get_category.assert_not_called()


class Saving(_Base):
	def setUp(self):
		super().setUp()
		context = {
			"serial": frappe._dict({"item_code": "SG551", "firearm_type": "Rifle", "year_made": 0}),
			"item": frappe._dict({"firearm_type": "Rifle", "gb_category_id": 0, "is_antique": 0}),
		}
		for target, kw in (
			("frappe.db.set_value", {}),
			("ffl_integrations.gunbroker.listings._settings",
				{"return_value": fx.settings_stub(category_map=CATEGORY_MAP)}),
		):
			patcher = patch(target, **kw)
			mock = patcher.start()
			self.addCleanup(patcher.stop)
			if target == "frappe.db.set_value":
				self.set_value = mock
		patcher = patch.object(mapping, "listing_context", return_value=context)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_an_offered_sibling_is_saved_on_the_item(self):
		moved = listings.set_listing_category("SN-1", "3024")
		self.assertEqual(moved, {"serial_no": "SN-1", "item_code": "SG551",
			"chosen": 3024, "previous": 0})
		self.set_value.assert_called_once_with(
			"Item", "SG551", "gb_category_id", 3024, update_modified=False)

	def test_the_current_category_writes_nothing(self):
		self.assertIsNone(listings.set_listing_category("SN-1", 3025))
		self.set_value.assert_not_called()

	def test_a_refused_push_puts_the_model_back(self):
		"""A Send that listed nothing must not have moved every gun of the model."""
		moved = {"serial_no": "SN-1", "item_code": "SG551", "chosen": 3024, "previous": 3022}
		listings.settle_listing_category(moved, listed=False)
		self.set_value.assert_called_once_with(
			"Item", "SG551", "gb_category_id", 3022, update_modified=False)

	def test_a_kept_choice_is_noted_on_the_item(self):
		moved = {"serial_no": "SN-1", "item_code": "SG551", "chosen": 3024, "previous": 0}
		with patch("frappe.get_doc") as get_doc, \
				patch.object(listings, "_", side_effect=lambda text: text):
			listings.settle_listing_category(moved, listed=True)
		get_doc.assert_called_once_with("Item", "SG551")
		self.assertIn("SN-1", get_doc.return_value.add_comment.call_args.args[1])
		self.set_value.assert_not_called()

	def test_a_category_not_offered_is_refused(self):
		for refused in (3022, 2323):  # the antique row, and one off the list
			with self.assertRaises(frappe.ValidationError):
				listings.set_listing_category("SN-1", refused)
		self.set_value.assert_not_called()
