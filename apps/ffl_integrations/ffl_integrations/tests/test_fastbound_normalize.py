"""Unit tests for the bound-book attribute canonicaliser (fastbound.normalize).

Two layers: `squash` (safe — only removes spacing/punctuation) and the alias
tables (judgement — must fold true equivalences yet keep different guns apart).
"""
from __future__ import annotations

import unittest

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import normalize


class Normalize(FrappeTestCase):
	def test_squash_is_spacing_and_punctuation_insensitive(self):
		self.assertEqual(normalize.squash("M1911 A1"), "M1911A1")
		self.assertEqual(normalize.squash(".45 ACP"), "45ACP")
		self.assertEqual(normalize.squash("M28/30"), normalize.squash("M28-30"))

	def test_caliber_folds_safe_equivalences(self):
		self.assertEqual(normalize.canon_caliber("45 AUTO"), "45ACP")
		self.assertEqual(normalize.canon_caliber(".45ACP"), "45ACP")
		self.assertEqual(normalize.canon_caliber("9mm Para"), "9MM")
		self.assertEqual(normalize.canon_caliber("9x19"), "9MM")
		self.assertEqual(
			normalize.canon_caliber("7.62x53R"), normalize.canon_caliber("7.62x54R"))

	def test_caliber_keeps_distinct_cartridges_apart(self):
		# .30 (Garand, .30-06) and .30 Carbine are different cartridges.
		self.assertNotEqual(
			normalize.canon_caliber(".30"), normalize.canon_caliber(".30 Carbine"))

	def test_model_folds_synonyms_but_keeps_garand_and_carbine_apart(self):
		self.assertEqual(normalize.canon_model("U.S. Rifle M1"), normalize.canon_model("M1"))
		self.assertEqual(normalize.canon_model("U.S.Carbine M1"), "M1CARBINE")
		self.assertNotEqual(normalize.canon_model("M1"), normalize.canon_model("U.S.Carbine M1"))

	def test_1911_family_folds_descriptors_but_keeps_variants_distinct(self):
		# Every 1911A1 spelling — bare, "M" prefix, "U.S. Army" descriptor, spaced —
		# folds to one model (not every 1911A1 is U.S. Army; that's just a descriptor).
		for raw in ("1911A1", "M1911A1", "M1911A1 U.S.ARMY", "M1911 A1 U.S.ARMY"):
			self.assertEqual(normalize.canon_model(raw), "1911A1", raw)
		for raw in ("1911", "M1911"):
			self.assertEqual(normalize.canon_model(raw), "1911", raw)
		# … but 1911 / 1911A1 / 1911A2 stay distinct — they're different models.
		self.assertNotEqual(normalize.canon_model("1911"), normalize.canon_model("M1911A1"))
		self.assertNotEqual(normalize.canon_model("M1911A1"), normalize.canon_model("M1911A2"))

	def test_new_caliber_folds_from_real_boundbook(self):
		# .45 Colt / .45 Long Colt spellings → one
		self.assertEqual(
			normalize.canon_caliber(".45 Colt (Long Colt)"),
			normalize.canon_caliber("45 Colt / .45 Long Colt"))
		self.assertEqual(normalize.canon_caliber(".45 Long Colt"), "45COLT")
		# 5.56 NATO renderings → one, but NEVER merged with .223 Remington
		self.assertEqual(normalize.canon_caliber("5.56 NATO"), normalize.canon_caliber("5.56mm"))
		self.assertEqual(normalize.canon_caliber("5.56x45"), "556")
		self.assertNotEqual(
			normalize.canon_caliber("5.56 NATO"), normalize.canon_caliber(".223 Remington"))
		# .357 Mag renderings → one
		self.assertEqual(normalize.canon_caliber(".357"), normalize.canon_caliber(".357 Mag"))
		# 8mm Mauser verbose strings → one, but never 7.65 / 7.62
		self.assertEqual(
			normalize.canon_caliber("8mm Mauser / 7.92x57mm / 8x57 IS JS"),
			normalize.canon_caliber("8MM"))
		self.assertEqual(normalize.canon_caliber("7.92mm"), "8MM")
		self.assertNotEqual(normalize.canon_caliber("7.65mm"), normalize.canon_caliber("8mm"))

	def test_new_model_folds_keep_dealer_separates_apart(self):
		self.assertEqual(normalize.canon_model("Model 1903"), normalize.canon_model("1903"))
		self.assertEqual(normalize.canon_model("No.4 Mk1 Lee Enfield"), normalize.canon_model("No.4 Mk1"))
		self.assertEqual(normalize.canon_model("Mk VI"), normalize.canon_model("Mark VI"))
		self.assertEqual(normalize.canon_model("High Power"), normalize.canon_model("Hi-Power"))
		self.assertEqual(normalize.canon_model("P35"), normalize.canon_model("Hi Power"))
		self.assertEqual(normalize.canon_model("Mod 98"), normalize.canon_model("98"))
		# the dealer's explicit "keep separate" calls — must NOT fold
		self.assertNotEqual(normalize.canon_model("SKS"), normalize.canon_model("Type 56"))
		self.assertNotEqual(normalize.canon_model("King Cobra"), normalize.canon_model("King Cobra Talo"))
		self.assertNotEqual(normalize.canon_model("K98k"), normalize.canon_model("98"))

	def test_manufacturer_canonical_display(self):
		self.assertEqual(
			normalize.canon_manufacturer("Harrington and Richardson Arms Co"), "H&R")
		self.assertEqual(normalize.canon_manufacturer("H&R ARMS CO"), "H&R")
		# an unmapped maker passes through unchanged (just trimmed)
		self.assertEqual(normalize.canon_manufacturer("  Springfield Armory "), "Springfield Armory")


if __name__ == "__main__":
	unittest.main()
