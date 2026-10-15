"""Lipseys Settings — the two Lipsey's logins (dealer for store orders, the
separate drop-ship login for orders to a buyer or a transfer dealer), the
order options and the shipping poll's cursor. Read by
``lipseys.api_client.LipseysConfig.from_settings`` and
``distributor.lipseys_provider``. The catalog has its own credentials in the Data
Service admin."""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class LipseysSettings(Document):
	pass


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
