"""The GunBroker Settings seed, and the blank it is there to stop it writing.

`gunbroker_seed.run()` fills the category map and the fee tiers, and to do that
it saves the Single. That save is the hazard: a Single applies its docfield
defaults only while it has NO stored rows at all, so once anything has written
one row — a patch, an operator, an earlier run of this — every other field loads
blank, and `save()` writes the whole document back, freezing those blanks into
real rows. What comes out is a settings page whose checkboxes are all off and a
listing payload that says no FFL is required.

So the seed restores missing defaults before it saves, and the check it uses to
find them has to read the raw `tabSingles` row. Reading the document cannot
work: loading a Single casts every Check through `cint()` unconditionally, so an
unsaved checkbox arrives as `0` — identical to one somebody deliberately
unticked. That is the mistake this file exists to keep out.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.setup import gunbroker_seed as seed
from ffl_integrations.tests import gunbroker_fixtures as fx

# (fieldname, fieldtype, docfield default) — the shape `frappe.get_meta` yields.
META_FIELDS = (
	("enabled", "Check", "0"),
	("sandbox_mode", "Check", "1"),
	("is_ffl_required", "Check", "1"),
	("use_default_exclude_states", "Check", "1"),
	("pay_certified_check", "Check", "1"),
	("card_checkout_enabled", "Check", "0"),
	("country_code", "Data", "US"),
	("listing_duration", "Select", "30"),
	("username", "Data", None),
	("category_map", "Table", None),
	("fee_tiers", "Table", None),
)


class _Doc(types.SimpleNamespace):
	"""A settings document that mirrors how Frappe hands a Single back."""

	def __init__(self, **stored):
		super().__init__()
		self._values: dict = {}
		for field, fieldtype, _default in META_FIELDS:
			if field in stored:
				self._values[field] = stored[field]
			elif fieldtype == "Check":
				# The load-time cast. An unsaved checkbox is 0, never None.
				self._values[field] = 0
			elif fieldtype == "Table":
				# Frappe hands an empty child table back as [], not None.
				self._values[field] = []
			else:
				self._values[field] = None
		self.saved = False

	def get(self, key, default=None):
		return self._values.get(key, default)

	def set(self, key, value):
		self._values[key] = value

	def append(self, key, row):
		if not isinstance(self._values.get(key), list):
			self._values[key] = []
		self._values[key].append(row)

	def save(self, **_kwargs):
		self.saved = True

	@property
	def flags(self):
		if not hasattr(self, "_flags"):
			self._flags = types.SimpleNamespace()
		return self._flags


class _MetaStub:
	fields = [frappe._dict({"fieldname": f, "fieldtype": t, "default": d})
		for f, t, d in META_FIELDS]

	def get(self, key, default=None):
		return getattr(self, key, default)


class SeedRestoresMissingDefaults(FrappeTestCase):
	def setUp(self):
		self.doc = _Doc()
		for target, kw in (
			("frappe.get_single", {"return_value": self.doc}),
			("frappe.get_meta", {"return_value": _MetaStub()}),
		):
			patcher = patch(target, **kw)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _run(self, **stored):
		with patch("frappe.db.sql", side_effect=fx.singles_sql(**stored)):
			seed.run()

	def test_the_defaults_survive_a_site_that_has_one_stored_row(self):
		"""The whole point. A patch wrote `sandbox_mode`; everything else must
		still be saved with the value its DocType declares, not with the 0 the
		document handed back."""
		self._run(sandbox_mode=1)
		self.assertEqual(self.doc.get("is_ffl_required"), "1")
		self.assertEqual(self.doc.get("use_default_exclude_states"), "1")
		self.assertEqual(self.doc.get("pay_certified_check"), "1")
		self.assertEqual(self.doc.get("country_code"), "US")

	def test_a_stored_zero_is_never_overwritten(self):
		"""An operator who unticked a box has stated a value. The restore must be
		able to tell that from a box nobody has touched — which is exactly what
		reading the document cannot do."""
		self._run(sandbox_mode=1, is_ffl_required=0)
		self.assertEqual(self.doc.get("is_ffl_required"), 0,
			"a deliberately unchecked box was reset to its default")

	def test_a_field_with_no_default_is_left_alone(self):
		self._run(sandbox_mode=1)
		self.assertIsNone(self.doc.get("username"))

	def test_the_defaults_are_restored_before_the_save(self):
		"""Ordering is the whole protection: this function runs because `save()`
		is about to persist whatever the document currently holds."""
		order: list[str] = []
		original_set = self.doc.set
		self.doc.set = lambda k, v: (order.append("set"), original_set(k, v))[1]
		original_save = self.doc.save
		self.doc.save = lambda **kw: (order.append("save"), original_save(**kw))[1]
		self._run(sandbox_mode=1)
		self.assertIn("set", order)
		self.assertLess(order.index("set"), order.index("save"))

	def test_the_seed_fills_the_lookup_tables(self):
		self._run(sandbox_mode=1)
		self.assertEqual(
			len(self.doc.get("category_map")), len(seed.DEFAULT_CATEGORY_MAP))
		self.assertEqual(
			len(self.doc.get("fee_tiers")), len(seed.DEFAULT_FEE_TIERS))
		self.assertTrue(self.doc.saved)

	def test_an_operators_edited_tables_are_never_reinstated(self):
		"""Categories are a merchandising decision and fee tiers change when
		GunBroker changes its rate card. A migrate must not quietly put back a
		row somebody deleted on purpose."""
		self.doc.set("category_map", [{"firearm_type": "Rifle", "category_id": 9999}])
		self.doc.set("fee_tiers", [{"upper_bound": 0, "rate": 5}])
		self._run(sandbox_mode=1, is_ffl_required=1, use_default_exclude_states=1,
			pay_certified_check=1, enabled=0, card_checkout_enabled=0,
			country_code="US", listing_duration="30")
		self.assertEqual(len(self.doc.get("category_map")), 1)
		self.assertEqual(self.doc.get("category_map")[0]["category_id"], 9999)
		self.assertFalse(self.doc.saved,
			"nothing needed changing, so the seed should not have saved at all")

	def test_the_curios_and_relics_row_is_deliberately_not_seeded(self):
		"""LD-4 makes the plain rifle category the default and C&R an opt-in.
		Routing every surplus rifle out of the rifle categories is a merchandising
		call, not a migration's."""
		seeded = {(row[0], row[1]) for row in seed.DEFAULT_CATEGORY_MAP}
		self.assertNotIn(2323, {row[2] for row in seed.DEFAULT_CATEGORY_MAP})
		self.assertTrue(all(is_cr == 0 for _t, is_cr, _id, _n in seed.DEFAULT_CATEGORY_MAP))
		self.assertEqual(len(seeded), len(seed.DEFAULT_CATEGORY_MAP))


class SeedIsSafeToRerun(FrappeTestCase):
	def setUp(self):
		self.doc = _Doc()
		patcher = patch("frappe.get_meta", return_value=_MetaStub())
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_second_run_changes_nothing(self):
		stored = {f: (d if d is not None else "") for f, t, d in META_FIELDS
			if t == "Check" or d is not None}
		with patch("frappe.get_single", return_value=self.doc), \
				patch("frappe.db.sql", side_effect=fx.singles_sql(**stored)):
			seed.run()
			first = dict(self.doc._values)
			self.doc.saved = False
			seed.run()
		self.assertEqual(
			len(self.doc.get("category_map")), len(seed.DEFAULT_CATEGORY_MAP),
			"the seed appended a second copy of the default rows")
		self.assertEqual(first.keys(), self.doc._values.keys())
		self.assertFalse(self.doc.saved)


class SeedNeverBlocksAMigrate(FrappeTestCase):
	def test_it_saves_past_the_forms_own_validation(self):
		"""`validate()` refuses to save an enabled integration with no deposit
		account. That rule belongs to a human saving the form; a migrate filling
		in a lookup table must not be able to fail on it and take the whole
		migrate down."""
		doc = MagicMock()
		doc.get.return_value = None
		with patch("frappe.get_single", return_value=doc), \
				patch("frappe.get_meta", return_value=_MetaStub()), \
				patch("frappe.db.sql", side_effect=fx.singles_sql(sandbox_mode=1)):
			seed.run()
		self.assertTrue(doc.flags.ignore_validate)
		self.assertTrue(doc.flags.ignore_permissions)
		self.assertTrue(doc.flags.ignore_mandatory)
