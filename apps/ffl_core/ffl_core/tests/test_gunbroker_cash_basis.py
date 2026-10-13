"""The cash-basis adjustment schedule.

The store books GunBroker on the accrual basis and files its return on the cash
basis. The bridge between the two is this schedule, and every failure it has
already survived in review was the same failure: a predicate written against
the CURRENT state of an order rather than against the period end.

That matters because of WHEN the schedule is read. Month-end close happens in
the first week of the next month; the year-end copy is used by the tax preparer
in February through April. By then every order that was sitting paid-and-
undisposed on 12/31 has shipped, so a current-state query returns an almost
empty page and the year's whole end-of-December float — cash that was in the
bank on 12/31 — silently drops out of gross receipts.

So the rule is: the same `period_end` produces the same schedule on any day it
is run. `TheScheduleIsAsOfNotAsNow` is the test that would have caught it, and
the rest of this module pins the pieces that feed it.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments import (
	gunbroker_cash_basis_adjustments as cb)


def _order(name="GB-1", *, received="2026-12-28", pe=None, pe_date=None,
           pe_docstatus=1, order_total=1000.0, tax=55.0, paid=1000.0,
           mode="GunBroker Check", ref="9001", cancel=None, gb_id="1001",
           si=None):
	return {"name": name, "gb_order_id": gb_id, "sales_invoice": si,
		"payment_received_on": received, "payment_entry": pe,
		"pe_posting_date": pe_date, "pe_docstatus": pe_docstatus,
		"order_total": order_total, "sales_tax_total": tax,
		"payment_amount": paid, "payment_mode_used": mode,
		"payment_reference_no": ref, "cancel_date": cancel}


def _refund(date, amount=1000.0, pe="PE-R"):
	return {"name": pe, "posting_date": date, "paid_amount": amount}


def _rows_for(rows, item):
	return [r for r in rows if r.get("item") == item and r.get("row_kind") == "detail"]


def _subtotal(rows, item, which="closing"):
	return next(r for r in rows if r.get("item") == item
		and r.get("row_kind") == "subtotal" and r.get("basis") == which)


# ------------------------------------------------------- the as-of predicate

class TheScheduleIsAsOfNotAsNow(FrappeTestCase):
	"""Three orders, three shapes of the same question."""

	def test_paid_before_period_end_and_never_disposed_is_listed(self):
		# (a) 12/28 paid, still undisposed on 12/31. Threshold is ZERO days —
		# the seven-day figure belongs to the stale-payment ALARM, and reusing
		# it here would drop the last week of every month.
		self.assertTrue(cb._still_in_hand(_order(received="2026-12-28"),
			[], "2026-12-31"))

	def test_paid_after_period_end_is_not_listed(self):
		self.assertFalse(cb._still_in_hand(_order(received="2027-01-02"),
			[], "2026-12-31"))

	def test_disposed_after_period_end_is_still_listed(self):
		# (b) THE test. Disposed 1/3, schedule run 1/5 for period_end 12/31:
		# on 12/31 the cash was in the bank and the ledger had nothing, so it
		# belongs in the schedule no matter what happened afterwards.
		order = _order(received="2026-12-28", pe="PE-1", pe_date="2027-01-03")
		self.assertTrue(cb._still_in_hand(order, [], "2026-12-31"))

	def test_the_same_order_is_gone_from_the_next_period(self):
		order = _order(received="2026-12-28", pe="PE-1", pe_date="2027-01-03")
		self.assertFalse(cb._still_in_hand(order, [], "2027-01-31"))

	def test_a_receipt_posted_inside_the_period_is_not_listed(self):
		order = _order(received="2026-12-05", pe="PE-1", pe_date="2026-12-20")
		self.assertFalse(cb._still_in_hand(order, [], "2026-12-31"))

	def test_a_cancelled_payment_entry_counts_as_no_payment_entry(self):
		# after an NSF reversal the link survives on the order. Reading
		# the link alone would drop the order off the schedule for good, while
		# the money question is still open.
		order = _order(received="2026-12-05", pe="PE-1", pe_date="2026-12-20",
			pe_docstatus=2)
		self.assertTrue(cb._still_in_hand(order, [], "2026-12-31"))

	def test_a_link_to_a_vanished_entry_keeps_the_order_listed(self):
		order = _order(received="2026-12-05", pe="PE-GONE", pe_date=None)
		self.assertTrue(cb._still_in_hand(order, [], "2026-12-31"))

	def test_an_order_with_no_payment_date_is_not_silently_included(self):
		# The invariant upstream guarantees a date whenever a mode was recorded;
		# if one is missing anyway it is a data fault, not a receipt.
		self.assertFalse(cb._still_in_hand(_order(received=None),
			[], "2026-12-31"))


class CancellationIsNotTheSameThingAsRefund(FrappeTestCase):
	"""On 12/31 the bank still holds the money; what takes
	it back out is a refund that has POSTED, not a cancellation that has been
	agreed."""

	def test_cancelled_in_period_but_refunded_next_year_is_still_listed(self):
		order = _order(received="2026-12-28", cancel="2026-12-30 10:00:00")
		self.assertTrue(cb._still_in_hand(order, [_refund("2027-01-05")], "2026-12-31"))

	def test_the_same_order_drops_out_once_the_refund_posts_in_period(self):
		order = _order(received="2026-12-28", cancel="2026-12-30 10:00:00")
		self.assertFalse(cb._still_in_hand(order, [_refund("2026-12-30")], "2026-12-31"))

	def test_a_cancellation_alone_never_excludes(self):
		# The v5 rule this replaces excluded on cancel_date, which understated
		# the year by every cheque cancelled in December and refunded in January
		# — and then produced a January refund with no receipt to sit against.
		order = _order(received="2026-12-28", cancel="2026-12-30 10:00:00")
		self.assertTrue(cb._still_in_hand(order, [], "2026-12-31"))

	def test_a_cancellation_at_half_eleven_on_new_years_eve_belongs_to_december(self):
		# The column is stored site-local, so the date part is taken
		# directly. The value below is 2027-01-01T05:30Z as stored — reading it
		# as UTC would move a December cancellation into January.
		order = _order(received="2026-12-28", cancel="2026-12-31 23:30:00")
		self.assertEqual(cb._cancel_note(order, "2026-12-31"),
			"Cancelled — refund pending")

	def test_a_cancellation_after_period_end_is_labelled_as_such(self):
		order = _order(received="2026-12-28", cancel="2027-01-04 09:00:00")
		self.assertEqual(cb._cancel_note(order, "2026-12-31"),
			"Cancelled after period end")

	def test_a_live_order_carries_no_note(self):
		self.assertEqual(cb._cancel_note(_order(), "2026-12-31"), "")


class TheRefundEntryIsFoundByTheOrderNumberInItsRemarks(FrappeTestCase):
	"""The refund is keyed by the order number the operator must write in the
	entry's remarks — the same marker every GunBroker receipt already carries.

	Every refund against an order is collected, with its amount, because a
	partial refund is not a cancellation: taking $100 back off a $1,000 cheque
	leaves $900 in the bank, and that $900 is still the year's income."""

	def test_the_marker_matches_its_own_order(self):
		matched = cb._match_refunds([_order(gb_id="1001")], [
			{"name": "PE-R", "posting_date": "2027-01-05", "paid_amount": 1000.0,
			 "remarks": "Refund of GunBroker Order #1001 — cheque 40012"}])
		self.assertEqual(matched["GB-1"][0]["posting_date"], "2027-01-05")
		self.assertEqual(matched["GB-1"][0]["name"], "PE-R")

	def test_the_text_the_alerts_give_operators_is_what_the_report_matches(self):
		# refund_remarks_rule builds the marker from ORDER_REMARK_PREFIX; writing
		# exactly the quoted text into a Payment Entry's remarks must match.
		import re

		from ffl_core.marketplace_accounts import refund_remarks_rule
		for kwargs in ({}, {"receive_too": True}):
			rule = refund_remarks_rule("998877", **kwargs)
			self.assertNotIn("<", rule)
			self.assertIn("Pay-type Payment Entry", rule)
			self.assertIn("Party Type Customer", rule)
			self.assertIn("Custom Remarks first", rule)
			marker = re.search(r'"([^"]+)"', rule).group(1)
			matched = cb._match_refunds([_order(gb_id="998877")], [
				{"name": "PE-R", "posting_date": "2027-01-05", "paid_amount": 1.0,
				 "remarks": marker}])
			self.assertEqual(matched["GB-1"][0]["name"], "PE-R")
		self.assertIn("unreferenced Receive and the Pay",
			refund_remarks_rule("998877", receive_too=True))

	def test_a_longer_order_number_is_not_a_prefix_match(self):
		# "#1001" must not be satisfied by "#10011" — an unrelated refund would
		# take a live order off the schedule.
		matched = cb._match_refunds([_order(gb_id="1001")], [
			{"name": "PE-R", "posting_date": "2027-01-05", "paid_amount": 1000.0,
			 "remarks": "Refund of GunBroker Order #10011"}])
		self.assertEqual(matched, {})

	def test_every_refund_is_kept_not_just_the_earliest(self):
		# Two part refunds have to add up; keeping only the first understates
		# what went back and overstates what is still in the bank.
		matched = cb._match_refunds([_order(gb_id="1001")], [
			{"name": "PE-A", "posting_date": "2027-02-01", "paid_amount": 100.0,
			 "remarks": "GunBroker Order #1001"},
			{"name": "PE-B", "posting_date": "2027-01-05", "paid_amount": 200.0,
			 "remarks": "GunBroker Order #1001"}])
		self.assertEqual(len(matched["GB-1"]), 2)
		self.assertEqual(sum(r["paid_amount"] for r in matched["GB-1"]), 300.0)

	def test_an_entry_with_no_remarks_matches_nothing(self):
		self.assertEqual(cb._match_refunds([_order()], [
			{"name": "PE-R", "posting_date": "2027-01-05", "paid_amount": 1.0,
			 "remarks": None}]), {})

	def test_only_customer_payments_are_considered(self):
		# Paying GunBroker's own fee bill by Payment Entry and typing
		# the order number in the remarks would otherwise read as a refund to
		# the buyer, and take a live order off the schedule — understating the
		# year's receipts with nothing to show for it.
		module = ("ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments"
			".gunbroker_cash_basis_adjustments")
		with patch("frappe.db.sql", return_value=[]) as sql:
			cb._fetch_refund_entries("TSA", "2026-12-31")
		query = " ".join(sql.call_args[0][0].split())
		self.assertIn("party_type = 'Customer'", query)

	def test_only_submitted_payments_are_considered(self):
		with patch("frappe.db.sql", return_value=[]) as sql:
			cb._fetch_refund_entries("TSA", "2026-12-31")
		self.assertIn("docstatus = 1", " ".join(sql.call_args[0][0].split()))


class APartRefundIsNotACancellation(FrappeTestCase):
	"""The old rule was binary: any refund at all, and the whole order
	left the schedule. A buyer who negotiates $100 off a $1,000 cheque before
	the gun ships leaves $900 sitting in the bank on 31 December, and that $900
	is that year's income."""

	def test_a_part_refunded_order_is_still_in_hand(self):
		order = _order(received="2026-12-28", paid=1000.0)
		self.assertTrue(cb._still_in_hand(order,
			[_refund("2026-12-30", amount=100.0)], "2026-12-31"))

	def test_a_fully_refunded_order_is_not(self):
		order = _order(received="2026-12-28", paid=1000.0)
		self.assertFalse(cb._still_in_hand(order,
			[_refund("2026-12-30", amount=1000.0)], "2026-12-31"))

	def test_part_refunds_add_up(self):
		order = _order(received="2026-12-28", paid=1000.0)
		self.assertFalse(cb._still_in_hand(order, [
			_refund("2026-12-29", amount=400.0, pe="PE-A"),
			_refund("2026-12-30", amount=600.0, pe="PE-B")], "2026-12-31"))

	def test_a_refund_posted_after_the_period_does_not_reduce_it(self):
		order = _order(received="2026-12-28", paid=1000.0)
		self.assertAlmostEqual(cb._in_hand(order,
			[_refund("2027-01-05", amount=1000.0)], "2026-12-31"), 1000.0)

	def test_the_amount_still_in_hand_is_net_of_refunds(self):
		order = _order(received="2026-12-28", paid=1000.0)
		self.assertAlmostEqual(cb._in_hand(order,
			[_refund("2026-12-30", amount=100.0)], "2026-12-31"), 900.0)

	def test_the_closing_column_carries_the_net_not_the_gross(self):
		rows = cb._build_item3([_order(received="2026-12-28", paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["closing"], 900.0)
		self.assertAlmostEqual(row["refunded"], 100.0)
		self.assertAlmostEqual(row["payment_amount"], 1000.0)   # gross, as banked

	def test_item_five_and_seven_stay_GROSS(self):
		# The refund cheque posts the day it is written — Dr Debtors /
		# Cr Bank — so the LEDGER is already down by it. What items 5 and 7 are
		# about is the gap between the real bank and the ledger, and that gap is
		# the deposit itself. Netting the refund off subtracts it twice.
		#
		# Item 7 the same way, and it is provable: before the entry the ledger's
		# bank is −100. Post the entry at 1,000 and the bank reads 900, which is
		# what is there. Post it at 900 and it reads 800.
		orders = [_order(received="2026-12-28", paid=1000.0)]
		refunds = {"GB-1": [_refund("2026-12-30", amount=100.0)]}
		five = _rows_for(cb._build_item5(orders, refunds, "2026-12-31"), 5)[0]
		self.assertAlmostEqual(five["closing"], 1000.0)
		seven = next(r for r in _rows_for(cb._build_item7(orders, refunds,
			"2026-12-31", debit_account="A", credit_account="B"), 7)
			if r["reference"].startswith("Dr "))
		self.assertAlmostEqual(seven["amount"], 1000.0)

	def test_item_five_ties_to_item_threes_banked_column(self):
		# The subtotal label says it does. With a refund in the period the two
		# were silently different, and the label is what a reader trusts.
		orders = [_order(received="2026-12-28", paid=1000.0)]
		refunds = {"GB-1": [_refund("2026-12-30", amount=100.0)]}
		item3 = _subtotal(cb._build_item3(orders, refunds, "2026-12-01",
			"2026-12-31"), 3)
		item5 = next(r for r in cb._build_item5(orders, refunds, "2026-12-31")
			if r.get("row_kind") == "subtotal")
		self.assertAlmostEqual(item5["closing"], item3["payment_amount"])

	def test_the_refunds_get_their_own_line_in_item_seven(self):
		# Not netted into either figure above: a refund is Dr Debtors, so it is
		# in neither the accrual income nor item 4, and no other line on the
		# schedule takes it off. Left invisible, the year it belongs to is wrong
		# in both directions — this year over, next year under.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		less = next(r for r in rows if r["label"].startswith("Less:"))
		self.assertAlmostEqual(less["amount"], 100.0)

	def test_the_refund_line_names_gunbroker_as_the_thing_that_recomputes(self):
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		less = next(r for r in rows if r["label"].startswith("Less:"))
		self.assertIn("GunBroker", less["note"])

	def test_the_adjustment_line_says_the_refunds_are_still_to_come_off(self):
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertIn("refund", (adj["label"] + adj["note"]).lower())

	def test_a_refund_gunbroker_has_not_recomputed_comes_off_in_full(self):
		# If GunBroker has not recomputed the order, the tax it bills
		# us has not moved by a cent — we still owe all of it — so every dollar
		# the buyer got back was a price concession. There is no tax share to
		# carve out, and carving one out under-deducts.
		#
		# 1,000 order / 55 collected → 945 revenue, deposit 1,000, refund 100.
		# The deposit still matches the order total (Difference 0), which is how
		# the page shows that nothing was recomputed.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=1000.0, tax=55.0,
				paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 845.00)   # 945.00 − 100.00

	def test_a_refund_gunbroker_has_recomputed_comes_off_nothing(self):
		# The other branch, and the reason a ratio is wrong in both. Once the
		# poll copies the recomputed total back, the revenue column is ALREADY
		# net of the refund. Deducting again double-counts it.
		#
		# The recomputation shows up as a deposit larger than the order now is:
		# banked 1,000 against a 900 total, Difference +100.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=900.0, tax=49.5,
				paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 850.50)   # 900 − 49.5, nothing off

	def test_returning_an_overpayment_is_not_a_deduction_from_income(self):
		# Banked 1,050 against a 1,000 order and gave the extra 50 back. Nothing
		# about the sale changed, so gross receipts must not move — a ratio
		# would have taken 47.25 off it.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=1000.0, tax=55.0,
				paid=1050.0)],
			{"GB-1": [_refund("2026-12-30", amount=50.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 945.00)

	def test_a_short_paid_order_does_not_deduct_more_than_was_refunded(self):
		# The term that reads "how much the deposit exceeds the order" goes
		# NEGATIVE on a short payment, and subtracting a negative adds. Banked
		# 900 against a 1,000 order, refunded 100: the deduction is the 100 that
		# went back, not 200. Without the floor the year is under-reported by
		# the shortfall, on top of everything else a short payment already
		# leaves open.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=1000.0, tax=55.0,
				paid=900.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		# 945 × (900/1,000) = 850.50 of revenue collected, less the 100 refunded.
		self.assertAlmostEqual(adj["amount"], 750.50)

	def test_a_part_paid_order_reports_only_the_revenue_it_collected(self):
		# Gross receipts on the cash basis cannot exceed the
		# cash received — an order banked 900 against a 1,000 total was adding
		# 945 of income against a 900 deposit. The revenue is taken in the same
		# proportion as the payment, which is the split this page already uses
		# everywhere else.
		orders = [_order(received="2026-12-28", order_total=1000.0, tax=55.0,
			paid=900.0)]
		rows = _rows_for(cb._build_item7(orders, {}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 850.50)   # 945 × 0.9

	def test_the_adjustment_never_exceeds_the_cash_the_entry_is_for(self):
		# The relation that makes it checkable rather than a number to trust.
		# It was false on exactly the short-paid case above, and true elsewhere
		# only by coincidence of the orders being paid in full.
		for kw in (dict(order_total=1000.0, tax=55.0, paid=900.0),
				dict(order_total=1000.0, tax=55.0, paid=1000.0),
				dict(order_total=1000.0, tax=55.0, paid=1050.0),
				dict(order_total=1000.0, tax=0.0, paid=400.0)):
			rows = _rows_for(cb._build_item7(
				[_order(received="2026-12-28", **kw)], {}, "2026-12-31",
				debit_account="A", credit_account="B"), 7)
			entry = next(r for r in rows if r["reference"].startswith("Dr "))
			adj = next(r for r in rows if "gross receipts" in r["reference"])
			self.assertLessEqual(adj["amount"], entry["amount"], msg=str(kw))

	def test_an_overpayment_does_not_scale_the_revenue_up(self):
		# The ratio is capped at 1: banking more than the order is worth does
		# not create income, it creates a liability to give it back.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=1000.0, tax=55.0,
				paid=1050.0)], {}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 945.00)

	def test_the_refund_line_shows_what_came_off_the_revenue(self):
		# The gross refund stays visible — it is what left the bank — with the
		# part that came off revenue named, so the two lines can be checked
		# against each other instead of taken on trust.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=1000.0, tax=55.0,
				paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		less = next(r for r in rows if r["label"].startswith("Less:"))
		self.assertAlmostEqual(less["amount"], 100.0)     # gross, off the bank
		self.assertIn("100.00", less["note"])             # and off the revenue

	def test_the_refund_line_points_at_the_difference_column(self):
		# What decides how much comes off is not a tax ratio but whether the
		# deposit still matches the order — which the schedule already shows,
		# in item 3's Difference column. Say so, so the two agree on the page.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=900.0, tax=49.5,
				paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		less = next(r for r in rows if r["label"].startswith("Less:"))
		self.assertIn("Difference", less["note"])

	def test_no_line_claims_the_gap_between_them_is_the_collected_tax(self):
		# It was that, before refunds existed on this page. With one, the gap is
		# the collected tax PLUS the refund, and the sentence quietly stopped
		# being true.
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertNotIn("Not the entry amount", adj["note"])

	def test_an_order_with_no_total_deducts_nothing_rather_than_dividing(self):
		rows = _rows_for(cb._build_item7(
			[_order(received="2026-12-28", order_total=0.0, tax=0.0, paid=100.0)],
			{"GB-1": [_refund("2026-12-30", amount=50.0)]}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
		adj = next(r for r in rows if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 0.0)

	def test_with_no_refunds_the_line_is_zero_and_nothing_else_moves(self):
		rows = _rows_for(cb._build_item7([_order(paid=1050.0, order_total=1000.0,
			tax=55.0)], {}, "2026-12-31", debit_account="A", credit_account="B"), 7)
		by_kind = {r["label"]: r["amount"] for r in rows}
		less = next(v for k, v in by_kind.items() if k.startswith("Less:"))
		self.assertAlmostEqual(less, 0.0)
		self.assertIn(1050.0, list(by_kind.values()))   # the entry, gross
		self.assertIn(945.0, list(by_kind.values()))    # the adjustment


class ARefundedOrderStaysVisible(FrappeTestCase):
	"""An order that drops out because it was refunded looked exactly
	like an order that was never there: no row at all. The accountant cannot
	tell "refunded" from "missed", and the schedule is the only place either
	would show."""

	def test_a_part_refund_names_the_entry_and_the_date(self):
		rows = cb._build_item3([_order(received="2026-12-28", paid=1000.0)],
			{"GB-1": [_refund("2026-12-30", amount=100.0, pe="PE-R9")]},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		self.assertIn("2026-12-30", row["note"])
		self.assertIn("PE-R9", row["note"])

	def test_an_order_refunded_in_the_period_still_gets_a_row(self):
		# In the opening set, out of the closing set — the roll-forward has to
		# show where it went.
		rows = cb._build_item3([_order(received="2026-11-20", paid=1000.0)],
			{"GB-1": [_refund("2026-12-15", amount=1000.0, pe="PE-R9")]},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["opening"], 1000.0)
		self.assertAlmostEqual(row["closing"], 0.0)
		self.assertIn("PE-R9", row["note"])

	def test_a_row_that_shipped_says_so_even_though_it_was_part_refunded(self):
		# reviewer minor-3. The suppression asked "was there a refund", not "is
		# a refund why this left". Paid in November, part refunded on the 20th,
		# shipped and posted in December: the row reads opening 900 / closing 0
		# with only "Refunded" beside it, and note is the ONLY column that says
		# where the money went. It says the wrong thing.
		rows = cb._build_item3([_order(received="2026-11-10", paid=1000.0,
				pe="PE-1", pe_date="2026-12-10")],
			{"GB-1": [_refund("2026-11-20", amount=100.0, pe="PE-R")]},
			period_start="2026-12-01", period_end="2026-12-31")
		note = _rows_for(rows, 3)[0]["note"]
		self.assertIn("Settled during the period", note)
		self.assertIn("Refunded", note)

	def test_a_refund_is_not_reported_as_a_disposal(self):
		# "Settled during the period" means the gun shipped and the receipt
		# posted. A refund is the opposite event and must not borrow its words.
		rows = cb._build_item3([_order(received="2026-11-20", paid=1000.0)],
			{"GB-1": [_refund("2026-12-15", amount=1000.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		self.assertNotIn("Settled during the period", _rows_for(rows, 3)[0]["note"])


# --------------------------------------------------------------- the amounts

class EachOrderSplitsFourWays(FrappeTestCase):
	"""Revenue, money collected for somebody else, what the bank
	actually took in, and the gap between the last two."""

	def test_the_revenue_part_excludes_the_marketplace_tax(self):
		# 1000 total, 55 of it collected tax (marketplace service fee included).
		split = cb._split(_order(order_total=1000.0, tax=55.0))
		self.assertAlmostEqual(split["revenue_portion"], 945.0)
		self.assertAlmostEqual(split["collected_portion"], 55.0)

	def test_a_short_payment_shows_a_negative_difference(self):
		split = cb._split(_order(order_total=1000.0, tax=55.0, paid=950.0))
		self.assertAlmostEqual(split["payment_amount"], 950.0)
		self.assertAlmostEqual(split["difference"], -50.0)

	def test_an_overpayment_shows_a_positive_difference(self):
		split = cb._split(_order(order_total=1000.0, tax=55.0, paid=1050.0))
		self.assertAlmostEqual(split["difference"], 50.0)

	def test_revenue_plus_collected_is_not_the_banked_amount_when_they_differ(self):
		# The reason the two extra columns exist: item 5 and item 7 want what
		# the bank took in, and "revenue + collected" is the invoice, not the
		# cheque. They coincide only on an exactly-paid order.
		split = cb._split(_order(order_total=1000.0, tax=55.0, paid=1050.0))
		self.assertNotAlmostEqual(
			split["revenue_portion"] + split["collected_portion"],
			split["payment_amount"])


# ----------------------------------------------------------- item 3 assembly

class ItemThreeGivesOpeningAndClosing(FrappeTestCase):
	"""Roll-forward straight off the page, without having to believe that
	last year's reversing entry did its job."""

	def _rows(self, orders, refunds=None):
		return cb._build_item3(orders, refunds or {},
			period_start="2027-01-01", period_end="2027-01-31")

	def test_an_order_carried_in_from_last_period_fills_the_opening_column(self):
		# Paid 12/28, still undisposed at 1/31 → in both sets.
		rows = self._rows([_order(received="2026-12-28")])
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["opening"], 1000.0)
		self.assertAlmostEqual(row["closing"], 1000.0)

	def test_an_order_settled_during_the_period_leaves_only_an_opening(self):
		rows = self._rows([_order(received="2026-12-28", pe="PE-1",
			pe_date="2027-01-10")])
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["opening"], 1000.0)
		self.assertAlmostEqual(row["closing"], 0.0)

	def test_an_order_paid_during_the_period_has_no_opening(self):
		rows = self._rows([_order(received="2027-01-20")])
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["opening"], 0.0)
		self.assertAlmostEqual(row["closing"], 1000.0)

	def test_an_order_in_neither_set_is_not_a_row_at_all(self):
		rows = self._rows([_order(received="2026-11-01", pe="PE-1",
			pe_date="2026-11-02")])
		self.assertEqual(_rows_for(rows, 3), [])

	def test_the_closing_subtotal_sums_only_the_closing_set(self):
		rows = self._rows([
			_order("GB-1", received="2026-12-28"),                       # both
			_order("GB-2", gb_id="1002", received="2026-12-29",
				pe="PE-1", pe_date="2027-01-10"),                        # opening only
			_order("GB-3", gb_id="1003", received="2027-01-20")])        # closing only
		sub = _subtotal(rows, 3)
		self.assertAlmostEqual(sub["closing"], 2000.0)
		self.assertAlmostEqual(sub["opening"], 2000.0)
		self.assertAlmostEqual(sub["revenue_portion"], 1890.0)   # 2 × 945
		self.assertAlmostEqual(sub["collected_portion"], 110.0)  # 2 × 55

	def test_the_row_carries_the_cheque_trail(self):
		row = _rows_for(self._rows([_order(received="2027-01-20")]), 3)[0]
		self.assertEqual(row["payment_date"], "2027-01-20")
		self.assertEqual(row["payment_mode"], "GunBroker Check")
		self.assertEqual(row["check_no"], "9001")


# -------------------------------------------------------------- items 4 to 7

def _si(name="SI-1", date="2026-12-30", order="GB-1"):
	return {"name": name, "posting_date": date, "gunbroker_order": order}


class InvoicedButNotYetCollected(FrappeTestCase):
	"""Item 4, also as-of: the balance ON the period end, not the invoice's
	current `outstanding_amount`.

	Rebuilt from the receivable ledger rather than from Payment Entry
	allocations. Payment Entries are not the only thing that clears a
	receivable: this repo's own bad-debt procedure clears one with a Journal
	Entry, and a credit note clears one with an invoice. Counting only
	Payment Entries leaves a written-off debt on this list for ever — and item
	4 is what the accountant SUBTRACTS from accrual income, so an inflated one
	understates cash-basis receipts."""

	def test_an_invoice_paid_after_the_period_still_shows_its_full_balance(self):
		rows = cb._build_item4([_si()], {"SI-1": 500.0}, period_end="2026-12-31")
		self.assertAlmostEqual(_rows_for(rows, 4)[0]["closing"], 500.0)

	def test_an_invoice_settled_by_the_period_end_is_not_listed(self):
		rows = cb._build_item4([_si()], {"SI-1": 0.0}, period_end="2026-12-31")
		self.assertEqual(_rows_for(rows, 4), [])

	def test_a_part_payment_leaves_the_remainder(self):
		rows = cb._build_item4([_si()], {"SI-1": 300.0}, period_end="2026-12-31")
		self.assertAlmostEqual(_rows_for(rows, 4)[0]["closing"], 300.0)

	def test_an_invoice_dated_after_the_period_is_not_listed(self):
		rows = cb._build_item4([_si(date="2027-01-02")], {"SI-1": 500.0},
			period_end="2026-12-31")
		self.assertEqual(_rows_for(rows, 4), [])

	def test_an_invoice_with_no_ledger_row_shows_no_balance(self):
		rows = cb._build_item4([_si()], {}, period_end="2026-12-31")
		self.assertEqual(_rows_for(rows, 4), [])


class TheReceivableIsRebuiltFromThePaymentLedger(FrappeTestCase):
	"""The balance comes from Payment Ledger Entry, not GL Entry.

	The two agree everywhere except the one path a partial refund sends
	people down (cancel and amend the invoice, then re-allocate the
	receipt). Reconciling a payment against an amended invoice
	(`reconcile_against_document`, erpnext/accounts/utils.py) DELETES that
	payment's payment-ledger rows and rebuilds them from the re-saved document,
	which now carries the invoice reference — and it does NOT rebuild
	`tabGL Entry`. The GL row keeps whatever `against_voucher` it was posted
	with, which for a receipt unlinked when its invoice was cancelled is
	nothing at all.

	So after the three-step price adjustment the invoice is paid, the payment
	ledger says so, and the general ledger has no row tying the two together.
	Read from GL, item 4 reports the whole invoice as still owed — and item 4
	is what gets subtracted from accrual income, so cash-basis receipts come
	out LOW by the price of the gun.

	Nor is the payment ledger a narrower source: `create_payment_ledger_entry`
	takes `against_voucher_no` as `gle.against_voucher or gle.voucher_no` and
	runs from `make_gl_entries` for every voucher with party rows, so a
	bad-debt journal carrying Reference Type / Name (case 5 requires it) and a
	credit note both land there too."""

	def _query(self):
		with patch("frappe.db.sql", return_value=[]) as sql:
			cb._fetch_receivable_balances("TSA", "2026-12-31", ["SI-1", "SI-2"])
		return " ".join(sql.call_args[0][0].split())

	def test_it_asks_only_about_the_invoices_on_this_schedule(self):
		# reviewer minor-2. Without this the year-end run groups every invoice
		# the company has ever had a receivable line against into a dict, to
		# read a handful of GunBroker ones out of it — growing every year, with
		# no lower bound on the date either.
		self.assertIn("against_voucher_no IN %(invoices)s", self._query())

	def test_execute_asks_about_exactly_the_invoices_it_found(self):
		# The wiring, not the query. Every test above calls the fetch directly,
		# so passing it a wildcard — or nothing — from execute() leaves them all
		# green while the schedule reads balances for invoices it never lists.
		# Fourth time this shape has slipped through on this branch.
		module = ("ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments"
			".gunbroker_cash_basis_adjustments")
		found = [{"name": "SI-1", "posting_date": "2026-12-02",
			"gunbroker_order": "GB-1"}]
		with patch(f"{module}._fetch_orders", return_value=[]), \
				patch(f"{module}._fetch_invoices", return_value=found) as invoices, \
				patch(f"{module}._fetch_accrual_rollforward", return_value={
					"account": "A", "opening": 0.0, "credits": 0.0,
					"debits": 0.0, "paid": 0.0}), \
				patch(f"{module}._fetch_liability_balances", return_value=[]), \
				patch(f"{module}._fetch_refund_entries", return_value=[]), \
				patch(f"{module}._fetch_settlements", return_value=[]), \
				patch(f"{module}._deposit_account", return_value="Bank - TSA"), \
				patch(f"{module}._credit_account", return_value="Deposits - TSA"), \
				patch(f"{module}._fetch_receivable_balances",
					return_value={}) as receivable:
			cb.execute({"company": "TSA", "period_start": "2026-12-01",
				"period_end": "2026-12-31"})
		self.assertEqual(receivable.call_args[0][2], ["SI-1"])
		# And found once, not once per consumer (reviewer minor-2's third point:
		# the same query run twice for the same answer).
		self.assertEqual(invoices.call_count, 1)

	def test_with_no_invoices_it_does_not_ask_at_all(self):
		with patch("frappe.db.sql") as sql:
			self.assertEqual(
				cb._fetch_receivable_balances("TSA", "2026-12-31", []), {})
		sql.assert_not_called()

	def test_it_reads_the_payment_ledger_and_not_the_general_ledger(self):
		query = self._query()
		self.assertIn("`tabPayment Ledger Entry`", query)
		self.assertNotIn("`tabGL Entry`", query)

	def test_it_sums_the_ledger_against_the_invoice(self):
		query = self._query()
		self.assertIn("against_voucher_type = 'Sales Invoice'", query)
		self.assertIn("SUM(ple.amount)", query)

	def test_it_is_as_of_the_period_end(self):
		self.assertIn("posting_date <= %(period_end)s", self._query())

	def test_it_skips_delinked_rows(self):
		# The payment ledger keeps a cancelled voucher's rows and marks them
		# rather than deleting them; counting those would put a cancelled
		# receipt back into the balance.
		self.assertIn("delinked = 0", self._query())

	def test_it_is_scoped_to_the_company(self):
		self.assertIn("company = %(company)s", self._query())

	def test_it_only_counts_receivable_rows(self):
		# The payment ledger holds Payable rows too, and
		# it labels them itself — no join needed for this one.
		self.assertIn("account_type = 'Receivable'", self._query())


class WhatItemFourShowsAfterAPriceAdjustment(FrappeTestCase):
	"""The same as outcomes rather than as a query.

	Run against `_build_item4` with the balances the payment ledger produces at
	each step of a partial refund (amend the invoice, re-allocate the
	receipt, write the refund), so they keep saying what the page must
	show even if the way the balance is fetched changes again."""

	SI = "ACC-SINV-2026-99001-1"

	def _rows(self, balance):
		return _rows_for(cb._build_item4([_si(name=self.SI)],
			{self.SI: balance}, period_end="2026-12-31"), 4)

	def test_a_fully_reconciled_amendment_is_not_listed(self):
		# All three steps done: invoice cancelled and amended, the surviving
		# receipt reconciled against the amendment. Nothing is owed, and this
		# is the case that was reporting the whole invoice.
		self.assertEqual(self._rows(0.0), [])

	def test_a_part_reconciled_amendment_shows_only_the_remainder(self):
		self.assertAlmostEqual(self._rows(145.99)[0]["closing"], 145.99)

	def test_the_unallocated_remainder_is_not_a_receivable(self):
		# Partial refund. The receipt keeps 100.00 unallocated until the refund
		# cheque is written, and the refund entry carries +100 of its own. Both
		# rows sit against a Payment Entry, not a Sales Invoice, so neither is
		# selected at all — they cancel each other on the customer's account and
		# belong to the refund side of the schedule, matched by remarks.
		self.assertEqual(self._rows(0.0), [])

	def test_the_draft_before_reconciling_shows_the_amended_total(self):
		# Until the receipt is reconciled the invoice IS owed,
		# and the schedule says so. That is a book not finished, not a wrong
		# report — the si_outstanding alert is ringing on the same order.
		self.assertAlmostEqual(self._rows(1145.99)[0]["closing"], 1145.99)

	def test_reconciling_after_the_period_end_changes_the_answer(self):
		# The payment ledger row keeps the RECEIPT's posting date,
		# not the day somebody allocated it, so re-running an already-finished
		# period is stable (E1) — but a period whose books were finished later
		# legitimately reads differently before and after. Using the allocation
		# date instead would push August money into September, and across a
		# year end into the wrong year.
		before = self._rows(1145.99)          # run on the 4th, step 3 not done
		after = self._rows(0.0)               # re-run on the 6th, step 3 done
		self.assertAlmostEqual(before[0]["closing"], 1145.99)
		self.assertEqual(after, [])

	def test_the_subtotal_says_which_ledger_it_came_from(self):
		# The figure is not the invoice's outstanding_amount and not the general
		# ledger, and those are the two places a reader would go to check it.
		# Naming the source — and naming the report that reads the same rows —
		# is what turns "that number looks wrong" into a comparison somebody
		# can actually make.
		sub = next(r for r in cb._build_item4([_si(name=self.SI)],
			{self.SI: 145.99}, period_end="2026-12-31")
			if r.get("row_kind") == "subtotal")
		self.assertIn("Payment Ledger", sub["note"])
		self.assertIn("Accounts Receivable", sub["note"])

	def test_a_bad_debt_journal_dated_after_the_period_still_shows_the_debt(self):
		# Written off in January; at 31 December it was owed.
		self.assertAlmostEqual(self._rows(1245.99)[0]["closing"], 1245.99)


class WhatSitsInTheDepositAccount(FrappeTestCase):
	"""Item 5. The in-transit pile is item 3's cheques at the amount the bank
	actually took in — not the invoice total."""

	def test_it_is_the_banked_amount_not_the_order_total(self):
		rows = cb._build_item5([_order(order_total=1000.0, paid=1050.0)], {}, "2026-12-31")
		self.assertAlmostEqual(_rows_for(rows, 5)[0]["closing"], 1050.0)

	def test_the_row_names_the_account_the_money_is_sitting_in(self):
		# Condition for keeping the per-method split: the page shows
		# "the GunBroker items in transit in check_deposit_account",
		# so the page has to say which account that is. Reconciling a bank
		# statement against a figure whose account is not named means opening
		# GunBroker Settings to find out which of three bank-ish accounts it is.
		rows = _rows_for(cb._build_item5([_order(paid=1000.0)], {}, "2026-12-31",
			deposit_account="Operating Bank - TSA"), 5)
		self.assertIn("Operating Bank - TSA", rows[0]["note"])

	def test_an_unset_deposit_account_says_so_instead_of_going_blank(self):
		# Before go-live there is no such account — Settings will not save with
		# one missing, but this schedule can be run before that ever happens.
		rows = _rows_for(cb._build_item5([_order(paid=1000.0)], {}, "2026-12-31",
			deposit_account=None), 5)
		self.assertIn("GunBroker Settings", rows[0]["note"])

	def test_each_payment_method_gets_its_own_line(self):
		# Phase one is cheques only; a card line appearing here later is the
		# clearing-account balance, and the two must never be added together.
		rows = _rows_for(cb._build_item5([
			_order("GB-1", mode="GunBroker Check", paid=1000.0),
			_order("GB-2", mode="GunBroker Card", paid=500.0)],
			{}, "2026-12-31"), 5)
		by_mode = {r["reference"]: r["closing"] for r in rows}
		self.assertAlmostEqual(by_mode["GunBroker Check"], 1000.0)
		self.assertAlmostEqual(by_mode["GunBroker Card"], 500.0)


class CashActuallyPaidToTheMarketplace(FrappeTestCase):
	"""Item 6: two lines, because they arrive by two different routes — the tax
	statement is remitted, the fee bill is auto-debited."""

	def test_the_bank_credit_is_attributed_to_the_liability_it_settled(self):
		rows = cb._build_item6([
			{"voucher": "JE-1", "posting_date": "2027-01-20",
			 "tax_debit": 300.0, "fee_debit": 0.0, "bank_credit": 300.0},
			{"voucher": "JE-2", "posting_date": "2027-01-20",
			 "tax_debit": 0.0, "fee_debit": 57.0, "bank_credit": 57.0}])
		by_ref = {r["reference"]: r["amount"] for r in _rows_for(rows, 6)}
		self.assertAlmostEqual(by_ref["Sales tax statement remitted"], 300.0)
		self.assertAlmostEqual(by_ref["Fee statement debited"], 57.0)

	def test_one_entry_settling_both_is_split_by_its_debits(self):
		rows = cb._build_item6([
			{"voucher": "JE-1", "posting_date": "2027-01-20",
			 "tax_debit": 300.0, "fee_debit": 100.0, "bank_credit": 400.0}])
		by_ref = {r["reference"]: r["amount"] for r in _rows_for(rows, 6)}
		self.assertAlmostEqual(by_ref["Sales tax statement remitted"], 300.0)
		self.assertAlmostEqual(by_ref["Fee statement debited"], 100.0)

	def test_the_split_never_reports_more_cash_than_the_liability_settled(self):
		# One cheque paying the fee bill AND the office rent —
		# Dr Accrued 57 / Dr Office 100 / Cr Bank 157 — reported 157 of
		# marketplace fees paid, while item 1 (which caps) reported 57. The
		# "two deductions on one page" that capping item 1 removed, reappearing
		# on the other side of it.
		rows = _rows_for(cb._build_item6([
			{"voucher": "JE-1", "posting_date": "2027-01-20",
			 "tax_debit": 0.0, "fee_debit": 57.0, "bank_credit": 157.0}]), 6)
		by_ref = {r["reference"]: r["amount"] for r in rows}
		self.assertAlmostEqual(by_ref["Fee statement debited"], 57.0)

	def test_the_row_no_longer_calls_the_capped_share_the_bank_credit(self):
		# reviewer r4 minor-3. Once the share is capped it is no longer "the
		# bank credit on the entry" — on the rent-and-fees cheque the entry's
		# bank credit is 157 and the row says 57. The label described the
		# figure accurately until the cap made it stop.
		rows = _rows_for(cb._build_item6([
			{"voucher": "JE-1", "posting_date": "2027-01-20",
			 "tax_debit": 0.0, "fee_debit": 57.0, "bank_credit": 157.0}]), 6)
		self.assertNotIn("Bank credit on the settlement entry",
			[r["label"] for r in rows])

	def test_the_two_lines_add_back_to_the_bank_credit(self):
		rows = _rows_for(cb._build_item6([
			{"voucher": "JE-1", "posting_date": "2027-01-20",
			 "tax_debit": 300.0, "fee_debit": 100.0, "bank_credit": 400.0}]), 6)
		self.assertAlmostEqual(sum(r["amount"] for r in rows), 400.0)


class TheYearEndEntry(FrappeTestCase):
	"""Item 7. Two numbers that are NOT the same number, printed
	separately so nobody has to work one out from the other."""

	def test_the_entry_is_for_what_the_bank_holds(self):
		rows = cb._build_item7([_order(order_total=1000.0, tax=55.0, paid=1050.0)],
			{}, "2026-12-31", debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA")
		je = next(r for r in _rows_for(rows, 7) if r["reference"].startswith("Dr "))
		self.assertAlmostEqual(je["amount"], 1050.0)

	def test_the_cash_basis_adjustment_is_only_the_revenue_part(self):
		rows = cb._build_item7([_order(order_total=1000.0, tax=55.0, paid=1050.0)],
			{}, "2026-12-31", debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA")
		adj = next(r for r in _rows_for(rows, 7)
			if "gross receipts" in r["reference"])
		self.assertAlmostEqual(adj["amount"], 945.0)

	def test_the_two_figures_are_printed_as_separate_lines(self):
		amounts = [r["amount"] for r in _rows_for(cb._build_item7(
			[_order(order_total=1000.0, tax=55.0, paid=1050.0)], {}, "2026-12-31",
			debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA"), 7)]
		self.assertIn(1050.0, amounts)
		self.assertIn(945.0, amounts)

	def test_the_entry_names_both_sides(self):
		rows = _rows_for(cb._build_item7([_order()], {}, "2026-12-31",
			debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA"), 7)
		text = " ".join(r["reference"] for r in rows)
		self.assertIn("Operating Bank - TSA", text)
		self.assertIn("Customer Deposits - TSA", text)


class TheAccrualAccountRollsForward(FrappeTestCase):
	"""Item 1's four columns, and the one that decides a tax deduction.

	"Paid this period" is the only column the cash basis deducts, so it has to
	mean money that left the bank. Not every debit to the accrual account is
	that: cancelling an invoice posts an automatic reversing journal, which
	debits the same account and moves nothing. That is the ORDINARY path —
	every price adjustment amends, and every amendment cancels first — so
	reading raw debits overstates the deduction on a routine event, and the
	same page's item 6 (which does insist on bank money) would contradict it.
	"""

	def test_the_four_columns_tie(self):
		rows = cb._build_item1("Accrued GunBroker Fees - TSA",
			opening=100.0, credits=57.0, debits=120.0, paid=120.0)
		row = _rows_for(rows, 1)[0]
		self.assertAlmostEqual(row["opening"], 100.0)
		self.assertAlmostEqual(row["accrued"], 57.0)
		self.assertAlmostEqual(row["paid"], 120.0)
		self.assertAlmostEqual(row["closing"], 37.0)

	def test_the_closing_is_derived_not_asserted(self):
		row = _rows_for(cb._build_item1("A", opening=0.0, credits=57.0,
			debits=0.0, paid=0.0), 1)[0]
		self.assertAlmostEqual(row["closing"], 57.0)

	def test_a_reversal_is_not_a_payment(self):
		# Repro: sell (accrue 57) → discount, which cancels and
		# re-accrues (reverse 57, accrue 57 again). Nothing was paid.
		row = _rows_for(cb._build_item1("A", opening=0.0, credits=114.0,
			debits=57.0, paid=0.0), 1)[0]
		self.assertAlmostEqual(row["paid"], 0.0)
		self.assertAlmostEqual(row["accrued"], 57.0)   # net of the reversal
		self.assertAlmostEqual(row["closing"], 57.0)

	def test_a_settlement_is_a_payment(self):
		row = _rows_for(cb._build_item1("A", opening=57.0, credits=0.0,
			debits=57.0, paid=57.0), 1)[0]
		self.assertAlmostEqual(row["paid"], 57.0)
		self.assertAlmostEqual(row["accrued"], 0.0)
		self.assertAlmostEqual(row["closing"], 0.0)

	def test_a_whole_order_cancelled_and_never_rebilled_deducts_nothing(self):
		# accrue 57, cancel, reverse 57. GunBroker credits the fee on next
		# month's bill; deducting it as cash paid would be a double benefit.
		row = _rows_for(cb._build_item1("A", opening=0.0, credits=57.0,
			debits=57.0, paid=0.0), 1)[0]
		self.assertAlmostEqual(row["paid"], 0.0)
		self.assertAlmostEqual(row["accrued"], 0.0)
		self.assertAlmostEqual(row["closing"], 0.0)

	def test_the_note_does_not_call_a_settlement_shortfall_a_reversal(self):
		# reviewer r3 minor-2. "debits − paid" is two different things: journals
		# that reversed an accrual, and the non-cash part of a settlement where
		# GunBroker credited a fee. Calling the whole of it "reversed on
		# cancelled invoices" tells a reader to go looking for cancellations
		# that do not exist.
		row = _rows_for(cb._build_item1("A", opening=57.0, credits=0.0,
			debits=57.0, paid=50.0), 1)[0]
		# Both causes named. Asserting only the ABSENCE of one phrase is not
		# enough — "reversals on cancelled invoices" passes a check for
		# "reversed on cancelled invoices" while saying the same wrong thing.
		self.assertIn("credited", row["note"])
		self.assertIn("7.00", row["note"])

	def test_the_reversal_is_shown_not_silently_netted(self):
		# Netting without saying so leaves an accountant unable to see why the
		# accrued figure is not the sum of the month's invoices.
		row = _rows_for(cb._build_item1("A", opening=0.0, credits=114.0,
			debits=57.0, paid=0.0), 1)[0]
		self.assertIn("57", row["note"])
		self.assertIn("revers", row["note"].lower())

	def test_a_mixed_settlement_deducts_only_the_bank_half(self):
		# The repro as arithmetic: the query hands _build_item1 a `paid` of
		# 50 out of a 57 debit, and the reversal maths must treat the other 7 as
		# what it is — not cash.
		row = _rows_for(cb._build_item1("A", opening=57.0, credits=0.0,
			debits=57.0, paid=50.0), 1)[0]
		self.assertAlmostEqual(row["paid"], 50.0)
		# The other 7 is GunBroker crediting a fee: the liability goes, no cash
		# moves. It nets off Accrued exactly as a reversal does, and the
		# account closes at nil — 57 + (0 − 7) − 50.
		self.assertAlmostEqual(row["accrued"], -7.0)
		self.assertAlmostEqual(row["closing"], 0.0)

	def test_the_reversal_note_prints_money_not_a_float(self):
		# "57.0" in a figure an accountant is reading.
		row = _rows_for(cb._build_item1("A", opening=0.0, credits=114.0,
			debits=57.0, paid=0.0), 1)[0]
		self.assertIn("57.00", row["note"])

	def test_a_month_with_both_a_reversal_and_a_settlement(self):
		# credits 114, debits 114 (57 reversal + 57 settlement), paid 57.
		row = _rows_for(cb._build_item1("A", opening=57.0, credits=114.0,
			debits=114.0, paid=57.0), 1)[0]
		self.assertAlmostEqual(row["accrued"], 57.0)   # 114 − (114 − 57)
		self.assertAlmostEqual(row["paid"], 57.0)
		self.assertAlmostEqual(row["closing"], 57.0)   # 57 + 57 − 57


class OnlyBankBackedDebitsCountAsPaid(FrappeTestCase):
	"""The query half. The question is "did the same voucher also take
	money out of a Bank or Cash account", asked of the ledger rather than of
	the journal tables — so a fee bill settled by Payment Entry counts too, and
	a reversing journal never can."""

	def _query(self):
		module = ("ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments"
			".gunbroker_cash_basis_adjustments")
		with patch(f"{module}.account_for",
					return_value="Accrued GunBroker Fees - TSA"), \
				patch("frappe.db.sql", return_value=[{"credits": 0.0,
					"debits": 0.0, "paid": 0.0, "bal": 0.0}]) as sql:
			cb._fetch_accrual_rollforward("TSA", "2026-09-01", "2026-09-30")
		return " ".join(sql.call_args_list[0][0][0].split())

	def test_paid_is_measured_against_the_bank_money_on_the_voucher(self):
		# A voucher-level yes/no counted the WHOLE accrual debit as
		# paid the moment any bank money appeared on it. GunBroker's bill can
		# carry a credit — Dr Accrued 57 / Cr Bank 50 / Cr Marketplace Fees 7 —
		# and only 50 of that left the bank. Item 6 already prorates; item 1 has
		# to agree with it or the same page states two deductions.
		query = self._query()
		self.assertIn("'Bank', 'Cash'", query)
		self.assertIn("bank_credit", query)
		self.assertIn("gb_debit", query)
		self.assertNotIn("EXISTS", query)   # the voucher-level boolean is gone

	def test_the_share_can_never_exceed_the_debit_itself(self):
		# A voucher whose bank credit is larger than its GunBroker debits (one
		# cheque settling the fee bill AND something unrelated) must not report
		# more paid than was accrued.
		self.assertIn("LEAST(", self._query())

	def test_a_voucher_with_no_gunbroker_debit_contributes_nothing(self):
		# NULLIF guards the divide; a null share sums as zero rather than
		# raising, which is what a reversal-only month must produce.
		self.assertIn("NULLIF(", self._query())

	def test_the_three_figures_come_back_under_their_own_names(self):
		# The wiring that matters. Every arithmetic test hands
		# _build_item1 its numbers directly, so mapping the query's `debits`
		# column onto the returned `paid` key restores the entire bug with
		# every one of them still green.
		module = ("ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments"
			".gunbroker_cash_basis_adjustments")
		with patch(f"{module}.account_for", return_value="Accrued - TSA"), \
				patch("frappe.db.sql", return_value=[{"credits": 114.0,
					"debits": 57.0, "paid": 0.0, "bal": 12.0}]):
			out = cb._fetch_accrual_rollforward("TSA", "2026-09-01", "2026-09-30")
		self.assertAlmostEqual(out["credits"], 114.0)
		self.assertAlmostEqual(out["debits"], 57.0)
		self.assertAlmostEqual(out["paid"], 0.0)

	def test_a_period_with_no_accrual_account_reports_zeroes_not_a_crash(self):
		module = ("ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments"
			".gunbroker_cash_basis_adjustments")
		with patch(f"{module}.account_for", return_value=None):
			out = cb._fetch_accrual_rollforward("TSA", "2026-09-01", "2026-09-30")
		self.assertEqual(
			{out["credits"], out["debits"], out["paid"]}, {0.0})

	def test_the_bank_side_is_matched_on_the_same_voucher(self):
		self.assertIn("v.voucher_no = gl.voucher_no", self._query())

	def test_credits_and_debits_are_both_reported_raw(self):
		# The netting happens in _build_item1, where it is visible and tested.
		# A query that pre-nets makes the reversal vanish from the page.
		query = self._query()
		self.assertIn("SUM(gl.credit)", query)
		self.assertIn("SUM(gl.debit)", query)


class AnOpeningOnlyRowShowsNoClosingMoney(FrappeTestCase):
	"""reviewer minor-3. A row that left the closing set kept printing all four
	money columns, while the subtotal summed only the closing set. Drag the
	Revenue Portion column down in the CSV and it comes out larger than the
	subtotal, by exactly the orders that were settled this period — and there
	is nothing on the page saying so."""

	def _row(self, **kw):
		rows = cb._build_item3([_order(received="2026-11-20", pe="PE-1",
			pe_date="2026-12-10", **kw)], {},
			period_start="2026-12-01", period_end="2026-12-31")
		return _rows_for(rows, 3)[0]

	def test_the_money_columns_are_zero_once_the_row_leaves(self):
		row = self._row()
		self.assertAlmostEqual(row["opening"], 1000.0)
		self.assertAlmostEqual(row["closing"], 0.0)
		for column in ("revenue_portion", "collected_portion", "payment_amount",
				"difference"):
			self.assertAlmostEqual(row[column], 0.0, msg=column)

	def test_the_detail_column_now_sums_to_the_subtotal(self):
		rows = cb._build_item3([
			_order("GB-1", received="2026-12-28"),
			_order("GB-2", gb_id="1002", received="2026-11-20", pe="PE-1",
				pe_date="2026-12-10")], {},
			period_start="2026-12-01", period_end="2026-12-31")
		details = _rows_for(rows, 3)
		subtotal = _subtotal(rows, 3)
		self.assertAlmostEqual(sum(r["revenue_portion"] for r in details),
			subtotal["revenue_portion"])

	def test_a_refunded_opening_row_keeps_its_refunded_figure(self):
		# Clearing every money column took the Refunded figure with it,
		# leaving a note that says "Refunded 100" beside a column reading 0.00 —
		# and that figure is precisely the explanation of where the opening
		# balance went.
		rows = cb._build_item3([_order(received="2026-11-20", paid=1000.0)],
			{"GB-1": [_refund("2026-12-15", amount=1000.0, pe="PE-R9")]},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["closing"], 0.0)
		self.assertAlmostEqual(row["refunded"], 1000.0)
		self.assertAlmostEqual(row["revenue_portion"], 0.0)

	def test_it_still_says_where_the_row_went(self):
		self.assertIn("Settled during the period", self._row()["note"])

	def test_a_cancellation_does_not_swallow_the_settled_note(self):
		# `or` made the two labels exclusive, so a cancelled-and-settled order
		# said only "refund pending" — inaccurate once its receipt has posted,
		# and it hid the fact that the row had left the closing set.
		row = self._row(cancel="2026-12-05 10:00:00")
		self.assertIn("Settled during the period", row["note"])
		self.assertIn("Cancelled", row["note"])


class AnOrderWithNoAmountIsAFaultNotASettlement(FrappeTestCase):
	"""Judging "still in hand" by the remaining amount quietly dropped
	a row whose amount was never recorded at all — an order with a payment date
	and no figure, which is a data fault and used to be listed. Silently
	excluding it is the one direction this file is not allowed to err in."""

	def test_a_payment_with_no_amount_is_still_listed(self):
		order = _order(received="2026-12-28", paid=0.0)
		self.assertTrue(cb._still_in_hand(order, [], "2026-12-31"))

	def test_a_fault_row_that_shipped_still_says_it_shipped(self):
		# "Did a refund take it off the list" is answered by what is
		# left in hand, and a row with no amount has nothing left in hand
		# either way — so the fault row lost the one word explaining that it
		# left because the gun went out. Two different facts, both true.
		rows = cb._build_item3([_order(received="2026-11-20", paid=0.0,
				pe="PE-1", pe_date="2026-12-10")], {},
			period_start="2026-12-01", period_end="2026-12-31")
		note = _rows_for(rows, 3)[0]["note"]
		self.assertIn("No amount recorded", note)
		self.assertIn("Settled during the period", note)

	def test_and_the_row_says_the_amount_is_missing(self):
		rows = cb._build_item3([_order(received="2026-12-28", paid=0.0)], {},
			period_start="2026-12-01", period_end="2026-12-31")
		self.assertIn("No amount recorded", _rows_for(rows, 3)[0]["note"])

	def test_an_order_refunded_to_nothing_is_still_excluded(self):
		# The distinction: nothing recorded (fault, list it) versus recorded and
		# given back (settled, drop it).
		order = _order(received="2026-12-28", paid=1000.0)
		self.assertFalse(cb._still_in_hand(order,
			[_refund("2026-12-30", amount=1000.0)], "2026-12-31"))


class AFaultRowIsListedButNeverSummed(FrappeTestCase):
	"""reviewer r3 major-1. Keeping the "no amount recorded" order on the page
	was right; letting it into the sums was not.

	Its revenue portion comes from `order_total`, which has nothing to do with
	what the bank received — so an order the bank received NOTHING for was
	adding its whole revenue to the figure an accountant copies into the year's
	gross receipts. The fix for a silently-dropped row turned into a silently
	inflated tax number, which is the worse of the two."""

	FAULT = dict(received="2026-12-28", order_total=1245.99, tax=100.99, paid=0.0)

	def test_the_row_is_still_on_the_page(self):
		rows = cb._build_item3([_order(**self.FAULT)], {},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		self.assertIn("No amount recorded", row["note"])

	def test_but_its_money_columns_are_blank(self):
		# Printing 1,145.00 of revenue beside "no amount recorded" is the page
		# arguing with itself, and it is what the column sums.
		rows = cb._build_item3([_order(**self.FAULT)], {},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		for column in ("revenue_portion", "collected_portion", "payment_amount",
				"difference", "opening", "closing"):
			self.assertAlmostEqual(row[column], 0.0, msg=column)

	def test_and_it_is_not_in_the_item_three_subtotal(self):
		rows = cb._build_item3([_order(**self.FAULT)], {},
			period_start="2026-12-01", period_end="2026-12-31")
		sub = _subtotal(rows, 3)
		self.assertAlmostEqual(sub["revenue_portion"], 0.0)
		self.assertAlmostEqual(sub["payment_amount"], 0.0)

	def test_nor_in_item_five(self):
		rows = cb._build_item5([_order(**self.FAULT)], {}, "2026-12-31")
		sub = next(r for r in rows if r.get("row_kind") == "subtotal")
		self.assertAlmostEqual(sub["closing"], 0.0)

	def test_a_fault_row_with_a_refund_does_not_print_a_negative_balance(self):
		# "What is left in hand" on a row with no recorded amount and
		# a refund against it is a negative number, and it went straight into
		# the opening and closing columns — and into the opening total, which
		# is the figure the roll-forward is built on.
		# The refund posts BEFORE the opening date, so both columns are exposed:
		# with the refund only inside the period the opening one reads 0.00 by
		# luck rather than by the guard.
		rows = cb._build_item3([_order(received="2026-10-01", paid=0.0)],
			{"GB-1": [_refund("2026-11-20", amount=100.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(row["opening"], 0.0)
		self.assertAlmostEqual(row["closing"], 0.0)

	def test_nor_does_it_drag_the_opening_total_negative(self):
		rows = cb._build_item3([
			_order("GB-1", received="2026-10-01", paid=0.0),
			_order("GB-2", gb_id="1002", received="2026-11-20", paid=500.0)],
			{"GB-1": [_refund("2026-11-20", amount=100.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		self.assertAlmostEqual(_subtotal(rows, 3)["opening"], 500.0)

	def test_and_item_five_does_not_invent_a_line_for_it(self):
		# The subtotal is 0.00 either way, so this is the assertion with teeth:
		# an in-transit section listing "GunBroker Check — 0.00" says the bank
		# is holding a cheque for this order, and the whole point of the row is
		# that no cheque was ever recorded.
		rows = cb._build_item5([_order(**self.FAULT)], {}, "2026-12-31")
		self.assertEqual(_rows_for(rows, 5), [])

	def test_nor_in_any_of_item_sevens_three_lines(self):
		# The one that mattered: B was 1,145.00 on an order that banked nothing.
		rows = _rows_for(cb._build_item7([_order(**self.FAULT)], {},
			"2026-12-31", debit_account="A", credit_account="B"), 7)
		for row in rows:
			self.assertAlmostEqual(row["amount"], 0.0, msg=row["label"])

	def test_a_real_order_beside_it_is_unaffected(self):
		# The exclusion must take the fault row out, not the period.
		orders = [_order("GB-1", **self.FAULT),
			_order("GB-2", gb_id="1002", received="2026-12-28",
				order_total=1000.0, tax=55.0, paid=1000.0)]
		sub = _subtotal(cb._build_item3(orders, {}, "2026-12-01", "2026-12-31"), 3)
		self.assertAlmostEqual(sub["revenue_portion"], 945.0)
		self.assertAlmostEqual(sub["payment_amount"], 1000.0)

	def test_items_five_and_seven_sum_the_same_set(self):
		# One filter, two readers — asserted on the FILTER, not on the totals.
		# Comparing the two sums proves nothing here: the only orders the filter
		# removes are the ones contributing zero, so both figures come out equal
		# whether or not the second reader bothers to filter at all.
		orders = [_order("GB-1", **self.FAULT),
			_order("GB-2", gb_id="1002", received="2026-12-28",
				order_total=1000.0, tax=55.0, paid=1000.0)]
		seen = []
		real = cb._banked

		def spy(rows):
			seen.append([o.get("name") for o in rows])
			return real(rows)

		module = ("ffl_core.ffl_core.report.gunbroker_cash_basis_adjustments"
			".gunbroker_cash_basis_adjustments")
		with patch(f"{module}._banked", side_effect=spy):
			cb._build_item5(orders, {}, "2026-12-31")
			cb._build_item7(orders, {}, "2026-12-31",
				debit_account="A", credit_account="B")
		self.assertEqual(len(seen), 2, "one of the two readers did not filter")
		self.assertEqual(seen[0], seen[1], "the two readers filtered different sets")

	def test_the_totals_still_agree(self):
		# Kept alongside, because the filter agreeing is necessary and the
		# arithmetic agreeing is what the page shows.
		orders = [_order("GB-1", **self.FAULT),
			_order("GB-2", gb_id="1002", received="2026-12-28",
				order_total=1000.0, tax=55.0, paid=1000.0)]
		five = next(r for r in cb._build_item5(orders, {}, "2026-12-31")
			if r.get("row_kind") == "subtotal")
		seven = next(r for r in _rows_for(cb._build_item7(orders, {}, "2026-12-31",
			debit_account="A", credit_account="B"), 7)
			if r["reference"].startswith("Dr "))
		self.assertAlmostEqual(five["closing"], seven["amount"])


class TheRefundedColumnAddsUpToItsOwnSubtotal(FrappeTestCase):
	"""Keeping Refunded on a row that had left the closing
	set left it out of the subtotal, so the one column on the page whose
	rows are all populated stopped summing to the figure under it — the exact
	complaint the code three lines above says it exists to prevent."""

	def test_a_refund_that_took_the_row_off_the_list_still_counts(self):
		rows = cb._build_item3([_order(received="2026-11-20", paid=1000.0)],
			{"GB-1": [_refund("2026-12-15", amount=1000.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		detail = _rows_for(rows, 3)[0]
		self.assertAlmostEqual(detail["refunded"], 1000.0)
		self.assertAlmostEqual(_subtotal(rows, 3)["refunded"], 1000.0)

	def test_the_column_sums_to_the_subtotal_across_both_kinds_of_row(self):
		rows = cb._build_item3([
			_order("GB-1", received="2026-11-20", paid=1000.0),
			_order("GB-2", gb_id="1002", received="2026-12-05", paid=500.0)],
			{"GB-1": [_refund("2026-12-15", amount=1000.0)],
			 "GB-2": [_refund("2026-12-20", amount=50.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		details = _rows_for(rows, 3)
		self.assertAlmostEqual(sum(r["refunded"] for r in details),
			_subtotal(rows, 3)["refunded"])

	def test_every_money_column_sums_to_its_own_subtotal(self):
		# reviewer r4 minor-1. Asserting one column at a time is how a second
		# column quietly stops adding up — the Refunded fault was found that
		# way, and Closing had the same shape waiting behind a fault row with a
		# refund against it (Opening and Closing are assigned outside the
		# `split` dict, so the zeroing that covers the other columns cannot
		# reach them). Every column, and the input that exposes it.
		orders = [
			_order("GB-1", received="2026-12-05", paid=1000.0),
			_order("GB-2", gb_id="1002", received="2026-11-20", paid=500.0),
			# the composite fault: no amount recorded AND a refund against it
			_order("GB-3", gb_id="1003", received="2026-10-01", paid=0.0),
		]
		refunds = {
			"GB-1": [_refund("2026-12-20", amount=50.0, pe="PE-A")],
			"GB-3": [_refund("2026-11-10", amount=100.0, pe="PE-C")],
		}
		rows = cb._build_item3(orders, refunds,
			period_start="2026-12-01", period_end="2026-12-31")
		details, sub = _rows_for(rows, 3), _subtotal(rows, 3)
		for column in ("revenue_portion", "collected_portion", "payment_amount",
				"difference", "refunded", "opening", "closing"):
			self.assertAlmostEqual(sum(r[column] for r in details),
				sub[column], msg=column)

	def test_no_column_on_a_fault_row_goes_negative(self):
		# "How much of this cheque is still in the bank" cannot be below zero,
		# and on a row whose whole point is that no cheque was recorded it is
		# not a quantity at all.
		rows = cb._build_item3([_order(received="2026-10-01", paid=0.0)],
			{"GB-1": [_refund("2026-11-10", amount=100.0)]},
			period_start="2026-12-01", period_end="2026-12-31")
		row = _rows_for(rows, 3)[0]
		for column in ("opening", "closing", "payment_amount", "revenue_portion"):
			self.assertGreaterEqual(row[column], 0.0, msg=column)


class TheCompanyFilterReachesTheQueriesThatCanHonourIt(FrappeTestCase):
	"""reviewer minor-2. Company is a required filter; half the page ignored
	it. Harmless on one company, and exactly the shape that stops being
	harmless later."""

	def test_the_invoice_query_is_scoped_to_the_company(self):
		with patch("frappe.db.has_column", return_value=True), \
				patch("frappe.get_all", return_value=[]) as get_all:
			cb._fetch_invoices("TSA", "2026-12-31")
		self.assertEqual(get_all.call_args[1]["filters"]["company"], "TSA")

	def test_the_order_query_says_why_it_cannot_be(self):
		# GunBroker Order has no company column. Saying so stops the next
		# reader "fixing" it with a column that does not exist.
		self.assertIn("company", cb._fetch_orders.__doc__.lower())


class TheYearEndCreditAccountIsAProposal(FrappeTestCase):
	"""Which liability the year-end entry credits is the accountant's
	call and has not been made; the report picks one to be useful, and has to
	say that it picked."""

	def test_the_reference_marks_it_as_a_default(self):
		rows = _rows_for(cb._build_item7([_order()], {}, "2026-12-31",
			debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA"), 7)
		entry = next(r for r in rows if r["reference"].startswith("Dr "))
		self.assertIn("default", entry["reference"].lower())


class ADraftReceiptHasSettledNothing(FrappeTestCase):
	"""The exclusion asked "not cancelled"; it should ask "submitted".
	A draft Payment Entry has posted nothing to the ledger, so an order behind
	one is still cash in hand."""

	def test_a_draft_receipt_leaves_the_order_in_hand(self):
		order = _order(received="2026-12-05", pe="PE-1", pe_date="2026-12-20",
			pe_docstatus=0)
		self.assertTrue(cb._still_in_hand(order, [], "2026-12-31"))

	def test_a_submitted_receipt_settles_it(self):
		order = _order(received="2026-12-05", pe="PE-1", pe_date="2026-12-20",
			pe_docstatus=1)
		self.assertFalse(cb._still_in_hand(order, [], "2026-12-31"))


# ------------------------------------------------------------ the front page

class TheThreeStatementsAtTheTop(FrappeTestCase):
	"""Without the first one an accountant reconciling the bank will
	count the same December cheque twice — once off the statement and once off
	the invoice — and the other two say what "as-of" means here."""

	def test_the_posting_date_policy_is_stated(self):
		text = cb._preamble("2026-12-31")
		self.assertIn("Sales Invoice", text)
		self.assertIn("not back-dated", text)

	def test_the_rerun_promise_states_what_it_depends_on(self):
		# "The same figures on any later date" is true of a period
		# whose books are finished, and only of that: a ledger row dated inside
		# the period can still be added, cancelled or re-allocated afterwards —
		# reconciling a receipt is exactly that — and then the figures move,
		# correctly. Promising it unconditionally would train the reader to
		# distrust the page the first time it happened.
		text = cb._preamble("2026-12-31")
		self.assertIn("provided no ledger row", text)
		self.assertIn("si_outstanding", text)

	def test_the_as_of_rerun_promise_is_stated(self):
		self.assertIn("as-of", cb._preamble("2026-12-31"))
		self.assertIn("2026-12-31", cb._preamble("2026-12-31"))

	def test_the_opening_and_closing_columns_are_explained(self):
		self.assertIn("opening", cb._preamble("2026-12-31").lower())

	def test_there_are_exactly_three_statements(self):
		# Three, numbered, so a reviewer can see at a glance that none was
		# dropped in an edit.
		text = cb._preamble("2026-12-31")
		for n in ("1.", "2.", "3."):
			self.assertIn(n, text)


class TheColumnSet(FrappeTestCase):
	# Keys a row may carry without a column of its own. Named one by one on
	# purpose: an undeclared key renders nowhere, so a figure written into a
	# typo'd fieldname is invisible on the page and correct in the code.
	DATA_ONLY = {"basis"}

	def test_every_column_a_row_writes_is_declared(self):
		declared = {c["fieldname"] for c in cb._columns()} | self.DATA_ONLY
		written = set()
		for builder in (
			lambda: cb._build_item1("A", opening=1.0, credits=1.0, debits=1.0,
				paid=1.0),
			lambda: cb._build_item2([("MPF - TSA", 89.99)]),
			lambda: cb._build_item3([_order()], {}, "2026-12-01", "2026-12-31"),
			lambda: cb._build_item4([_si()], {"SI-1": 500.0},
				period_end="2026-12-31"),
			lambda: cb._build_item5([_order()], {}, "2026-12-31"),
			lambda: cb._build_item6([{"voucher": "JE-1", "posting_date": "2026-12-20",
				"tax_debit": 1.0, "fee_debit": 1.0, "bank_credit": 2.0}]),
			lambda: cb._build_item7([_order()], {}, "2026-12-31",
				debit_account="A", credit_account="B"),
		):
			for row in builder():
				written |= set(row)
		self.assertEqual(written - declared, set())

	def test_the_seven_items_are_all_emitted_by_the_assembler(self):
		# The schedule is seven items; a missing section is a missing year-end
		# adjustment, and nothing else in the report would say so.
		rows = cb._assemble(
			accrued={"account": "Accrued GunBroker Fees - TSA",
				"opening": 0.0, "credits": 0.0, "debits": 0.0, "paid": 0.0},
			liabilities=[("GunBroker MPF Sales Tax Payable - TSA", 89.99),
				("GunBroker Marketplace Fee Payable - TSA", 11.0)],
			orders=[], refunds={}, invoices=[], receivable={}, settlements=[],
			period_start="2026-12-01", period_end="2026-12-31",
			debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA")
		self.assertEqual(sorted({r["item"] for r in rows}), [1, 2, 3, 4, 5, 6, 7])

	def test_the_deposit_account_reaches_item_five_through_the_assembler(self):
		# The builder is tested directly, so dropping the argument here leaves
		# every one of those tests green and the account name off the page —
		# which is the entire finding this was raised for.
		rows = cb._assemble(
			accrued={"account": "A", "opening": 0.0, "credits": 0.0,
				"debits": 0.0, "paid": 0.0},
			liabilities=[], orders=[_order(received="2026-12-28")], refunds={},
			invoices=[], receivable={}, settlements=[],
			period_start="2026-12-01", period_end="2026-12-31",
			debit_account="Operating Bank - TSA",
			credit_account="Customer Deposits - TSA")
		detail = next(r for r in rows
			if r.get("item") == 5 and r.get("row_kind") == "detail")
		self.assertIn("Operating Bank - TSA", detail["note"])

	def test_an_empty_period_still_emits_all_seven_headings(self):
		rows = cb._assemble(
			accrued={"account": "A", "opening": 0.0, "credits": 0.0,
				"debits": 0.0, "paid": 0.0},
			liabilities=[], orders=[], refunds={}, invoices=[], receivable={},
			settlements=[], period_start="2026-12-01", period_end="2026-12-31",
			debit_account=None, credit_account=None)
		headings = [r["item"] for r in rows if r.get("row_kind") == "section"]
		self.assertEqual(headings, [1, 2, 3, 4, 5, 6, 7])


class TheFiltersRefuseWhatTheyCannotAnswer(FrappeTestCase):
	"""Both dates are required, and the schedule says why rather than quietly
	picking one — a default period end is a wrong period end that looks right."""

	def test_a_missing_period_end_is_refused(self):
		with self.assertRaises(Exception) as caught:
			cb._validate(frappe._dict({"period_start": "2026-12-01"}))
		self.assertIn("as-of", str(caught.exception))

	def test_a_missing_period_start_is_refused(self):
		with self.assertRaises(Exception) as caught:
			cb._validate(frappe._dict({"period_end": "2026-12-31"}))
		self.assertIn("opening", str(caught.exception))

	def test_a_backwards_period_is_refused(self):
		with self.assertRaises(Exception):
			cb._validate(frappe._dict({"period_start": "2026-12-31",
				"period_end": "2026-12-01"}))

	def test_a_valid_pair_comes_back_as_dates(self):
		start, end = cb._validate(frappe._dict({"period_start": "2026-12-01",
			"period_end": "2026-12-31"}))
		self.assertEqual(str(start), "2026-12-01")
		self.assertEqual(str(end), "2026-12-31")
