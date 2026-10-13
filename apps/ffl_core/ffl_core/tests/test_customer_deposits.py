"""The "Customer Deposits" liability account (setup/customer_deposits): one per
company, under CURRENT liabilities, created on every migrate."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class CustomerDepositsSetup(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

	def test_ensure_creates_a_current_liability_account(self):
		from ffl_core.setup.customer_deposits import DEPOSIT_ACCOUNT_TITLE, deposit_account, ensure
		ensure()
		acct = deposit_account(self.company)
		self.assertTrue(acct)
		info = frappe.db.get_value("Account", acct,
			["root_type", "is_group", "account_name", "parent_account"], as_dict=True)
		self.assertEqual(info.root_type, "Liability")
		self.assertEqual(info.is_group, 0)
		self.assertEqual(info.account_name, DEPOSIT_ACCOUNT_TITLE)
		# A deposit is settled when the goods are handed over — weeks, not years — so
		# it is a CURRENT liability. Taking the first liability group in
		# tree order put it under Non-Current Liabilities on a standard chart, where
		# the balance sheet reads it as long-term debt.
		self.assertTrue((info.parent_account or "").startswith("Current Liabilities"),
			f"deposit account hangs under {info.parent_account}")

	def test_every_company_gets_one(self):
		from ffl_core.setup.customer_deposits import deposit_account, ensure
		ensure()
		for company in frappe.get_all("Company", pluck="name"):
			has_chart = frappe.db.exists("Account",
				{"company": company, "root_type": "Liability", "is_group": 1})
			if has_chart:
				self.assertTrue(deposit_account(company), f"{company} has no Customer Deposits account")

	def test_ensure_moves_an_unposted_account_out_of_non_current(self):
		"""The first chart this shipped against put it under Non-Current Liabilities
		(the old code took the first liability group in tree order). ensure() heals
		that on migrate — but only while the account has never been posted to."""
		from ffl_core.setup.customer_deposits import deposit_account, ensure

		ensure()
		acct = deposit_account(self.company)
		non_current = frappe.db.get_value("Account",
			{"company": self.company, "is_group": 1,
				"account_name": "Non-Current Liabilities"}, "name")
		if not non_current:
			self.skipTest("this chart has no Non-Current Liabilities group")
		doc = frappe.get_doc("Account", acct)
		doc.parent_account = non_current
		doc.flags.ignore_permissions = True
		doc.save()
		ensure()
		self.assertTrue(frappe.db.get_value("Account", acct, "parent_account")
			.startswith("Current Liabilities"))

	def test_ensure_is_idempotent(self):
		from ffl_core.setup.customer_deposits import ensure
		ensure()
		self.assertEqual(ensure().get("accounts", 0), 0)

	def test_after_migrate_runs_it(self):
		from ffl_core import hooks
		self.assertIn("ffl_core.setup.customer_deposits.ensure", hooks.after_migrate)
