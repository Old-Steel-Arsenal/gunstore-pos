"""One-shot — move the data-service connection off FFL Settings onto its own
Single, Data Service Settings, and rename the EZ Check provider choice.

The three fields (endpoint / client id / secret) lived under FFL Settings →
FFL Verification even though the same connection also serves every
distributor catalog read. Runs after the doctype sync, so the old fields are
gone from the FFL Settings meta: the values are read straight from tabSingles
and __Auth (keyed by fieldname alone), copied only when the new Single is still
blank (an operator's explicit setting wins), and the old rows deleted either
way — nothing reads them any more.
"""
from __future__ import annotations

import frappe
from frappe.utils.password import get_decrypted_password, remove_encrypted_password

OLD = "FFL Settings"
NEW = "Data Service Settings"
OLD_PROVIDER, NEW_PROVIDER = "OSA API", "Data Service"


def _old(field: str) -> str:
	# tabSingles has no creation column, so no get_value (it orders by it).
	row = frappe.db.sql("select value from `tabSingles` where doctype=%s and field=%s", (OLD, field))
	return ((row[0][0] if row else "") or "").strip()


def execute() -> str:
	"""Returns "moved" | "kept" | "absent" — never a credential."""
	frappe.reload_doc("ffl_integrations", "doctype", "data_service_settings")
	endpoint, client_id = _old("osa_api_endpoint"), _old("osa_api_client_id")
	secret = (get_decrypted_password(OLD, OLD, "osa_api_secret", raise_exception=False) or "").strip()

	if not (endpoint or client_id or secret):
		outcome = "absent"
	elif frappe.db.get_single_value(NEW, "endpoint"):
		outcome = "kept"
	else:
		doc = frappe.get_single(NEW)
		doc.endpoint, doc.client_id, doc.secret = endpoint, client_id, secret
		doc.save(ignore_permissions=True)
		outcome = "moved"

	remove_encrypted_password(OLD, OLD, "osa_api_secret")
	frappe.db.delete("Singles", {"doctype": OLD,
		"field": ["in", ["osa_api_endpoint", "osa_api_client_id", "osa_api_secret"]]})
	if _old("live_check_provider") == OLD_PROVIDER:
		frappe.db.set_single_value(OLD, "live_check_provider", NEW_PROVIDER)
	return outcome
