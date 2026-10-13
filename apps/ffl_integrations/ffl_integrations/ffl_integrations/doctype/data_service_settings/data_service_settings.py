"""Data Service Settings — the site-wide connection to the shared data service
(FFL verification + distributor catalogs). Read by ``data_service.client``
only; the Catalog API contract the service must honour is docs/catalog-api.
Field rationale lives in docs/field-notes.md."""
from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


# The product change-feed cursors the catalog consumers keep, each registered
# as "DocType.field" (a Single) under this hook — this app registers the category
# follower's; an extension that follows the feed (the drop-ship layer) registers
# its own, so the core never names its DocTypes. The two catalog sources number
# changes differently, so a cursor from one is meaningless against the other.
CHANGE_CURSOR_HOOK = "gunshop_catalog_change_cursors"


class DataServiceSettings(Document):
	def validate(self):
		self.endpoint = (self.endpoint or "").strip().rstrip("/")
		self.client_id = (self.client_id or "").strip()
		self.catalog_source = self.catalog_source or "Data Service"
		before = frappe.db.get_single_value("Data Service Settings", "catalog_source",
			cache=False) or "Data Service"
		if self.catalog_source == before:
			return
		if before == "Local":
			# Local → Data Service: everything the local catalog holds is a copy of the
			# distributors' feeds, so nothing is lost. The service must be reachable
			# from the first tick, or the catalog goes off and drop-ship stock with it.
			if not (self.endpoint and self.client_id and self.secret):
				frappe.throw(frappe._("Fill in the endpoint, client ID and secret before switching from the local catalog to a Data Service."))  # noqa: E501 — one translatable literal
			self.flags.switching_to_service = True
			# This form's own copies too: the doc the form gets back is saved again
			# whole, and must not carry the local numbering back.
			for doctype, field in change_cursor_fields():
				if doctype == self.doctype:
					self.set(field, "")
			self.category_products_baselined = 0
			self.category_tree_cursor = ""
		elif blocking := stored_change_cursors():
			frappe.throw(frappe._("Switching an existing site from a Data Service to the local catalog is not supported yet; the local catalog is for new installs. Holding a change cursor: {0}.").format(", ".join(blocking)))  # noqa: E501 — one translatable literal

	def on_update(self):
		if self.flags.pop("switching_to_service", None):
			# The saved connection must answer before the switch commits: a refusal
			# here rolls the whole save back, so the site stays on its local catalog.
			_probe_service()
			reset_change_cursors()


def _probe_service() -> None:
	"""One catalog health read through the connection just saved (this transaction),
	for an enabled house the site-wide service serves (not one on its own service)."""
	from ffl_integrations.data_service import client

	codes = frappe.get_all("Distributor", filters={"enabled": 1}, pluck="provider",
		order_by="priority asc")
	code = next((c for c in codes if not client.own_service_row(c)), "rsr")
	try:
		client.get("/v1/catalog/health", {"distributor": code})
	except client.DataServiceUnavailable as exc:
		frappe.throw(frappe._("The Data Service check failed, so the site stays on its local catalog: {0}").format(exc))  # noqa: E501 — one translatable literal


def reset_change_cursors() -> None:
	"""Clear every registered change cursor (a blank cursor means "start over"): the
	category follower takes a fresh baseline (its flag is cleared in validate); a
	blank drop-ship cursor reads the feed from its start, re-syncing every listing once)."""
	# Written as stored_change_cursors() reads them: straight on tabSingles.
	for doctype, field in change_cursor_fields():
		frappe.db.sql("UPDATE `tabSingles` SET value = '' WHERE doctype = %s AND field = %s",
			(doctype, field))
		frappe.clear_document_cache(doctype, doctype)
	frappe.db.value_cache.clear()


def change_cursor_fields() -> list:
	"""``[(doctype, field)]`` registered under ``gunshop_catalog_change_cursors``.
	A malformed entry is skipped (it cannot name a Single field)."""
	out = []
	for entry in frappe.get_hooks(CHANGE_CURSOR_HOOK) or []:
		doctype, _dot, field = str(entry).rpartition(".")
		if doctype.strip() and field.strip():
			out.append((doctype.strip(), field.strip()))
	return list(dict.fromkeys(out))


def stored_change_cursors() -> list:
	"""``["DocType.field"]`` for every registered change cursor holding a value."""
	pairs = change_cursor_fields()
	if not pairs:
		return []
	cond = " OR ".join(["(doctype = %s AND field = %s)"] * len(pairs))
	return [f"{dt}.{field}" for dt, field in frappe.db.sql(f"""SELECT doctype, field
		FROM `tabSingles` WHERE ({cond}) AND IFNULL(value, '') != '' ORDER BY doctype, field""",
		[v for pair in pairs for v in pair])]


@frappe.whitelist()
def test_connection() -> dict:
	"""Settings toolbar: catalog health for every enabled house through the
	site-wide service (or this site's own tables when the catalog source is
	Local), recording the contract version on the Status tab.

	Returns:
	    {checks: [{label, ok: bool|None, detail}]}

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	from ffl_integrations.catalog_local import is_local
	from ffl_integrations.data_service import client
	from ffl_integrations.distributor import remote_catalog

	local = is_local()

	codes = [r.provider for r in frappe.get_all("Distributor", filters={"enabled": 1},
		fields=["provider"], order_by="priority asc")] or ["rsr"]
	checks, contract, error = [], "", ""
	for code in codes:
		try:
			h = (remote_catalog.health(code) if local
				else client.get("/v1/catalog/health", {"distributor": code})) or {}
			contract = h.get("contract_version") or contract
			checks.append({"label": f"Catalog · {code}" + (" (local)" if local else ""), "ok": True,
				"detail": "catalog {0} · quantity {1} · contract {2}".format(
					h.get("last_catalog_sync_at") or "?", h.get("last_quantity_sync_at") or "?",
					h.get("contract_version") or "pre-1.0")})
		except client.DataServiceUnavailable as exc:
			error = str(exc)
			checks.append({"label": f"Catalog · {code}", "ok": False, "detail": error})
	frappe.db.set_single_value("Data Service Settings", {
		"contract_version": contract, "last_checked_at": now_datetime(), "last_error": error})
	return {"checks": checks}
