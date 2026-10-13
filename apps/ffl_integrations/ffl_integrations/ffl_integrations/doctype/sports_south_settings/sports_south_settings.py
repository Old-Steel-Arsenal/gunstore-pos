"""Sports South Settings — ordering credentials, delivery-option defaults and
the flat-rate table from the Order Fulfillment Overview. Read by
``sports_south.api_client.SSConfig.from_settings`` and ``distributor.ss_provider``.
The catalog side has its own credentials in the Data Service admin."""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class SportsSouthSettings(Document):
	def validate(self):
		if self.ordering_enabled:
			for key, label in (("ship_via_ground", "Ground ship code"), ("ship_via_2day", "2-Day Air ship code")):
				if not (self.get(key) or "").strip():
					frappe.msgprint(_("{0} is empty — placement fails until it is set (Ground Premium = G, 2-Day Air = 2).").format(label),
						indicator="orange")
			if self.auto_email_ffl and not (self.fulfillment_email or "").strip():
				frappe.msgprint(_("Automatic FFL emails need a Sports South fulfillment email address."),
					indicator="orange")


@frappe.whitelist()
def test_connection() -> dict:
	"""Settings toolbar: catalog service + orders/invoices authentication.

	Returns:
	    {checks: [{label, ok: bool|None, detail}]}

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	from ffl_integrations.distributor.ss_provider import SSProvider

	return SSProvider().connection_test()
