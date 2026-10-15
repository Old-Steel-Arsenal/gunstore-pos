"""Lipseys Settings — the two Lipsey's logins (dealer for store orders, the
separate drop-ship login for orders to a buyer or a transfer dealer), the
order options and the shipping poll's cursor. Read by
``lipseys.api_client.LipseysConfig.from_settings`` and
``distributor.lipseys_provider``. A Data Service catalog has its own credentials
in the service's admin; a Local catalog (``catalog_local.drivers.lipseys``) reads
Lipsey's feeds with the dealer login here."""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class LipseysSettings(Document):
	def on_update(self):
		# A save is the operator's retry after Lipsey's refused the login: it lifts
		# the local catalog's auth hold. The shipping poll's own writes never get here.
		from ffl_integrations.catalog_local.drivers import lipseys

		lipseys.note_credentials_saved()


@frappe.whitelist()
def test_connection() -> dict:
	"""Settings toolbar: catalog service + a sign-in and one read per Lipsey's login.

	Read-only at Lipsey's: it places no order.

	Returns:
	    {checks: [{label, ok: bool|None, detail}]}

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	from ffl_integrations.distributor.lipseys_provider import LipseysProvider

	return LipseysProvider().connection_test()
