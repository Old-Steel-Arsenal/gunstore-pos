"""The daily read of half-finished GunBroker orders.

Every step of a GunBroker sale logs its failures rather than raising them, so a
booked bound-book exit is never rolled back by a bookkeeping problem. That is
the right call in the moment and a terrible one over a month: nothing in the
system objects afterwards. This is the thing that objects.

What is asserted here is mostly that each check is looking for its OWN failure
and not accidentally for another one — and that one broken check does not take
the other six down with it, because the day these matter is the day something
is already wrong.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import audit


class _AuditCase(FrappeTestCase):
	def setUp(self):
		self.alerts: list[tuple] = []
		self._patch_object(audit, "notify_admins",
			side_effect=lambda subject, message, **kw: self.alerts.append(
				(kw["dedup_key"], message)))
		self._patch_object(audit, "gunbroker_enabled", return_value=True)

	def _patch(self, target, *args, **kw):
		patcher = patch(target, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, *args, **kw):
		patcher = patch.object(target, attr, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started


def _where(query: str) -> str:
	"""Just the WHERE clause, flattened.

	The assertions below are substring matches against a query the offline
	suite can only mock, and a predicate is only doing its job while it is in
	the WHERE: moving a COALESCE out of the WHERE and leaving it in the ORDER
	BY satisfied the old assertions exactly, and changed the answer. Splitting
	first makes that variant red.
	"""
	flat = " ".join(query.split())
	head = flat.split(" ORDER BY ")[0]
	return head.split(" WHERE ", 1)[1] if " WHERE " in head else head


class TestChecks(_AuditCase):
	def test_a_shipped_order_with_no_invoice_is_reported(self):
		# The guns left under a booked disposition and the sale is missing from
		# gross receipts: stock out against no revenue.
		rows = [frappe._dict({"name": "GB-ORD-1", "sales_invoice": None})]
		with patch("frappe.db.sql", return_value=rows) as sql:
			self.assertEqual(audit.disposed_no_si(), ["GB-ORD-1"])
		self.assertIn("o.order_status = 'Transferred'",
			_where(sql.call_args[0][0]))

	def test_an_invoice_record_that_has_gone_is_reported_as_gone(self):
		# A link pointing at nothing is its own kind of wrong, and
		# calling it a Draft sends the reader looking for a document to open.
		rows = [frappe._dict({"name": "GB-ORD-1", "sales_invoice": "ACC-SINV-1"})]
		with patch("frappe.db.sql", return_value=rows), \
				patch("frappe.db.get_value", return_value=None):
			out = audit.disposed_no_si()
		self.assertIn("no longer exists", out[0])

	def test_a_cancelled_invoice_is_not_called_a_draft(self):
		# Telling somebody to run Create Invoice on a cancelled one walks
		# them into re-deriving the amendment they are part way through writing.
		rows = [frappe._dict({"name": "GB-ORD-1", "sales_invoice": "ACC-SINV-1"})]
		with patch("frappe.db.sql", return_value=rows), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"docstatus": 2, "name": "ACC-SINV-1"})):
			out = audit.disposed_no_si()
		self.assertIn("was cancelled", out[0])
		self.assertIn("amendment", out[0])
		self.assertNotIn("still Draft", out[0])

	def test_a_draft_invoice_counts_as_no_invoice(self):
		# For gross receipts it IS none: the guns left, the sale is not on the
		# ledger, and the Draft exists because something could not be posted.
		# Being linked to the order used to hide it from this check entirely,
		# leaving it to si_no_fee_je to report under the words "invoiced with no
		# fee accrual" — which sends the reader hunting for a journal.
		rows = [frappe._dict({"name": "GB-ORD-1", "sales_invoice": "ACC-SINV-1"})]
		with patch("frappe.db.sql", return_value=rows), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"docstatus": 0, "name": "ACC-SINV-1"})):
			out = audit.disposed_no_si()
		self.assertEqual(len(out), 1)
		self.assertIn("GB-ORD-1", out[0])
		self.assertIn("still Draft", out[0])

	def test_a_posted_invoice_is_not_reported_here(self):
		rows = [frappe._dict({"name": "GB-ORD-1", "sales_invoice": "ACC-SINV-1"})]
		with patch("frappe.db.sql", return_value=rows), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"docstatus": 1, "name": "ACC-SINV-1"})):
			self.assertEqual(audit.disposed_no_si(), [])

	def test_the_grace_period_runs_from_the_disposition_not_the_last_poll(self):
		# GunBroker touches an order every time anything happens to it (4 → 5,
		# a tracking write-back), and each touch saves it. Ageing on `modified`
		# pushed the clock forward for ever on exactly the orders that were
		# still moving.
		with patch("frappe.db.sql", return_value=[]) as sql:
			audit.disposed_no_si()
		where = _where(sql.call_args[0][0])
		self.assertIn("MIN(d.creation)", where)
		self.assertIn("tabFFL Disposition", where)
		self.assertNotIn("o.modified", where)

	def test_an_invoice_with_no_fee_accrual_is_reported(self):
		with patch("frappe.db.sql", return_value=[frappe._dict({
				"name": "GB-ORD-2", "fee_accrual_je": None,
				"state": "none"})]) as sql:
			out = audit.si_no_fee_je()
		self.assertIn("GB-ORD-2", out[0])
		self.assertIn("Create Invoice", out[0])
		where = _where(sql.call_args[0][0])
		# Posted, not merely raised: a Draft has no accrual by construction and
		# never will, so reporting it here says something untrue every day.
		self.assertIn("si.docstatus = 1", where)
		# And aged from the invoice, for the same reason as above.
		self.assertIn("si.creation <", where)

	def _fee_je_rows(self, rows):
		with patch("frappe.db.sql", return_value=rows) as sql:
			return audit.si_no_fee_je(), sql

	def test_a_reversed_accrual_behind_a_LIVE_invoice_is_reported(self):
		# The eighth check asks about CANCELLED invoices, so
		# this one falls between them: somebody reversed the journal by hand,
		# could not clear the read-only link, and then amended. The amendment
		# submits, the link is still set, so no fee is accrued for the sale
		# that replaced it — and until now nothing said a word.
		out, _sql = self._fee_je_rows([frappe._dict({
			"name": "GB-ORD-12", "fee_accrual_je": "ACC-JV-2026-99001",
			"state": "reversed"})])
		self.assertIn("GB-ORD-12", out[0])
		self.assertIn("ACC-JV-2026-99001", out[0])
		self.assertIn("Fee Accrual Journal Entry", out[0])

	def test_a_hand_cancelled_accrual_behind_a_live_invoice_is_reported(self):
		out, _sql = self._fee_je_rows([frappe._dict({
			"name": "GB-ORD-13", "fee_accrual_je": "ACC-JV-2026-99002",
			"state": "cancelled"})])
		self.assertIn("GB-ORD-13", out[0])
		self.assertIn("cancelled", out[0].lower())

	def test_an_invoice_that_never_had_an_accrual_reads_differently(self):
		# Same category, different fix: this one just needs Create Invoice.
		out, _sql = self._fee_je_rows([frappe._dict({
			"name": "GB-ORD-14", "fee_accrual_je": None, "state": "none"})])
		self.assertIn("GB-ORD-14", out[0])
		self.assertNotIn("Fee Accrual Journal Entry", out[0])

	def test_the_query_asks_all_three_ways_an_accrual_can_be_absent(self):
		_out, sql = self._fee_je_rows([])
		where = _where(sql.call_args[0][0])
		self.assertIn("si.docstatus = 1", where)
		self.assertIn("COALESCE(o.fee_accrual_je, '') = ''", where)
		self.assertIn("je.docstatus = 2", where)
		self.assertIn("reversal_of", where)

	def test_a_live_accrual_is_not_reported(self):
		# The negative control: the ordinary, correct state.
		self.assertEqual(self._fee_je_rows([])[0], [])

	def test_an_unpaid_invoice_is_reported_with_what_is_owed(self):
		rows = [frappe._dict({"name": "GB-ORD-3", "sales_invoice": "ACC-SINV-3"})]
		with patch("frappe.get_all", return_value=rows), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"docstatus": 1, "outstanding_amount": 245.99})):
			out = audit.si_outstanding()
		self.assertEqual(len(out), 1)
		self.assertIn("245.99", out[0])

	def test_a_cancelled_invoice_is_not_an_unpaid_one(self):
		rows = [frappe._dict({"name": "GB-ORD-3", "sales_invoice": "ACC-SINV-3"})]
		with patch("frappe.get_all", return_value=rows), \
				patch("frappe.db.get_value", return_value=frappe._dict(
					{"docstatus": 2, "outstanding_amount": 245.99})):
			self.assertEqual(audit.si_outstanding(), [])

	def _paid_rows(self, rows):
		with patch("frappe.db.sql", return_value=rows), \
				patch("frappe.db.get_single_value", return_value=7):
			return audit.paid_not_disposed()

	def test_a_payment_recorded_in_pos_is_reported_with_its_cheque(self):
		out = self._paid_rows([frappe._dict({
			"name": "GB-ORD-4", "order_status": "Pending Fulfillment",
			"payment_mode_used": "GunBroker Check", "payment_reference_no": "4471",
			"payment_received_on": "2026-08-01", "payment_amount": 1245.99})])
		self.assertIn("GB-ORD-4", out[0])
		self.assertIn("2026-08-01", out[0])
		self.assertIn("4471", out[0])
		self.assertIn("recorded in POS", out[0])

	def test_a_gunbroker_only_flag_is_reported_as_exactly_that(self):
		# The two are not the same problem and must not read the same. One is
		# money in the bank with a cheque number against it; the other is
		# GunBroker asserting something POS has no evidence for.
		out = self._paid_rows([frappe._dict({
			"name": "GB-ORD-5", "order_status": "Pending Fulfillment",
			"payment_mode_used": "", "payment_reference_no": None,
			"payment_received_on": None, "payment_amount": 0})])
		self.assertIn("GB-ORD-5", out[0])
		self.assertIn("nothing recorded in POS", out[0])
		self.assertNotIn("None", out[0])

	def test_a_cancelled_order_that_was_paid_is_called_a_refund(self):
		out = self._paid_rows([frappe._dict({
			"name": "GB-ORD-6", "order_status": "Cancelled",
			"payment_mode_used": "GunBroker Check", "payment_reference_no": "4472",
			"payment_received_on": "2026-08-01", "payment_amount": 1245.99})])
		self.assertIn("CANCELLED", out[0])
		self.assertIn("refund", out[0].lower())

	def test_the_predicate_leads_on_what_pos_recorded(self):
		# THE case. The GunBroker mirror fields are rewritten by every poll:
		# PUT /Flags PaymentReceived is best-effort, so one failure takes
		# payment_received back to 0, and GunBroker does not always send a date,
		# so payment_received_at goes back to NULL. Either way an order with a
		# cheque banked and nothing on the ledger fell out of this check
		# silently — the one kind it exists for.
		with patch("frappe.db.sql", return_value=[]) as sql, \
				patch("frappe.db.get_single_value", return_value=3):
			audit.paid_not_disposed()
		# The WHERE clause specifically: leaving the COALESCE in the ORDER BY
		# and taking it out of the WHERE satisfied the old assertion and
		# changed which orders come back.
		where = _where(sql.call_args[0][0])
		self.assertIn("COALESCE(payment_mode_used, '') != '' OR payment_received = 1",
			where)
		self.assertIn(
			"COALESCE(payment_received_on, DATE(payment_received_at), DATE(creation))",
			where)

	def test_the_stale_threshold_is_a_setting_and_only_a_nudge(self):
		# Emphatically NOT the threshold the cash-basis schedule uses: that one
		# is every unshipped paid order as at the period end, with no age filter
		# at all, or the last week of December falls out of the year's income.
		with patch("frappe.db.sql", return_value=[]) as sql, \
				patch("frappe.db.get_single_value", return_value=3):
			audit.paid_not_disposed()
		cutoff = sql.call_args[0][1]["cutoff"]
		self.assertEqual((frappe.utils.getdate() - frappe.utils.getdate(cutoff)).days, 3)

	def test_a_cancelled_order_whose_refund_is_recorded_stops_being_reported(self):
		# The refund itself is two Payment Entries a human keys, and
		# nothing in POS can see them, so this row repeated every morning for
		# ever — the same illness the chargeback stamp was added to cure. An
		# alert that never stops is one people stop reading, including on the
		# day a real one joins the list.
		self.assertEqual(self._paid_rows([frappe._dict({
			"name": "GB-ORD-7", "order_status": "Cancelled",
			"payment_mode_used": "GunBroker Check", "payment_reference_no": "4473",
			"payment_received_on": "2026-08-01", "payment_amount": 1245.99,
			"refund_handled_at": "2026-08-20 14:00:00"})]), [])

	def test_a_cancelled_order_with_no_refund_stamp_is_still_reported(self):
		out = self._paid_rows([frappe._dict({
			"name": "GB-ORD-8", "gb_order_id": "998877", "order_status": "Cancelled",
			"payment_mode_used": "GunBroker Check", "payment_reference_no": "4474",
			"payment_received_on": "2026-08-01", "payment_amount": 1245.99,
			"refund_handled_at": None})])
		self.assertIn("CANCELLED", out[0])
		self.assertIn("following your GunBroker refund procedure", out[0])
		self.assertIn('"GunBroker Order #998877"', out[0])
		self.assertIn("fill in Refund Handled At on the GunBroker Order", out[0])
		self.assertNotIn("<", out[0])

	def test_the_refund_stamp_does_not_silence_a_live_order(self):
		# The dangerous over-reach: the stamp answers "this cancellation has
		# been refunded", not "stop looking at this order". A live order with
		# money banked and nothing shipped is the check's whole reason to exist.
		out = self._paid_rows([frappe._dict({
			"name": "GB-ORD-9", "order_status": "Pending Fulfillment",
			"payment_mode_used": "GunBroker Check", "payment_reference_no": "4475",
			"payment_received_on": "2026-08-01", "payment_amount": 1245.99,
			"refund_handled_at": "2026-08-20 14:00:00"})])
		self.assertIn("GB-ORD-9", out[0])

	def test_the_refund_stamp_does_not_silence_what_pos_never_recorded(self):
		# "GunBroker says paid, nothing recorded in POS" is a different problem
		# with a different fix, and there is no receipt to refund — so no
		# refund can have been keyed, whatever the stamp says. Stamping it must
		# not make it disappear. (The line it comes back on still reads
		# "refund not dealt with", because the formatter leads on the
		# cancellation; that wording is the existing check's and is left alone.)
		out = self._paid_rows([frappe._dict({
			"name": "GB-ORD-10", "order_status": "Cancelled",
			"payment_mode_used": "", "payment_reference_no": None,
			"payment_received_on": None, "payment_amount": 0,
			"refund_handled_at": "2026-08-20 14:00:00"})])
		self.assertEqual(len(out), 1)
		self.assertIn("GB-ORD-10", out[0])

	def test_the_stamp_is_actually_fetched(self):
		# Deciding in Python on a column the query never selected reads every
		# row as unstamped, silently, for ever.
		with patch("frappe.db.sql", return_value=[]) as sql, \
				patch("frappe.db.get_single_value", return_value=7):
			audit.paid_not_disposed()
		self.assertIn("refund_handled_at", sql.call_args[0][0])

	def test_a_chargeback_somebody_has_dealt_with_stops_being_reported(self):
		# Resolving one is a decision POS cannot observe, so without the stamp
		# the alert repeats until the order is deleted — and an alert that never
		# stops is one people stop reading, including on the day a new
		# chargeback joins the list.
		with patch("frappe.get_all", return_value=["GB-ORD-6"]) as get_all:
			self.assertEqual(audit.chargebacks(), ["GB-ORD-6"])
		filters = get_all.call_args[1]["filters"]
		self.assertEqual(filters["is_chargeback"], 1)
		self.assertEqual(filters["chargeback_handled_at"], ("is", "not set"))

	def test_a_refunded_order_with_live_revenue_is_reported(self):
		rows = [frappe._dict({"name": "GB-ORD-5", "gb_order_id": "998877",
			"gb_status_label": "Refund Order", "sales_invoice": "ACC-SINV-5"})]
		with patch("frappe.get_all", return_value=rows), \
				patch("frappe.db.get_value", return_value=1):
			out = audit.status_conflict()
		self.assertIn("Refund Order", out[0])
		# The row carries the Remarks text with the real GunBroker order id.
		self.assertIn('"GunBroker Order #998877"', out[0])
		self.assertNotIn("<", out[0])

	def test_a_refunded_order_whose_invoice_was_cancelled_is_not(self):
		rows = [frappe._dict({"name": "GB-ORD-5", "gb_status_label": "Refund Order",
			"sales_invoice": "ACC-SINV-5"})]
		with patch("frappe.get_all", return_value=rows), \
				patch("frappe.db.get_value", return_value=2):
			self.assertEqual(audit.status_conflict(), [])

	def test_an_order_the_poll_gave_up_on_is_reported(self):
		with patch("frappe.get_all", return_value=["GB-ORD-7"]) as get_all:
			self.assertEqual(audit.stuck_sync(), ["GB-ORD-7"])
		self.assertEqual(get_all.call_args[1]["filters"]["sync_fail_count"][0], ">=")


class TestAccrualLeftOnACancelledInvoice(_AuditCase):
	"""The eighth check.

	When a GunBroker invoice is cancelled the seller-fee accrual behind it is
	reversed automatically and the order's link to it is cleared. When that
	reversal FAILS, one alert goes out and then nothing ever mentions it again:
	`si_no_fee_je` and `status_conflict` both only look at posted invoices, and
	this invoice is cancelled. So `Dr Marketplace Fees / Cr Accrued GunBroker
	Fees` stays on the ledger against a sale that no longer exists — channel
	profit reads low for ever, the monthly statement will not reconcile, and
	the still-set link means an amendment of that invoice re-accrues nothing.
	"""

	def _rows(self, rows):
		with patch("frappe.db.sql", return_value=rows) as sql:
			return audit.accrual_on_cancelled_si(), sql

	def test_a_live_accrual_behind_a_cancelled_invoice_is_reported(self):
		out, _sql = self._rows([frappe._dict({
			"name": "GB-ORD-11", "sales_invoice": "ACC-SINV-2026-99001",
			"fee_accrual_je": "ACC-JV-2026-99001"})])
		self.assertIn("GB-ORD-11", out[0])
		self.assertIn("ACC-JV-2026-99001", out[0])

	def test_the_message_says_to_clear_the_link_afterwards(self):
		# The field is read-only, so somebody who reverses the journal by hand
		# cannot clear it — and while it still points at the reversed journal
		# an amendment will not re-accrue the fee at all.
		out, _sql = self._rows([frappe._dict({
			"name": "GB-ORD-11", "sales_invoice": "ACC-SINV-2026-99001",
			"fee_accrual_je": "ACC-JV-2026-99001"})])
		self.assertIn("Fee Accrual Journal Entry", out[0])

	def test_it_asks_only_about_cancelled_invoices(self):
		_out, sql = self._rows([])
		self.assertIn("si.docstatus = 2", _where(sql.call_args[0][0]))

	def test_it_asks_only_about_a_journal_that_is_still_live(self):
		# A cancelled accrual is already unwound; reporting it would put a
		# permanent row in front of the reader.
		_out, sql = self._rows([])
		self.assertIn("je.docstatus = 1", _where(sql.call_args[0][0]))

	def test_a_reversed_accrual_is_excluded_by_the_query(self):
		# The reversal ERPNext writes carries reversal_of pointing back at the
		# original. Without this clause the check fires on every correctly
		# unwound cancellation, i.e. on the normal path, daily.
		where = _where(self._rows([])[1].call_args[0][0])
		self.assertIn("reversal_of", where)
		self.assertIn("NOT EXISTS", where)

	def test_only_a_submitted_reversal_counts_as_a_reversal(self):
		# A draft or cancelled reversal has unwound nothing.
		where = _where(self._rows([])[1].call_args[0][0])
		self.assertIn("rev.docstatus = 1", where)

	def test_an_order_with_no_accrual_link_is_not_asked_about(self):
		where = _where(self._rows([])[1].call_args[0][0])
		self.assertIn("COALESCE(o.fee_accrual_je, '') != ''", where)


class TestTheRun(_AuditCase):
	def test_the_table_resolves_its_checks_by_name(self):
		# Binding the functions into the table at import time would make the
		# audit unpatchable — and unpatchable is untestable.
		for _kind, finder in audit._CHECKS:
			self.assertTrue(callable(getattr(audit, finder)), finder)

	def test_every_category_is_covered(self):
		names = [kind for kind, _finder in audit._CHECKS]
		self.assertEqual(sorted(names), sorted([
			"accrual_on_cancelled_si", "auction_unsold", "chargeback", "disposed_no_si",
			"paid_not_disposed", "si_no_fee_je", "si_outstanding",
			"status_conflict", "stuck_sync"]))

	def test_every_category_has_an_explanation_to_send(self):
		# An alert that only names orders makes whoever reads it go and work out
		# what it means, at eight in the morning, once a day.
		subjects, why = audit._subjects(), audit._why()
		for kind, _finder in audit._CHECKS:
			self.assertIn(kind, subjects)
			self.assertIn(kind, why)
			self.assertTrue(why[kind])

	def test_each_category_sends_one_email_naming_its_orders(self):
		self._patch_object(audit, "disposed_no_si", return_value=["GB-ORD-1"])
		self._patch_object(audit, "si_no_fee_je", return_value=[])
		self._patch_object(audit, "si_outstanding", return_value=[])
		self._patch_object(audit, "paid_not_disposed", return_value=["GB-ORD-4"])
		self._patch_object(audit, "status_conflict", return_value=[])
		self._patch_object(audit, "chargebacks", return_value=[])
		self._patch_object(audit, "stuck_sync", return_value=[])
		self._patch_object(audit, "accrual_on_cancelled_si", return_value=[])
		findings = audit.daily_gb_audit()
		self.assertEqual(sorted(findings), ["disposed_no_si", "paid_not_disposed"])
		keys = [k for k, _m in self.alerts]
		self.assertEqual(sorted(keys),
			["gb_audit_disposed_no_si", "gb_audit_paid_not_disposed"])
		self.assertIn("GB-ORD-1", dict(self.alerts)["gb_audit_disposed_no_si"])

	def test_a_clean_day_sends_nothing(self):
		for _kind, finder in audit._CHECKS:
			self._patch_object(audit, finder, return_value=[])
		self.assertEqual(audit.daily_gb_audit(), {})
		self.assertEqual(self.alerts, [])

	def test_one_broken_check_does_not_hide_the_others(self):
		# The day these matter is the day something is already wrong.
		self._patch_object(audit, "disposed_no_si",
			side_effect=RuntimeError("bad query"))
		self._patch_object(audit, "si_no_fee_je", return_value=[])
		self._patch_object(audit, "si_outstanding", return_value=[])
		self._patch_object(audit, "paid_not_disposed", return_value=[])
		self._patch_object(audit, "status_conflict", return_value=[])
		self._patch_object(audit, "chargebacks", return_value=["GB-ORD-6"])
		self._patch_object(audit, "stuck_sync", return_value=[])
		self._patch_object(audit, "accrual_on_cancelled_si", return_value=[])
		self._patch_object(audit, "safe_log_error", side_effect=lambda *a: None)
		findings = audit.daily_gb_audit()
		self.assertEqual(list(findings), ["chargeback"])

	def test_the_audit_still_runs_with_the_poll_switched_off(self):
		# Switching the poll off does not un-ship the guns that already went,
		# and those are exactly the orders somebody needs told about.
		self._patch_object(audit, "disposed_no_si", return_value=["GB-ORD-1"])
		for name in ("si_no_fee_je", "si_outstanding", "paid_not_disposed",
				"status_conflict", "chargebacks", "stuck_sync",
				"accrual_on_cancelled_si"):
			self._patch_object(audit, name, return_value=[])
		with patch("ffl_core.utils.single_flag", return_value=False):
			findings = audit.daily_gb_audit()
		self.assertEqual(list(findings), ["disposed_no_si"])

	def test_a_disabled_integration_reports_nothing(self):
		self._patch_object(audit, "gunbroker_enabled", return_value=False)
		self.assertEqual(audit.daily_gb_audit(), {})

	def test_the_scheduled_entry_never_raises(self):
		self._patch_object(audit, "gunbroker_enabled",
			side_effect=RuntimeError("settings table is missing"))
		self._patch_object(audit, "safe_log_error", side_effect=lambda *a: None)
		audit.daily_gb_audit()  # must not raise



class AuctionsThatRanOut(FrappeTestCase):
	def test_yesterdays_unsold_auctions_are_listed_as_serials(self):
		"""The one audit line about guns rather than orders, worded as such."""
		from ffl_integrations.gunbroker import audit

		with patch("frappe.get_all", return_value=["SN-1", "SN-2"]) as get_all:
			self.assertEqual(audit.auctions_ended_unsold(), ["SN-1", "SN-2"])
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["gb_listing_type"], "Auction")
		self.assertEqual(filters["gb_sync_error"], "auction ended without a sale")
		sent = {}
		with patch.object(audit, "notify_admins",
				side_effect=lambda subject, message, **kw: sent.update(
					subject=subject, message=message, **kw)):
			audit._alert("auction_unsold", ["SN-1", "SN-2"])
		self.assertIn("2 serial(s)", sent["subject"])
		self.assertIn("auction ended without a sale", sent["subject"])
		self.assertIn("SN-2", sent["message"])
		self.assertEqual(sent["dedup_key"], "gb_audit_auction_unsold")
