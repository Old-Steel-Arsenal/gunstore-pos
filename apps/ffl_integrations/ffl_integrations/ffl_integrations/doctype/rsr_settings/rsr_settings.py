"""RSR Settings — Direct Connect ordering credentials, plus the FTPS feed login a
Local-catalog site downloads RSR's catalog, quantity, deleted and
ship-restriction files with (``catalog_local/drivers/rsr.py``). A site on the
Data Service never uses the feed login, and the form hides it there."""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class RSRSettings(Document):
	def onload(self):
		"""The form shows the Catalog feed section only to a Local-catalog site."""
		from ffl_integrations.catalog_local import is_local

		self.set_onload("catalog_local", is_local())


@frappe.whitelist()
def test_connection() -> dict:
	"""Settings toolbar: catalog health, the feed login (Local catalog only, no
	download) and a Direct Connect authentication round trip per configured
	account.

	Returns:
	    {checks: [{label, ok: bool|None, detail}]}

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	from ffl_integrations.distributor.rsr_provider import RSRProvider

	return RSRProvider().connection_test()
