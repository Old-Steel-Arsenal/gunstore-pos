"""Cash Drawer Entry: one movement of cash out of the drawer (or a seller payment), with its Journal Entry.

All rules live in ffl_core.cash_drawer; the endpoints and the trade-in dialog create entries through it.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core import cash_drawer


class CashDrawerEntry(Document):
	def validate(self):
		cash_drawer.validate_entry(self)

	def after_insert(self):
		cash_drawer.post_entry_je(self)

	def on_trash(self):
		if not frappe.flags.in_test:
			frappe.throw(_("Cash entries cannot be deleted. Undo the entry instead."))
