"""The core's side of the website drop-ship boundary.

Listing distributor products on the website and routing web orders to them is an
optional layer (the `osa_dropship` extension). Core code that
needs something from it asks here, through named hooks, and gets a neutral answer
when no provider is installed — so the counter, receiving and distributor orders
work the same with or without it. A provider that fails raises into the caller,
exactly as the inline code it replaced did (a wrong answer here overwrites a
listing's house, misprices the counter or leaves stale stock published); only
the hub's display columns are best effort. Contract: docs/extensions/hooks.md.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

LISTING_HOOK = "gunshop_dropship_listing"
PRICE_HOOK = "gunshop_dropship_suggested_price"
SETTLED_HOOK = "gunshop_distributor_source_settled"
FEED_STALE_HOOK = "gunshop_distributor_feed_stale"
HUB_ROWS_HOOK = "gunshop_distributor_hub_rows"


def _answers(hook: str, *args, best_effort: bool = False, **kwargs):
	"""Each provider's answer in turn. A failure raises; with ``best_effort`` it is
	logged and skipped instead."""
	for path in frappe.get_hooks(hook):
		try:
			yield frappe.get_attr(path)(*args, **kwargs)
		except Exception:
			if not best_effort:
				raise
			frappe.log_error(title=f"{hook} failed: {path}", message=frappe.get_traceback())


def _first(hook: str, *args, **kwargs):
	return next((a for a in _answers(hook, *args, **kwargs) if a is not None), None)


def layer_active() -> bool:
	"""Whether a drop-ship layer is here AND switched on: a listing provider whose
	app is not a POS extension (always on), or an extension the store has on.
	Screens that open the layer's pages (the Distributors hub) follow this, so a
	switched-off extension shows no button that would only lead to a refusal."""
	from ffl_core.extensions import is_enabled

	for path in frappe.get_hooks(LISTING_HOOK):
		app = path.split(".", 1)[0]
		if not frappe.db.exists("POS Extension", app) or is_enabled(app):
			return True
	return False


def listing_for(item_code, distributor, fields=("sku",), *, any_house: bool = False):
	"""The Item's website listing (a dict of ``fields``) at ``distributor``, or None
	— also when no drop-ship layer is installed. Scoped to the house: a blank
	distributor matches nothing unless ``any_house`` asks for whichever house
	lists it."""
	if not item_code or not (distributor or any_house):
		return None
	return _first(LISTING_HOOK, item_code, None if any_house else distributor, tuple(fields))


def suggested_price(dist, row, listing) -> float:
	"""Counter price for a catalog row: the drop-ship pricing rules when the layer
	is installed, else MAP, then MSRP, then cost."""
	price = _first(PRICE_HOOK, dist, row, listing)
	if price is not None:
		return flt(price)
	return flt(row.get("map")) or flt(row.get("msrp")) or flt(row.get("cost"))


def source_settled(doctype: str, name: str) -> None:
	"""Every distributor order of a non-counter source (a web order) has settled:
	the layer that routed it completes it."""
	for _ in _answers(SETTLED_HOOK, doctype, name):
		pass


def feed_stale(distributor: str, *, aged: bool) -> bool:
	"""A distributor's quantity feed went stale (``aged``: it had a heartbeat and
	aged out). True when a provider is zeroing the published stock."""
	return any([bool(a) for a in _answers(FEED_STALE_HOOK, distributor, aged=aged)])


def decorate_hub_rows(rows) -> None:
	"""Let the layer add its own columns (the markup in force) to the Distributors
	hub rows, in place."""
	for _ in _answers(HUB_ROWS_HOOK, rows, best_effort=True):
		pass
