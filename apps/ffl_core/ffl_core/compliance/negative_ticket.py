"""A ticket whose trade-ins exceed the goods: the one place ERPNext's own refusal is lifted.

ERPNext refuses any non-return invoice with a negative grand total (AccountsController.validate ->
is_negative_grand_total_allowed). The owner decided that a ticket may go
negative when — and only when — trade-in credit is what takes it there; the store then pays the customer the
difference on the same ticket. Mixed into POS Invoice and Sales Invoice (the consolidated copy) through
hooks.extend_doctype_class: core keeps deciding, this only adds the trade-in case.
"""
from __future__ import annotations


class NegativeTradeInTicket:
	def is_negative_grand_total_allowed(self) -> bool:
		from ffl_core.compliance.trade_in_credit import negative_trade_in_ok
		if super().is_negative_grand_total_allowed():
			return True
		# A Sales Invoice only as the consolidated copy of a POS ticket: one typed in Desk has no payout path.
		if self.doctype == "Sales Invoice" and not self.get("is_consolidated"):
			return False
		return negative_trade_in_ok(self)
