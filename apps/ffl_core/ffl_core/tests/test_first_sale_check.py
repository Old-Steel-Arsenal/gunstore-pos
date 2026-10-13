"""``ffl_core.setup.first_sale_check``: the zero-credential smoke test a new site runs after store setup."""
from __future__ import annotations

import contextlib
import io
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import dev_site, first_sale_check
from ffl_core.tests.pos_opening import close_stale_pos_openings


class FirstSaleCheck(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# A site that has had no store setup has no POS Profile and no customer on it: the test makes the dev site's.
		dev_site._pos_profile()
		cls.profile = dev_site.COMPANY
		mop = frappe.get_doc("Mode of Payment", "Cash")
		if not any(r.company == dev_site.COMPANY for r in mop.accounts):
			mop.append("accounts", {"company": dev_site.COMPANY, "default_account": frappe.db.get_value("Company", dev_site.COMPANY, "default_cash_account")})
			mop.save(ignore_permissions=True)
		if not frappe.db.exists("Customer", "_Test FSC Customer"):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test FSC Customer",
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)
		frappe.db.set_value("POS Profile", cls.profile, "customer", "_Test FSC Customer")

	def setUp(self):
		# ERPNext's merge at a POS close commits; keep the test's rows in its transaction (docs/testing.md rule 1).
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		close_stale_pos_openings()
		frappe.db.delete("POS Opening Entry", {"status": "Open"})  # rolled back with the test
		super().setUp()

	def _run(self, **kw):
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			result = first_sale_check.run(pos_profile=self.profile, **kw)
		return result, out.getvalue()

	def test_a_cash_sale_goes_from_shift_to_a_paid_balanced_ledger(self):
		result, out = self._run(force=1)
		self.assertIn(first_sale_check.MARKER, out)
		self.assertEqual(frappe.db.get_value("POS Opening Entry", result["opening"], "status"), "Closed")
		self.assertEqual(frappe.db.get_value("POS Closing Entry", result["closing"], "docstatus"), 1)
		si = frappe.db.get_value("Sales Invoice", result["sales_invoice"], ["docstatus", "outstanding_amount"], as_dict=True)
		self.assertEqual((si.docstatus, si.outstanding_amount), (1, 0))
		self.assertEqual(frappe.db.get_value("POS Invoice", result["pos_invoice"], "consolidated_invoice"), result["sales_invoice"])

	def test_it_stops_when_a_shift_is_already_open(self):
		self._run(force=1)
		frappe.db.set_value("POS Opening Entry", {"pos_profile": self.profile}, "status", "Open")
		with self.assertRaises(frappe.ValidationError):
			self._run(force=1)

	def test_it_refuses_a_site_that_already_has_other_sales(self):
		with patch("ffl_core.setup.first_sale_check.frappe.db.sql", return_value=[[3]]):
			with self.assertRaises(frappe.ValidationError):
				first_sale_check._refuse_on_a_live_ledger("Anything")

	def test_without_a_profile_name_it_uses_the_only_enabled_one(self):
		frappe.db.set_value("POS Profile", {"name": ("!=", self.profile)}, "disabled", 1)
		self.assertEqual(first_sale_check._profile(None).name, self.profile)

	def test_without_a_profile_name_and_several_enabled_it_refuses_and_lists_them(self):
		other = frappe.copy_doc(frappe.get_doc("POS Profile", self.profile))
		other.name = other.pos_profile_name = "_Test FSC Other Profile"
		other.insert(ignore_permissions=True)
		frappe.db.set_value("POS Profile", {"name": ("not in", [self.profile, other.name])}, "disabled", 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			first_sale_check._profile(None)
		self.assertIn(self.profile, str(ctx.exception))
		self.assertIn(other.name, str(ctx.exception))
