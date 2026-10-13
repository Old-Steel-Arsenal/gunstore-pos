"""ATF FFL number shape + license-type classification (C&R / Type 03).

Pure module — no frappe import, so this runs under `bench run-tests` AND
standalone (`python3 -m unittest`). That is deliberate: this is the one place
that decides "is this string a Type 03 (Curio & Relic) license?", and the
answer gates whether a firearm ships to a private collector instead of a
dealer. It must be testable without a site.

The trailing-newline cases below are NOT hypothetical padding. The contract
regex is shared verbatim across four languages, and `$` does not mean the same
thing in all of them: JS rejects "…89012\\n", while PHP and Python both ACCEPT
it (PHP needs /D, Python needs \\Z). A 21-character string with a trailing
newline would otherwise be blessed as a valid 03 license and then reach
FastBound in its 15-character normalized form — a stored value that no longer
equals the value we validated. Cross-repo finding from the storefront plugin
side; these tests are the POS-side lock.
"""
from __future__ import annotations

import unittest

from ffl_core.compliance.ffl_number import (
	CR_LICENSE_RE,
	FFL_NUMBER_RE,
	is_cr_license,
	license_type_group,
	normalize,
)

# A real-shaped Type 03 (Curio & Relic) license and a Type 01 dealer license.
CR = "1-23-456-03-7C-89012"
DEALER = "5-99-888-01-9D-26128"


class NormalizeTest(unittest.TestCase):
	def test_strips_hyphens_and_uppercases(self):
		self.assertEqual(normalize(CR), "123456037C89012")
		self.assertEqual(normalize("5-99-888-01-9d-26128"), "599888019D26128")

	def test_already_normalized_is_unchanged(self):
		self.assertEqual(normalize("599888019D26128"), "599888019D26128")

	def test_strips_incidental_whitespace_and_punctuation(self):
		self.assertEqual(normalize(" 1-23-456-03-7C-89012 \n"), "123456037C89012")

	def test_empty_and_none(self):
		self.assertEqual(normalize(""), "")
		self.assertEqual(normalize(None), "")


class LicenseTypeGroupTest(unittest.TestCase):
	def test_type_group_from_hyphenated(self):
		self.assertEqual(license_type_group(CR), "03")
		self.assertEqual(license_type_group(DEALER), "01")

	def test_type_group_from_already_normalized(self):
		self.assertEqual(license_type_group("599888019D26128"), "01")

	def test_short_input_yields_empty_not_a_wrong_guess(self):
		# Truncated / garbage must NOT silently slice into a plausible "03".
		self.assertEqual(license_type_group("1-23-456"), "")
		self.assertEqual(license_type_group("03"), "")
		self.assertEqual(license_type_group(""), "")
		self.assertEqual(license_type_group(None), "")

	def test_over_length_input_yields_empty(self):
		# An ATF license is exactly 15 alphanumerics. A longer string is not a
		# license, so it has no type group — don't slice a confident "03" out of
		# a value that is really "the license plus something we didn't expect".
		self.assertEqual(license_type_group(CR + "X"), "")
		self.assertEqual(license_type_group(CR + "-99"), "")


class IsCrLicenseTest(unittest.TestCase):
	def test_type_03_is_cr(self):
		self.assertTrue(is_cr_license(CR))

	def test_accepts_normalized_form_too(self):
		self.assertTrue(is_cr_license("123456037C89012"))

	def test_dealer_license_is_not_cr(self):
		self.assertFalse(is_cr_license(DEALER))
		self.assertFalse(is_cr_license("1-99-888-07-XX-12345"))

	def test_blank_and_garbage_are_not_cr(self):
		self.assertFalse(is_cr_license(""))
		self.assertFalse(is_cr_license(None))
		self.assertFalse(is_cr_license("not-an-ffl"))


class FflNumberShapeTest(unittest.TestCase):
	def test_canonical_shape_matches(self):
		self.assertIsNotNone(FFL_NUMBER_RE.match(CR))
		self.assertIsNotNone(FFL_NUMBER_RE.match(DEALER))

	def test_malformed_shapes_rejected(self):
		self.assertIsNone(FFL_NUMBER_RE.match("123456037C89012"))  # no hyphens
		self.assertIsNone(FFL_NUMBER_RE.match("1-23-456-03-7C-8901"))  # short sequence
		self.assertIsNone(FFL_NUMBER_RE.match(""))

	def test_trailing_newline_is_rejected(self):
		"""Python's `$` also matches before a final newline — \\Z must be used."""
		self.assertIsNone(FFL_NUMBER_RE.match(CR + "\n"))
		self.assertIsNone(FFL_NUMBER_RE.match(DEALER + "\n"))

	def test_leading_or_embedded_whitespace_rejected(self):
		self.assertIsNone(FFL_NUMBER_RE.match(" " + CR))
		self.assertIsNone(FFL_NUMBER_RE.match(CR + " "))


class CrLicenseShapeTest(unittest.TestCase):
	def test_type_03_matches(self):
		self.assertIsNotNone(CR_LICENSE_RE.match(CR))

	def test_non_03_type_rejected(self):
		self.assertIsNone(CR_LICENSE_RE.match(DEALER))
		self.assertIsNone(CR_LICENSE_RE.match("1-23-456-01-7C-89012"))

	def test_trailing_newline_is_rejected(self):
		"""The exact 21-char string the cross-repo finding names."""
		self.assertIsNone(CR_LICENSE_RE.match(CR + "\n"))

	def test_unhyphenated_is_not_accepted_as_input_format(self):
		# Input acceptance is strict (hyphenated); classification (is_cr_license)
		# is the tolerant one. Keeping them separate is what stops a malformed
		# string from being accepted as input merely because it classifies as 03.
		self.assertIsNone(CR_LICENSE_RE.match("123456037C89012"))


if __name__ == "__main__":
	unittest.main()
