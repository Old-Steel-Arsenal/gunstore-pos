"""Inventory Count Entry — one scan or manual entry, insert-and-delete only.

Not a child table of Inventory Count on purpose: several devices scan into the
same count at once, and saving a shared parent from each would collide on its
modified timestamp. Separate rows insert independently; the counted quantity is
the SUM of qty_delta at query time, so there is no stored total to go stale.
"""
import frappe
from frappe import _
from frappe.model.document import Document


def serial_key(count: str, serial: str) -> str:
	"""The unique key that stops one gun being counted twice in one count. Serial No
	names compare case-insensitively, so the key is lower-cased."""
	return f"{count}|{serial.strip().lower()}"


class InventoryCountEntry(Document):
	def validate(self):
		# Scans are written only by the Inventory Count page's endpoints, which set this flag.
		if not self.flags.via_inventory_count:
			frappe.throw(_("Count entries can only be added from the Inventory Count page."))

	def before_insert(self):
		# The unique index on this key is the last line of defence against the same
		# gun being counted twice by two devices at the same instant. Left NULL for
		# every other kind (NULLs never collide).
		if self.kind == "serial" and self.serial_no:
			self.serial_key = serial_key(self.inventory_count, self.serial_no)
		else:
			self.serial_key = None
