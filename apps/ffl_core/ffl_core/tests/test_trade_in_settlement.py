"""A negative trade-in ticket after the register: merged, cancelled, returned.

The example ticket: $565 of taxable goods against five bought guns (450 / 1200 / 300 / 2750 / 750 = 5,450 of credit),
tax 0, grand total -4,885, the store paying the customer 4,885. Run on the two stand-in stores of test_cash_drawer
(Store A-like, Store B-like: accounts come from each company's abbr), the bought guns booked with record_firearm_acquisition
(the store's own trade-in booking); the last class runs the real trade-in dialog on the shared dev company, whose
deferred booking makes the Material Receipts the cancel must leave alone.

Nothing commits: ShiftFixtures neutralises the merges' commits and gives every test its own savepoint.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime, nowdate

from ffl_core import cash_drawer as cd
from ffl_core.api.pos_consolidate import consolidate_firearm_invoice_on_submit, consolidate_pos_invoice_now
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM
from ffl_core.tests.test_cash_drawer import _store_tests
from ffl_core.tests.test_cash_drawer_pos import ShiftFixtures, trade_in_acquisition

CUSTOMER = "_Test CD Customer"
GOODS = 565
CREDITS = (450, 1200, 300, 2750, 750)
PAYOUT = 4885


class SettlementFixtures(ShiftFixtures):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup.trade_in_credit import ensure

		ensure()
		cls.debtors = frappe.get_cached_value("Company", cls.company, "default_receivable_account")
		title = "_Test NTS Sales Tax"
		cls.tax_template = f"{title} - {cls.abbr}"
		if not frappe.db.exists("Sales Taxes and Charges Template", cls.tax_template):
			frappe.get_doc({
				"doctype": "Sales Taxes and Charges Template", "title": title, "company": cls.company,
				"taxes": [{"charge_type": "On Net Total", "account_head": cls.stp, "description": "Sales Tax", "rate": 8.25}],
			}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		self.opening = self._open_shift()

	def _acquisition(self, serial, cost=300, item="CD-GUN"):
		return trade_in_acquisition(self.company, serial, cost, item)

	# --- the ticket ---------------------------------------------------------------------------

	def _ticket(self, credits=CREDITS, goods=GOODS, method="Cash", reference=None, tag="") -> str:
		"""A submitted negative trade-in ticket rung up by the cashier: taxable goods, one credit line per bought gun,
		nothing tendered, the difference paid to the customer by ``method``."""
		stem = f"NT{self.KIND}-{self._testMethodName[5:45]}{tag}"
		acquisitions = [self._acquisition(f"{stem}-{i}", cost) for i, cost in enumerate(credits)]
		frappe.set_user(self.cashier)
		try:
			inv = frappe.new_doc("POS Invoice")
			inv.pos_profile = self.profile
			inv.customer = CUSTOMER
			inv.append("items", {"item_code": "CD-SERVICE", "qty": 1, "rate": goods, "price_list_rate": goods})
			for acq, cost in zip(acquisitions, credits):
				inv.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -cost, "price_list_rate": -cost,
					"ffl_trade_in_acquisition": acq})
			inv.set_missing_values()
			inv.items[0].rate = inv.items[0].price_list_rate = goods
			inv.taxes_and_charges = self.tax_template
			inv.set("taxes", [])
			inv.set_taxes()
			inv.set("payments", [])
			inv.append("payments", {"mode_of_payment": "Cash", "amount": 0})
			inv.paid_amount = inv.base_paid_amount = 0  # nothing tendered, as the register sends it
			inv.ffl_trade_in_payout_method = method
			inv.ffl_trade_in_payout_reference = reference
			inv.flags.ignore_permissions = True
			inv.save()
			inv.submit()
			self.assertEqual(flt(inv.grand_total), goods - sum(credits))  # no tax: the credit covers the goods
			return inv.name
		finally:
			frappe.set_user("Administrator")

	def _payout(self, ticket):
		return frappe.db.get_value("Cash Drawer Entry", {"invoice": ticket, "entry_type": "Trade-in Payout"},
			["name", "status", "journal_entry", "amount"], as_dict=True)

	def _merge(self, ticket) -> str:
		out = consolidate_pos_invoice_now(ticket)
		self.assertTrue(out["consolidated"], out)
		return out["sales_invoice"]

	def _merge_log(self, si) -> str:
		return frappe.db.get_value("POS Invoice Merge Log", {"consolidated_invoice": si, "docstatus": 1})

	def _cancel(self, doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.flags.ignore_permissions = True
		doc.cancel()

	def _refused(self, doctype, name) -> Exception:
		"""Cancel and expect a refusal; roll its partial writes back as the failed request would."""
		frappe.db.savepoint("refused_cancel")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._cancel(doctype, name)
		frappe.db.rollback(save_point="refused_cancel")
		return ctx.exception

	def _customer_balance(self) -> float:
		"""What the customer owes (+) or is owed (-) on the books: their Debtors GL and the payment ledger agree."""
		gl = flt(frappe.db.sql(
			"SELECT IFNULL(SUM(debit - credit), 0) FROM `tabGL Entry` WHERE company = %s AND account = %s AND party = %s "
			"AND is_cancelled = 0", (self.company, self.debtors, CUSTOMER))[0][0], 2)
		ple = flt(frappe.db.sql(
			"SELECT IFNULL(SUM(amount), 0) FROM `tabPayment Ledger Entry` WHERE company = %s AND account = %s AND party = %s "
			"AND delinked = 0", (self.company, self.debtors, CUSTOMER))[0][0], 2)
		self.assertEqual(gl, ple)
		return gl

	def _counted_now(self):
		"""A drawer count taken right now (the row close_day leaves; its shift merging is not what is tested here)."""
		close = frappe.get_doc({
			"doctype": "Cash Drawer Close", "company": self.company, "business_date": nowdate(), "status": "Closed",
			"is_counted": 1, "counted": 0, "expected": 0, "closed_at": now_datetime(),
			"active_key": f"{self.company}|{nowdate()}",
		})
		close.db_insert()
		return close.name


class SettlementCases:
	# ------------------------------------------------------------------ merge (AC 8, 13)

	def test_a_negative_ticket_merges_at_once(self):
		"""Design 7: the submit asks for its own immediate merge (a firearm ticket's path), not tonight's close."""
		with patch("ffl_core.api.pos_consolidate.frappe.enqueue") as enqueue:
			ticket = self._ticket()
		calls = [c for c in enqueue.call_args_list if c.kwargs.get("pos_invoice") == ticket]
		self.assertEqual(len(calls), 1)
		self.assertTrue(calls[0].kwargs["enqueue_after_commit"])
		self.assertEqual(calls[0].args[0], "ffl_core.api.pos_consolidate.consolidate_pos_invoice_now")

	def test_the_merged_invoice_is_negative_with_no_tax_and_the_customer_owes_nothing(self):
		"""The consolidated Sales Invoice submits at -4,885 with its tax row copied at 0; the payout is
		set against it, so it reads Paid with nothing outstanding and the customer's Debtors are flat."""
		cash = self._gl_cash()
		ticket = self._ticket()
		payout = self._payout(ticket)
		self.assertEqual((payout.status, flt(payout.amount)), ("Posted", PAYOUT))
		si = frappe.get_doc("Sales Invoice", self._merge(ticket))
		self.assertEqual(self._gl_cash(), cash - PAYOUT)  # paid out once, by the payout entry
		self.assertEqual((si.docstatus, si.is_consolidated), (1, 1))
		self.assertEqual(flt(si.grand_total), -PAYOUT)
		self.assertEqual(flt(si.total_taxes_and_charges), 0)
		self.assertTrue(si.taxes, "the ticket's tax row is copied")
		self.assertTrue(all(flt(t.tax_amount) == 0 and flt(t.base_tax_amount_after_discount_amount) == 0 for t in si.taxes))
		self.assertEqual(flt(si.outstanding_amount), 0)
		self.assertEqual(si.status, "Paid")
		self.assertEqual(frappe.db.get_value("Journal Entry Account",
			{"parent": payout.journal_entry, "account": self.debtors}, ["reference_type", "reference_name"]),
			("Sales Invoice", si.name))
		self.assertEqual(self._customer_balance(), 0)
		# the merge never posts sales tax for this ticket
		self.assertFalse(frappe.db.sql(
			"SELECT 1 FROM `tabGL Entry` WHERE voucher_no = %s AND account = %s AND is_cancelled = 0 AND (debit <> 0 OR credit <> 0)",
			(si.name, self.stp)))

	def _error_logs(self, needle):
		return frappe.get_all("Error Log", filters={"method": ["like", f"%{needle}%"]}, pluck="name")

	def test_a_reconcile_that_fails_never_blocks_the_merge(self):
		"""A reconcile that throws halfway is rolled back to before it and logged; the merge (and the
		sold gun's stock-out with it) still goes through, the invoice simply left unpaired."""
		import erpnext.accounts.utils as accounts_utils

		ticket = self._ticket()
		payout = self._payout(ticket)

		def half_then_fail(args):
			frappe.db.set_value("Journal Entry Account", args[0].voucher_detail_no,
				{"reference_type": "Sales Invoice", "reference_name": args[0].against_voucher})
			frappe.flags.ignore_party_validation = True
			raise frappe.ValidationError("reconcile blew up")

		with patch.object(accounts_utils, "reconcile_against_document", side_effect=half_then_fail):
			si = frappe.get_doc("Sales Invoice", self._merge(ticket))
		self.assertEqual((si.docstatus, flt(si.outstanding_amount)), (1, -PAYOUT))
		self.assertFalse(frappe.db.get_value("Journal Entry Account",
			{"parent": payout.journal_entry, "account": self.debtors}, "reference_name"), "the half-write is rolled back")
		self.assertFalse(frappe.flags.ignore_party_validation)
		self.assertTrue(self._error_logs(f"not reconciled with {si.name}"))
		self.assertEqual(self._customer_balance(), 0)  # the books are right; only the pairing is missing

	def test_a_payout_on_another_debtors_account_is_logged_not_forced(self):
		"""The payout's own Debtors line (its customer, the ticket's account) is what gets paired; one
		the invoice cannot take is left for Payment Reconciliation and logged."""
		ticket = self._ticket()
		payout = self._payout(ticket)
		other = frappe.db.get_value("Account", {"company": self.company, "account_type": "Receivable", "is_group": 0,
			"name": ["!=", self.debtors]}) or f"_Test NTS Other Debtors - {self.abbr}"
		frappe.db.set_value("Cash Drawer Entry", payout.name, "debit_account", other)
		si = frappe.get_doc("Sales Invoice", self._merge(ticket))
		self.assertEqual((si.docstatus, flt(si.outstanding_amount)), (1, -PAYOUT))
		self.assertTrue(self._error_logs(f"{payout.name} not reconciled with {si.name}"))

	def test_the_sweep_leaves_a_ticket_being_cancelled(self):
		"""Cancelling the merge is the manager's first step to cancelling the ticket; the 15-minute
		sweep must not merge it straight back. A negative ticket that simply never merged is still swept."""
		from ffl_core.api.pos_consolidate import _find_stuck_pos_invoices

		being_cancelled = self._ticket(tag="c")
		self._cancel("POS Invoice Merge Log", self._merge_log(self._merge(being_cancelled)))
		stranded = self._ticket(tag="s")
		found = _find_stuck_pos_invoices(-60)  # settle window in the future: everything settled
		self.assertNotIn(being_cancelled, found)
		self.assertIn(stranded, found)

	def test_no_desk_credit_note_against_a_merged_negative_ticket(self):
		"""ERPNext's Return button refuses a consolidated invoice, but a credit note typed in Desk with
		Return Against would refund the trade-in credit and tax. Refused, naming the ticket."""
		ticket = self._ticket()
		si = self._merge(ticket)
		note = frappe.new_doc("Sales Invoice")
		note.update({"customer": CUSTOMER, "company": self.company, "is_return": 1, "return_against": si})
		note.append("items", {"item_code": "CD-SERVICE", "qty": -1, "rate": GOODS})
		note.set_missing_values()
		note.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError) as ctx:
			note.insert()
		self.assertIn(ticket, str(ctx.exception))

	def test_a_second_merge_attempt_changes_nothing(self):
		ticket = self._ticket()
		si = self._merge(ticket)
		self.assertFalse(consolidate_pos_invoice_now(ticket)["consolidated"])
		self.assertEqual(frappe.db.count("POS Invoice Reference", {"pos_invoice": ticket, "docstatus": 1}), 1)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")), 0)
		self.assertEqual(self._customer_balance(), 0)

	def test_a_ticket_the_shift_close_merges_with_other_sales_still_nets_to_zero(self):
		"""Should the immediate merge never run, the shift's close takes the ticket, batched per customer with the
		day's other sales; the payout is still set against that invoice."""
		from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

		cash = self._gl_cash()
		ticket = self._ticket()
		sale = self._pos_sale(rate=100, tendered=120)  # 20 change
		closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", self.opening))
		closing.flags.ignore_permissions = True
		closing.insert()
		closing.submit()
		si = frappe.db.get_value("POS Invoice", ticket, "consolidated_invoice")
		self.assertTrue(si)
		self.assertEqual(frappe.db.get_value("POS Invoice", sale, "consolidated_invoice"), si)
		doc = frappe.get_doc("Sales Invoice", si)
		self.assertEqual((flt(doc.grand_total), flt(doc.outstanding_amount), doc.status), (100 - PAYOUT, 0, "Paid"))
		# the sale's change only: ERPNext's paid - total would pay the payout out again as change
		self.assertEqual(flt(doc.change_amount), 20)
		self.assertEqual(self._gl_cash(), cash + 100 - PAYOUT)
		self.assertEqual(self._customer_balance(), 0)

	def test_an_ordinary_ticket_is_still_left_for_the_nightly_close(self):
		with patch("ffl_core.api.pos_consolidate.frappe.enqueue") as enqueue:
			consolidate_firearm_invoice_on_submit(frappe._dict(name="X", grand_total=10,
				items=[frappe._dict(item_code="CD-SERVICE")]))
			consolidate_firearm_invoice_on_submit(frappe._dict(name="Y", grand_total=-10, is_return=1,
				items=[frappe._dict(item_code="CD-SERVICE")]))
		enqueue.assert_not_called()

	# ------------------------------------------------------------------ cancel

	def test_cancelling_an_unmerged_ticket_puts_the_cash_back_and_keeps_the_guns(self):
		ticket = self._ticket()
		acquisitions = frappe.get_all("POS Invoice Item", {"parent": ticket, "item_code": TRADE_IN_CREDIT_ITEM},
			pluck="ffl_trade_in_acquisition")
		expected = self._expected()
		payout = self._payout(ticket)
		self._cancel("POS Invoice", ticket)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", payout.name, "status"), "Voided")
		self.assertEqual(frappe.db.get_value("Journal Entry", payout.journal_entry, "docstatus"), 2)
		self.assertEqual(self._expected(), expected + PAYOUT)
		self.assertEqual([frappe.db.get_value("FFL Acquisition", a, "docstatus") for a in acquisitions], [1] * len(CREDITS))
		self.assertEqual(self._customer_balance(), 0)

	def test_cancelling_a_merged_ticket_goes_through_its_merge(self):
		"""ERPNext refuses the ticket while its merged invoice is live; cancelling the merge takes the invoice out and
		leaves the payout with the ticket (unlinked from the invoice); the ticket's own cancel then voids it."""
		ticket = self._ticket()
		si = self._merge(ticket)
		payout = self._payout(ticket)
		self._refused("POS Invoice", ticket)
		self._cancel("POS Invoice Merge Log", self._merge_log(si))
		self.assertEqual(frappe.db.get_value("Sales Invoice", si, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", payout.name, "status"), "Posted")
		self.assertFalse(frappe.db.get_value("Journal Entry Account",
			{"parent": payout.journal_entry, "account": self.debtors}, "reference_name"))
		self._cancel("POS Invoice", ticket)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", payout.name, "status"), "Voided")
		self.assertEqual(self._customer_balance(), 0)

	def test_a_ticket_merged_again_is_reconciled_again(self):
		ticket = self._ticket()
		self._cancel("POS Invoice Merge Log", self._merge_log(self._merge(ticket)))
		again = frappe.get_doc("Sales Invoice", self._merge(ticket))
		self.assertEqual((flt(again.outstanding_amount), again.status), (0, "Paid"))
		self.assertEqual(self._customer_balance(), 0)

	def test_no_cancel_once_a_count_relied_on_the_payout(self):
		ticket = self._ticket()
		payout = self._payout(ticket)
		self._counted_now()
		self._refused("POS Invoice", ticket)
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", payout.name, "status"), "Posted")

	def test_no_cancel_of_the_merged_invoice_once_a_count_relied_on_the_payout(self):
		ticket = self._ticket()
		si = self._merge(ticket)
		self._counted_now()
		self.assertIn(ticket, str(self._refused("POS Invoice Merge Log", self._merge_log(si))))
		self.assertIn(ticket, str(self._refused("Sales Invoice", si)))
		self.assertEqual(frappe.db.get_value("Sales Invoice", si, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", self._payout(ticket).name, "status"), "Posted")

	def test_a_bank_payout_is_voided_too_and_never_touches_the_drawer(self):
		ticket = self._ticket(method="Zelle", reference="ZL-1")
		expected = self._expected()
		payout = self._payout(ticket)
		self._cancel("POS Invoice", ticket)
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", payout.name, "status"), "Voided")
		self.assertEqual(self._expected(), expected)

	def test_void_trade_in_payout_is_none_without_a_payout(self):
		self.assertIsNone(cd.void_trade_in_payout("NO-SUCH-TICKET"))

	# ------------------------------------------------------------------ return

	def _return(self, ticket):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		ret = make_sales_return(ticket)
		# As the register would send it: the ticket's tax template applied to the goods coming back.
		ret.taxes_and_charges = self.tax_template
		ret.set("taxes", [])
		ret.set_taxes()
		ret.calculate_taxes_and_totals()
		self.assertNotEqual(flt(ret.total_taxes_and_charges), 0, "without the rule the return would move tax")
		ret.set("payments", [])
		ret.append("payments", {"mode_of_payment": "Cash", "amount": -GOODS})
		ret.flags.ignore_permissions = True
		return ret

	def test_a_return_gives_back_the_goods_and_no_tax(self):
		"""The goods come back at 565.00 with 0.00 tax (the ticket collected none); the credit lines stay out —
		the bought guns are the store's."""
		ticket = self._ticket()
		self._merge(ticket)
		acquisitions = frappe.get_all("POS Invoice Item", {"parent": ticket, "item_code": TRADE_IN_CREDIT_ITEM},
			pluck="ffl_trade_in_acquisition")
		ret = self._return(ticket)
		ret.save()
		self.assertEqual([r.item_code for r in ret.items], ["CD-SERVICE"])
		self.assertEqual(flt(ret.total_taxes_and_charges), 0)
		self.assertEqual(flt(ret.grand_total), -GOODS)
		ret.submit()
		self.assertEqual([frappe.db.get_value("FFL Acquisition", a, "docstatus") for a in acquisitions], [1] * len(CREDITS))
		note = frappe.get_doc("Sales Invoice", self._merge(ret.name))
		self.assertEqual((note.is_return, flt(note.grand_total), flt(note.total_taxes_and_charges)), (1, -GOODS, 0))
		self.assertFalse(frappe.db.sql(
			"SELECT 1 FROM `tabGL Entry` WHERE voucher_no = %s AND account = %s AND is_cancelled = 0 AND (debit <> 0 OR credit <> 0)",
			(note.name, self.stp)))
		self.assertEqual(self._customer_balance(), 0)

	def test_a_refund_that_still_asks_for_the_tax_is_refused(self):
		"""Once the cart is goods-only the refund is the cashier's, and ERPNext refuses one above the return."""
		ticket = self._ticket()
		ret = self._return(ticket)
		ret.save()
		ret.payments[0].amount = -(GOODS + round(GOODS * 0.0825, 2))
		ret.paid_amount = ret.payments[0].amount
		with self.assertRaises(frappe.ValidationError):
			ret.submit()

	# The register's Return button. It maps the ticket (make_sales_return), seeds its default tender with
	# the cart total while the credit lines are still in (+4,885), then asks the server for its POS defaults
	# (set_missing_values, through run_doc_method); Checkout saves before the payment screen.

	def _register_return(self, ticket):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		ret = make_sales_return(ticket)
		ret.pos_profile = self.profile
		ret.set("payments", [])
		ret.append("payments", {"mode_of_payment": "Cash", "amount": PAYOUT, "default": 1})
		ret.paid_amount = ret.base_paid_amount = PAYOUT
		ret.flags.ignore_permissions = True
		return ret

	def test_the_register_return_cart_comes_back_goods_only(self):
		"""The cart the cashier sees after Return: no credit lines, no tax, refund -565 on the default tender, and
		none of the ticket's payout facts — before Checkout."""
		ticket = self._ticket()
		self._merge(ticket)
		ret = self._register_return(ticket)
		self.assertEqual(flt(ret.grand_total), PAYOUT, "the mapped cart still carries the credit lines")
		self.assertFalse(ret.ffl_trade_in_payout_method, "no_copy: the return does not inherit the payout")
		ret.ffl_trade_in_payout_method, ret.ffl_trade_in_payout_reference = "Cash", "stale"
		# What frappe.handler.run_doc_method does for the register's frm.call (it needs a live request): the doc
		# rebuilt from the browser's JSON, then run_method with the call's (absent) args — which fires doc_events.
		cart = frappe.get_doc(frappe.parse_json(ret.as_json()))
		cart.run_method("set_missing_values", None)
		self.assertEqual([r.item_code for r in cart.items], ["CD-SERVICE"])
		self.assertEqual((flt(cart.grand_total), flt(cart.total_taxes_and_charges)), (-GOODS, 0))
		self.assertFalse(cart.taxes_and_charges, "the register must not fetch the template's tax back")
		self.assertEqual([(p.mode_of_payment, flt(p.amount)) for p in cart.payments if flt(p.amount)], [("Cash", -GOODS)])
		self.assertFalse(cart.ffl_trade_in_payout_method or cart.ffl_trade_in_payout_reference)

	def test_a_checkout_save_sets_the_refund_to_the_goods(self):
		"""The reported cart: credit lines in, Cash +4,885. Checkout's save corrects the cart and the refund; it
		then submits as a -565 refund."""
		ticket = self._ticket()
		self._merge(ticket)
		ret = self._register_return(ticket)
		ret.save()
		self.assertEqual([r.item_code for r in ret.items], ["CD-SERVICE"])
		self.assertEqual((flt(ret.grand_total), flt(ret.paid_amount)), (-GOODS, -GOODS))
		self.assertEqual([(p.mode_of_payment, flt(p.amount)) for p in ret.payments if flt(p.amount)], [("Cash", -GOODS)])
		ret.submit()
		self.assertEqual(frappe.db.get_value("POS Invoice", ret.name, ["docstatus", "grand_total", "paid_amount"]),
			(1, -GOODS, -GOODS))
		note = frappe.get_doc("Sales Invoice", self._merge(ret.name))
		self.assertEqual((flt(note.grand_total), flt(note.total_taxes_and_charges)), (-GOODS, 0))
		self.assertEqual(self._customer_balance(), 0)

	def test_a_return_submitted_with_the_uncorrected_refund_is_refused_by_name(self):
		"""Straight to submit (an API caller) with the refund set against the uncorrected cart: nothing is changed
		on submit, the refund is refused with what it should be."""
		ticket = self._ticket()
		ret = self._register_return(ticket)
		with self.assertRaises(frappe.ValidationError) as ctx:
			ret.submit()
		self.assertIn("goods only", str(ctx.exception))


StoreASettlement = _store_tests("StoreASettlement", "store_a", SettlementFixtures, SettlementCases)
StoreBSettlement = _store_tests("StoreBSettlement", "store_b", SettlementFixtures, SettlementCases)


class TradeInDialogSettlement(ShiftFixtures):
	"""The real trade-in dialog on the shared dev company: the guns are booked (acquisition submitted, Material
	Receipt made) only when the ticket submits, and cancelling the ticket must leave both alone."""

	KIND = "store_a"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup.trade_in_credit import ensure
		from ffl_core.tests.pos_opening import todays_pos_opening

		ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls.abbr = frappe.db.get_value("Company", cls.company, "abbr")
		cls.cash = f"Cash - {cls.abbr}"
		frappe.defaults.set_user_default("Company", cls.company)
		cd.ensure_accounts(cls.company)
		todays_pos_opening(cls.company, "FFL Test")
		if not frappe.db.exists("Customer", "_Test Trade-in Seller"):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test Trade-in Seller",
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)

	def test_cancel_leaves_the_booked_guns_and_their_receipts(self):
		from ffl_core.api import trade_in

		acquisitions = []
		for i, cost in enumerate((300, 900)):
			res = trade_in.create_trade_in_intake({
				"serial_no": f"NTD-{i}", "manufacturer": "Glock", "model_name": "19", "caliber": "9mm",
				"firearm_type": "Pistol", "condition_grade": "Good", "id_type": "Driver License", "id_number": f"DL-NTD-{i}",
				"source_name": "_Test Trade-in Seller", "source_first_name": "Testy", "source_last_name": "Seller",
				"source_address": "123 Main St, Springfield TX 75024", "acquisition_cost": cost, "apply_credit": 1,
			})
			acquisitions.append((res["acquisition"], cost, f"NTD-{i}"))
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = "_Test Trade-in Seller"
		inv.append("items", {"item_code": "CD-SERVICE", "qty": 1, "rate": 200, "price_list_rate": 200})
		for acq, cost, _serial in acquisitions:
			inv.append("items", {"item_code": TRADE_IN_CREDIT_ITEM, "qty": 1, "rate": -cost, "price_list_rate": -cost,
				"ffl_trade_in_acquisition": acq})
		inv.set_missing_values()
		inv.items[0].rate = inv.items[0].price_list_rate = 200
		inv.set("payments", [])
		inv.append("payments", {"mode_of_payment": "Cash", "amount": 0})
		inv.paid_amount = inv.base_paid_amount = 0
		inv.ffl_trade_in_payout_method = "Cash"
		inv.flags.ignore_permissions = True
		inv.save()
		inv.submit()
		self.assertEqual(flt(inv.grand_total), -1000)
		receipts = {serial: frappe.db.sql_list(
			"SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry` WHERE voucher_type = 'Stock Entry' AND serial_no = %s "
			"OR serial_and_batch_bundle IN (SELECT parent FROM `tabSerial and Batch Entry` WHERE serial_no = %s)",
			(serial, serial)) for _a, _c, serial in acquisitions}
		self.assertTrue(all(receipts.values()), receipts)
		self.assertEqual([frappe.db.get_value("FFL Acquisition", a, "docstatus") for a, _c, _s in acquisitions], [1, 1])
		inv.reload()
		inv.flags.ignore_permissions = True
		inv.cancel()
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", {"invoice": inv.name}, "status"), "Voided")
		self.assertEqual([frappe.db.get_value("FFL Acquisition", a, "docstatus") for a, _c, _s in acquisitions], [1, 1])
		self.assertTrue(all(frappe.db.get_value("Stock Entry", se, "docstatus") == 1
			for names in receipts.values() for se in names))
		self.assertTrue(all(frappe.db.get_value("Serial No", s, "warehouse") for _a, _c, s in acquisitions))
