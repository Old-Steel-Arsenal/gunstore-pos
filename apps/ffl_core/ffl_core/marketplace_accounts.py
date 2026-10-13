"""The names ffl_core has to know about a marketplace integration it cannot
import.

ffl_core runs on sites where ffl_integrations is absent — the daily Sales
Report and the month-end schedules are core reads and cannot sit behind an
optional dependency. So the account names, the order doctype and the remark
marker are duplicated here, on purpose.

Duplication with a reason is fine. Duplication with nothing checking it is how
the two copies drift apart, and both drifts are silent: rename an account on
one side and every marketplace tax row falls into `unclassified` (the report
still renders, and the number that is wrong is the one the Texas return is
reconciled against); change the remark marker and the cash-basis schedule stops
seeing refunds. So every name below is pinned against the constants
`ffl_integrations.gunbroker.setup` actually creates, in both directions, by
ffl_integrations/tests/test_gunbroker_report_contract.py — from that side,
because that is the app that may be missing.
"""
from __future__ import annotations

import frappe
from frappe import _

# Liabilities a marketplace collects THROUGH us. The money lands in our bank on
# its way to somebody else: the facilitator files the sales tax with the state,
# and keeps the buyer's service fee. Neither is our revenue and neither belongs
# in the sales-tax figure the report reconciles against the return.
MPF_TAX_ACCOUNT = "GunBroker MPF Sales Tax Payable"
MSF_ACCOUNT = "GunBroker Marketplace Fee Payable"
# Fees the marketplace will bill us for, accrued when the sale posts.
ACCRUED_FEES_ACCOUNT = "Accrued GunBroker Fees"
FEES_EXPENSE_ACCOUNT = "Marketplace Fees - GunBroker"

MARKETPLACE_TAX_ACCOUNTS = frozenset({MPF_TAX_ACCOUNT})
MARKETPLACE_FEE_ACCOUNTS = frozenset({MSF_ACCOUNT})

ORDER_DOCTYPE = "GunBroker Order"
SETTINGS_DOCTYPE = "GunBroker Settings"

# Every GunBroker receipt this system writes puts "GunBroker Order #<id>" in the
# entry's remarks, and the refund alerts tell operators to do the same on a refund entry.
# It is the only key tying a manually-keyed refund back to its order.
ORDER_REMARK_PREFIX = "GunBroker Order #"


def refund_remarks_rule(gb_order_id, *, receive_too: bool = False) -> str:
	"""The one rule a hand-keyed GunBroker refund must follow, as plain text
	for emails and field help (no markup).

	`gunbroker_cash_basis_adjustments._fetch_refund_entries` only reads a
	submitted Pay-type Payment Entry with Party Type Customer in the sale's
	company, and `_match_refunds` keys on the numeric GunBroker order id (not
	the POS record name) after `ORDER_REMARK_PREFIX`. The marker is built from
	the constant, outside the translatable text. `receive_too`: a cancelled
	order whose money was banked has an unreferenced Receive entry as well,
	and both entries carry the marker.

	Built at call time, never at import: `_()` resolves against the language in
	force where it runs."""
	marker = f"{ORDER_REMARK_PREFIX}{gb_order_id}"
	if receive_too:
		return _(
			"Both Payment Entries (the unreferenced Receive and the Pay) must "
			"carry the order number: tick Custom Remarks first, then write "
			"\"{0}\" in the Remarks of each. The cash-basis report finds the "
			"refund only as a submitted Pay-type Payment Entry to the customer "
			"(Party Type Customer) in the same company, one order per entry."
		).format(marker)
	return _(
		"The cash-basis report finds a refund only as a submitted Pay-type "
		"Payment Entry to the customer (Party Type Customer) in the same "
		"company. Tick Custom Remarks first, then write \"{0}\" in its "
		"Remarks, one order per entry."
	).format(marker)


def account_for(company, bare_name):
	"""`"<bare name> - <abbr>"` if that account exists for the company, else
	None.

	Resolved per company rather than hard-coded, so a dev site (FT) and
	production (any store) both work, and so a schedule run before the integration's
	setup has ever run reports "not created yet" instead of raising."""
	if not company or not bare_name:
		return None
	abbr = frappe.get_cached_value("Company", company, "abbr")
	if not abbr:
		return None
	name = f"{bare_name} - {abbr}"
	return name if frappe.db.exists("Account", name) else None
