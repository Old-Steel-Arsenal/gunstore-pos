"""Storage Floor Plan — a drawing of one room of the store (where each zone sits, plus walls,
doors, counters and labels). Created and changed only by ffl_core.storage (the Storage
Locations page): the controller refuses anything else, so every saved layout was checked."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class StorageFloorPlan(Document):
	def validate(self):
		if not self.flags.via_storage:
			frappe.throw(_("Floor plans are drawn on the Storage Locations page."))

	def on_trash(self):
		if not self.flags.via_storage:
			frappe.throw(_("Floor plans are deleted on the Storage Locations page."))
