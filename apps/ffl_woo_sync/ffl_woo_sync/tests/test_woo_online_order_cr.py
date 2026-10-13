"""Woo Online Order.validate() — FFL number shape + derived C&R flag.

The format check MUST NOT be unconditional. Live orders carry historical junk in
`ffl_license` (imported records, hand-typed dealer names), and an unconditional
check would make those orders unsaveable forever — which takes the serial
backfill webhook down with them, because it saves the same document.

So: only a CHANGED value is judged. And the value that gets judged is the
stripped one, which is where the second trap lives — see
test_legacy_dirty_value_survives_when_untouched.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

CR = "1-23-456-03-7C-89012"
DEALER = "5-99-888-01-9D-26128"


def _doc(license_no, before=None):
	"""A Woo Online Order controller with a controlled 'value before save'."""
	doc = frappe.get_doc({
		"doctype": "Woo Online Order",
		"woo_order_id": "",          # skips the composite-uniqueness branch
		"site": "retail",
		"ffl_license": license_no,
	})
	# Explicit in both cases: None means "brand new document", which frappe
	# reports as has_value_changed() == True.
	doc._doc_before_save = (frappe._dict({"ffl_license": before})
		if before is not None else None)
	return doc


class FflLicenseFormatTest(FrappeTestCase):
	def test_valid_number_passes(self):
		doc = _doc(DEALER)
		doc.validate()
		self.assertEqual(doc.ffl_license, DEALER)

	def test_new_malformed_number_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			_doc("ACME Guns").validate()

	def test_blank_is_allowed(self):
		"""An order with no FFL yet is normal — the Store API race fills it later."""
		doc = _doc("")
		doc.validate()
		self.assertEqual(doc.is_cr_transfer, 0)

	def test_legacy_dirty_value_survives_when_untouched(self):
		"""The guard's whole purpose: an order already carrying junk must still
		save (its serial backfill depends on it), as long as nobody edited the
		field. A naive strip-in-place would break this — stripping ' ACME '
		counts as a change, and the stripped value still fails the format, so
		the order would become permanently unsaveable."""
		doc = _doc(" ACME ", before=" ACME ")
		doc.validate()  # must not raise
		self.assertEqual(doc.is_cr_transfer, 0)

	def test_trailing_newline_is_normalized_away_when_it_yields_a_valid_number(self):
		"""The cross-repo newline case reaching the DB. Storing the newline would make
		the persisted value differ from the one we validated, and would put a
		newline inside the ATF FFL Record's name."""
		doc = _doc(CR + "\n", before="")
		doc.validate()
		self.assertEqual(doc.ffl_license, CR)

	def test_junk_that_cannot_be_salvaged_by_stripping_is_still_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			_doc(" not-an-ffl ", before="").validate()


class CrFlagDerivationTest(FrappeTestCase):
	def test_flag_set_for_a_type_03_number(self):
		doc = _doc(CR)
		doc.validate()
		self.assertEqual(doc.is_cr_transfer, 1)

	def test_flag_cleared_for_a_dealer_number(self):
		doc = _doc(DEALER)
		doc.is_cr_transfer = 1  # stale flag from a previous licence
		doc.validate()
		self.assertEqual(doc.is_cr_transfer, 0)

	def test_flag_is_recomputed_even_when_the_number_did_not_change(self):
		"""The flag is derived, so it must be repaired on every save rather than
		trusted — a hand-set or migrated value must not survive."""
		doc = _doc(DEALER, before=DEALER)
		doc.is_cr_transfer = 1
		doc.validate()
		self.assertEqual(doc.is_cr_transfer, 0)


class InboundSyncIsNotJudgedTest(FrappeTestCase):
	"""The format check is for what STAFF type, not for what WooCommerce sends.

	Format validation was asked for on hand-entered licences. Applying it
	to an inbound sync instead takes the whole order down: that same save carries
	serial backfill, status and refunds, and on the create path it aborts
	insert() outright — the order would never exist, its serial would never be
	soft-reserved, and the firearm would stay sellable at the counter while the
	webhook still answered 200 so Woo never retried.

	`woo_sync_inbound` is the repo's existing "this write came from the Woo
	webhook" flag. Under it the bad value is recorded and stored, and still fails
	loudly later at dispose, where the destination cannot be resolved.
	"""

	def test_a_human_edit_is_still_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_doc("ACME GUNS LLC", before="").validate()

	def test_the_same_value_from_the_webhook_is_stored_not_rejected(self):
		doc = _doc("ACME GUNS LLC", before="")
		with patch.dict(frappe.flags, {"woo_sync_inbound": True}), \
			 patch("frappe.log_error") as log:
			doc.validate()   # must NOT raise
		self.assertEqual(doc.ffl_license, "ACME GUNS LLC", "the sync's value was dropped")
		self.assertTrue(log.called, "a rejected-looking value must still be recorded")

	def test_the_flag_does_not_disable_the_cr_derivation(self):
		"""Narrowing the THROW must not narrow the classification — a Type 03
		number arriving from Woo still has to be flagged as C&R."""
		doc = _doc(CR, before="")
		with patch.dict(frappe.flags, {"woo_sync_inbound": True}):
			doc.validate()
		self.assertEqual(doc.is_cr_transfer, 1)
