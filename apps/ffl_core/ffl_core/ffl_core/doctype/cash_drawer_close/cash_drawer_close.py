"""Cash Drawer Close: the drawer's close for one company and day (see ffl_core.cash_drawer)."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CashDrawerClose(Document):
	def validate(self):
		# At most one close that has not been undone per company and day, enforced by the database: the key is
		# empty (NULL, so it never collides) once a close is undone.
		self.active_key = f"{self.company}|{self.business_date}" if self.status in ("Closed", "Closing") else None

	def on_trash(self):
		if not frappe.flags.in_test:
			frappe.throw(_("A close cannot be deleted. Undo it instead."))
