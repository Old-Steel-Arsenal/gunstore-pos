"""Inventory Count — one stocktake. The scans live in Inventory Count Entry; the
logic (scope, scanning, variance, finalize) is ffl_core.api.inventory_count."""
import frappe
from frappe import _
from frappe.model.document import Document


class InventoryCount(Document):
	def validate(self):
		# Created and changed only by the Inventory Count page's own endpoints (which set
		# this flag and write status / result fields directly): nobody holds write
		# permission, and this stops a hand-rolled REST save from rewriting a count's
		# scope, status or saved report if one ever did.
		if not self.flags.via_inventory_count:
			frappe.throw(_("An inventory count can only be started and changed from the Inventory Count page."))
