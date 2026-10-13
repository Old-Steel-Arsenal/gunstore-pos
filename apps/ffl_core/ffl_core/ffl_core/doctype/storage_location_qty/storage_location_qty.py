"""Storage Location Qty — units of one non-serialized item in one location.

The name is ``<location>::<item>`` so the pair is unique by construction and a
row can be locked by name. Written only by ffl_core.storage."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


def qty_row_name(location: str, item_code: str) -> str:
	return f"{location}::{item_code}"


class StorageLocationQty(Document):
	def autoname(self):
		self.name = qty_row_name(self.location, self.item_code)

	def validate(self):
		if not self.flags.via_storage:
			frappe.throw(_("Quantities are changed from the Storage Locations page."))
		if (self.qty or 0) < 0:
			frappe.throw(_("A location cannot hold less than zero of {0}.").format(self.item_code))
