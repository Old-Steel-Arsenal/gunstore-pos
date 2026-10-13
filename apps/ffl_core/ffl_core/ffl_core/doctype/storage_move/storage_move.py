"""Storage Move — one entry in the append-only log of location changes.

Written only by ffl_core.storage.move(), in the same transaction that updates the
Serial No / Storage Location Qty it describes. Never edited, never deleted."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class StorageMove(Document):
	def validate(self):
		if not self.flags.via_storage:
			frappe.throw(_("Location changes are made from the Storage Locations page."))
		if not self.is_new():
			frappe.throw(_("The location log is never edited."))

	def on_trash(self):
		frappe.throw(_("The location log is never deleted."))
