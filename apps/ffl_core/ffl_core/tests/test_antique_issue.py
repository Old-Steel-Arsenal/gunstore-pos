"""issue_antique_serial — the stock exit for a gun the bound book never held.

An antique is a firearm for inventory and not for ATF. Every other gun leaves
stock through an FFL Disposition; this helper is the one door an antique has,
so its three edges are what matter: it issues exactly once, it is a no-op on a
retry, and it refuses a regulated firearm outright.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import firearm


class TestIssueAntiqueSerial(FrappeTestCase):
	def _run(self, *, row, antique=True):
		with patch("frappe.db.get_value", return_value=row), \
				patch.object(firearm, "is_antique_item", return_value=antique), \
				patch.object(firearm, "issue_serial", return_value="MAT-STE-0001") as issue:
			result = firearm.issue_antique_serial("205745", remarks="GunBroker Order #1")
		return result, issue

	def test_an_antique_on_the_shelf_is_issued_out_of_its_own_warehouse(self):
		row = frappe._dict(item_code="M39-ANTIQUE", warehouse="Stores - TSA", status="Active")
		result, issue = self._run(row=row)
		self.assertEqual(result, "MAT-STE-0001")
		self.assertEqual(issue.call_args.args, ("M39-ANTIQUE", "205745", "Stores - TSA"))
		self.assertEqual(issue.call_args.kwargs["remarks"], "GunBroker Order #1")

	def test_a_gun_that_already_left_is_not_issued_twice(self):
		# The retry after a failed invoice: status moved off Active by the first
		# issue, so the second call must find nothing to do.
		row = frappe._dict(item_code="M39-ANTIQUE", warehouse="", status="Consumed")
		result, issue = self._run(row=row)
		self.assertIsNone(result)
		issue.assert_not_called()

	def test_an_unknown_serial_issues_nothing(self):
		result, issue = self._run(row=None)
		self.assertIsNone(result)
		issue.assert_not_called()

	def test_a_regulated_firearm_is_refused_before_anything_moves(self):
		row = frappe._dict(item_code="GLOCK-19", warehouse="Stores - TSA", status="Active")
		with patch("frappe.db.get_value", return_value=row), \
				patch.object(firearm, "is_antique_item", return_value=False), \
				patch.object(firearm, "issue_serial") as issue, \
				self.assertRaises(Exception) as caught:
			firearm.issue_antique_serial("ABC123")
		self.assertIn("not an antique", str(caught.exception))
		issue.assert_not_called()
