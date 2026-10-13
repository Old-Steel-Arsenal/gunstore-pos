"""Whitelisted helpers used by the FastBound Settings client script."""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.roles import STOCK_ROLES, SYSTEM_ROLES

from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError, FastBoundError
from ffl_integrations.integration_gate import fastbound_enabled


@frappe.whitelist()
def test_connection() -> dict:
	"""Probe the FastBound connection via GET /Account.

	Returns:
	    {"ok": True, "account_name": ..., "raw": <account body>} — throws
	    a readable error when unconfigured or the API rejects.
	"""
	frappe.only_for(SYSTEM_ROLES)
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError as exc:
		frappe.throw(_("FastBound is not configured: {0}").format(str(exc)))
	try:
		body = client.get_account()
	except FastBoundError as exc:
		frappe.throw(_("FastBound API error: {0}").format(str(exc)))
	return {
		"ok": True,
		"account_name": (body or {}).get("name") or (body or {}).get("accountName"),
		"raw": body,
	}


# SmartLists synced from FastBound (account presets + custom) so the Acquisition
# and Disposition "type" dropdowns offer the dealer's current FastBound values.
_SMART_LISTS = {"AcquireType": "acquire_types", "DisposeType": "dispose_types"}


@frappe.whitelist()
def sync_smart_lists() -> dict:
	"""Sync FastBound's AcquireType/DisposeType SmartLists into Settings.

	Pulls presets + custom values and caches them on FastBound Settings
	for the Acquisition / Disposition type dropdowns.

	Returns:
	    {"ok": True, "acquire_count": n, "dispose_count": n}.
	"""
	frappe.only_for(SYSTEM_ROLES)
	try:
		client = FastBoundClient.from_settings()
	except FastBoundConfigError as exc:
		frappe.throw(_("FastBound is not configured: {0}").format(str(exc)))

	settings = frappe.get_single("FastBound Settings")
	counts: dict[str, int] = {}
	for list_name, fieldname in _SMART_LISTS.items():
		try:
			values = client.get_smartlist(list_name)
		except FastBoundError as exc:
			frappe.throw(_("FastBound API error fetching {0}: {1}").format(list_name, str(exc)))
		settings.set(fieldname, "\n".join(values))
		counts[fieldname] = len(values)
	settings.smart_lists_synced_at = frappe.utils.now_datetime()
	settings.flags.ignore_permissions = True
	settings.save()
	return {
		"ok": True,
		"acquire_count": counts.get("acquire_types", 0),
		"dispose_count": counts.get("dispose_types", 0),
	}


def sync_smart_lists_scheduled() -> None:
	"""Daily scheduler entry — refresh cached SmartLists. Skips silently when
	FastBound is switched off or isn't configured; logs (not raises) on transient
	API errors so a FastBound outage doesn't fail the scheduler."""
	if not fastbound_enabled():
		return
	if not frappe.db.get_single_value("FastBound Settings", "account_num"):
		return
	try:
		sync_smart_lists()
	except Exception:
		frappe.log_error(title="FastBound: daily SmartList sync failed")


def _cached_list(fieldname: str) -> list[str]:
	raw = frappe.db.get_single_value("FastBound Settings", fieldname) or ""
	return [t.strip() for t in raw.split("\n") if t.strip()]


@frappe.whitelist()
def get_acquire_types() -> list[str]:
	"""Cached AcquireType options for the Acquisition type dropdown.

	Returns:
	    List of type strings ([] until sync_smart_lists has run).
	"""
	frappe.only_for(STOCK_ROLES)
	return _cached_list("acquire_types")


@frappe.whitelist()
def get_dispose_types() -> list[str]:
	"""Cached DisposeType options for the Disposition type dropdown.

	Returns:
	    List of type strings ([] until sync_smart_lists has run).
	"""
	frappe.only_for(STOCK_ROLES)
	return _cached_list("dispose_types")
