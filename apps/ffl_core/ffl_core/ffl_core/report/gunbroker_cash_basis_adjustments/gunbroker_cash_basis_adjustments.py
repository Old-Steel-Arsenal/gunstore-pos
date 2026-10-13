"""GunBroker Cash Basis Adjustments — the schedule that bridges the books to
the return.

The store keeps its GunBroker books on the accrual basis (fees accrued when the
sale posts, the marketplace's sales tax and buyer fee held as liabilities) and
files on the cash basis. Seven items reconcile the two, and the tax preparer
works from this page.

Two things about it are load-bearing.

**It is as-of, not as-now.** Every figure is recomputed at `period_end`, from
dates rather than from current status. That is not a nicety: the schedule is
read weeks or months later — month-end close in the first week of the next
month, the year-end copy in February through April — and by then every order
that was paid-but-undisposed on 31 December has shipped. A query written
against today's status returns an almost empty page and quietly drops the
year's entire year-end float out of gross receipts. Run the same `period_end`
on any day and you get the same schedule.

**The receipt's posting date is the invoice's date, never the deposit date**
A GunBroker order is always paid before it is invoiced, so the money is
in the bank days or weeks before anything reaches the ledger. That gap IS
item 3, and it is also why the bank reconciliation carries a standing pile of
"deposited, not yet on the ledger" items — by design, not as an error. The
preamble says so on the page, because an accountant who reconciles the bank AND
reads the invoices without that sentence counts the same December cheque twice.

Amounts, three of them per order, deliberately not netted:

  revenue portion    goods + freight — the only part that is gross receipts
  collected portion  the marketplace's sales tax and buyer service fee, money
                     held for somebody else, no effect on the return
  payment amount     what the bank actually took in

They coincide only on an exactly-paid order. Items 5 and 7 want the third one;
the cash-basis adjustment wants the first. Both are printed, so nobody has to
subtract one from the other and guess which way round it went.

Lives in ffl_core, and reads GunBroker Order / GunBroker Settings by doctype
NAME behind an existence guard — never by import. ffl_core has to run on a site
where ffl_integrations is absent (same contract as the Sales Report's
marketplace account names, pinned from the integration side by
ffl_integrations/tests/test_gunbroker_report_contract.py).
"""
from __future__ import annotations

import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import flt, getdate

from ffl_core.marketplace_accounts import (
	ACCRUED_FEES_ACCOUNT, MPF_TAX_ACCOUNT, MSF_ACCOUNT, ORDER_DOCTYPE,
	ORDER_REMARK_PREFIX, SETTINGS_DOCTYPE, account_for)

# The two candidate homes for the year-end credit. Which one is the
# accountant's single call; until it is made, prefer the account the
# requirement names first and print whichever was used.
_DEPOSIT_LIABILITIES = ("Customer Deposits", "Customer Advances")


# ------------------------------------------------------------------ the page

def execute(filters=None):
	filters = frappe._dict(filters or {})
	period_start, period_end = _validate(filters)
	company = filters.get("company") or frappe.defaults.get_user_default("Company")
	if not company:
		frappe.throw(_("Company is required — the schedule resolves its accounts per company."))

	orders = _fetch_orders(period_end)
	invoices = _fetch_invoices(company, period_end)
	rows = _assemble(
		accrued=_fetch_accrual_rollforward(company, period_start, period_end),
		liabilities=_fetch_liability_balances(company, period_end),
		orders=orders,
		refunds=_match_refunds(orders, _fetch_refund_entries(company, period_end)),
		invoices=invoices,
		receivable=_fetch_receivable_balances(company, period_end,
			[si["name"] for si in invoices]),
		settlements=_fetch_settlements(company, period_start, period_end),
		period_start=period_start, period_end=period_end,
		debit_account=_deposit_account(),
		credit_account=_credit_account(company))
	return _columns(), rows, _preamble(period_end), None, None


def _validate(filters):
	if not filters.get("period_end"):
		frappe.throw(_("Period End is required — every figure on this schedule is as-of that date."))
	period_end = getdate(filters.period_end)
	if not filters.get("period_start"):
		frappe.throw(_(
			"Period Start is required — items 1 and 6 report movement DURING the "
			"period, and item 3's opening column is the prior period's closing."))
	period_start = getdate(filters.period_start)
	if period_start > period_end:
		frappe.throw(_("Period Start must be on or before Period End"))
	return period_start, period_end


def _preamble(period_end):
	"""The three statements printed at the head of the schedule. Each
	one exists because a specific reader made a specific mistake without it."""
	return (
		'<div class="text-muted" style="line-height:1.6">'
		+ _("<b>1.</b> Every GunBroker receipt posts on the date of its Sales Invoice "
			"(D-16) and is <b>not back-dated</b> to the day the money was banked. "
			"Item 3 below IS that gap. Do not count it again from the bank "
			"statement — the same cheque would be income twice.")
		+ "<br>"
		+ _("<b>2.</b> Items 3, 4, 5 and 7 are recomputed <b>as-of {0}</b>, not from "
			"today's status. Re-running this schedule for the same period end on "
			"any later date returns the same figures — provided no ledger row "
			"dated on or before that period end has been added, cancelled or "
			"re-allocated since (see the si_outstanding alert)."
		).format(period_end)
		+ "<br>"
		+ _("<b>3.</b> Item 3 gives an <b>opening</b> and a <b>closing</b> column "
			"(opening = the prior period's closing, on the same basis). Roll "
			"forward from those two columns directly; it does not depend on last "
			"year's reversing entry having been made.")
		+ "</div>")


def _columns():
	return [
		{"label": _("Item"), "fieldname": "item", "fieldtype": "Int", "width": 50},
		{"label": _("Line"), "fieldname": "label", "fieldtype": "Data", "width": 300},
		{"label": _("Reference"), "fieldname": "reference", "fieldtype": "Data", "width": 260},
		{"label": _("Payment Date"), "fieldname": "payment_date", "fieldtype": "Date", "width": 105},
		{"label": _("Method"), "fieldname": "payment_mode", "fieldtype": "Data", "width": 130},
		{"label": _("Cheque / Ref"), "fieldname": "check_no", "fieldtype": "Data", "width": 110},
		{"label": _("Revenue Portion"), "fieldname": "revenue_portion", "fieldtype": "Currency", "width": 125},
		{"label": _("Collected Portion"), "fieldname": "collected_portion", "fieldtype": "Currency", "width": 130},
		{"label": _("Banked Amount"), "fieldname": "payment_amount", "fieldtype": "Currency", "width": 120},
		{"label": _("Refunded"), "fieldname": "refunded", "fieldtype": "Currency", "width": 105},
		{"label": _("Difference"), "fieldname": "difference", "fieldtype": "Currency", "width": 100},
		{"label": _("Opening"), "fieldname": "opening", "fieldtype": "Currency", "width": 110},
		{"label": _("Accrued"), "fieldname": "accrued", "fieldtype": "Currency", "width": 110},
		{"label": _("Paid"), "fieldname": "paid", "fieldtype": "Currency", "width": 110},
		{"label": _("Closing"), "fieldname": "closing", "fieldtype": "Currency", "width": 115},
		{"label": _("Amount"), "fieldname": "amount", "fieldtype": "Currency", "width": 115},
		{"label": _("Note"), "fieldname": "note", "fieldtype": "Data", "width": 200},
		{"label": _("Kind"), "fieldname": "row_kind", "fieldtype": "Data", "width": 80},
	]


# -------------------------------------------------------------- the predicate

def _in_hand(order, refunds, as_of):
	"""What of this order's cheque is still in the bank on `as_of`.

	Banked less every refund that had POSTED by then. A part refund is not a
	cancellation: taking $100 back off a $1,000 cheque before the gun ships
	leaves $900 in the bank on 31 December, and that $900 is that year's
	income. The old rule was binary — any refund at all and the whole order
	left the schedule — which lost the remaining $900 with nothing said."""
	returned = sum(flt(r.get("paid_amount")) for r in (refunds or [])
		if r.get("posting_date") and getdate(r["posting_date"]) <= getdate(as_of))
	return flt(flt(order.get("payment_amount")) - returned, 2)


def _has_banked_amount(order):
	"""Did the bank actually receive anything on this order?

	An order with a payment date and no figure stays ON the schedule — it is a
	data fault somebody has to fix, and dropping it silently is the one thing
	this file must not do. But it must not be SUMMED, and the reason is not
	symmetry: the revenue portion is derived from `order_total`, which knows
	nothing about what was banked, so an order the bank received nothing for
	would otherwise contribute its entire revenue to the figure an accountant
	copies into the year's gross receipts. Listing a row nobody asked for is a
	nuisance; inflating a tax number is not."""
	return flt(order.get("payment_amount")) > 0


def _refund_revenue(order, refunds, as_of):
	"""How much of an order's refunds came off REVENUE.

	Not a tax apportionment, and the reason is that a ratio is wrong in both
	directions. Either GunBroker has recomputed the order or it has not:

	* **Not recomputed.** The tax GunBroker bills us has not moved by a cent —
	  we still owe all of it — so every dollar the buyer got back was a price
	  concession. It comes off revenue in FULL; carving out a "tax share"
	  under-deducts.
	* **Recomputed**, and the poll has copied the new total back onto the
	  order. The revenue column is then ALREADY net of the refund, and taking
	  anything off again double-counts it.

	What separates the two is on the page already: a recomputed order has a
	deposit larger than the order now is, which is item 3's Difference column.
	So the deduction is the refund less whatever the deposit exceeds the order
	by — which also gets the third case right, returning an overpayment
	(banked 1,050 on a 1,000 order, gave 50 back): nothing about the sale
	changed, so nothing comes off income, where a ratio would have taken 47.25.

	An order with no total deducts nothing, and reaches that by subtraction
	rather than by dividing."""
	returned = flt(order.get("payment_amount")) - _in_hand(order, refunds, as_of)
	over_deposit = max(0.0, flt(order.get("payment_amount"))
		- flt(order.get("order_total")))
	return flt(max(0.0, returned - over_deposit), 2)


def _refund_note(refunds, as_of):
	""""Refunded <date> (<entry>)" for the refunds posted by `as_of`.

	An order that leaves the schedule because it was refunded used to look
	exactly like an order that was never on it: no row at all. From the page
	there is no way to tell "refunded" from "missed", and the schedule is the
	only place either would show."""
	posted = [r for r in (refunds or [])
		if r.get("posting_date") and getdate(r["posting_date"]) <= getdate(as_of)]
	if not posted:
		return ""
	return _("Refunded {0}").format("; ".join(
		f"{r['posting_date']} ({r.get('name') or '?'})"
		for r in sorted(posted, key=lambda r: str(r["posting_date"]))))


def _still_in_hand(order, refunds, as_of):
	"""Item 3, as the accountant defined it — no timezone maths, because the
	date columns are already stored site-local:

	    payment_received_on <= period_end
	      AND (payment_entry empty OR PE.docstatus = 2 OR PE.posting_date > period_end)
	      AND NOT (a refund entry has posted with posting_date <= period_end)

	Threshold zero days. The seven-day `stale_paid_days` figure belongs to the
	daily alarm and must not be reused here: at zero it is "cash in the bank on
	the period end", at seven it silently drops the last week of every period —
	and at a year end, the whole float that the return is short by.

	Only a SUBMITTED receipt settles anything. A cancelled one (after an NSF
	reversal) leaves its link on the order, and reading the link alone would
	take the order off the schedule while the money question is still open
 a draft one has posted nothing at all. A link pointing at a
	receipt that cannot be found is treated the same way — err toward listing,
	since the alternative is dropping receipts silently.

	Cancellation does NOT exclude; a POSTED refund reduces. On
	31 December a cheque banked on the 28th and cancelled on the 30th is still
	money in the bank: cash-basis income of that year, with the refund a
	deduction in the year the cheque is written. A PART refund leaves the rest
	of the cheque in hand and the order on the schedule."""
	received = order.get("payment_received_on")
	if not received or getdate(received) > getdate(as_of):
		return False
	if (order.get("payment_entry")
			and order.get("pe_docstatus") == 1
			and order.get("pe_posting_date")
			and getdate(order["pe_posting_date"]) <= getdate(as_of)):
		return False
	# An order with a payment date and no amount is a data fault, and it stays
	# on the page. Judging it by what is left in hand would drop it silently —
	# indistinguishable from an order that was refunded in full, and the one
	# direction this file must never err in.
	if flt(order.get("payment_amount")) <= 0:
		return True
	return _in_hand(order, refunds, as_of) > 0.01


def _cancel_note(order, as_of):
	""""Cancelled, refund pending" vs "cancelled after the period" — a label,
	never a filter. The date part is taken directly: GunBroker Order timestamps
	are stored site-local, so a cancellation at 23:30 on 31 December
	belongs to December, which is exactly what reading it as UTC would break."""
	cancel = order.get("cancel_date")
	if not cancel:
		return ""
	return (_("Cancelled — refund pending") if getdate(cancel) <= getdate(as_of)
		else _("Cancelled after period end"))


def _match_refunds(orders, entries):
	"""{order name: [refund entries]} keyed off the order number in the refund
	entry's remarks.

	Operators are told (in the refund alerts) to put "GunBroker Order #<id>"
	in the remarks of every refund Payment Entry they key by hand — the same
	marker every GunBroker receipt this system writes already carries, so the
	convention is one the ledger is already full of. The digit guard matters: without it a
	refund on #10011 would take #1001 off the schedule.

	EVERY match is kept, with its amount. Two part refunds have to add up, and
	the amount decides how much of the cheque is still in the bank — an earlier
	version kept only the first date and treated any refund as the whole
	cheque, which lost the remainder silently.

	`re.compile` has its own cache, so there is no second one here."""
	out: dict[str, list] = {}
	for order in orders:
		gb_id = str(order.get("gb_order_id") or "")
		if not gb_id:
			continue
		pattern = re.compile(re.escape(f"{ORDER_REMARK_PREFIX}{gb_id}") + r"(?!\d)")
		for entry in entries:
			if not entry.get("remarks") or not pattern.search(entry["remarks"]):
				continue
			out.setdefault(order["name"], []).append({
				"name": entry.get("name"),
				"posting_date": str(entry.get("posting_date")),
				"paid_amount": flt(entry.get("paid_amount")),
			})
	return out


def _split(order):
	"""The three amounts, plus the gap between the invoice and the cheque.

	`sales_tax_total` is what the marketplace collected — its sales tax AND the
	buyer's service fee, which is why the revenue portion is the total less that
	one figure and not "total less tax less fee".

	The revenue is taken in the PROPORTION actually banked. On the cash basis
	gross receipts cannot exceed the cash received, and a part-paid order was
	reporting the whole invoice's revenue against a smaller deposit — 945
	against 900 banked. Splitting a part payment by the order's own
	revenue-to-tax ratio is the convention this page already uses everywhere
	else, and the remainder arrives in the year it is collected.

	Capped at 1: banking more than the order is worth does not create income,
	it creates something to give back. An order with no total takes nothing —
	its revenue is zero anyway, and the cap is what keeps that a subtraction
	rather than a divide."""
	total = flt(order.get("order_total"))
	collected = flt(order.get("sales_tax_total"))
	banked = flt(order.get("payment_amount"))
	collected_share = min(1.0, banked / total) if total > 0 else 0.0
	return {"revenue_portion": flt((total - collected) * collected_share, 2),
		"collected_portion": collected,
		"payment_amount": banked,
		"difference": flt(banked - total, 2)}


# ------------------------------------------------------------- the seven items

def _section(item, label):
	return {"item": item, "label": label, "row_kind": "section"}


def _build_item1(account, *, opening, credits, debits, paid):
	"""Item 1 — the accrued-fee account in four columns.

	Only "paid this period" is deductible on the cash basis, and a closing
	balance cannot produce it: a month that accrued $57 and paid $120 of last
	month's bill nets to a $63 movement that is neither figure.

	`paid` is NOT every debit to the account. Cancelling a GunBroker invoice
	posts an automatic reversing journal — `Dr Accrued / Cr Marketplace Fees` —
	which debits this account and moves no money at all, and that happens on
	the ordinary path: every price adjustment amends, and every amendment
	cancels first. Counting it would deduct a fee we never paid, and on a
	whole-order cancellation it would deduct a fee GunBroker is about to CREDIT
	us. So the caller passes the debits that had bank money behind them
	separately.

	What is left over is not all reversals, and the variable name below is a
	simplification the note deliberately does not repeat. A settlement where
	GunBroker credits a fee it had charged — `Dr Accrued 57 / Cr Bank 50 /
	Cr Marketplace Fees 7` — leaves 7 of debit with no money behind it, and
	that 7 is a credit note, not a cancelled invoice. Both behave identically
	here (liability down, no cash), so both are netted off "accrued"; the note
	names the amount and says it is one or the other, rather than sending a
	reader to look for cancellations that never happened.

	Netted off rather than hidden — otherwise the accrued figure silently stops
	being the sum of the month's invoices and nothing on the page explains why.
	Closing is derived, so the four columns tie by construction rather than by
	hope."""
	# debits that no bank money stood behind: reversals AND the credited part
	# of a mixed settlement. See the docstring — the name is the common case.
	reversed_ = flt(flt(debits) - flt(paid), 2)
	accrued = flt(flt(credits) - reversed_, 2)
	note = _("Cash basis deducts the Paid column, not the movement.")
	if abs(reversed_) >= 0.01:
		note = _(
			"Accrued is net of {0} debited to this account this period not "
			"backed by bank money — reversals on cancelled invoices, or fees "
			"GunBroker credited back. Neither is Paid, and cash basis deducts "
			"the Paid column, not the movement."
		).format(f"{reversed_:,.2f}")
	return [
		_section(1, _("Accrued GunBroker Fees — roll-forward")),
		{"item": 1, "row_kind": "detail", "label": _("Accrued fee liability"),
		 "reference": account, "opening": flt(opening, 2), "accrued": accrued,
		 "paid": flt(paid, 2), "closing": flt(flt(opening) + accrued - flt(paid), 2),
		 "note": note},
	]


def _build_item2(liabilities):
	"""Item 2 — the two collected-for-somebody-else balances. Agency
	liabilities: no profit-and-loss effect on either basis, listed so the
	closing balance sheet can be tied out and the remittance chased."""
	rows = [_section(2, _("Collected on behalf of the marketplace — closing balances"))]
	for account, balance in liabilities:
		rows.append({"item": 2, "row_kind": "detail",
			"label": _("Collected, not yet remitted"), "reference": account,
			"closing": flt(balance, 2),
			"note": _("Agency liability — no effect on gross receipts.")})
	return rows


def _build_item3(orders, refunds, period_start, period_end):
	"""Item 3 — paid, not yet disposed, on both period ends.

	The union of the two as-of sets, so a single page rolls forward: an order in
	the opening set only was settled during the period; one in the closing set
	only was banked during it; one in both carried across. The subtotal's money
	columns sum the CLOSING set — that is the set items 5 and 7 consume — while
	the opening column totals the prior one."""
	# The prior period's last day — a plain calendar step, kept out of frappe so
	# the whole of item 3 stays a pure function over dicts.
	prior_end = getdate(period_start) - timedelta(days=1)
	rows = [_section(3, _("Paid, not yet disposed (as-of {0})").format(period_end))]
	open_total = close_total = 0.0
	sums = {"revenue_portion": 0.0, "collected_portion": 0.0,
		"payment_amount": 0.0, "refunded": 0.0, "difference": 0.0}
	for order in sorted(orders, key=lambda o: (str(o.get("payment_received_on") or ""),
			str(o.get("gb_order_id") or ""))):
		refund = refunds.get(order.get("name")) or []
		in_open = _still_in_hand(order, refund, prior_end)
		in_close = _still_in_hand(order, refund, period_end)
		if not (in_open or in_close):
			continue
		banked = _has_banked_amount(order)
		split = _split(order)
		split["refunded"] = flt(flt(split["payment_amount"])
			- _in_hand(order, refund, period_end), 2)
		# The money columns describe what this row contributes to the subtotal
		# below. A row that has left the closing set — or that has no banked
		# amount at all — contributes nothing, and printing its full amounts
		# anyway makes the CSV's Revenue Portion column sum to more than the
		# subtotal, with nothing on the page saying so.
		if not in_close or not banked:
			# Refunded survives: it is the explanation of where the opening
			# balance went, and a note reading "Refunded 1,000" beside a column
			# reading 0.00 is a page arguing with itself.
			split = {k: (v if k == "refunded" else 0.0) for k, v in split.items()}
		# A fault row has no amount to be holding, so "what is left in hand"
		# comes out negative once a refund is set against it. That negative went
		# into the two columns the roll-forward is built from.
		open_hand = _in_hand(order, refund, prior_end) if (in_open and banked) else 0.0
		close_hand = _in_hand(order, refund, period_end) if (in_close and banked) else 0.0
		open_total += open_hand
		# Refunded is summed on every listed row, closing or not, because it is
		# printed on every listed row — a column whose rows do not add up to
		# the figure under them is the thing this block exists to prevent.
		sums["refunded"] += flt(split.get("refunded"))
		# `banked` is belt to the braces above: a fault row's split has already
		# been zeroed, so adding it would contribute nothing. Kept so the
		# subtotal does not silently depend on that zeroing still happening.
		if in_close and banked:
			close_total += _in_hand(order, refund, period_end)
			for key in sums:
				if key != "refunded":
					sums[key] += flt(split.get(key))
		# Concatenated, not either-or: a cancelled order that also settled this
		# period is both things, and `or` printed only the first.
		notes = [n for n in (
			_("No amount recorded — a payment date with no figure behind it")
				if flt(order.get("payment_amount")) <= 0 else "",
			_cancel_note(order, period_end),
			_refund_note(refund, period_end),
			# "Was there a refund" is the wrong question — a row can be part
			# refunded in one period and shipped in the next, and this note is
			# the only column that says where the money went. Ask whether a
			# refund is what took it off the list.
			_("Settled during the period") if in_open and not in_close
				and (_in_hand(order, refund, period_end) > 0.01
					or not banked) else "",
		) if n]
		rows.append({
			"item": 3, "row_kind": "detail",
			"label": _("GunBroker Order #{0}").format(order.get("gb_order_id") or "?"),
			"reference": order.get("sales_invoice") or order.get("name") or "",
			"payment_date": order.get("payment_received_on"),
			"payment_mode": order.get("payment_mode_used") or "",
			"check_no": order.get("payment_reference_no") or "",
			**split,
			"opening": flt(open_hand, 2),
			"closing": flt(close_hand, 2),
			"note": "; ".join(notes),
		})
	rows.append({
		"item": 3, "row_kind": "subtotal", "basis": "closing",
		"label": _("Subtotal — closing as-of {0}; Opening column totals the prior period"
			).format(period_end),
		"opening": flt(open_total, 2), "closing": flt(close_total, 2),
		**{k: flt(v, 2) for k, v in sums.items()},
		"note": _("Revenue Portion is the gross-receipts figure; Banked Amount is what the bank took in."),
	})
	return rows


def _build_item4(invoices, receivable, period_end):
	"""Item 4 — invoiced and not yet collected, also as-of.

	Deliberately NOT `Sales Invoice.outstanding_amount`: that field is today's
	balance, so an invoice settled in January would show nil on a 31 December
	schedule and the receivable would vanish from the year it belonged to.

	The balance comes from the PAYMENT ledger — not from Payment Entry
	allocations, and not from the general ledger. A receipt is not the only
	thing that clears an invoice (the bad-debt procedure uses a Journal Entry,
	a return uses a credit note), and after a reconciliation the general ledger
	no longer holds the link at all while the payment ledger does; see
	`_fetch_receivable_balances`. Item 4 is what the accountant SUBTRACTS from
	accrual income, so an inflated one understates cash-basis receipts — the
	opposite of the direction everything else here errs in, and why it is worth
	reading a second table for."""
	rows = [_section(4, _("Invoiced, not yet collected (as-of {0})").format(period_end))]
	total = 0.0
	for si in sorted(invoices, key=lambda s: (str(s.get("posting_date") or ""), s.get("name") or "")):
		if getdate(si.get("posting_date")) > getdate(period_end):
			continue
		balance = flt(receivable.get(si.get("name")), 2)
		if abs(balance) < 0.01:
			continue
		total += balance
		rows.append({"item": 4, "row_kind": "detail",
			"label": _("Sales Invoice {0}").format(si.get("name")),
			"reference": si.get("gunbroker_order") or "",
			"payment_date": si.get("posting_date"), "closing": balance})
	rows.append({"item": 4, "row_kind": "subtotal", "basis": "closing",
		"label": _("Subtotal — receivable at {0}").format(period_end),
		"closing": flt(total, 2),
		"note": _("Rebuilt from ERPNext's Payment Ledger — the same rows the "
			"Accounts Receivable report reads — so a reconciled receipt, a "
			"bad-debt journal and a credit note all clear it.")})
	return rows


def _banked(orders):
	"""The orders that belong in a total. One filter, both readers — items 5
	and 7 diverging on which orders they count is the shape of fault this
	round was raised for."""
	return [o for o in orders if _has_banked_amount(o)]


def _build_item5(closing_orders, refunds, period_end, deposit_account=None):
	"""Item 5 — what is sitting in the deposit account on the period end.

	One line per payment method rather than one lump: phase one is cheques into
	the bank account, phase two adds a card clearing account, and the two are
	different accounts that must never be added together. The amount is what
	the bank took in, NOT the order total — on a short or over payment
	those are different numbers and it is the bank that is being reconciled.

	GROSS of refunds, deliberately. A refund cheque posts on the day it is
	written — Dr Debtors / Cr Bank — so the LEDGER is already down by it, while
	the receipt behind the deposit does not reach the ledger until the invoice
	does. What sits in this item is the gap between the two, and that gap is
	the deposit itself. Netting the refund off here subtracts it a second time,
	and breaks the one thing the subtotal below promises: that it equals item
	3's Banked column. The refunds have their own column there and their own
	line in item 7.

	Each line names the account the money is sitting in. The requirement asks for
	"the GunBroker items in transit in `check_deposit_account`", and whoever
	reconciles a bank statement against this figure has to know which of the
	several bank-ish accounts on the chart that is without opening Settings to
	find out. Named in a way that stays true when the card path wakes up: cheque
	deposits land in the account below, another method lands in its own
	clearing account, and the two are never added together."""
	rows = [_section(5, _("Held in the deposit account — GunBroker items in transit"))]
	by_mode: dict[str, float] = {}
	for order in _banked(closing_orders):
		mode = order.get("payment_mode_used") or _("(unrecorded)")
		by_mode[mode] = flt(by_mode.get(mode)) + flt(order.get("payment_amount"))
	whereabouts = (
		_("Cheque and money-order deposits land in {0}; another method sits in "
			"its own clearing account.").format(deposit_account)
		if deposit_account else
		_("No deposit account is set — fill in Check Deposit Account in "
			"GunBroker Settings."))
	for mode in sorted(by_mode):
		rows.append({"item": 5, "row_kind": "detail",
			"label": _("Banked, ledger not yet posted"), "reference": mode,
			"closing": flt(by_mode[mode], 2),
			"note": _("{0} By design (D-16) — the standing bank-reconciliation "
				"item.").format(whereabouts)})
	rows.append({"item": 5, "row_kind": "subtotal", "basis": "closing",
		"label": _("Subtotal — equals item 3's Banked Amount"),
		"closing": flt(sum(by_mode.values()), 2)})
	return rows


def _build_item6(settlements):
	"""Item 6 — cash actually paid to the marketplace during the period, in two
	lines because the money leaves by two routes: the sales-tax statement is
	remitted by us, the fee bill is auto-debited from the account on file.

	Each line is the SHARE of the entry's bank credit attributable to that
	liability, capped at what the entry actually settled of it. Not the bank
	credit itself: a cheque paying the fee bill and the office rent on one
	journal moves 157 while settling 57 of marketplace liability, and reporting
	157 here would say the marketplace received cash it never did. Within the
	cap the split is by the debits, so the two lines add back to the money that
	left the bank for the marketplace's account."""
	rows = [_section(6, _("Cash paid to the marketplace during the period"))]
	tax_paid = fee_paid = 0.0
	for je in settlements:
		tax, fee = flt(je.get("tax_debit")), flt(je.get("fee_debit"))
		bank = flt(je.get("bank_credit"))
		base = tax + fee
		if base <= 0:
			continue
		# Capped at the liabilities this voucher actually settled. One cheque
		# can pay the fee bill and the office rent on the same journal, and
		# attributing all of it here reports cash the marketplace never got —
		# the same "two deductions on one page" item 1's own cap removes,
		# arriving from the other side.
		bank = min(bank, base)
		tax_paid += bank * tax / base
		fee_paid += bank * fee / base
	for label, amount in ((_("Sales tax statement remitted"), tax_paid),
			(_("Fee statement debited"), fee_paid)):
		rows.append({"item": 6, "row_kind": "detail",
			"label": _("Settlement entry's bank credit, share for this liability"),
			"reference": label, "amount": flt(amount, 2)})
	rows.append({"item": 6, "row_kind": "subtotal", "basis": "period",
		"label": _("Subtotal — total cash out to the marketplace"),
		"amount": flt(tax_paid + fee_paid, 2)})
	return rows


def _build_item7(closing_orders, refunds, period_end, *, debit_account,
		credit_account):
	"""Item 7 — the year-end entry (UD-5 ④), and the adjustment figure, printed
	as two separate lines because they are two different numbers.

	The entry is for what the bank holds: the whole cheque is sitting there, so
	recording only the revenue half would leave the account short by the
	marketplace's tax. The cash-basis adjustment to gross receipts is the
	revenue half alone. They are equal only when every order was paid to the
	penny, and on the one occasion they are not, an accountant asked to
	"subtract the tax" will subtract the wrong tax.

	The entry is GROSS of refunds, and that is provable rather than a
	preference. A refund cheque has already posted Dr Debtors / Cr Bank, so
	before this entry the ledger's bank is short by it. Post the entry at the
	gross deposit and the bank reads deposit less refund, which is what is
	there; post it net and the bank reads deposit less TWICE the refund.

	The refunds then get a line of their own, because nothing else on the
	schedule takes them off: a refund touches Debtors, so it is in neither the
	accrual income nor item 4. Left invisible it lands in the wrong year in
	both directions — this year over, next year under."""
	orders = _banked(closing_orders)
	banked = flt(sum(flt(o.get("payment_amount")) for o in orders), 2)
	gross_revenue = flt(sum(_split(o)["revenue_portion"] for o in orders), 2)
	refunded = flt(sum(
		flt(o.get("payment_amount")) - _in_hand(
			o, (refunds or {}).get(o.get("name")), period_end)
		for o in orders), 2)
	refund_revenue = flt(sum(_refund_revenue(
		o, (refunds or {}).get(o.get("name")), period_end)
		for o in orders), 2)
	revenue = flt(gross_revenue - refund_revenue, 2)
	rows = [_section(7, _("Year-end entry for cash held against undelivered orders"))]
	rows.append({"item": 7, "row_kind": "detail",
		"label": _("Journal Entry to post at year end, reversed on day one of the next year"),
		# The credit side is a DEFAULT, and says so: which liability it belongs
		# in is the accountant's single call and has not been made yet.
		"reference": _("Dr {0} / Cr {1} (default — awaiting the accountant's choice)").format(
			debit_account or _("<deposit account — set it in GunBroker Settings>"),
			credit_account or _("<Customer Deposits — the accountant picks this once>")),
		"amount": banked,
		"note": _("Amount is what the bank took in, not the order total.")})
	rows.append({"item": 7, "row_kind": "detail",
		"label": _("Cash-basis adjustment to gross receipts"),
		"reference": _("Revenue portion, net of refunds — add to gross receipts "
			"for the year"),
		"amount": revenue,
		"note": _("{0} of revenue on these orders, less {1} given back in "
			"refunds by the period end. The collected tax is in neither figure "
			"— it is somebody else's money."
		).format(f"{gross_revenue:,.2f}", f"{refund_revenue:,.2f}")})
	rows.append({"item": 7, "row_kind": "detail",
		"label": _("Less: refunds paid on these orders by period end"),
		"reference": _("Already on the ledger (Dr Debtors / Cr Bank) — the "
			"entry above is gross of it"),
		"amount": refunded,
		"note": _("Gross, as it left the bank. {0} of it comes off the revenue "
			"line above. A refund GunBroker has not recomputed is a price "
			"concession and comes off in full — the tax it bills us has not "
			"moved. One it HAS recomputed is already out of the revenue "
			"figure, and shows up here as a deposit larger than the order "
			"(item 3's Difference column); that part comes off nothing."
		).format(f"{refund_revenue:,.2f}")})
	return rows


def _assemble(*, accrued, liabilities, orders, refunds, invoices, receivable,
		settlements, period_start, period_end, debit_account, credit_account):
	"""All seven items, always all seven — an empty period prints seven
	headings with nothing under them, which reads as "nothing to adjust". A
	section that disappears when it is empty reads as a section somebody
	forgot, and the reader cannot tell the two apart."""
	item3 = _build_item3(orders, refunds, period_start, period_end)
	closing = [o for o in orders
		if _still_in_hand(o, refunds.get(o.get("name")) or [], period_end)]
	return [
		*_build_item1(accrued.get("account"), opening=flt(accrued.get("opening")),
			credits=flt(accrued.get("credits")), debits=flt(accrued.get("debits")),
			paid=flt(accrued.get("paid"))),
		*_build_item2(liabilities),
		*item3,
		*_build_item4(invoices, receivable, period_end),
		*_build_item5(closing, refunds, period_end,
			deposit_account=debit_account),
		*_build_item6(settlements),
		*_build_item7(closing, refunds, period_end, debit_account=debit_account,
			credit_account=credit_account),
	]


# ----------------------------------------------------------------- data fetch

def _gb_installed():
	return frappe.db.table_exists(ORDER_DOCTYPE)


def _fetch_orders(period_end):
	"""Every GunBroker order that had been paid by the period end, with its
	receipt's own posting date and docstatus joined on.

	Filtered on `payment_received_on <= period_end` and nothing else: the
	as-of predicate does the rest in Python, where it is testable and where the
	receipt-docstatus rule cannot be lost in a join.

	No company filter, unlike every other query here: GunBroker Order has no
	company column — one site, one company, one GunBroker account — so there is
	nothing to filter on. That is a fact about the doctype, not an omission;
	said out loud so the next reader does not add a column that is not there."""
	if not _gb_installed():
		return []
	return frappe.db.sql("""
		SELECT o.name, o.gb_order_id, o.sales_invoice, o.payment_received_on,
		       o.payment_entry, o.order_total, o.sales_tax_total, o.payment_amount,
		       o.payment_mode_used, o.payment_reference_no, o.cancel_date,
		       pe.posting_date AS pe_posting_date, pe.docstatus AS pe_docstatus
		FROM `tabGunBroker Order` o
		LEFT JOIN `tabPayment Entry` pe ON pe.name = o.payment_entry
		WHERE o.payment_received_on IS NOT NULL
		  AND o.payment_received_on <= %(period_end)s
	""", {"period_end": period_end}, as_dict=True)


def _fetch_refund_entries(company, period_end):
	"""Pay-type entries to a CUSTOMER that could be a GunBroker refund.

	The party filter is not cosmetic. Paying GunBroker's own fee bill by
	Payment Entry — supplier side — and typing the order number into the
	remarks would otherwise read as a refund to the buyer, and take a live
	order off the schedule: cash-basis receipts understated, with nothing
	anywhere saying why.

	Narrowed by the remarks marker in SQL and matched exactly (digit-guarded)
	in Python; the amount comes back too, because a part refund leaves the rest
	of the cheque in the bank."""
	return frappe.db.sql("""
		SELECT name, posting_date, paid_amount, remarks
		FROM `tabPayment Entry`
		WHERE docstatus = 1 AND payment_type = 'Pay'
		  AND party_type = 'Customer'
		  AND company = %(company)s
		  AND posting_date <= %(period_end)s
		  AND remarks LIKE %(marker)s
	""", {"company": company, "period_end": period_end,
		"marker": f"%{ORDER_REMARK_PREFIX}%"}, as_dict=True)


def _fetch_invoices(company, period_end):
	if not frappe.db.has_column("Sales Invoice", "gunbroker_order"):
		return []
	return frappe.get_all("Sales Invoice",
		filters={"docstatus": 1, "company": company,
			"gunbroker_order": ["is", "set"],
			"posting_date": ["<=", period_end], "is_return": 0},
		fields=["name", "posting_date", "gunbroker_order"])


def _fetch_receivable_balances(company, period_end, invoice_names):
	"""{Sales Invoice: receivable balance as at the period end}.

	Read from the PAYMENT ledger, not the general ledger, and the difference
	shows up on the one path the part-refund steps send people down.

	Reconciling a receipt against an amended invoice (after a partial refund,
	the original payment is re-allocated to the amended invoice) goes
	through `reconcile_against_document`, which DELETES that receipt's
	payment-ledger rows and rebuilds them from the re-saved document, which now
	carries the invoice reference. It does not rebuild `tabGL Entry`. The GL
	row keeps whatever `against_voucher` it was posted with, and for a receipt
	unlinked when its invoice was cancelled that is nothing at all. So after a
	price adjustment the invoice is paid, the payment ledger says so, and the
	general ledger holds no row tying the two together: read from GL this
	reported the whole invoice as still owed. Item 4 is what gets subtracted
	from accrual income, so that understated cash-basis receipts by the price
	of the gun.

	The payment ledger is not the narrower source either. Its rows come from
	`create_payment_ledger_entry`, which takes `against_voucher_no` as
	`gle.against_voucher or gle.voucher_no` and runs from `make_gl_entries` for
	every voucher with party rows — so the invoice itself, its receipts, a
	bad-debt Journal Entry carrying Reference Type / Name (case 5 requires it
	for exactly this reason) and a credit note all land there.

	`delinked` rather than `is_cancelled`: this table keeps a cancelled
	voucher's rows and marks them. Same rule item 3 applies to an order's own
	receipt link, spelled the way this table spells it.

	Asked about the invoices on this schedule and no others. There is no lower
	date bound — a balance as-of a date has to see every row before it — so an
	unrestricted version groups every invoice the company has ever had a
	receivable row against, growing every year, to read a handful of GunBroker
	ones out of it."""
	if not invoice_names:
		return {}
	return {r["invoice"]: flt(r["balance"]) for r in frappe.db.sql("""
		SELECT ple.against_voucher_no AS invoice,
		       COALESCE(SUM(ple.amount), 0) AS balance
		FROM `tabPayment Ledger Entry` ple
		WHERE ple.against_voucher_type = 'Sales Invoice'
		  AND ple.against_voucher_no IN %(invoices)s
		  AND ple.company = %(company)s
		  AND ple.account_type = 'Receivable'
		  AND ple.delinked = 0
		  AND ple.posting_date <= %(period_end)s
		GROUP BY ple.against_voucher_no
	""", {"company": company, "period_end": period_end,
		"invoices": tuple(invoice_names)}, as_dict=True)}


def _account_balance(account, upto):
	"""Credit-positive balance of a liability account at a date."""
	if not account:
		return 0.0
	row = frappe.db.sql("""
		SELECT COALESCE(SUM(credit - debit), 0) AS bal FROM `tabGL Entry`
		WHERE account = %(account)s AND is_cancelled = 0 AND posting_date <= %(upto)s
	""", {"account": account, "upto": upto}, as_dict=True)
	return flt(row[0]["bal"]) if row else 0.0


def _fetch_accrual_rollforward(company, period_start, period_end):
	"""The period's movement on the accrued-fee account, with the debits split.

	`paid` is the SHARE of each debit that bank money stood behind, not the
	whole debit. Asked of the ledger rather than of the journal tables on
	purpose: it is the same question `_fetch_settlements` asks, and asking it
	in GL terms means a fee bill settled by Payment Entry counts too, while an
	automatic reversing journal (which has no bank side, ever) never can.

	A voucher-level yes/no is not enough, and the case is a real one. GunBroker
	can credit a fee on the same bill it charges others: `Dr Accrued 57 /
	Cr Bank 50 / Cr Marketplace Fees 7` settles 57 of liability with 50 of
	money. Item 6 already apportions the bank credit that way, so a boolean
	here puts two different deductions on the same page. The share is capped at
	the debit — one cheque can settle the fee bill and something unrelated —
	and a voucher with no GunBroker debit divides by NULL, which sums as
	nothing rather than raising.

	Credits and debits come back raw. The netting belongs in `_build_item1`,
	where it is visible on the page and covered by tests; a query that pre-nets
	makes the reversal disappear from the schedule entirely."""
	account = account_for(company, ACCRUED_FEES_ACCOUNT)
	if not account:
		return {"account": _("{0} — not created yet").format(ACCRUED_FEES_ACCOUNT),
			"opening": 0.0, "credits": 0.0, "debits": 0.0, "paid": 0.0}
	liabilities = [a for a in (account, account_for(company, MPF_TAX_ACCOUNT),
		account_for(company, MSF_ACCOUNT)) if a]
	movement = frappe.db.sql("""
		SELECT COALESCE(SUM(gl.credit), 0) AS credits,
		       COALESCE(SUM(gl.debit), 0) AS debits,
		       COALESCE(SUM(LEAST(gl.debit,
		             gl.debit * COALESCE(v.bank_credit, 0) / NULLIF(v.gb_debit, 0)
		           )), 0) AS paid
		FROM `tabGL Entry` gl
		LEFT JOIN (
		      SELECT g.voucher_type, g.voucher_no,
		             SUM(CASE WHEN acc.account_type IN ('Bank', 'Cash')
		                      THEN g.credit ELSE 0 END) AS bank_credit,
		             SUM(CASE WHEN g.account IN %(liabilities)s
		                      THEN g.debit ELSE 0 END) AS gb_debit
		      FROM `tabGL Entry` g
		      JOIN `tabAccount` acc ON acc.name = g.account
		      WHERE g.is_cancelled = 0 AND g.company = %(company)s
		        AND g.posting_date BETWEEN %(start)s AND %(end)s
		      GROUP BY g.voucher_type, g.voucher_no
		    ) v ON v.voucher_type = gl.voucher_type AND v.voucher_no = gl.voucher_no
		WHERE gl.account = %(account)s AND gl.is_cancelled = 0
		  AND gl.posting_date BETWEEN %(start)s AND %(end)s
	""", {"account": account, "company": company, "start": period_start,
		"end": period_end, "liabilities": tuple(liabilities)}, as_dict=True)
	row = movement[0] if movement else {}
	return {"account": account,
		"opening": _account_balance(account, getdate(period_start) - timedelta(days=1)),
		"credits": flt(row.get("credits")), "debits": flt(row.get("debits")),
		"paid": flt(row.get("paid"))}


def _fetch_liability_balances(company, period_end):
	out = []
	for bare in (MPF_TAX_ACCOUNT, MSF_ACCOUNT):
		account = account_for(company, bare)
		if account:
			out.append((account, _account_balance(account, period_end)))
	return out


def _fetch_settlements(company, period_start, period_end):
	"""Journal Entries in the period that debited a GunBroker liability, with
	the bank money that left on the same voucher.

	Bank-side is identified by account TYPE (Bank/Cash) rather than by name:
	the settlement may be drawn on any operating account, and hard-coding one
	would silently report zero the first time somebody pays from another."""
	accounts = [a for a in (account_for(company, MPF_TAX_ACCOUNT),
		account_for(company, MSF_ACCOUNT),
		account_for(company, ACCRUED_FEES_ACCOUNT)) if a]
	if not accounts:
		return []
	tax_account = account_for(company, MPF_TAX_ACCOUNT)
	return frappe.db.sql("""
		SELECT je.name AS voucher, je.posting_date,
		       COALESCE(SUM(CASE WHEN jea.account = %(tax)s THEN jea.debit_in_account_currency END), 0) AS tax_debit,
		       COALESCE(SUM(CASE WHEN jea.account IN %(accounts)s AND jea.account != %(tax)s
		                         THEN jea.debit_in_account_currency END), 0) AS fee_debit,
		       COALESCE(SUM(CASE WHEN acc.account_type IN ('Bank', 'Cash')
		                         THEN jea.credit_in_account_currency END), 0) AS bank_credit
		FROM `tabJournal Entry` je
		JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
		JOIN `tabAccount` acc ON acc.name = jea.account
		WHERE je.docstatus = 1 AND je.company = %(company)s
		  AND je.posting_date BETWEEN %(start)s AND %(end)s
		  AND je.name IN (
		      SELECT parent FROM `tabJournal Entry Account`
		      WHERE account IN %(accounts)s AND debit_in_account_currency > 0)
		GROUP BY je.name, je.posting_date
	""", {"company": company, "start": period_start, "end": period_end,
		"accounts": tuple(accounts), "tax": tax_account or ""}, as_dict=True)


def _deposit_account():
	"""The bank account GunBroker cheques are deposited into, read off the
	integration's Settings by name. Absent integration → None, and item 7
	prints a placeholder telling the reader where to set it."""
	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return None
	return frappe.db.get_single_value(SETTINGS_DOCTYPE, "check_deposit_account")


def _credit_account(company):
	for bare in _DEPOSIT_LIABILITIES:
		account = account_for(company, bare)
		if account:
			return account
	return None
