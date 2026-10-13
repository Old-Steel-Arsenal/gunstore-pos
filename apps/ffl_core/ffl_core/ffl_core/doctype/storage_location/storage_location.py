"""Storage Location — one position in a zone. Created and changed only by
ffl_core.storage; its name (``<company abbr>-<label>``) doubles as the barcode."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class StorageLocation(Document):
	def autoname(self):
		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		self.name = f"{abbr}-{(self.label or '').strip()}"

	def validate(self):
		if not self.flags.via_storage:
			frappe.throw(_("Locations are managed from the Storage Locations page."))

	def on_trash(self):
		if not self.flags.via_storage:
			frappe.throw(_("Locations are deleted from the Storage Locations page (Zones tab)."))
