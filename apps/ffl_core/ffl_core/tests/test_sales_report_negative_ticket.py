"""Sales Report figures of REAL negative trade-in tickets — saved and submitted through the ticket tax floor, not
hand-built rows: the floor sets the ticket's tax rate to 0, ERPNext then rewrites every row's stored
``item_tax_rate`` to 0, and a report that asked that column would read the gun as tax-exempt (trade_in_applied 0
instead of 565). Taxability is asked of the line's Item Tax Template instead."""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core.ffl_core.report.sales_report import sales_report as sr
from ffl_core.tests.test_cash_drawer import _store_tests
from ffl_core.tests.test_negative_trade_in import NegFixtures


class Cases:
	def _report_row(self, inv, view="Order"):
		cols, data, *_ = sr.execute({"from_date": str(inv.posting_date), "to_date": str(inv.posting_date), "view": view})
		return next(r for r in data if r.get("invoice") == inv.name and r.get("row_kind", "order") == "order")

	def test_the_example_ticket_reports_the_allowance_applied_and_the_payout(self):
		"""Net Sales 565, Tax 0, Total -4,885, trade_in_applied 565, trade_in_payout 4,885, no tax mismatch."""
		inv = self._ticket()
		# the cause, pinned: after the floor every row's stored rate is 0 — the gun included
		gun = next(r for r in inv.items if r.item_code == "NEG-GOODS")
		self.assertEqual(flt(frappe.parse_json(gun.item_tax_rate).get(self.stp)), 0)
		row = self._report_row(inv)
		self.assertEqual((row["net_sales"], row["total"]), (565.0, -4885.0))
		self.assertEqual((row["trade_in_applied"], row["trade_in_payout"]), (565.0, 4885.0))
		detail = self._report_row(inv, "Order Detail")
		self.assertEqual((detail["net_sales"], detail["sales_tax"], detail["total"]), (565.0, 0, -4885.0))
		self.assertEqual((detail["trade_in_applied"], detail["trade_in_payout"]), (565.0, 4885.0))
		self.assertFalse(detail["tax_mismatch"])

	def test_a_tax_free_service_is_not_taxable_goods(self):
		"""565 taxable + 50 tax-free service - 600 of trade-in: 565 came off the tax base, 15.00 paid, no payout."""
		inv = self._ticket(credits=(600,), exempt=50, submit=False)
		inv.payments[0].amount = 15
		inv.flags.ignore_permissions = True
		inv.save()
		inv.submit()
		row = self._report_row(inv)
		self.assertEqual((row["trade_in_applied"], row["trade_in_payout"], row["total"]), (565.0, 0, 15.0))

	def test_an_ordinary_trade_in_applies_the_whole_credit(self):
		"""565 - 300: the 300 came off the tax base, nothing paid out."""
		inv = self._ticket(credits=(300,), submit=False)
		inv.payments[0].amount = flt(inv.grand_total)
		inv.flags.ignore_permissions = True
		inv.save()
		inv.submit()
		row = self._report_row(inv)
		self.assertEqual((row["trade_in_applied"], row["trade_in_payout"]), (300.0, 0))

	def test_line_taxable_follows_the_item_tax_template_and_the_tickets_own_account(self):
		template = frappe.db.get_value("Item Tax", {"parent": self.exempt_item}, "item_tax_template")
		self.assertTrue(template)
		own = [self.stp]
		# a template that zeroes the ticket's sales-tax account: exempt, whatever the row's stored rate says
		self.assertFalse(sr._line_taxable({"item_tax_template": template, "item_tax_rate": "{}"}, self.company, own))
		# no template: taxable, even though the floor rewrote the stored rate to 0
		self.assertTrue(sr._line_taxable({"item_tax_template": None, "item_tax_rate": '{"%s": 0}' % self.stp}, self.company, own))
		# the template says nothing about an account this ticket does not charge: that account stays taxable
		self.assertTrue(sr._line_taxable({"item_tax_template": template}, self.company, ["Some Other Tax - XX"]))


StoreANegativeReport = _store_tests("StoreANegativeReport", "store_a", NegFixtures, Cases)
StoreBNegativeReport = _store_tests("StoreBNegativeReport", "store_b", NegFixtures, Cases)
