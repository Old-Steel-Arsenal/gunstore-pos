"""The daily read of everything a GunBroker order can be stuck halfway through.

Every step of a GunBroker sale is resumable and every one of them can fail
quietly: the invoice is raised after the guns have already gone, the fee accrual
after the invoice, the receipt after that, and each failure is logged rather
than raised so a bound-book exit is never rolled back. That is the right
trade-off in the moment and a terrible one over a month, because nothing in the
system objects afterwards.

Eight checks, and each one is a specific way the books end up wrong:

``disposed_no_si``          guns shipped, no invoice — gross receipts understated
``si_no_fee_je``            invoice posted, no LIVE accrual — profit overstated
``si_outstanding``          invoice posted, money never landed on the ledger
``paid_not_disposed``       money in the bank, nothing on the ledger at all
``status_conflict``         returned or refunded on GunBroker, revenue still booked
``accrual_on_cancelled_si`` the sale is gone, its fee accrual is not
``chargeback``              the bank has taken the money back
``stuck_sync``              an order the poll has given up carrying forward

Alerting only. Nothing here fixes anything: each of these needs a decision
(which buyer gets the gun, whether the cheque cleared, whether the return was
agreed) and a job that guessed would be worse than one that asked.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime, nowdate

from ffl_core.marketplace_accounts import refund_remarks_rule
from ffl_integrations.gunbroker.orders import (
	CANCELLED_STATUS,
	MAX_CONSECUTIVE_FAILURES,
	REFUND_STATUS,
	RETURNED_STATUS,
)
from ffl_integrations.integration_gate import gunbroker_enabled
from ffl_integrations.notifications import notify_admins, safe_log_error

ORDER_DOCTYPE = "GunBroker Order"
SETTINGS_DOCTYPE = "GunBroker Settings"

# A booking that has been outstanding less than this is a job that has not run
# yet, not a problem. One day, because the retries are minutes apart.
SETTLE_GRACE_DAYS = 1


def daily_gb_audit() -> dict:
	"""Scheduled daily. One email per category, listing the orders in it."""
	findings: dict[str, list[str]] = {}
	try:
		# Gated on the integration being ON, not on auto_sync_enabled: switching
		# the poll off does not un-ship the guns that already went, and those are
		# exactly the orders somebody needs told about.
		if not gunbroker_enabled():
			return findings
		for kind, finder in _CHECKS:
			try:
				rows = globals()[finder]()
			except Exception:  # noqa: BLE001 — one check must not hide the rest
				safe_log_error(f"GunBroker audit check {kind} failed",
					frappe.get_traceback())
				continue
			if rows:
				findings[kind] = rows
				_alert(kind, rows)
	except Exception:  # noqa: BLE001 — a scheduler tick must survive
		safe_log_error("GunBroker daily audit failed", frappe.get_traceback())
	return findings


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _stale_cutoff(days: int = SETTLE_GRACE_DAYS):
	"""The datetime before which a booking is late rather than merely young.

	Site local, and so is everything it is compared against: every GunBroker
	Order Datetime is written in site time (LD-20) and `creation` always was.
	Before that, this compared a local clock with UTC numbers and every
	threshold here was five or six hours out.
	"""
	return add_to_date(now_datetime(), days=-days)


def _stale_date(days: int):
	"""The same cutoff as a DATE, for the columns that hold one."""
	return add_to_date(nowdate(), days=-days)


def disposed_no_si() -> list[str]:
	"""Guns shipped under a booked disposition with no POSTED invoice behind them.

	A Draft counts as no invoice, because for gross receipts it is one: the guns
	left, the sale is not on the ledger, and the Draft exists precisely because
	something about it could not be posted. It is linked to the order (so a
	retry can find it), which used to make this check skip it entirely and leave
	it to be reported by si_no_fee_je — under the words "invoiced with no fee
	accrual", which sends the reader looking for a journal instead of at the
	Draft.

	The age is measured from the disposition, not from `modified`: every poll
	that touches the order (GunBroker moving it 4 → 5, a tracking write-back)
	saves it and would push the clock forward for ever.
	"""
	rows = frappe.db.sql(
		"""
		SELECT o.name, o.sales_invoice
		FROM `tabGunBroker Order` o
		WHERE o.order_status = 'Transferred'
		  AND COALESCE(
		        (SELECT MIN(d.creation) FROM `tabFFL Disposition` d
		         WHERE d.gunbroker_order = o.name AND d.docstatus = 1),
		        o.creation) < %(cutoff)s
		""",
		{"cutoff": _stale_cutoff()}, as_dict=True)
	out = []
	for row in rows:
		if not row.sales_invoice:
			out.append(row.name)
			continue
		invoice = frappe.db.get_value("Sales Invoice", row.sales_invoice,
			["docstatus", "name"], as_dict=True)
		if not invoice:
			out.append(f"{row.name} (invoice {row.sales_invoice} no longer exists)")
		elif invoice.docstatus == 2:
			# Not a Draft, and the difference matters: telling somebody to run
			# Create Invoice on a cancelled one walks them into re-deriving the
			# amendment they are part way through writing.
			out.append(f"{row.name} — invoice {row.sales_invoice} was cancelled. "
				"Submit its amendment in the form, then run Create Invoice on "
				"the order to re-accrue the fee and re-apply the receipt")
		elif invoice.docstatus != 1:
			out.append(f"{row.name} (invoice {row.sales_invoice} is still Draft — "
				f"see the Error Log for why it could not be posted)")
	return out


def si_no_fee_je() -> list[str]:
	"""Invoice POSTED, and no LIVE seller-fee accrual behind it.

	Posted, not merely raised. An invoice deliberately held in Draft — the
	revenue module does that when GunBroker's figures cannot be split — is
	linked to its order and has, necessarily, no accrual behind it. Counting
	those says something untrue about them ("invoiced with no fee accrual"
	when nothing has been invoiced) and says it every single day, for ever: a
	Draft never grows an accrual. They have their own alert already.

	Three ways the accrual can be absent, not one. The link being
	empty is the obvious one. The other two are what happens after somebody
	unwinds an accrual by hand:

	* the journal was reversed, or cancelled outright, and `fee_accrual_je` —
	  read-only — still points at it. Nothing re-accrues while it does, so
	  amending that invoice books no fee for the sale that replaces it;
	* and neither of those is visible anywhere else. The cancelled-invoice
	  check asks about `docstatus = 2` invoices; this invoice is live.

	The three cases need different things done to them, so the line says which
	one it is rather than making the reader open the order to find out.
	"""
	rows = frappe.db.sql(
		"""
		SELECT o.name, o.fee_accrual_je,
		       CASE WHEN COALESCE(o.fee_accrual_je, '') = '' THEN 'none'
		            WHEN je.docstatus = 2 THEN 'cancelled'
		            ELSE 'reversed' END AS state
		FROM `tabGunBroker Order` o
		JOIN `tabSales Invoice` si ON si.name = o.sales_invoice
		LEFT JOIN `tabJournal Entry` je ON je.name = o.fee_accrual_je
		WHERE si.docstatus = 1
		  AND si.creation < %(cutoff)s
		  AND (COALESCE(o.fee_accrual_je, '') = ''
		       OR je.docstatus = 2
		       OR EXISTS (SELECT 1 FROM `tabJournal Entry` rev
		                  WHERE rev.reversal_of = o.fee_accrual_je
		                    AND rev.docstatus = 1))
		""",
		{"cutoff": _stale_cutoff()}, as_dict=True)
	out = []
	for r in rows:
		if r.state == "none":
			out.append(f"{r.name} — no fee accrual. Run Create Invoice on the order.")
			continue
		out.append(
			f"{r.name} — its accrual {r.fee_accrual_je} is {r.state}, and the "
			"order still points at it, so an amendment will re-accrue nothing. "
			"Ask a System Manager to clear Fee Accrual Journal Entry, then run "
			"Create Invoice on the order.")
	return out


def si_outstanding() -> list[str]:
	"""Invoice posted and still owed money.

	Covers both halves of the same hole: an order nobody recorded a payment for
	(so no receipt was ever created — on purpose, this code does not invent one)
	and one where the cheque came up short.
	"""
	rows = frappe.get_all(
		ORDER_DOCTYPE,
		filters={"sales_invoice": ("is", "set")},
		fields=["name", "sales_invoice"])
	out = []
	for row in rows:
		invoice = frappe.db.get_value("Sales Invoice", row.sales_invoice,
			["docstatus", "outstanding_amount"], as_dict=True)
		if invoice and invoice.docstatus == 1 and flt(invoice.outstanding_amount) > 0.01:
			out.append(f"{row.name} ({invoice.outstanding_amount} outstanding)")
	return out


def _refund_settled(row) -> bool:
	"""Has this row's refund been dealt with, so it can stop being reported?

	All three conditions, and the two beyond the stamp are the point of it.

	`order_status == Cancelled`, because the stamp answers "this cancellation
	has been refunded" and not "stop looking at this order": a LIVE order with
	money banked and nothing shipped is what this check exists for, and a stray
	stamp must not be able to hide one.

	`payment_mode_used` non-empty, because the other row this check produces —
	"GunBroker says paid, nothing recorded in POS" — has no receipt to refund
	and a different fix. Stamping it would silence a problem nobody solved.
	"""
	return bool(
		(row.get("order_status") or "") == "Cancelled"
		and (row.get("payment_mode_used") or "").strip()
		and row.get("refund_handled_at"))


def paid_not_disposed() -> list[str]:
	"""Money banked, guns not shipped, and it has been a while.

	The age is COALESCE of three columns and not the first of them alone.
	``payment_received_at`` mirrors GunBroker's paymentReceivedDate, and
	GunBroker sets ``paymentReceived`` without always sending a date — which is
	exactly the GB-set case the cash-basis rule calls the one cash basis cannot afford to
	lose. That column is then NULL, ``NULL < cutoff`` is never true in SQL, and
	the single order this check exists for was the one order it could never
	report. ``payment_received_on`` is the date a human recorded; ``creation``
	is when we first saw the order, and is always there.

	The threshold is a nudge, not a rule, and it is emphatically NOT the
	threshold the cash-basis schedule uses: that one has to be every unshipped
	paid order as at the period end, with no age filter at all, or the last
	week of December falls out of the year's income.

	A cancelled-and-paid order leaves once ``refund_handled_at`` is set. The
	refund is two Payment Entries a human keys against the bank
	and nothing in POS can see them happen, so before the stamp existed
	this row came back every morning for ever — the same illness the chargeback
	stamp was added to cure, and an alert that never stops is one people stop
	reading, including on the day a real one joins the list.
	"""
	days = int(frappe.db.get_single_value(SETTINGS_DOCTYPE, "stale_paid_days") or 7)
	rows = frappe.db.sql(
		"""
		SELECT name, gb_order_id, order_status, payment_mode_used, payment_reference_no,
		       payment_received_on, payment_amount, refund_handled_at
		FROM `tabGunBroker Order`
		WHERE (COALESCE(payment_mode_used, '') != '' OR payment_received = 1)
		  AND order_status != 'Transferred'
		  AND COALESCE(payment_received_on, DATE(payment_received_at), DATE(creation))
		      < %(cutoff)s
		ORDER BY COALESCE(payment_received_on, DATE(payment_received_at),
		                  DATE(creation)) ASC
		""",
		{"cutoff": _stale_date(days)}, as_dict=True)
	out = []
	for r in rows:
		if _refund_settled(r):
			continue
		if (r.order_status or "") == "Cancelled":
			line = (f"{r.name} — CANCELLED on GunBroker, refund not dealt with "
				f"({r.payment_amount} banked on {r.payment_received_on or '—'}, "
				f"cheque {r.payment_reference_no or '—'}). "
				+ _("Book the refund following your GunBroker refund procedure."))
			if r.get("gb_order_id"):
				line += " " + refund_remarks_rule(r.gb_order_id, receive_too=True)
			out.append(line + " " + _("When it is booked, fill in Refund Handled At on the GunBroker Order."))
		elif (r.payment_mode_used or "").strip():
			out.append(f"{r.name} — recorded in POS: {r.payment_amount} banked on "
				f"{r.payment_received_on or '—'}, cheque "
				f"{r.payment_reference_no or '—'} ({r.payment_mode_used})")
		else:
			out.append(f"{r.name} — GunBroker says paid, nothing recorded in POS "
				"(no amount, no cheque number, nothing on the ledger)")
	return out


def status_conflict() -> list[str]:
	"""Returned or refunded on GunBroker while the revenue is still booked."""
	rows = frappe.get_all(
		ORDER_DOCTYPE,
		filters={"gb_status": ("in", (RETURNED_STATUS, REFUND_STATUS,
			CANCELLED_STATUS)), "sales_invoice": ("is", "set")},
		fields=["name", "gb_order_id", "gb_status_label", "sales_invoice"])
	out = []
	for row in rows:
		docstatus = frappe.db.get_value("Sales Invoice", row.sales_invoice, "docstatus")
		if docstatus == 1:
			line = f"{row.name} ({row.gb_status_label}, invoice {row.sales_invoice})"
			if row.get("gb_order_id"):
				line += " — " + refund_remarks_rule(row.gb_order_id)
			out.append(line)
	return out


def accrual_on_cancelled_si() -> list[str]:
	"""The sale is cancelled and its seller-fee accrual is still on the ledger.

	Cancelling a GunBroker invoice reverses the accrual and clears the order's
	link to it. When that reversal fails it is logged, one alert goes out, and
	then NOTHING mentions it again: `si_no_fee_je` and `status_conflict` both
	only look at posted invoices, and this invoice is cancelled. So
	`Dr Marketplace Fees / Cr Accrued GunBroker Fees` stands against a sale
	that no longer exists — channel profit reads low for ever and the monthly
	statement cannot be reconciled.

	Worse, the link is what stops a second accrual being raised. While it
	points at a live journal, amending that invoice re-accrues nothing at all,
	and the fee for the replacement sale is simply never booked.

	Excluded once a reversal exists: ERPNext's own reversal carries
	`reversal_of` pointing back at the original, and only a SUBMITTED one has
	unwound anything — a draft or cancelled reversal has unwound nothing. The
	normal path (cancel → automatic reversal → link cleared) never reaches
	here twice over: the link is gone, and the reversal is there anyway.
	"""
	rows = frappe.db.sql(
		"""
		SELECT o.name, o.sales_invoice, o.fee_accrual_je
		FROM `tabGunBroker Order` o
		JOIN `tabSales Invoice` si ON si.name = o.sales_invoice
		JOIN `tabJournal Entry` je ON je.name = o.fee_accrual_je
		WHERE COALESCE(o.fee_accrual_je, '') != ''
		  AND si.docstatus = 2
		  AND je.docstatus = 1
		  AND NOT EXISTS (
		        SELECT 1 FROM `tabJournal Entry` rev
		        WHERE rev.reversal_of = o.fee_accrual_je AND rev.docstatus = 1)
		""", as_dict=True)
	return [
		f"{r.name} — invoice {r.sales_invoice} is cancelled and its fee accrual "
		f"{r.fee_accrual_je} is still posted. Reverse the journal, then ask a "
		"System Manager to clear Fee Accrual Journal Entry on the order — the "
		"field is read-only, and while it is set an amendment will not "
		"re-accrue the fee."
		for r in rows]


def chargebacks() -> list[str]:
	"""Chargebacks nobody has recorded dealing with.

	Filtered on the handled stamp rather than reported for ever. A chargeback
	is resolved by a decision (write it off, represent it, chase the buyer) and
	nothing in POS can tell that it happened, so the alert would repeat daily
	until the order was deleted — and an alert that never stops is an alert
	people stop reading, including on the day a new one appears in the list.
	"""
	return frappe.get_all(
		ORDER_DOCTYPE,
		filters={"is_chargeback": 1, "chargeback_handled_at": ("is", "not set")},
		pluck="name")


def auctions_ended_unsold() -> list[str]:
	"""Guns whose auction ran out with no winner in the last day.

	Not a fault — the ordinary end of an auction with no bids or an unmet
	reserve — so the sweep marks them quietly and this is where they surface,
	once a day, for somebody to relist or reprice.
	"""
	from ffl_integrations.gunbroker.tasks import AUCTION_UNSOLD_NOTE

	# `_stale_date`, like every other check here: a date built from `nowdate()`
	# touches no document meta, which matters under a patched `get_all`.
	return frappe.get_all(
		"Serial No",
		filters={
			"gb_listing_type": "Auction",
			"gb_sync_error": AUCTION_UNSOLD_NOTE,
			"gb_synced_at": (">=", _stale_date(1)),
		},
		pluck="name")


def stuck_sync() -> list[str]:
	return frappe.get_all(
		ORDER_DOCTYPE,
		filters={"sync_fail_count": (">=", MAX_CONSECUTIVE_FAILURES)},
		pluck="name")


# (category, the function that finds it). Named rather than bound so the table
# resolves at run time: a tuple of function objects captured at import is a
# second binding that a patched or reloaded module never reaches, which makes
# the whole audit untestable in exactly the way that matters.
_CHECKS: tuple[tuple[str, str], ...] = (
	("auction_unsold", "auctions_ended_unsold"),
	("disposed_no_si", "disposed_no_si"),
	("si_no_fee_je", "si_no_fee_je"),
	("si_outstanding", "si_outstanding"),
	("paid_not_disposed", "paid_not_disposed"),
	("status_conflict", "status_conflict"),
	("accrual_on_cancelled_si", "accrual_on_cancelled_si"),
	("chargeback", "chargebacks"),
	("stuck_sync", "stuck_sync"),
)

# Both tables are built when the alert is sent, not when the module is
# imported. `_()` resolves against the language in force where it runs, and a
# worker imports this file once at boot — so a module-level table would freeze
# every alert into whatever language that worker happened to start in.

def _subjects() -> dict[str, str]:
	return {
		"auction_unsold": _("whose auction ended without a sale"),
		"disposed_no_si": _("shipped with no invoice"),
		"si_no_fee_je": _("invoiced with no live fee accrual"),
		"si_outstanding": _("invoiced and still unpaid"),
		"paid_not_disposed": _("paid and not shipped"),
		"status_conflict": _(
			"returned, refunded or cancelled but still booked as revenue"),
		"accrual_on_cancelled_si": _(
			"cancelled with the seller fee still accrued"),
		"chargeback": _("charged back"),
		"stuck_sync": _("failing to sync"),
	}


def _why() -> dict[str, str]:
	return {
		"auction_unsold": _(
			"The auction ran out with no bids, or the reserve was not met. Nothing "
			"is wrong and nothing has been relisted: press Send to GunBroker on "
			"the serial to run it again, as an auction or at a fixed price."),
		"disposed_no_si": _(
			"The guns left under a booked disposition and there is no Sales "
			"Invoice, so the sale is missing from gross receipts and the stock "
			"has gone out against no revenue. Run Create Invoice on the order."),
		"si_no_fee_je": _(
			"The sale is posted and there is no live accrual for GunBroker's "
			"seller fee, so channel profit reads high and the monthly statement "
			"will not reconcile. Either none was ever raised, or one was "
			"reversed or cancelled by hand and the order still points at it — "
			"which also stops an amendment re-accruing. Each line says which."),
		"si_outstanding": _(
			"The invoice is posted and still owed money. Either nobody recorded "
			"what was banked — POS will not invent a receipt — or the payment "
			"came up short of the total."),
		"paid_not_disposed": _(
			"The money is in the bank and nothing has been booked for it. Until "
			"the order is disposed there is no invoice and no receipt, so this "
			"sits off the ledger entirely."),
		"status_conflict": _(
			"GunBroker has unwound this order and the revenue is still on our "
			"books. Returned (12), Refund Order (13) AND Cancelled (6) all count "
			"here — a cancellation after the invoice was posted leaves exactly "
			"the same hole as a refund does. Nothing is reversed automatically, "
			"because which entries are right depends on whether the gun came "
			"back. Book the refund following your GunBroker refund procedure; "
			"each line below carries the Remarks text its Payment Entries need."),
		"accrual_on_cancelled_si": _(
			"The invoice is cancelled and the seller-fee accrual behind it is "
			"still posted, so channel profit reads low and the GunBroker "
			"statement will not reconcile. Automatic reversal failed here — it "
			"is deliberately never allowed to block a cancel — and nothing else "
			"reports a cancelled invoice, which is why this check exists. "
			"Reverse the journal by hand, then have a System Manager clear Fee "
			"Accrual Journal Entry on the order: the field is read-only, and "
			"while it points at a journal an amendment re-accrues nothing."),
		"chargeback": _("The buyer's bank has reclaimed the money."),
		"stuck_sync": _(
			"The order poll has failed on this order repeatedly and has stepped "
			"over it so later sales keep flowing. It will not fix itself."),
	}


# The one check that lists guns rather than orders.
_SERIAL_KINDS = ("auction_unsold",)


def _alert(kind: str, rows: list[str]) -> None:
	noun = _("serial(s)") if kind in _SERIAL_KINDS else _("order(s)")
	notify_admins(
		_("GunBroker: {0} {1} {2}").format(len(rows), noun, _subjects().get(kind, kind)),
		_("{0}\n\nAs at {1}:\n\n{2}").format(
			_why().get(kind, ""), nowdate(), "\n".join(f"  - {r}" for r in rows)),
		dedup_key=f"gb_audit_{kind}", window_minutes=1440)
