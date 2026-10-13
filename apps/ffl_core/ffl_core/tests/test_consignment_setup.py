"""Consignment setup — the schema + account + default % later consignment
tasks depend on.

Commission `c%` is the store's cut (its profit); the consignor is owed
`P·(1-c%)` where `P` is the line net (pre-tax) price. This module only
covers setup/consignment.ensure(): idempotent creation of the Serial No /
invoice-row custom fields, the FFL Settings default commission field, and
the per-company Consignment Payable liability account (mirrors
setup/trade_in_credit.py and setup/customer_deposits.py).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import consignment


class ConsignmentSetup(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)

	def test_ensure_creates_fields_and_account(self):
		consignment.ensure()
		for dt, fn in [("Serial No", "is_consignment"),
					   ("Serial No", "consignment_commission_pct"),
					   ("Serial No", "consignor"),
					   ("POS Invoice Item", "consignment_commission_pct"),
					   ("Sales Invoice Item", "consignment_commission_pct")]:
			self.assertTrue(frappe.db.exists("Custom Field", f"{dt}-{fn}"), f"{dt}-{fn}")
		self.assertTrue(frappe.db.exists(
			"Account", {"account_name": consignment.CONSIGNMENT_PAYABLE_TITLE, "company": self.company, "is_group": 0}))

	def test_ensure_is_idempotent(self):
		consignment.ensure()
		consignment.ensure()  # must not raise
