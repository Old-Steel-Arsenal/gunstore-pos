"""A register ticket whose trade-ins exceed the goods (negative trade-in ticket): the sales-tax floor, the credit line's account, the tender guards and the payout the store
books at submit.

Run on the two stand-in stores of test_cash_drawer (Store A-like 8.25%, Store B-like 8.75%), each with its own POS profile,
tax template, a 0% service and trade-ins parked as drafts the way the counter's dialog parks them, so the checkout
books them (acquisition submitted, Material Receipt made) exactly as at the register. Nothing commits: ShiftFixtures neutralises ERPNext's own commits and every test rolls back to
its savepoint.
"""
from __future__ import annotations

import json

import frappe
from frappe.utils import flt, getdate, nowdate

from ffl_core import cash_drawer as cd
from ffl_core.api import cash_drawer as api
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
from ffl_core.tests.test_cash_drawer import _store_tests, _user
from ffl_core.tests.test_cash_drawer_pos import ShiftFixtures, legacy_payout

RATES = {"store_a": 8.25, "store_b": 8.75}
EXAMPLE_CREDITS = (450, 1200, 300, 2750, 750)  # an example ticket: 5,450 of trade-ins against a 565 gun


class NegFixtures(ShiftFixtures):
	@classmethod
	def setUpClass(cls):
		from ffl_core.setup import trade_in_credit

		# First, before this class makes anything: on a fresh site ensure() adds columns (custom fields), and DDL
		# commits the open transaction -- it would make the stand-in stores below permanent (docs/testing.md rule 1).
		trade_in_credit.ensure()
		super().setUpClass()
		trade_in_credit.ensure()  # again, now that the stand-in company exists: its Item Default is made here
		cls.rate = RATES[cls.KIND]
		cls.stock_adjustment = f"Stock Adjustment - {cls.abbr}"
		cls.debtors = f"Debtors - {cls.abbr}"
		cls.bank = f"Operating Bank - {cls.abbr}"
		cls.template = cls._tax_template()
		cls.exempt_item = cls._exempt_service()
		for code in ("NEG-GOODS",):
			if not frappe.db.exists("Item", code):
				frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
					"stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "NEG-GUN"):
			frappe.get_doc({"doctype": "Item", "item_code": "NEG-GUN", "item_name": "NEG-GUN", "item_group": "Pistol",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
				"serial_no_series": "NEG-GUN-.####"}).insert(ignore_permissions=True)
		cls.neg_profile = cls._profile(" NEG")
		# The register's own income account is Sales: what ERPNext hands every line of a register ticket.
		cls.sales_income = f"Sales - {cls.abbr}"
		frappe.db.set_value("POS Profile", cls.neg_profile, {"taxes_and_charges": cls.template, "income_account": cls.sales_income})
		# A register cashier: sells, and books the trade-ins the checkout completes (stock receipt).
		cls.neg_cashier = _user(f"neg-cashier-{cls.KIND}@example.com", "Sales User", "Stock User")
		cls._open_shift(cls, profile=cls.neg_profile, user=cls.neg_cashier)

	@classmethod
	def _tax_template(cls) -> str:
		title = "_Test Neg Sales Tax"
		name = frappe.db.get_value("Sales Taxes and Charges Template", {"title": title, "company": cls.company})
		if name:
			return name
		return frappe.get_doc({
			"doctype": "Sales Taxes and Charges Template", "title": title, "company": cls.company,
			"taxes": [{"charge_type": "On Net Total", "account_head": cls.stp, "rate": RATES[cls.KIND],
				"description": "Sales Tax"}],
		}).insert(ignore_permissions=True).name

	@classmethod
	def _exempt_service(cls) -> str:
		code = f"NEG-SVC-{cls.abbr}"
		if frappe.db.exists("Item", code):
			return code
		title = "_Test Neg Non-Taxable"
		itt = frappe.db.get_value("Item Tax Template", {"title": title, "company": cls.company}) or frappe.get_doc({
			"doctype": "Item Tax Template", "title": title, "company": cls.company,
			"taxes": [{"tax_type": cls.stp, "tax_rate": 0}],
		}).insert(ignore_permissions=True).name
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": "All Item Groups",
			"stock_uom": "Nos", "is_stock_item": 0, "taxes": [{"item_tax_template": itt}]}).insert(ignore_permissions=True)
		return code

	# --- the ticket -------------------------------------------------------------------------------

	def _trade_in(self, cost: float) -> str:
		"""A trade-in as the counter's dialog leaves it: a DRAFT acquisition, booked when the ticket submits."""
		from ffl_core.acquisition import record_firearm_acquisition

		serial = f"NEG-{self.abbr}-{frappe.generate_hash(length=8).upper()}"
		return record_firearm_acquisition(
			item_code="NEG-GUN", serial=serial, source_type="Trade-in", source_name="Testy Seller", cost=cost,
			company=self.company, source_id_type="Driver License", source_id_number="DL-1", source_first_name="Testy",
			source_last_name="Seller", source_address="123 Main St, Springfield TX 75024", submit_now=False,
			pending_intake_payload=json.dumps({"manufacturer": "Glock", "model_name": "19", "caliber": "9mm"}))

	def _ticket(self, goods=565.0, credits=EXAMPLE_CREDITS, exempt=0.0, pay=0.0, method="Cash", reference=None,
			submit=True, acquisitions=None, extra=()):
		frappe.set_user(self.neg_cashier)
		try:
			inv = frappe.new_doc("POS Invoice")
			inv.pos_profile = self.neg_profile
			inv.customer = "_Test CD Customer"
			lines = [("NEG-GOODS", goods)] if goods else []
			if exempt:
				lines.append((self.exempt_item, exempt))
			lines += list(extra)
			for code, rate in lines:
				inv.append("items", {"item_code": code, "qty": 1, "rate": rate, "price_list_rate": rate})
			acquisitions = acquisitions or [self._trade_in(c) for c in credits]
			for acq in acquisitions:
				cost = flt(frappe.db.get_value("FFL Acquisition", acq, "cost"))
				inv.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -cost, "ffl_trade_in_acquisition": acq})
			inv.set_missing_values()
			for row, (code, rate) in zip(inv.items, lines):
				row.rate = row.price_list_rate = rate
			inv.set("payments", [])
			inv.append("payments", {"mode_of_payment": "Cash", "amount": pay})
			inv.ffl_trade_in_payout_method = method
			inv.ffl_trade_in_payout_reference = reference
			inv.flags.ignore_permissions = True
			inv.save()
			if submit:
				inv.submit()
			return inv
		finally:
			frappe.set_user("Administrator")

	def _tax(self, inv) -> float:
		return flt(sum(flt(t.tax_amount) for t in inv.taxes), 2)

	def _breakdown(self, inv) -> float:
		return flt(frappe.db.sql(
			"SELECT IFNULL(SUM(amount), 0) FROM `tabItem Wise Tax Detail` WHERE parent = %s", inv.name)[0][0], 2)

	def _payout(self, inv) -> frappe._dict | None:
		return frappe.db.get_value("Cash Drawer Entry", {"invoice": inv.name, "status": "Posted"},
			["name", "amount", "payment_method", "reference", "journal_entry", "customer", "payout_key", "posting_date"],
			as_dict=True)


class NegCases:
	# ------------------------------------------------------------------ the tax floor

	def test_the_example_ticket_takes_no_tax_and_pays_the_customer(self):
		"""565 taxable goods against 5,450 of trade-ins: tax 0.00 on the row and its breakdown, the ticket
		comes to -4,885.00 and submits; the five trade-ins are booked (acquisition submitted, Material Receipt made)
		and the store's payout is Dr Debtors (the customer) / Cr Cash for 4,885.00, dated with the ticket."""
		inv = self._ticket()
		self.assertEqual(inv.docstatus, 1)
		self.assertEqual(self._tax(inv), 0)
		self.assertEqual(flt(inv.total_taxes_and_charges), 0)
		self.assertEqual(self._breakdown(inv), 0)
		self.assertEqual((flt(inv.net_total), flt(inv.grand_total)), (-4885.0, -4885.0))
		self.assertEqual((flt(inv.change_amount), flt(inv.paid_amount)), (0, 0))
		for row in inv.items:
			if row.item_code == TRADE_IN_CREDIT_ITEM:
				self.assertEqual(frappe.db.get_value("FFL Acquisition", row.ffl_trade_in_acquisition, "docstatus"), 1)
				serial = frappe.db.get_value("FFL Acquisition", row.ffl_trade_in_acquisition, "serial")
				self.assertTrue(frappe.db.exists("Stock Ledger Entry", {"serial_no": serial, "voucher_type": "Stock Entry",
					"is_cancelled": 0}) or frappe.db.exists("Serial No", {"name": serial, "warehouse": ["is", "set"]}), serial)
				self.assertEqual(row.income_account, self.stock_adjustment)  # G2, whatever the profile says
			else:
				self.assertEqual(row.income_account, self.sales_income)
		payout = self._payout(inv)
		self.assertEqual((flt(payout.amount), payout.payment_method, payout.customer, payout.payout_key),
			(4885.0, "Cash", "_Test CD Customer", inv.name))
		self.assertEqual(str(payout.posting_date), str(inv.posting_date))
		self.assertEqual(frappe.db.get_value("POS Invoice", inv.name, "ffl_trade_in_payout_entry"), payout.name)
		self.assertEqual(self._lines(payout.journal_entry), {self.debtors: (4885.0, 0), self.cash: (0, 4885.0)})
		party = frappe.db.get_value("Journal Entry Account", {"parent": payout.journal_entry, "account": self.debtors},
			["party_type", "party"], as_dict=True)
		self.assertEqual((party.party_type, party.party), ("Customer", "_Test CD Customer"))
		self.assertTrue(frappe.db.get_value("Journal Entry", payout.journal_entry, "user_remark").startswith(
			f"CASH-TRADEIN {inv.name}"))

		# Once the ticket is merged into its Sales Invoice: Sales takes the 565 only, the trade-ins' Stock
		# Adjustment nets to zero against their receipts, no sales tax, the customer's Debtors nets to zero.
		from ffl_core.api.pos_consolidate import consolidate_pos_invoice_now

		self.assertTrue(consolidate_pos_invoice_now(inv.name)["consolidated"])
		si = frappe.db.get_value("POS Invoice", inv.name, "consolidated_invoice")
		receipts = frappe.db.sql_list(
			"SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry` WHERE voucher_type = 'Stock Entry' AND is_cancelled = 0 "
			"AND item_code = 'NEG-GUN' AND company = %s", self.company)
		self.assertEqual(len(receipts), 5)
		net = {r.account: flt(r.net, 2) for r in frappe.db.sql(
			"SELECT account, SUM(debit - credit) AS net FROM `tabGL Entry` WHERE is_cancelled = 0 AND voucher_no IN %s "
			"GROUP BY account", (tuple([si, payout.journal_entry, *receipts]),), as_dict=True)}
		self.assertEqual(net.get(self.sales), -565.0)
		self.assertEqual(net.get(self.stock_adjustment), 0)
		self.assertEqual(net.get(self.stp, 0), 0)
		self.assertEqual(net.get(self.debtors), 0)
		self.assertEqual(net.get(self.cash), -4885.0)
		self.assertEqual(net.get(f"Stock In Hand - {self.abbr}"), 5450.0)

	def test_a_credit_smaller_than_the_goods_still_taxes_the_difference(self):
		"""565 - 300 taxes 265, and nothing is paid out."""
		inv = self._ticket(credits=(300,), pay=0, submit=False)
		tax = flt(265 * self.rate / 100, 2)
		self.assertEqual(self._tax(inv), tax)
		self.assertEqual(flt(inv.grand_total), flt(265 + tax, 2))
		self.assertEqual(self._breakdown(inv), tax)
		inv.payments[0].amount = inv.grand_total
		inv.flags.ignore_permissions = True
		inv.save()
		inv.submit()
		self.assertIsNone(self._payout(inv))
		self.assertFalse(inv.ffl_trade_in_payout_method)  # a positive ticket carries no payout choice

	def test_a_tax_free_service_keeps_the_ticket_positive_with_no_tax(self):
		"""565 taxable + 50 tax-free service - 600 of trade-in: tax 0.00, the customer pays 15.00, no payout."""
		inv = self._ticket(credits=(600,), exempt=50, submit=False)
		self.assertEqual((self._tax(inv), flt(inv.grand_total)), (0, 15.0))
		inv.payments[0].amount = 15
		inv.flags.ignore_permissions = True
		inv.save()
		inv.submit()
		self.assertEqual((flt(inv.paid_amount), flt(inv.grand_total)), (15.0, 15.0))
		self.assertIsNone(self._payout(inv))

	def test_a_line_whose_tax_template_arrives_during_the_save_is_still_floored(self):
		"""The before_validate guess cannot see a tax template ERPNext only fills in during the save (an API caller's
		bare line): the validate pass settles on the final lines and totals again."""
		inv = self._ticket(credits=(600,), exempt=50, pay=20, submit=False)
		exempt = next(r for r in inv.items if r.item_code == self.exempt_item)
		exempt.item_tax_template = exempt.item_tax_rate = None
		inv.taxes[0].rate = self.rate
		inv.change_amount = 0
		inv.flags.ignore_permissions = True
		inv.save()
		self.assertTrue(next(r for r in inv.items if r.item_code == self.exempt_item).item_tax_template)  # filled in
		self.assertEqual((self._tax(inv), flt(inv.grand_total)), (0, 15.0))
		# change worked out again on the floored total (20 - 15), not on ERPNext's first one (20 - 12.11)
		self.assertEqual(flt(inv.change_amount), 5.0)

	def test_a_small_purchase_against_a_big_trade_in_is_not_refused(self):
		"""20 of goods against a 500 trade-in: ERPNext's own refusal of a negative ticket must see the floored total
		(-480), not the unfloored one (-480 x 1.08 > the 500 credit)."""
		inv = self._ticket(goods=20, credits=(500,))
		self.assertEqual((self._tax(inv), flt(inv.grand_total)), (0, -480.0))
		self.assertEqual(flt(self._payout(inv).amount), 480.0)

	def test_the_template_rate_comes_back_when_the_cart_turns_positive(self):
		inv = self._ticket(credits=(600,), submit=False)
		self.assertEqual([flt(t.rate) for t in inv.taxes], [0.0])
		inv.items = [r for r in inv.items if r.item_code != TRADE_IN_CREDIT_ITEM]
		inv.flags.ignore_permissions = True
		inv.save()
		self.assertEqual([flt(t.rate) for t in inv.taxes], [self.rate])
		self.assertEqual(self._tax(inv), flt(565 * self.rate / 100, 2))

	def test_a_zero_rate_set_on_purpose_is_never_raised(self):
		"""Only a rate this hook took away comes back: a 0% row someone set (an exemption, a hand edit) stays 0%,
		with or without trade-in credit on the ticket."""
		for credits in ((), (100,)):
			inv = self._ticket(credits=credits, submit=False)
			inv.taxes[0].rate = 0
			inv.flags.ignore_permissions = True
			inv.save()
			inv.save()
			self.assertEqual(([flt(t.rate) for t in inv.taxes], self._tax(inv)), ([0.0], 0), credits)

	def test_typed_in_tax_rows_without_a_template_are_floored_too(self):
		"""No Sales Taxes and Charges Template on the ticket, only its own rows: floored and given back the same way."""
		inv = self._ticket(credits=(600,), submit=False)
		inv.taxes_and_charges = None
		inv.taxes[0].rate = self.rate
		inv.taxes[0].ffl_trade_in_floored_rate = 0
		inv.flags.ignore_permissions = True
		inv.save()
		self.assertFalse(inv.taxes_and_charges)
		self.assertEqual(([flt(t.rate) for t in inv.taxes], self._tax(inv)), ([0.0], 0))
		self.assertEqual(flt(inv.taxes[0].ffl_trade_in_floored_rate), self.rate)
		inv.items = [r for r in inv.items if r.item_code != TRADE_IN_CREDIT_ITEM]
		inv.save()
		self.assertEqual(([flt(t.rate) for t in inv.taxes], self._tax(inv)), ([self.rate], flt(565 * self.rate / 100, 2)))
		self.assertFalse(flt(inv.taxes[0].ffl_trade_in_floored_rate))

	# ------------------------------------------------------------------ what may go negative

	def test_a_negative_ticket_without_trade_in_credit_is_refused(self):
		"""Goods 500 and a -600 line that is not a trade-in (a deposit release, a typed negative price)."""
		with self.assertRaises(frappe.ValidationError):
			self._ticket(goods=500, credits=(), extra=(("NEG-GOODS", -600),))
		with self.assertRaises(frappe.ValidationError):  # nor does a credit make another negative line acceptable
			self._ticket(goods=500, credits=(100,), extra=(("NEG-GOODS", -600),))

	def test_only_a_merged_sales_invoice_may_go_negative(self):
		"""A Sales Invoice typed in Desk has no payout path: negative only as the consolidated copy of a ticket."""
		si = frappe.new_doc("Sales Invoice")
		si.append("items", {"item_code": "NEG-GOODS", "qty": 1, "rate": 565, "amount": 565})
		si.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -5450, "amount": -5450})
		si.base_grand_total = si.grand_total = -4885
		self.assertFalse(si.is_negative_grand_total_allowed())
		si.is_consolidated = 1
		self.assertTrue(si.is_negative_grand_total_allowed())

	def test_a_negative_ticket_takes_no_tender(self):
		"""Any payment on a ticket below zero is refused at checkout; a draft keeps no change."""
		inv = self._ticket(pay=20, submit=False)
		self.assertEqual(flt(inv.change_amount), 0)
		with self.assertRaises(frappe.ValidationError):
			inv.submit()
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"invoice": inv.name}))

	def test_a_trade_in_already_paid_out_cannot_be_credited(self):
		"""A payout made the old way, before the register paid sellers: the seller of that gun was already paid in cash, so it cannot also pay the customer on a ticket."""
		from ffl_core.api.firearm_intake import complete_intake

		acq = self._trade_in(300)
		complete_intake(acq)
		legacy_payout(acq, "Cash")
		with self.assertRaises(frappe.ValidationError):
			self._ticket(credits=(), acquisitions=[acq, self._trade_in(1000)])

	def test_a_gun_bought_from_a_dealer_is_not_paid_out_to_a_customer(self):
		acq = self._trade_in(1000)
		frappe.db.set_value("FFL Acquisition", acq, "source_type", "FFL Dealer")
		with self.assertRaises(frappe.ValidationError):
			self._ticket(credits=(), acquisitions=[acq])

	# ------------------------------------------------------------------ how the store paid

	def test_the_payout_needs_a_method_and_a_reference_unless_cash(self):
		for method, reference in ((None, None), ("Zelle", None), ("Check", "  ")):
			with self.assertRaises(frappe.ValidationError, msg=f"{method} {reference!r}"):
				self._ticket(credits=(1000,), method=method, reference=reference)

	def test_a_bank_payout_leaves_the_drawer_alone(self):
		"""Zelle / Check / ACH come out of the bank, with their reference; the drawer's figure does not move."""
		before = cd.drawer_expected(self.company).expected
		inv = self._ticket(method="Zelle", reference="ZL-12345")
		payout = self._payout(inv)
		self.assertEqual((payout.payment_method, payout.reference), ("Zelle", "ZL-12345"))
		self.assertEqual(self._lines(payout.journal_entry), {self.debtors: (4885.0, 0), self.bank: (0, 4885.0)})
		self.assertEqual(cd.drawer_expected(self.company).expected, before)

	def test_a_cash_payout_is_in_the_drawer_and_its_log(self):
		"""Cash leaves the drawer: the expected figure drops by 4,885.00, the log shows a trade_in_payout line
		with the ticket's number, and the weekly report adds it to what was paid out."""
		before = cd.drawer_expected(self.company).expected
		inv = self._ticket()
		self.assertEqual(cd.drawer_expected(self.company).expected, flt(before - 4885, 2))
		self.assertEqual(api.preview_close(self.company, 0)["expected"], flt(before - 4885, 2))
		_opening, rows = cd.log_rows(self.company, self.cash, getdate(), getdate())
		lines = [r for r in rows if r.kind == "trade_in_payout"]
		self.assertEqual([flt(r.amount) for r in lines], [-4885.0])
		self.assertIn(inv.name, cd.entry_detail(lines[0].entry))
		self.assertTrue(cd.KIND_LABELS["trade_in_payout"])
		self.assertFalse([r for r in rows if r.voucher_no == inv.name])  # the ticket itself moved no cash
		self.assertEqual(api.get_log(self.company, nowdate(), nowdate())["cash_out"], 4885.0)
		_cols, data = self._report()
		self.assertEqual(next(d for d in data if d.get("day") == nowdate())["payouts"], -4885.0)

	def test_a_tickets_payout_is_cancelled_only_with_its_ticket(self):
		"""The payout's journal entry cancelled on its own in Desk would leave the ticket
		submitted with its payout gone -- refused, naming the ticket; cancelling the ticket takes it."""
		inv = self._ticket(credits=(1000,))
		payout = self._payout(inv)
		je = frappe.get_doc("Journal Entry", payout.journal_entry)
		je.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as ctx:
			je.cancel()
		self.assertIn(inv.name, str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", payout.name, "status"), "Posted")

	def test_a_ticket_pays_out_once(self):
		"""Booking the same ticket again returns the one payout; the database refuses a second one."""
		inv = self._ticket(credits=(1000,))
		first = self._payout(inv)
		again = cd.record_trade_in_payout(frappe.get_doc("POS Invoice", inv.name))
		self.assertEqual(again.entry, first.name)
		self.assertEqual(frappe.db.count("Cash Drawer Entry", {"invoice": inv.name}), 1)
		with self.assertRaises(frappe.UniqueValidationError):
			cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=435, payment_method="Cash",
				invoice=inv.name, customer=inv.customer)
		self.assertEqual(len(self._jes("CASH-TRADEIN")), 1)

	def test_a_payout_entry_must_match_its_ticket(self):
		inv = self._ticket(credits=(1000,))
		frappe.db.set_value("Cash Drawer Entry", self._payout(inv).name, "payout_key", None)  # let a second one through
		with self.assertRaises(frappe.ValidationError):  # not the amount the ticket owes
			cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=500, payment_method="Cash",
				invoice=inv.name)
		with self.assertRaises(frappe.ValidationError):  # not the way the ticket says the customer was paid
			cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=435, payment_method="Zelle",
				reference="ZL-1", invoice=inv.name)
		positive = self._ticket(credits=(100,), submit=False)
		with self.assertRaises(frappe.ValidationError):  # a ticket the customer pays is owed nothing
			cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=10, payment_method="Cash",
				invoice=positive.name)
		zelle = self._ticket(credits=(1000,), method="Zelle", reference="ZL-777")
		frappe.db.set_value("Cash Drawer Entry", self._payout(zelle).name, "payout_key", None)
		with self.assertRaises(frappe.ValidationError):  # another reference than the ticket's
			cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=435, payment_method="Zelle",
				reference="ZL-778", invoice=zelle.name)
		control = cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=435, payment_method="Zelle",
			reference="ZL-777", invoice=zelle.name)  # the same entry, made the ticket's way, goes through
		frappe.db.set_value("Cash Drawer Entry", control.entry, "payout_key", None)
		credit_row = next(r for r in zelle.items if r.item_code == TRADE_IN_CREDIT_ITEM)
		frappe.db.set_value("POS Invoice Item", credit_row.name, "item_code", "NEG-GOODS")
		with self.assertRaises(frappe.ValidationError):  # a ticket with no trade-in credit owes no trade-in payout
			cd._new_entry(entry_type="Trade-in Payout", company=self.company, amount=435, payment_method="Zelle",
				reference="ZL-777", invoice=zelle.name)

	def test_the_drawer_page_cannot_undo_a_ticket_payout(self):
		inv = self._ticket(credits=(1000,))
		with self.assertRaises(frappe.ValidationError):
			api.undo_entry(self._payout(inv).name)
		self.assertTrue(self._payout(inv))

	# ------------------------------------------------------------------ the credit line's account

	def test_the_credit_line_books_to_stock_adjustment(self):
		"""TRADE-IN-CREDIT's Item Default for the company is Stock Adjustment (idempotent), and the ticket's
		credit row is pinned to it even though the POS profile names a Sales account."""
		from ffl_core.setup import trade_in_credit

		default = frappe.db.get_value("Item Default", {"parent": TRADE_IN_CREDIT_ITEM, "company": self.company}, "income_account")
		self.assertEqual(default, self.stock_adjustment)
		self.assertEqual(trade_in_credit.ensure().get("defaults_set"), 0)
		self.assertNotEqual(self.sales_income, self.stock_adjustment)
		inv = self._ticket(credits=(100,), submit=False)
		row = next(r for r in inv.items if r.item_code == TRADE_IN_CREDIT_ITEM)
		row.income_account = self.sales_income  # what the POS profile hands the cart
		inv.flags.ignore_permissions = True
		inv.save()
		self.assertEqual(next(r for r in inv.items if r.item_code == TRADE_IN_CREDIT_ITEM).income_account,
			self.stock_adjustment)


StoreANegativeTradeIn = _store_tests("StoreANegativeTradeIn", "store_a", NegFixtures, NegCases)
StoreBNegativeTradeIn = _store_tests("StoreBNegativeTradeIn", "store_b", NegFixtures, NegCases)
