"""ShipStation Settings — single doctype for credentials + endpoint config.

Read by ffl_integrations.shipstation.client (ShipStationConfig.from_settings)
and gated on `enabled` by the Pending Order page's Ship button. Sandbox keys
start with TEST_ and hit the same base URL as production.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ShipStationSettings(Document):
	def validate(self):
		# Warn (never block) when the sandbox toggle disagrees with the key
		# flavor — a TEST_ key in "production" mode or vice versa is almost
		# always a mistake about to burn a smoke test.
		api_key = self.get_password("api_key", raise_exception=False) or ""
		if not api_key:
			return
		is_test_key = api_key.startswith("TEST_")
		if self.sandbox_mode and not is_test_key:
			frappe.msgprint(_(
				"Sandbox Mode is ON but the API key does not start with TEST_ — "
				"this looks like a production key."))
		elif not self.sandbox_mode and is_test_key:
			frappe.msgprint(_(
				"The API key starts with TEST_ (a sandbox key) but Sandbox Mode "
				"is OFF — pushes will go to the sandbox account regardless."))
