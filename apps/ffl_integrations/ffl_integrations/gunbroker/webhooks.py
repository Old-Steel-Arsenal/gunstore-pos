"""GunBroker → us webhook receivers.

POS learns about GunBroker events by polling (orders every 15 minutes, auctions
every 15 minutes, listings hourly). In the gap a gun that has just sold or taken a
bid is still buyable on the web store and at the counter. These endpoints close the
gap: GunBroker calls them the moment something happens and each one only *nudges*
the existing machinery — it does not replace it. Events are never trusted as the
source of truth (see ``tasks.py``: GunBroker misses, repeats and reorders them); the
polls stay as the safety net, and a lost webhook costs at most one poll interval.

Subscribe each URL (one per event, see :func:`webhook_urls`) at
https://www.gunbroker.com/account/webhooks with the user name and password set in
GunBroker Settings → Advanced → Webhooks. GunBroker signs nothing: it sends HTTP
Basic credentials and that is the only authentication there is.

Three decisions are load-bearing.

**Basic auth has to be taken off the request before Frappe sees it.** Frappe reads
``Authorization: Basic …`` as an API key and secret, and on any mismatch answers its
own 401 *before* the endpoint runs. Credentials that are ours, not a Frappe user's,
would therefore never arrive. ``webhook_shield.shield_basic_auth`` (a
``before_request`` hook, which runs ahead of that check) lifts the header off these
URLs only and parks it for the endpoint to compare itself.

**Every refusal is the same 401, byte for byte.** Switched off, credentials not
configured, header missing, malformed or wrong: one body. The endpoint is
guest-open by construction and must not tell an anonymous caller whether this site
runs GunBroker or is half-configured (the FastBound receiver reasons the same way).

**The body is untrusted and is never kept.** Only IDs, the SKU and (for a failed
listing) GunBroker's error text are taken out of it. The ItemsSold body carries the
buyer's name, address, phone and e-mail; it is not logged, not stored and not echoed.
The order itself is fetched from GunBroker's API by the poll, with our credentials.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from typing import Any, Callable

import frappe
from frappe.utils import cint, flt, get_url, now_datetime, strip_html
from frappe.utils.password import get_decrypted_password

from ffl_core.utils import commit as safe_commit  # no-op inside the test transaction
from ffl_integrations.gunbroker import item_listings, listings, orders, tasks
from ffl_integrations.gunbroker.webhook_shield import MODULE_PATH, STASH
from ffl_integrations.gunbroker.client import get_field
from ffl_integrations.integration_gate import gunbroker_enabled
from ffl_integrations.notifications import safe_log_error

SETTINGS_DOCTYPE = "GunBroker Settings"

# The one refusal. Named, not inlined, so the switched-off, unconfigured and
# bad-credentials branches cannot drift into three distinguishable answers.
UNAUTHORIZED = "Unauthorized"

# A real event is a few KB (an order with its lines is the biggest). Anything past
# this is not GunBroker, and parsing it would be work an unauthenticated-until-just-now
# caller chose for us. Frappe has already read the body by the time we run (25 MB cap),
# so this bounds what we parse and look at, not what was received.
MAX_BODY_BYTES = 256 * 1024

# Throttles the "credentials are blank" Error Log to one row an hour: the endpoint
# is guest-open, so an unthrottled row per anonymous call is disk amplification.
_UNCONFIGURED_LOG_KEY = "gunbroker_webhook:unconfigured_logged"

# Longest SKU / error text we carry. SKUs are `item_code::serial`; both are short.
_SKU_MAX = 140
_ERROR_MAX = 500

# url slug → (GunBroker's event name, label for the settings page)
EVENTS: dict[str, str] = {
	"items_sold": "ItemsSold",
	"winning_bid_received": "WinningBidReceived",
	"order_status_update": "OrderStatusUpdate",
	"items_unsold_not_relisted": "ItemsUnsoldNotRelisted",
	"fixed_price_listing_failed": "FixedPriceListingFailed",
}


# ---------------------------------------------------------------------------
# Settings page support
# ---------------------------------------------------------------------------

def webhook_urls() -> list[dict]:
	"""The five URLs to paste into gunbroker.com/account/webhooks.

	Built from the site's own URL (``frappe.utils.get_url``), so a staging copy
	shows its own addresses and not production's.
	"""
	return [
		{"event": name, "url": get_url(f"/api/method/{MODULE_PATH}.{slug}")}
		for slug, name in EVENTS.items()
	]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _authorization_header() -> str:
	parked = frappe.local.flags.get(STASH)
	if parked is not None:
		return parked
	try:
		return frappe.get_request_header("Authorization") or ""
	except RuntimeError:  # no request: bench execute, scheduler, tests
		return ""


def _presented_credentials() -> tuple[str, str] | None:
	"""The ``(user, password)`` the caller sent, or None when it sent no usable Basic header."""
	scheme, _sep, token = _authorization_header().strip().partition(" ")
	if scheme.lower() != "basic":
		return None
	try:
		decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
	except (binascii.Error, ValueError):  # UnicodeDecodeError is a ValueError
		return None
	user, sep, password = decoded.partition(":")
	return (user, password) if sep else None


def _configured_credentials() -> tuple[str, str]:
	user = (frappe.db.get_single_value(SETTINGS_DOCTYPE, "webhook_username") or "").strip()
	password = get_decrypted_password(
		SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "webhook_password", raise_exception=False) or ""
	return user, password


def _authorised() -> bool:
	"""True only for an enabled, configured site and exactly the configured credentials."""
	if not gunbroker_enabled():
		return False
	want_user, want_password = _configured_credentials()
	if not want_user or not want_password:
		if not frappe.cache.get_value(_UNCONFIGURED_LOG_KEY):
			frappe.cache.set_value(_UNCONFIGURED_LOG_KEY, 1, expires_in_sec=3600)
			safe_log_error(
				"GunBroker webhook: refused, webhook credentials are not set",
				"GunBroker is switched ON but GunBroker Settings has no webhook user "
				"name and password, so no delivery can verify. The caller was answered "
				"with the ordinary 401. (Logged at most once an hour; the refusals "
				"themselves are not logged.)")
		return False
	presented = _presented_credentials()
	if presented is None:
		return False
	# Both compared, always: no early exit that tells a right user name from a wrong one.
	user_ok = hmac.compare_digest(presented[0].encode(), want_user.encode())
	password_ok = hmac.compare_digest(presented[1].encode(), want_password.encode())
	return user_ok and password_ok


def _refuse() -> dict:
	frappe.local.response["http_status_code"] = 401
	return {"ok": False, "error": UNAUTHORIZED}


# ---------------------------------------------------------------------------
# Reading the (untrusted) body
# ---------------------------------------------------------------------------

def _read_body() -> dict | None:
	"""The JSON object GunBroker sent, or None if it sent anything else (or too much)."""
	try:
		raw = frappe.request.get_data()
	except RuntimeError:
		return None
	if not raw or len(raw) > MAX_BODY_BYTES:
		return None
	try:
		body = json.loads(raw)
	except ValueError:
		return None
	return body if isinstance(body, dict) else None


def _num_id(value: Any) -> str:
	"""A GunBroker item / order number as a string, or "" if it is not one."""
	return listings._numeric_id(value if isinstance(value, (str, int)) else "")


def _text(value: Any, limit: int) -> str:
	"""A short single-line plain string, or "" for anything that is not text."""
	if not isinstance(value, (str, int, float)) or isinstance(value, bool):
		return ""
	return re.sub(r"\s+", " ", strip_html(str(value))).strip()[:limit]


def _item_facts(item: Any) -> dict:
	"""Item number and SKU out of an Item or an Item Summary, whichever casing."""
	return {
		"item_id": _num_id(get_field(item, "itemID", "ItemID", "itemId")),
		"sku": _text(get_field(item, "sku", "SKU"), _SKU_MAX),
	}


def extract_facts(event: str, body: dict) -> dict:
	"""Only what routing needs, per event. Everything else in the body is dropped here."""
	if event in ("items_sold", "order_status_update"):
		return {"order_id": _num_id(get_field(body, "orderID", "OrderID", "orderId"))}
	if event == "winning_bid_received":
		item = get_field(body, "item", "Item")
		item = item if isinstance(item, dict) else body
		bid = get_field(body, "currentBid", "CurrentBid")
		if bid is None:
			bid = get_field(item, "currentBid", "CurrentBid")
		return {
			**_item_facts(item),
			"bid_count": max(cint(get_field(item, "bidCount", "BidCount") or 0), 1),
			"current_bid": flt(bid) if isinstance(bid, (int, float, str)) else 0.0,
			"ends_at": _text(get_field(item, "endingDateTimeUTC", "EndingDateTimeUTC"), 40),
			"fixed_price": get_field(item, "isFixedPrice", "IsFixedPrice") is True,
		}
	if event == "items_unsold_not_relisted":
		return _item_facts(body)
	if event == "fixed_price_listing_failed":
		return {
			**_item_facts(body),
			"error": _text(get_field(
				body, "Error Message", "errorMessage", "ErrorMessage", "error"), _ERROR_MAX),
		}
	return {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def resolve_listing(sku: str, item_id: str) -> tuple[str, str]:
	"""Which of our records a GunBroker listing is: ``("serial", name)``,
	``("item", item_code)`` or ``("", "")``.

	SKU first, item number second — GunBroker mints a new item number at every relist
	and keeps the SKU (``tasks.py`` has the reasoning). The number is still worth
	trying: a listing made by hand has no SKU of ours. A number that matches more than
	one record is ambiguous and resolves to nothing; guessing which gun it was is how
	a bid lands on the wrong one.
	"""
	item_code, serial = orders.split_sku(sku)
	if serial and frappe.db.exists("Serial No", serial):
		return "serial", serial
	if sku and orders.is_item_level_sku(sku):
		return "item", item_code
	if not item_id:
		return "", ""
	# lazy: scans the table (no index on gb_item_id); a store's few thousand serials
	# are nothing, and the SKU path above is the one that normally answers.
	for doctype, kind in (("Serial No", "serial"), ("Item", "item")):
		names = frappe.db.sql_list(
			f"SELECT name FROM `tab{doctype}` WHERE gb_item_id = %(id)s "
			"OR FIND_IN_SET(%(id)s, gb_item_history) LIMIT 2", {"id": item_id})
		if len(names) == 1:
			return kind, names[0]
	return "", ""


def _log_unresolved(event: str, facts: dict) -> None:
	"""One log line, ids only, so a mismatch is findable without keeping the body."""
	frappe.logger("gunbroker.webhooks", allow_site=True).info(
		f"{event}: no matching record for item {facts.get('item_id') or '-'} "
		f"sku {facts.get('sku') or '-'}")


def _enqueue(path: str, **kwargs) -> None:
	frappe.enqueue(path, queue="short", **kwargs)


def _on_order_event(event: str, facts: dict) -> str:
	orders.request_order_sync()
	return f"order {facts['order_id']}" if facts.get("order_id") else ""


def _on_winning_bid(event: str, facts: dict) -> str:
	kind, name = resolve_listing(facts.get("sku", ""), facts.get("item_id", ""))
	if kind == "serial" and not facts.get("fixed_price"):
		_enqueue("ffl_integrations.gunbroker.tasks.gb_bid_received_task",
			serial_no=name, bid_count=facts["bid_count"],
			current_bid=facts["current_bid"], ends_at=facts["ends_at"])
	else:
		# Not a gun we can name: let the full watch read /ItemsSelling and find it.
		_log_unresolved(event, facts)
		tasks.gb_auction_watch()
	return f"item {facts['item_id']}" if facts.get("item_id") else ""


def _reconcile(kind: str, name: str) -> None:
	if kind == "serial":
		_enqueue("ffl_integrations.gunbroker.tasks.gb_sync_serial_listing_task", serial_no=name)
	else:
		_enqueue("ffl_integrations.gunbroker.tasks.gb_sync_item_quantity_task", item_code=name)


def _on_listing_ended(event: str, facts: dict) -> str:
	kind, name = resolve_listing(facts.get("sku", ""), facts.get("item_id", ""))
	if kind:
		_reconcile(kind, name)
	else:
		_log_unresolved(event, facts)
	return f"item {facts['item_id']}" if facts.get("item_id") else ""


def _on_listing_failed(event: str, facts: dict) -> str:
	kind, name = resolve_listing(facts.get("sku", ""), facts.get("item_id", ""))
	if not kind:
		_log_unresolved(event, facts)
		return f"item {facts['item_id']}" if facts.get("item_id") else ""
	if facts.get("error"):
		message = f"GunBroker could not list this: {facts['error']}"
		if kind == "serial":
			listings._log_sync_error(name, message)
		else:
			item_listings.log_sync_error(name, message)
	_reconcile(kind, name)
	return f"item {facts['item_id']}" if facts.get("item_id") else ""


_ROUTES: dict[str, Callable[[str, dict], str]] = {
	"items_sold": _on_order_event,
	"order_status_update": _on_order_event,
	"winning_bid_received": _on_winning_bid,
	"items_unsold_not_relisted": _on_listing_ended,
	"fixed_price_listing_failed": _on_listing_failed,
}


def _record_delivery(event: str, ref: str) -> None:
	"""Stamp the settings page so staff can see deliveries arrive."""
	label = EVENTS[event] + (f" ({ref})" if ref else "")
	frappe.db.set_single_value(SETTINGS_DOCTYPE, {
		"last_webhook_at": now_datetime(),
		"last_webhook_event": label[:140],
	}, update_modified=False)


def _receive(event: str) -> dict:
	"""The one body behind all five endpoints."""
	# Authentication comes first and alone: nothing about the body is read, and no
	# branch below it can be reached, until the credentials have checked out.
	try:
		authorised = _authorised()
	finally:
		frappe.local.flags.pop(STASH, None)
	if not authorised:
		return _refuse()

	body = _read_body()
	if body is None:
		# Still 200: GunBroker documents no retry policy, and a retried unreadable
		# body is still unreadable. The poll covers whatever it was announcing.
		frappe.logger("gunbroker.webhooks", allow_site=True).info(
			f"{event}: body missing, too large or not a JSON object")
		return {"ok": True, "handled": False}

	frappe.db.savepoint("webhook_receive")
	try:
		ref = _ROUTES[event](event, extract_facts(event, body))
		_record_delivery(event, ref)
		safe_commit()
	except Exception:  # noqa: BLE001 — answer 200 whatever happens (see the module docstring)
		try:
			frappe.db.rollback(save_point="webhook_receive")
		except Exception:  # noqa: BLE001 — a handler that committed has already ended the savepoint
			frappe.db.rollback()
		# The traceback only: the body is deliberately not part of any log.
		safe_log_error(f"GunBroker webhook {event} failed", frappe.get_traceback())
		return {"ok": False, "error": "internal"}
	return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints — one URL per event, pasted into gunbroker.com/account/webhooks
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def items_sold() -> dict:
	"""GunBroker ItemsSold webhook: an item sold, so pull the order now.

	Payload:
	    GunBroker's Order JSON. Only the order number is read; the order is then
	    fetched from the GunBroker API by the order sync. The buyer details in the
	    body are not stored or logged.

	Returns:
	    200 ``{ok: true}`` once accepted, including when nothing could be matched.
	    401 when the HTTP Basic credentials are missing or wrong, word for word the
	    same as when GunBroker is switched off or the webhook credentials are not
	    set on this site.
	"""
	return _receive("items_sold")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def order_status_update() -> dict:
	"""GunBroker OrderStatusUpdate webhook: an order changed, so pull it now.

	Payload:
	    GunBroker's order-status JSON (OrderID and the milestone dates). Only the
	    order number is read.

	Returns:
	    200 ``{ok: true}`` once accepted. 401 on bad credentials, identical to the
	    other GunBroker webhook endpoints.
	"""
	return _receive("order_status_update")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def winning_bid_received() -> dict:
	"""GunBroker WinningBidReceived webhook: an auction took a bid, so lock the gun now.

	Payload:
	    GunBroker's ``{item, currentBid}`` JSON. The item number, SKU, bid count,
	    current bid and ending time are read; the gun is found by SKU, then item
	    number.

	Returns:
	    200 ``{ok: true}`` once accepted; a gun that cannot be matched falls back to a
	    full auction watch. 401 on bad credentials, identical to the other GunBroker
	    webhook endpoints.
	"""
	return _receive("winning_bid_received")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def items_unsold_not_relisted() -> dict:
	"""GunBroker ItemsUnsoldNotRelisted webhook: a listing ended with no sale, so re-check the gun.

	Payload:
	    GunBroker's Item JSON. Only the item number and SKU are read.

	Returns:
	    200 ``{ok: true}`` once accepted, including when no gun matches. 401 on bad
	    credentials, identical to the other GunBroker webhook endpoints.
	"""
	return _receive("items_unsold_not_relisted")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def fixed_price_listing_failed() -> dict:
	"""GunBroker FixedPriceListingFailed webhook: a listing failed, so record why and re-check the gun.

	Payload:
	    GunBroker's ``{ItemTitle, ItemID, SKU, Quantity, "Error Message"}`` JSON. The
	    item number, SKU and error text are read; the text is stored as plain text on
	    the gun's (or item's) GunBroker sync error.

	Returns:
	    200 ``{ok: true}`` once accepted, including when no gun matches. 401 on bad
	    credentials, identical to the other GunBroker webhook endpoints.
	"""
	return _receive("fixed_price_listing_failed")
