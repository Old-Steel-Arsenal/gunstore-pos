"""Cash Drawer Settings: the limits and lists behind the daily cash count (see ffl_core.cash_drawer)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core.cash_drawer import expense_account_problem


class CashDrawerSettings(Document):
	def validate(self):
		if (self.variance_threshold or 0) <= 0:
			frappe.throw(_("The count difference that needs a reason must be more than zero."))
		if (self.expense_cap or 0) < 1:
			frappe.throw(_("The biggest cash expense must be at least $1."))
		for row in self.expense_accounts or []:
			problem = expense_account_problem(row.account)
			if problem:
				frappe.throw(_("Row {0}: {1}").format(row.idx, problem))
