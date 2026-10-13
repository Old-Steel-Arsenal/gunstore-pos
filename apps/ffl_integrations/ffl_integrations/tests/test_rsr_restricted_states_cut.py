"""RSR data stops feeding `_osa_restricted_states`.

Background, because the shape of this cut is not obvious. `Item.restricted_states`
was populated from the RSR inventory file's per-state Y-flags. Those flags are read
from columns 16-66 on the strength of a GUESS: the RSR manual says "fields 16-56"
while the (since removed) feed loader listed 51 states and read columns 16-66.
Nobody ever reconciled the two, and a wrong column offset does not crash — it
silently mislabels which states an item is restricted in.

That guessed data had two consumers, and only one of them is cosmetic:

* `_osa_restricted_states` product meta -> the storefront plugin's `RestrictedStatesGuard`;
* ffl_core's `_validate_items`, which HARD-THROWS on a counter drop-ship line.

The cut keeps the guard mechanism and removes the RSR data behind it: the authoritative
ship-restriction file (a separate feed) replaces it for display, and the
guard becomes human-driven. So the cut is not "stop pushing the meta" — that would
collapse the guard's three-state contract, where an absent key means "never pushed"
and an empty string means "no restrictions". It is "stop letting the FEED be the
source, keep the operator-curated field as the source".

These are the assertions that keep the guessed data out. They say nothing about
whether the guard should fire — that is the storefront plugin's business and it is untouched.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


from ffl_integrations.distributor import items

_STATES_META = "_osa_restricted_states"


class StampItemNoLongerWritesRestrictedStates(unittest.TestCase):
	"""`_stamp_item` is the write that put feed data on the Item in the first
	place. After the cut it stamps the procurement source and nothing else."""

	def _stamp(self, row):
		with patch("frappe.db.set_value") as set_value:
			items._stamp_item("ITEM-1", "RSR", row, False)
		self.assertEqual(set_value.call_count, 1)
		args, _ = set_value.call_args
		return args[2]

	def test_a_feed_row_carrying_restricted_states_does_not_write_the_field(self):
		"""THE mutation target for this half: re-adding the key here silently
		restores the guessed-column pipeline end to end."""
		values = self._stamp({"restricted_states": ["NY", "NJ"]})
		self.assertNotIn("restricted_states", values)

	def test_the_procurement_source_is_still_stamped(self):
		"""The cut must not take the useful half of this write with it."""
		values = self._stamp({"restricted_states": ["NY"]})
		self.assertEqual(values["dropship_distributor"], "RSR")

	def test_an_operator_curated_value_is_not_cleared_by_a_listing_run(self):
		"""Absence of the key is what protects a hand-entered value: writing an
		empty string would wipe the operator's entry on the next `sync_listings`
		tick, which is worse than the bug being fixed."""
		values = self._stamp({"restricted_states": []})
		self.assertNotIn("restricted_states", values)


class TheDefensiveCleanupPatch(unittest.TestCase):
	"""The patch that undoes values the OLD pipeline wrote.

	Production exposure was measured at zero before this shipped (tabItem has
	neither column; the listing fixtures were never deployed), so it is deliberately NOT
	registered in patches.txt — it is the way back for when the listing pipeline
	does ship, written while the reasoning was fresh rather than invented later
	under pressure."""

	def test_it_is_not_registered_as_an_automatic_patch(self):
		"""THE assertion that keeps it defensive. Registering it would make a
		destructive write to a compliance-adjacent field run on every migrate, on
		every site, without anyone deciding to."""
		from pathlib import Path

		import ffl_integrations

		patches = (Path(ffl_integrations.__file__).resolve().parent
			/ "patches.txt").read_text()
		self.assertNotIn("clear_rsr_derived_restricted_states", patches)

	def test_it_defaults_to_a_dry_run(self):
		"""The first thing anyone running this by hand should get is a count."""
		import inspect

		from ffl_integrations.patches import clear_rsr_derived_restricted_states

		sig = inspect.signature(clear_rsr_derived_restricted_states.run)
		self.assertIs(sig.parameters["dry_run"].default, True)

	def test_a_site_without_the_column_is_reported_not_silently_zero(self):
		"""Exactly production's state today. "0 cleared" and "the field does not
		exist here" are different answers and must not look the same."""
		from ffl_integrations.patches import clear_rsr_derived_restricted_states

		with patch("frappe.db.has_column", return_value=False):
			out = clear_rsr_derived_restricted_states.run()
		self.assertEqual(out["cleared"], 0)
		self.assertIn("does not exist", out["note"])

	def test_it_finds_the_distributor_by_provider_not_by_the_literal_RSR(self):
		"""The distributor CODE is operator-chosen — the integration fixtures call
		it "P3-DC". A hardcoded "RSR" would quietly clean nothing."""
		from ffl_integrations.patches import clear_rsr_derived_restricted_states

		seen = {}

		def _get_all(doctype, filters=None, pluck=None, **kw):
			seen[doctype] = filters
			return ["P3-DC"] if doctype == "Distributor" else []

		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.get_all", side_effect=_get_all):
			out = clear_rsr_derived_restricted_states.run()
		self.assertEqual(seen["Distributor"], {"provider": "rsr"})
		self.assertEqual(out["distributors"], ["P3-DC"])

	def test_it_leaves_items_belonging_to_no_rsr_distributor_alone(self):
		"""A value on a non-RSR Item was hand-entered or came from elsewhere.
		Neither is ours to erase."""
		from ffl_integrations.patches import clear_rsr_derived_restricted_states

		captured = {}

		def _get_all(doctype, filters=None, pluck=None, **kw):
			if doctype == "Distributor":
				return ["P3-DC"]
			captured["item_filters"] = filters
			return []

		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.get_all", side_effect=_get_all):
			clear_rsr_derived_restricted_states.run()
		self.assertEqual(
			captured["item_filters"]["dropship_distributor"], ("in", ["P3-DC"]))

if __name__ == "__main__":
	unittest.main()
