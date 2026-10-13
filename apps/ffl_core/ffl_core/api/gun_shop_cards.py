"""The "Needs attention" number cards on the Gun Shop home page.

Each card is a standard Number Card of type Custom whose ``method`` is
:func:`get_count` and whose ``filters_json`` names the card (``{"card": "pending_orders"}``).
The count is the row count of the very list the card opens, so the number on the card and
the rows behind it cannot disagree: the queue builders and report ``execute`` functions are
called, not re-queried.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.ffl_core.report.pending_4473_orders.pending_4473_orders import execute as pending_4473_report
from ffl_core.ffl_core.report.pending_transfer_pickups.pending_transfer_pickups import (
	execute as transfer_pickups_report,
)

PENDING_ORDERS_PAGE = "pending-dispositions"
RESTOCK_PAGE = "distributor-restock"
COUNTER_SOURCE = "ffl_core.api.manual_order.list_pending_dispositions"
WOO_SOURCE = "ffl_woo_sync.woocommerce.fulfillment.list_pending_web_orders"
GUNBROKER_SOURCE = "ffl_integrations.gunbroker.fulfillment.list_pending_gb_orders"
CACHE_SECONDS = 60


CACHE_PREFIX = "gun_shop_cards:"


def clear_cache() -> None:
	"""Forget the cached counts (tests; a deploy's clear-cache does the same)."""
	frappe.cache.delete_keys(CACHE_PREFIX)


def _cached(key: str, count_fn) -> int:
	"""``count_fn()`` shared by every card and user for CACHE_SECONDS; a raise is not cached.

	# lazy: a card can trail the list behind it by up to CACHE_SECONDS (a new order or a shipped one
	# shows on the next minute); the queues and reports build every row just to count it, as their
	# pages do on load. Upgrade path = a COUNT mirroring each list's predicates.
	"""
	key = CACHE_PREFIX + key
	cached = frappe.cache.get_value(key, expires=True)
	if cached is not None:
		return cached
	count = count_fn()
	frappe.cache.set_value(key, count, expires_in_sec=CACHE_SECONDS)
	return count


def _queue(path: str) -> int:
	"""Rows in a whitelisted queue builder, for the Pending Order page's three sources.

	Treated the way the page treats a source (pending_dispositions.js ``_load``): the app that owns
	the builder is not installed, or the builder fails -> that source is 0 rows and the others
	still count. A failure is logged here (the page's call would have been logged by the request).
	Pending Orders and Web Orders share the cached Woo count.
	"""
	if path.split(".")[0] not in frappe.get_installed_apps():
		return 0
	try:
		return _cached(path, lambda: len(frappe.get_attr(path)()))
	except frappe.PermissionError:
		return 0        # this user may not read the queue; says nothing about it, so not cached
	except Exception:
		frappe.log_error(title=f"Gun Shop card: queue {path} failed, counted as 0")
		return 0        # not cached: the next view tries again


def _report(key: str, execute) -> int:
	return _cached(key, lambda: len(execute({})[1]))


def _pending_orders() -> int:
	return _queue(COUNTER_SOURCE) + _queue(WOO_SOURCE) + _queue(GUNBROKER_SOURCE)


def _to_receive() -> int:
	"""Distributor orders with units still to arrive at the store: the Receive bridge's incoming rule
	(``rank_candidates(_incoming_line_rows())``, what Receive Goods lists as incoming) -- store-bound,
	not a dead state, a line the house said it is sending that is not yet booked or killed."""
	if "ffl_integrations" not in frappe.get_installed_apps():
		return 0
	from ffl_integrations.distributor.receive_bridge import _incoming_line_rows, rank_candidates

	return _cached("to_receive", lambda: len({r["do_name"] for r in rank_candidates(_incoming_line_rows())}))


# card key -> (count function, route the card opens)
CARDS = {
	"pending_orders": (_pending_orders, [PENDING_ORDERS_PAGE]),
	"pending_4473": (lambda: _report("pending_4473", pending_4473_report), ["query-report", "Pending 4473 Orders"]),
	"transfer_pickups": (lambda: _report("transfer_pickups", transfer_pickups_report),
		["query-report", "Pending Transfer Pickups"]),
	"to_receive": (_to_receive, [RESTOCK_PAGE]),
	"web_orders": (lambda: _queue(WOO_SOURCE), [PENDING_ORDERS_PAGE]),
}


def _may_open(route: list) -> bool:
	"""Can this user open the page or report the card links to? A Number Card is shown to whoever
	may read its document type, so a card can reach someone the page behind it turns away; that
	person gets 0, not an error. Uses the Page / Report's own role check; a report also needs report
	permission on its reference doctype, the two checks frappe.desk.query_report.get_report_doc
	makes before it runs one, and a disabled report opens for nobody."""
	doctype, name = ("Report", route[1]) if route[0] == "query-report" else ("Page", route[0])
	if not frappe.db.exists(doctype, name):
		return False
	doc = frappe.get_cached_doc(doctype, name)
	if not doc.is_permitted():
		return False
	return doctype == "Page" or (not doc.disabled and bool(frappe.has_permission(doc.ref_doctype, "report")))


@frappe.whitelist()
def get_count(filters=None) -> dict:
	"""The number behind one "Needs attention" card on the Gun Shop home page.

	Args:
	    filters: The card's ``filters_json`` (a JSON object or its string): ``{"card": <key>}``
	        with key one of pending_orders, pending_4473, transfer_pickups, to_receive,
	        web_orders.

	Returns:
	    ``{"value": count, "fieldtype": "Int", "route": [...]}`` in the shape Frappe's custom
	    Number Card expects; clicking the card opens ``route``. ``value`` is 0 for a user who may
	    not open that page or report.
	"""
	frappe.only_for("Desk User")        # who a Number Card is shown to; the card itself decides what they count
	key = (frappe.parse_json(filters) or {}).get("card")
	if key not in CARDS:
		frappe.throw(_("Unknown Gun Shop card: {0}").format(key))
	count, route = CARDS[key]
	return {"value": count() if _may_open(route) else 0, "fieldtype": "Int", "route": route}
