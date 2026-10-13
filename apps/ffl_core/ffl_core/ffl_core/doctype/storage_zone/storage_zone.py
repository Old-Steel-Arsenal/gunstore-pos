"""Storage Zone — an area of the store. Created and changed only by ffl_core.storage
(the Storage Locations page): the controller refuses anything else so the positions
under it can never disagree with it."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class StorageZone(Document):
	def autoname(self):
		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		self.name = f"{abbr}-{(self.zone_name or '').strip()}"

	def validate(self):
		if not self.flags.via_storage:
			frappe.throw(_("Zones are managed from the Storage Locations page."))

	def on_trash(self):
		if not self.flags.via_storage:
			frappe.throw(_("Zones are deleted from the Storage Locations page (Zones tab)."))
