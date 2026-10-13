"""Drop-ship brand-policy resolution chain.

PURE precedence core. The chain::

    Dropship Brand Policy table (To Store and Direct OK are both overrides)
    → RSR per-SKU dropship_blocked / vendor_approval as the fallback → default Direct

Precedence REVERSED 2026-07-29: an explicit
``Direct OK`` brand entry now OVERRIDES the distributor's flags. The original
"flags are a physical constraint" premise was falsified in the field: RSR's
order system accepts drop-ship orders from a dealer holding the brand's
authorization (confirmed for Sig Sauer) while the FEED keeps flagging the SKUs
blocked — the flags describe the default account, not an authorized one. A
``Direct OK`` row is the operator certifying that authorization; a wrong
certification surfaces on the place-order ack, not silently.

The DB reader (`brand_policy_for`) is exercised in test_distributor_integration.
"""
from __future__ import annotations

import unittest

from ffl_integrations.distributor import policy


class TestBrandPolicyPrecedence(unittest.TestCase):
	def test_brand_table_to_store_wins(self):
		mode = policy.resolve_mode(brand_policy=policy.POLICY_TO_STORE)
		self.assertEqual(mode, policy.MODE_TO_STORE)

	def test_brand_table_to_store_wins_even_when_distributor_allows_dropship(self):
		mode = policy.resolve_mode(brand_policy=policy.POLICY_TO_STORE,
			dropship_blocked=False, vendor_approval_required=False)
		self.assertEqual(mode, policy.MODE_TO_STORE)

	def test_direct_ok_brand_with_no_flags_is_direct(self):
		mode = policy.resolve_mode(brand_policy=policy.POLICY_DIRECT_OK)
		self.assertEqual(mode, policy.MODE_DIRECT)

	def test_direct_ok_brand_overrides_dropship_blocked(self):
		"""THE reversal: brand authorization beats the feed flag. Sig's
		743 flagged SKUs drop-ship because RSR's order side accepts them."""
		mode = policy.resolve_mode(brand_policy=policy.POLICY_DIRECT_OK,
			dropship_blocked=True)
		self.assertEqual(mode, policy.MODE_DIRECT)

	def test_direct_ok_brand_overrides_vendor_approval_too(self):
		mode = policy.resolve_mode(brand_policy=policy.POLICY_DIRECT_OK,
			vendor_approval_required=True)
		self.assertEqual(mode, policy.MODE_DIRECT)

	def test_an_unlisted_brand_still_falls_to_the_flags(self):
		"""The reversal touches EXPLICIT entries only — no brand row means the
		distributor's flags keep deciding, exactly as before the reversal."""
		mode = policy.resolve_mode(brand_policy=None, dropship_blocked=True)
		self.assertEqual(mode, policy.MODE_TO_STORE)

	def test_dropship_blocked_falls_back_to_to_store(self):
		mode = policy.resolve_mode(brand_policy=None, dropship_blocked=True)
		self.assertEqual(mode, policy.MODE_TO_STORE)

	def test_vendor_approval_required_falls_back_to_to_store(self):
		mode = policy.resolve_mode(brand_policy=None, vendor_approval_required=True)
		self.assertEqual(mode, policy.MODE_TO_STORE)

	def test_default_is_direct(self):
		self.assertEqual(policy.resolve_mode(), policy.MODE_DIRECT)

	def test_blank_or_unknown_policy_string_is_treated_as_no_entry(self):
		self.assertEqual(policy.resolve_mode(brand_policy=""), policy.MODE_DIRECT)
		self.assertEqual(policy.resolve_mode(brand_policy="   "), policy.MODE_DIRECT)
		self.assertEqual(
			policy.resolve_mode(brand_policy="Whatever", dropship_blocked=True),
			policy.MODE_TO_STORE)


class TestModeFromCatalogRow(unittest.TestCase):
	"""Convenience over a NormalizedRow (what the listing pipeline actually holds)."""

	def test_row_flags_drive_the_fallback(self):
		row = {"dropship_blocked": True, "vendor_approval_required": False}
		self.assertEqual(policy.mode_for_row(row, brand_policy=None), policy.MODE_TO_STORE)

	def test_clean_row_with_no_brand_entry_is_direct(self):
		row = {"dropship_blocked": False, "vendor_approval_required": False}
		self.assertEqual(policy.mode_for_row(row, brand_policy=None), policy.MODE_DIRECT)

	def test_missing_keys_default_to_permissive_flags(self):
		self.assertEqual(policy.mode_for_row({}, brand_policy=None), policy.MODE_DIRECT)


if __name__ == "__main__":
	unittest.main()
