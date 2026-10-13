"""setup/selling.py pins ERPNext's "Allow Negative rates for Items" on.

Trade-in credit (setup/trade_in_credit) books a NEGATIVE-rate goods line. A
POS Invoice accepts it, but the consolidated Sales Invoice runs ERPNext's validate_qty, which
rejects a negative item rate unless Selling Settings
`allow_negative_rates_for_items` is on — so with it off, every trade-in
submits at the register but fails to consolidate (and for
a firearm the gun silently never leaves stock). We pin the flag on in code so
it can't drift off.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import selling


class SellingSettingsEnsure(FrappeTestCase):
	def test_ensure_turns_on_allow_negative_rates(self):
		frappe.db.set_single_value("Selling Settings", "allow_negative_rates_for_items", 0)
		selling.ensure()
		self.assertEqual(
			frappe.db.get_single_value("Selling Settings", "allow_negative_rates_for_items"), 1)


if __name__ == "__main__":
	unittest.main()
