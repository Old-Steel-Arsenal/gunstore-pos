"""After the ticket: a negative trade-in ticket merged, cancelled or returned.

A negative trade-in ticket (compliance/trade_in_credit.negative_trade_in_ok) ends with the store paying the
customer the difference; cash_drawer.record_trade_in_payout books that at submit as one Cash Drawer Entry,
Dr Debtors (the customer) / Cr Cash or the bank. Three later events keep that payment and the ticket in step:

* Merge. The ticket is consolidated at once (api/pos_consolidate) into a Sales Invoice whose outstanding is
  minus the payout. On that invoice's submit the payout's Debtors line is set against it with ERPNext's own
  reconcile_against_document (what Payment Reconciliation does), so the invoice reads Paid and the customer
  neither owes nor is owed anything.
* Cancel. Cancelling the ticket voids the payout first (the cash is back in the drawer's books), refused once
  a drawer count relied on it. ERPNext makes the merged invoice go first; cancelling it leaves the payout with
  the ticket (ERPNext unlinks it from the invoice), so a ticket merged again gets it reconciled again. The
  trade-in acquisitions and their Material Receipts are never touched: those guns were bought.
* Return. Only goods come back. The trade-in credit lines are dropped from the return (the guns stay in stock
  and in the bound book, the difference was paid out), and the return refunds only the sales tax the ticket
  collected: none, since a ticket that went negative had no taxable base left.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.cash_drawer import trade_in_payouts
from ffl_core.setup.trade_in_credit import TRADE_IN_CREDIT_ITEM


def _merged_tickets(doc) -> set[str]:
	"""The POS Invoices a consolidated Sales Invoice was merged from (each copied row names its ticket)."""
	return {r.get("pos_invoice") for r in (doc.get("items") or []) if r.get("pos_invoice")}


# --------------------------------------------------------------------------- merge


def keep_merged_change(doc, method: str | None = None) -> None:
	"""Sales Invoice validate: a consolidated invoice carrying a negative trade-in ticket gives the change its
	tickets gave, nothing more.

	ERPNext re-derives a merged invoice's change as paid - total (taxes_and_totals.calculate_change_amount). That
	is the tickets' own change only while every ticket was paid in full; a negative ticket takes no tender (the
	store pays the customer through its payout entry), so in a batch with any cash sale — the shift close merges
	a customer's tickets together — the re-derived change would book the payout a second time, out of the cash
	account. The merged change is the sum of the tickets' change, and the outstanding follows from it."""
	if not int(doc.get("is_consolidated") or 0) or int(doc.get("is_return") or 0):
		return
	tickets = _merged_tickets(doc)
	if not tickets:
		return
	rows = frappe.get_all("POS Invoice", filters={"name": ["in", list(tickets)]},
		fields=["grand_total", "change_amount", "base_change_amount"])
	if not any(flt(r.grand_total) < 0 for r in rows):
		return
	change = flt(sum(flt(r.change_amount) for r in rows), doc.precision("change_amount"))
	if abs(change - flt(doc.change_amount)) < 0.005:
		return
	doc.change_amount = change
	doc.base_change_amount = flt(sum(flt(r.base_change_amount) for r in rows), doc.precision("base_change_amount"))
	# ERPNext's own figure (taxes_and_totals.calculate_outstanding_amount), with the corrected change.
	total = flt(doc.rounded_total) or flt(doc.grand_total)
	doc.outstanding_amount = flt(
		total - flt(doc.get("total_advance")) - flt(doc.write_off_amount) - flt(doc.paid_amount) + change,
		doc.precision("outstanding_amount"))


def reconcile_payout_on_merge(doc, method: str | None = None) -> None:
	"""Sales Invoice on_submit: set each merged ticket's payout against this consolidated invoice.

	The invoice's GL leaves the customer's Debtors at minus the payout and the payout's Journal Entry at plus
	it, unallocated; reconciling the Journal Entry row against the invoice brings the invoice's outstanding
	to what the customer really owes (0 for a lone negative ticket) and marks it Paid. Idempotent: a row that
	is already set against an invoice is left alone. Never blocks the merge (the merge takes a sold gun out of
	stock): a payout that cannot be matched, or a reconcile that fails (rolled back to before it), is logged
	for the accountant instead — the customer's balance is still right, only the pairing is missing."""
	if not int(doc.get("is_consolidated") or 0) or int(doc.get("is_return") or 0):
		return
	payouts = trade_in_payouts(_merged_tickets(doc))
	if not payouts:
		return
	args = []
	for p in payouts:
		# The payout's own Debtors line: its customer, on the ticket's Debtors account (the account the ticket was booked to).
		row = frappe.db.get_value("Journal Entry Account", {
			"parent": p.journal_entry, "parenttype": "Journal Entry", "account": p.debit_account,
			"party_type": "Customer", "party": p.customer, "debit_in_account_currency": [">", 0],
			"reference_name": ["is", "not set"],
		}, ["name", "debit_in_account_currency"], as_dict=True)
		if row and (p.debit_account, p.customer) == (doc.debit_to, doc.customer):
			amount = flt(row.debit_in_account_currency)
			args.append(frappe._dict(
				voucher_type="Journal Entry", voucher_no=p.journal_entry, voucher_detail_no=row.name,
				against_voucher_type=doc.doctype, against_voucher=doc.name, account=doc.debit_to,
				party_type="Customer", party=doc.customer, dr_or_cr="debit_in_account_currency",
				unreconciled_amount=amount, unadjusted_amount=amount, allocated_amount=amount,
				difference_amount=0, exchange_rate=1))
		elif not frappe.db.exists("Journal Entry Account", {
				"parent": p.journal_entry, "reference_type": doc.doctype, "reference_name": doc.name}):
			frappe.log_error(
				title=f"Trade-in payout {p.name} not reconciled with {doc.name}",
				message=(f"Payout line {p.customer} on {p.debit_account} in {p.journal_entry}"
					f"{' (already set against another voucher)' if not row else ''}; invoice {doc.customer} "
					f"on {doc.debit_to}. Pair them in Payment Reconciliation."))
	if args:
		_reconcile(doc, args)


def _reconcile(doc, args) -> None:
	from erpnext.accounts.utils import reconcile_against_document

	frappe.db.savepoint("trade_in_payout_reconcile")
	try:
		reconcile_against_document(args)
	except Exception:
		frappe.db.rollback(save_point="trade_in_payout_reconcile")
		frappe.log_error(title=f"Trade-in payout not reconciled with {doc.name}", message=frappe.get_traceback())
		return
	finally:
		frappe.flags.ignore_party_validation = False  # reconcile_against_document leaves it set when it throws
	# reconcile_against_document wrote the new outstanding/status to the database; keep this copy in step for
	# the on_submit hooks after this one.
	doc.outstanding_amount, doc.status = frappe.db.get_value(doc.doctype, doc.name, ["outstanding_amount", "status"])


# --------------------------------------------------------------------------- cancel


def void_payout_on_cancel(doc, method: str | None = None) -> None:
	"""POS Invoice before_cancel: the money paid to the customer goes back first (cash_drawer.
	void_trade_in_payout), refused once a drawer count relied on it. ERPNext's own before_cancel runs before
	this one, so a ticket whose merged invoice is still live is refused there and never gets here."""
	if int(doc.get("is_return") or 0) or flt(doc.get("grand_total")) >= 0:
		return
	from ffl_core.cash_drawer import void_trade_in_payout

	void_trade_in_payout(doc.name)


def refuse_merged_cancel_after_count(doc, method: str | None = None) -> None:
	"""Sales Invoice on_cancel (first, so the whole cancel rolls back): a consolidated invoice holding a ticket
	whose payout a drawer count already relied on cannot be cancelled. The ticket itself can no longer be
	cancelled then, and an invoice cancelled alone would leave that ticket half undone (sale reversed, payout
	still out). on_cancel because the merge log cancels its invoice with ignore_validate (no before_cancel)."""
	if not int(doc.get("is_consolidated") or 0) or int(doc.get("is_return") or 0):
		return
	from ffl_core.cash_drawer import counted_since

	for p in trade_in_payouts(_merged_tickets(doc)):
		if counted_since(p.company, p.creation):
			frappe.throw(_(
				"Ticket {0} paid the customer for trade-ins and a drawer count was taken after that, so it cannot be "
				"cancelled any more. Ask a manager to undo the count first, or tell your accountant.").format(p.invoice))


# --------------------------------------------------------------------------- return


def refuse_desk_return_of_negative_ticket(doc, method: str | None = None) -> None:
	"""Sales Invoice before_validate: no Desk return / credit note against a merged invoice that holds a negative
	trade-in ticket. ERPNext's own Return button refuses consolidated invoices, but a credit note typed in Desk
	with Return Against would refund the trade-in credit and tax the ticket never collected. The store does not
	take such returns; the register ticket is where a return starts, goods only. A
	consolidated credit note (a register return merged) is that path and passes."""
	if not int(doc.get("is_return") or 0) or int(doc.get("is_consolidated") or 0) or not doc.get("return_against"):
		return
	if not frappe.db.get_value("Sales Invoice", doc.return_against, "is_consolidated"):
		return
	ticket = frappe.db.sql(
		"""SELECT pi.name FROM `tabPOS Invoice` pi
		WHERE pi.name IN (SELECT sii.pos_invoice FROM `tabSales Invoice Item` sii WHERE sii.parent = %s)
		  AND pi.grand_total < 0 AND IFNULL(pi.is_return, 0) = 0 LIMIT 1""", doc.return_against)
	if ticket:
		frappe.throw(_(
			"{0} holds register ticket {1}, where the store paid the customer for trade-ins. It cannot be returned "
			"or credited from here: the store does not take returns of such a sale this way. Return the goods from "
			"the register ticket if a manager agrees.").format(doc.return_against, ticket[0][0]),
			title=_("No credit note for a trade-in payout sale"))


def _negative_original(doc) -> str | None:
	"""The ticket a POS return gives goods back from, when that ticket was a negative trade-in ticket."""
	if not int(doc.get("is_return") or 0) or not doc.get("return_against"):
		return None
	row = frappe.db.get_value("POS Invoice", doc.return_against, ["grand_total", "is_return"], as_dict=True)
	return doc.return_against if row and not row.is_return and flt(row.grand_total) < 0 else None


def return_goods_only(doc, method: str | None = None) -> None:
	"""POS Invoice before_validate: a return of a negative trade-in ticket gives back goods, and only the tax
	that ticket collected. Done before ERPNext totals the return, so its payment checks see the real refund.

	* Its trade-in credit lines are dropped: the store keeps those guns (stock, bound book) and already paid
	  the customer the difference.
	* Its sales tax rows go (freight kept): such a ticket collected no sales tax (its credit exceeded
	  the taxable goods), so the return refunds none, not the rate times the goods it gives back. A POS invoice
	  only refills its template when the template is set, so the cleared tax stays cleared through validate.
	  lazy: only the collected-nothing case is handled (the only one a negative ticket produces); cap a partial
	  refund here if a negative ticket could ever collect tax.
	* It carries none of the ticket's payout facts (method / reference / entry): the payout was the sale's.
	* When this pass changed the cart, a draft's refund — set against the uncorrected cart (the register seeds
	  its default tender with the cart total: +4,885 with the credit lines still in) — is set to the corrected
	  total on that tender; nobody has chosen a refund yet then (the POS saves on Checkout, before the payment
	  screen). A cart that was already goods-only keeps the refund the cashier chose. On submit nothing is
	  changed: a refund set against the uncorrected cart is refused, by name."""
	before = _correct_return(doc)
	if before is None:
		return
	total = flt(doc.get("rounded_total")) or flt(doc.get("grand_total"))
	if abs(before - total) < 0.005:
		return  # already goods-only: the refund is the cashier's (ERPNext checks it)
	rows = doc.get("payments") or []
	refund = flt(sum(flt(p.amount) for p in rows), 2)
	if not rows or abs(refund - total) < 0.005:
		return
	if int(doc.get("docstatus") or 0) == 1:
		frappe.throw(_("This return gives back the goods only: the refund is {0}, not {1}. The trade-in lines "
			"and their tax are not returned.").format(frappe.format(total, {"fieldtype": "Currency"}),
			frappe.format(refund, {"fieldtype": "Currency"})), title=_("Refund the goods only"))
	tender = next((p for p in rows if p.get("default")), rows[0])
	rate = flt(doc.get("conversion_rate")) or 1
	for p in rows:
		p.amount = p.base_amount = 0
	tender.amount = total
	tender.base_amount = flt(total * rate, 2)
	doc.paid_amount = total
	doc.base_paid_amount = flt(total * rate, 2)


def correct_return_cart(doc, method: str | None = None, *args, **kwargs) -> None:
	"""POS Invoice set_missing_values (run by the register right after its Return button maps the ticket, and by
	the desk form): the cart the cashier sees is already the goods-only return — no trade-in lines, no tax, the
	refund at the goods' total. The before_validate pass above stays the authority on every save."""
	return_goods_only(doc)


def _correct_return(doc) -> float | None:
	"""Make a return of a negative trade-in ticket goods-only (see return_goods_only) and total it again. None
	when ``doc`` is no such return; else the total it carried before."""
	original = _negative_original(doc)
	if not original:
		return None
	before = flt(doc.get("rounded_total")) or flt(doc.get("grand_total"))
	for field in ("ffl_trade_in_payout_method", "ffl_trade_in_payout_reference", "ffl_trade_in_payout_entry"):
		if doc.get(field):
			doc.set(field, None)
	keep = [r for r in doc.items if r.item_code != TRADE_IN_CREDIT_ITEM]
	if len(keep) != len(doc.items):
		doc.set("items", keep)
		for idx, row in enumerate(keep, 1):
			row.idx = idx
		frappe.msgprint(_("Trade-in lines are not returned: the store keeps the traded-in guns and already paid "
			"the customer the difference."), alert=True, indicator="orange")
	if not _sales_tax_collected(original, doc.company):
		from ffl_core.compliance.customer_tax_exempt import clear_tax_keep_freight

		clear_tax_keep_freight(doc)
	doc.calculate_taxes_and_totals()
	return before


def _sales_tax_collected(ticket: str, company: str) -> float:
	"""The sales tax a ticket collected (its tax rows, freight excluded)."""
	from ffl_core.setup.shipping_charge import shipping_account_name

	freight = shipping_account_name(company)
	rows = frappe.get_all("Sales Taxes and Charges", filters={"parent": ticket, "parenttype": "POS Invoice"},
		fields=["account_head", "base_tax_amount_after_discount_amount"])
	return flt(sum(flt(r.base_tax_amount_after_discount_amount) for r in rows if r.account_head != freight), 2)
