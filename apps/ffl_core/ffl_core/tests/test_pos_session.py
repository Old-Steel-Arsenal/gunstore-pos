"""check_opening_entry override: own open entry wins; otherwise ANY open
entry is resumed (one register, many clerks) instead of deadlocking on
ERPNext's per-user lookup + per-profile single-open-entry validation.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api.pos_session import check_opening_entry


class PosSessionOpeningEntry(FrappeTestCase):
	@patch("ffl_core.api.pos_session.frappe.db.get_all")
	def test_own_entry_wins_no_fallback(self, ga):
		own = [{"name": "OPE-1", "pos_profile": "TSA"}]
		ga.return_value = own
		out = check_opening_entry("clerk-a@example.com")
		self.assertEqual(out, own)
		self.assertEqual(ga.call_count, 1)          # no fallback query
		self.assertEqual(ga.call_args.kwargs["filters"]["user"], "clerk-a@example.com")

	@patch("ffl_core.api.pos_session.frappe.db.get_all")
	def test_own_entry_query_requires_status_open(self, ga):
		# Regression: a closed-but-unlinked opening (status="Closed" with an empty
		# pos_closing_entry — e.g. its Closing Entry was deleted) must NOT be
		# resurfaced as the user's open shift. ERPNext's Close Shift then throws
		# "Selected POS Opening Entry should be open." and the register is stuck.
		# The own-entry query must constrain status="Open" (like the fallback),
		# not trust pos_closing_entry emptiness alone.
		ga.return_value = [{"name": "OPE-1", "pos_profile": "FFL Test"}]
		check_opening_entry("clerk-a@example.com")
		self.assertEqual(ga.call_args_list[0].kwargs["filters"].get("status"), "Open")

	@patch("ffl_core.api.pos_session.frappe.db.get_all")
	def test_falls_back_to_any_open_entry(self, ga):
		other = [{"name": "OPE-2", "pos_profile": "TSA"}]
		ga.side_effect = [[], other]                # none of mine -> the register's shift
		out = check_opening_entry("clerk-b@example.com")
		self.assertEqual(out, other)
		self.assertEqual(ga.call_count, 2)
		self.assertEqual(ga.call_args.kwargs["filters"]["status"], "Open")

	@patch("ffl_core.api.pos_session.frappe.db.get_all")
	def test_no_open_entry_anywhere_returns_empty(self, ga):
		ga.side_effect = [[], []]
		self.assertEqual(check_opening_entry("clerk-c@example.com"), [])
