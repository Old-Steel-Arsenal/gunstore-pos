"""Whitelisted RSR search — compatibility shim for ffl_core.bundle.js.

The catalog moved to Data Service; this endpoint keeps the FirearmItemPicker's
dotted-path RPC alive by delegating to the provider's remote search and
mapping rows back to the legacy key shape the JS consumes. Degrades to []
when the API is unreachable (same as the provider)."""
from __future__ import annotations

import frappe

from ffl_core.roles import COUNTER_ROLES


@frappe.whitelist()
def search(query: str, limit: int = 10) -> list[dict]:
	"""Search the RSR catalog (legacy-shaped rows for the firearm picker).

	Compatibility shim: delegates to the provider's remote search and maps
	rows back to the legacy key shape ffl_core.bundle.js consumes.
	Degrades to [] when the API is unreachable.

	Args:
	    query: Search term (blank returns []).
	    limit: Row cap, clamped to 1–50 (default 10).

	Returns:
	    [{rsr_stock_number, upc, mfg_number, manufacturer, model,
	    short_description, department_number, msrp, dealer_price,
	    map_price, quantity, ffl_required, status_flag, image_url,
	    image_url_hires}].
	"""
	frappe.only_for(COUNTER_ROLES)
	from ffl_integrations.distributor.api import get_provider

	q = (query or "").strip()
	if not q:
		return []
	limit = min(max(1, int(limit)), 50)
	rows = get_provider("rsr").search(q, limit)   # already degrades to [] on API failure
	return [{
		"rsr_stock_number": r.get("sku"),
		"upc": r.get("upc"),
		"mfg_number": "",
		"manufacturer": r.get("manufacturer"),
		"model": r.get("model"),
		"short_description": r.get("item_name"),
		"department_number": r.get("department"),
		"msrp": r.get("msrp"),
		"dealer_price": r.get("cost"),
		"map_price": r.get("map"),
		"quantity": r.get("qty"),
		"ffl_required": r.get("ffl_required"),
		"status_flag": "",
		"image_url": r.get("image_url"),
		"image_url_hires": "",
	} for r in rows]
