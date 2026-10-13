"""FFL Acquisition — A&D 'A' side. Records every firearm coming into our FFL.

Submitted = recorded in the bound book. Amended via cancel + new-with-amended_from.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core.acquisition import INDIVIDUAL_SOURCES


class FFLAcquisition(Document):
	def validate(self) -> None:
		self._fill_serial_from_item()
		self._reject_dealer_source_without_ffl()
		self._reject_individual_source_without_id()

	def _reject_dealer_source_without_ffl(self) -> None:
		if self.source_type == "FFL Dealer" and not (self.source_ffl or "").strip():
			frappe.throw(
				_("Source FFL number is required when source type is 'FFL Dealer'."),
				title=_("FFL Required"),
			)

	def _fill_serial_from_item(self) -> None:
		"""Server-side defence in depth: when there's exactly one active Serial
		No for the selected Item and the field is blank, fill it. (Every firearm
		is has_serial_no=1 now — the legacy Item.firearm_serial fallback was
		removed when the Used Firearm path was unified.)"""
		if not self.item or self.serial:
			return
		active = frappe.get_all(
			"Serial No",
			filters={"item_code": self.item, "status": "Active"},
			pluck="name", limit=2,
		)
		if len(active) == 1:
			self.serial = active[0]

	def _reject_individual_source_without_id(self) -> None:
		if self.source_type in INDIVIDUAL_SOURCES and not (
			self.source_id_type and self.source_id_number
		):
			frappe.throw(
				_("ID type and number are required for source type '{0}'.").format(self.source_type),
				title=_("ID Required"),
			)
