"""Whitelisted endpoints for the GunBroker buttons on the Serial No and Item forms.

Thin: each one gates on a role, hands off to `listings`, and returns what the
form needs. The rules live in `listings` so the MCP tools and the import skill
reach exactly the same guards as the button does.

Guard refusals come back as ``{"ok": False, "skipped": ..., "message": ...}``
rather than as a throw. A throw would roll the request back and take the
`gb_sync_error` the guard just wrote with it, so the operator would see the
reason once and the form would forget it; the client scripts show the message
and reload instead.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.roles import STOCK_ROLES, SYSTEM_ROLES
from ffl_integrations.gunbroker import item_listings, listings
from ffl_integrations.gunbroker.client import GunBrokerClient, GunBrokerConfigError


@frappe.whitelist()
def test_connection() -> dict:
	"""Check the GunBroker credentials and report which environment they reach.

	Returns:
	    {"ok": True, "sandbox": bool, "base_url": str, "server_time": str,
	    "account": str, "gateway_enabled": bool, "warnings": [str]} — or
	    {"ok": False, "sandbox": bool, "error": str} when it could not connect.
	    ``sandbox`` is included either way so an operator can see at a glance
	    which GunBroker was just contacted.
	"""
	frappe.only_for(SYSTEM_ROLES)
	return listings.test_connection()


@frappe.whitelist()
def fetch_listing_defaults() -> dict:
	"""Read the GunBroker account's own listing defaults.

	Returns:
	    {"ok": True, "defaults": {...}} — the raw account defaults, including
	    ``gatewayEnabled`` and ``standardTextID``, for filling in this store's
	    settings.
	"""
	frappe.only_for(SYSTEM_ROLES)
	try:
		client = GunBrokerClient.from_settings()
		return {"ok": True, "defaults": client.get_listing_defaults()}
	except GunBrokerConfigError as exc:
		frappe.throw(_("GunBroker is not configured: {0}").format(exc))


@frappe.whitelist()
def push_serial_now(serial_no: str, relist: int | str = 0,
		ship_cost: float | str | None = None, listing: dict | str | None = None,
		category_id: int | str | None = None, free_shipping: int | str | None = None) -> dict:
	"""List one firearm on GunBroker.

	Args:
	    serial_no: The Serial No to list.
	    relist: Pass 1 when the operator is deliberately re-listing a gun that was
	        ended by hand. It lifts that one sentinel and nothing else — it cannot
	        override custody, pricing or cross-channel guards.
	    ship_cost: The buyer-paid shipping rate for this listing, saved on the
	        Serial No before it is listed. Omit to keep what is there; 0 clears
	        it, so the default in GunBroker Settings applies.
	    free_shipping: 1 lists the gun with free shipping (seller pays,
	        whatever the rate), 0 takes that off. Saved on the Serial No.
	        Omit to keep what is there.
	    category_id: The GunBroker category chosen in the Send dialog, one of
	        the ``category.options`` listing_offer returned. Saved on the
	        gun's Item, so every gun of that model lists there, and put back
	        if the push is refused. Omit to keep the category the gun
	        resolves to now.
	    listing: The terms chosen for this listing, as JSON: {"type": "Fixed
	        Price" | "Auction", "title": "", "price": 0, "starting_bid": 0,
	        "reserve_price": 0, "buy_now_price": 0, "duration": 7,
	        "auto_relist": 2, "condition": 0, "premium": {"featured": 0, "showcase": 0,
	        "sponsored_onsite": 0, "view_counter": 0}}. Saved on the Serial
	        No before it is listed and refused, with the reason, when GunBroker
	        would refuse it. An empty title or a zero price follows the item
	        name / the POS sell price. "condition" is GunBroker's code for
	        this gun: 1 Factory New, 2 New Old Stock, 3 Used; 0 follows the
	        Item (used or not), and an antique always lists as Used. Omit
	        the key to keep what is recorded. "premium" is GunBroker's paid
	        promotions (extra fees, one per ticked flag): they can only be
	        bought here, when the listing is created, never added to a live
	        one, and a POS relist buys the recorded ones again. A "premium"
	        object replaces all four flags (omitted names count as off); omit
	        the key to keep the recorded ones; an unknown name is refused.
	        Omit "listing" to list on the terms already recorded (fixed price
	        by default, no paid promotions unless recorded).

	Returns:
	    {"ok": True, "gb_item_id": "<GunBroker item number>", "gb_url": "<the
	    listing's page>", "warnings": [...]} on success ("no_upc" among the
	    warnings means GunBroker had no UPC to fill Manufacturer / Model /
	    Caliber from), or {"ok": False, "skipped": "<reason>", "message":
	    "<why>"} when a guard refused. A refusal is a normal answer, not an error.
	"""
	frappe.only_for(STOCK_ROLES)
	if ship_cost not in (None, ""):
		listings.set_ship_cost("Serial No", serial_no, ship_cost)
	if free_shipping not in (None, ""):
		listings.set_free_shipping(serial_no, free_shipping)
	moved = None
	if category_id not in (None, ""):
		moved = listings.set_listing_category(serial_no, category_id)
	if listing not in (None, ""):
		if isinstance(listing, str):
			listing = frappe.parse_json(listing)
		listings.set_listing_spec(serial_no, listing)
	result = listings.push_serial(serial_no, relist=bool(frappe.utils.cint(relist)))
	if moved:
		listings.settle_listing_category(moved, listed=bool(result.get("ok")))
	return result


@frappe.whitelist()
def listing_offer(serial_no: str) -> dict:
	"""What the Send dialog offers for one gun: the figures a listing would
	carry if nothing were changed, and the choices GunBroker allows.

	Args:
	    serial_no: The Serial No about to be listed.

	Returns:
	    {"title", "derived_title", "price", "pos_price", "type", "types",
	    "starting_bid", "reserve_price", "buy_now_price", "duration",
	    "durations", "auto_relist", "auto_relist_options": [{"value",
	    "label"}], "shipping": {"own", "default", "free", "effective"},
	    "category": {"id": <the category it would list under, 0 for none>,
	    "options": [{"id", "name"}]}, "premium": {"featured", "showcase",
	    "sponsored_onsite", "view_counter"} (the paid promotions recorded on
	    the gun, each true/false; a listing made now buys them again),
	    "condition" (the code recorded for this gun, 0 for none),
	    "derived_condition" (what the Item alone gives), "condition_options"
	    (the codes allowed: only the derived one for an antique)}.
	"""
	frappe.only_for(STOCK_ROLES)
	return listings.listing_offer(serial_no)


@frappe.whitelist()
def listing_ship_cost(doctype: str, name: str) -> dict:
	"""What a listing of this record would charge the buyer for shipping.

	Args:
	    doctype: "Serial No" for a gun, "Item" for a model-level listing.
	    name: The record.

	Returns:
	    {"own": <rate saved on the record, 0 for none>, "default": <the store
	    default for this record's kind>, "free": <true when the gun is marked
	    free shipping, always false for an Item>, "effective": <the rate a
	    listing sent now would carry, 0 when free>}.
	"""
	frappe.only_for(STOCK_ROLES)
	return listings.ship_cost_summary(doctype, name)


@frappe.whitelist()
def reprice_serial_now(serial_no: str) -> dict:
	"""Send this gun's current sell price to its GunBroker listing.

	Args:
	    serial_no: The Serial No whose listing should be repriced.

	Returns:
	    {"ok": True, "fixed_price": <price>, "verified": True} — verified means
	    the listing was read back and shows the new price, which is the only
	    proof that counts here. Otherwise a skip dict.
	"""
	frappe.only_for(STOCK_ROLES)
	return listings.update_price(serial_no)


@frappe.whitelist()
def replace_pictures_now(serial_no: str = "", item_code: str = "") -> dict:
	"""Replace a live listing's photos with the ones a new push would send now.

	For when the photos or the store's watermark changed after the listing went
	up. GunBroker keeps the search thumbnail it took at listing time.

	Args:
	    serial_no: The gun whose per-serial listing to update.
	    item_code: Or the model / item whose item-level listing to update.

	Returns:
	    {"ok": True, "gb_item_id": ..., "uploaded": n, "removed": n, "warnings": [...]},
	    or a skip dict (not listed, an auction with bids, no photos, GunBroker refused).
	"""
	frappe.only_for(SYSTEM_ROLES)
	if bool(serial_no) == bool(item_code):
		frappe.throw(_("Give either a serial number or an item code."))
	if serial_no:
		return listings.replace_pictures("Serial No", serial_no)
	return listings.replace_pictures("Item", item_code)


@frappe.whitelist()
def end_listing_now(serial_no: str, reason: str | None = None) -> dict:
	"""End this gun's GunBroker listing.

	Args:
	    serial_no: The Serial No whose listing should end.
	    reason: Optional note recorded in the alert if it cannot be ended
	        automatically.

	Returns:
	    {"ok": True, "strategy": "delete_endpoint", "confirmed": True} when
	    GunBroker confirmed the listing is inactive. Otherwise "confirmed" is
	    False and the answer always carries "pending_manual": True, "gb_url" (the
	    listing, to end by hand) and a "message" saying it can still be bought —
	    whether the cause was the Manual Only setting, a failed call, or a
	    read-back that still showed it active, because the person reading this
	    has to do the same thing in every one of those cases. "strategy" says
	    whether the system will retry by itself, and "downgraded": True appears
	    when GunBroker withdrew permission and the setting was changed as a
	    result.
	"""
	frappe.only_for(STOCK_ROLES)
	return listings.end_listing(serial_no, reason=reason)


@frappe.whitelist()
def listing_status(serial_no: str) -> dict:
	"""Report what this POS and GunBroker each think of one listing.

	Args:
	    serial_no: The Serial No to inspect.

	Returns:
	    {"ok": True, "state": <one of unlisted|push_pending|listed|end_pending|
	    sold|sold_unknown|ended>, "gb_item_id": str, "gb_sync_error": str,
	    "remote": {...}|None} — ``remote`` is what GunBroker currently says, or
	    None when there is no listing to ask about. Read-only.
	"""
	frappe.only_for(STOCK_ROLES)
	return listings.listing_status(serial_no)


@frappe.whitelist()
def push_item_now(item_code: str, relist: int | str = 0,
		ship_cost: float | str | None = None, listing: dict | str | None = None) -> dict:
	"""List one Item on GunBroker as a quantity listing.

	Ammunition, optics, parts — one listing standing for the stock on hand — and a
	uniform-price firearm, listed as a MODEL: one listing standing for the guns that
	are free to sell, filed under the bare item code with the licence requirement on.
	A gun priced one by one is refused here; those are listed one at a time from the
	Serial No form.

	Args:
	    item_code: The Item to list.
	    relist: Pass 1 when the operator is deliberately re-listing something
	        that was ended by hand. It lifts that one sentinel and nothing else.
	    ship_cost: The buyer-paid shipping rate for this listing, saved on the
	        Item before it is listed. Omit to keep what is there; 0 clears it, so
	        the default in GunBroker Settings applies (the long gun / handgun one for
	        a model, the accessory / ammo one otherwise).
	    listing: For a model only, the terms chosen in the Send dialog, as JSON:
	        {"type": "Fixed Price", "premium": {"featured": 0, "showcase": 0,
	        "sponsored_onsite": 0, "view_counter": 0}}. "premium" is GunBroker's
	        paid promotions (extra fees, one per ticked flag): they can only be
	        bought here, when the listing is created, never added to a live one,
	        and a re-listing the system does by itself after a sell-out never buys
	        them. A "premium" object replaces all four flags (omitted names count
	        as off); omit the key to keep the recorded ones; an unknown name is
	        refused. A model lists at a fixed price only, and its title, price,
	        category and condition follow its Item, so any other key is refused.
	        Omit "listing" to list on the terms already recorded.

	Returns:
	    {"ok": True, "gb_item_id": "<GunBroker item number>", "quantity": <n>,
	    "warnings": [...]} on success, or {"ok": False, "skipped": "<reason>",
	    "message": "<why>"} when a guard refused. A refusal is a normal answer.
	"""
	frappe.only_for(STOCK_ROLES)
	if ship_cost not in (None, ""):
		listings.set_ship_cost("Item", item_code, ship_cost)
	if listing not in (None, ""):
		if isinstance(listing, str):
			listing = frappe.parse_json(listing)
		item_listings.set_listing_spec(item_code, listing)
	# A person pressing Send: the only caller allowed to buy the paid promotions.
	return item_listings.push_item(
		item_code, relist=bool(frappe.utils.cint(relist)), premium=True)


@frappe.whitelist()
def item_listing_offer(item_code: str) -> dict:
	"""What the Send dialog offers for a model: the figures a listing sent now would
	carry, so the person decides with them in front of them.

	Args:
	    item_code: The uniform-price firearm Item about to be listed.

	Returns:
	    {"is_model": <true for a uniform-price firearm>, "title", "sellable"
	    (guns that can be promised now, null when a sales channel could not
	    say), "price" (what GunBroker would show: the web store's price today),
	    "regular_price", "on_sale" (true when a promotion makes price lower),
	    "sale_to" (the promotion's last day, or ""), "category": {"id" (0 for
	    none), "name", "ambiguous" (the category depends on a year the Item
	    lacks)}, "shipping": {"own", "default", "free", "effective"}, "premium":
	    {"featured", "showcase", "sponsored_onsite", "view_counter"} (recorded on
	    the Item; a listing made now buys them again)}.
	"""
	frappe.only_for(STOCK_ROLES)
	return item_listings.listing_offer(item_code)


@frappe.whitelist()
def reprice_item_now(item_code: str) -> dict:
	"""Send this Item's current selling rate to its GunBroker listing.

	A model (uniform-price firearm) is sent the web store's price today, a running
	promotion included; anything else its standard rate.

	Args:
	    item_code: The Item whose listing should be repriced.

	Returns:
	    {"ok": True, "fixed_price": <price>, "verified": True} when the listing
	    was read back showing the new price, otherwise a skip dict.
	"""
	frappe.only_for(STOCK_ROLES)
	return item_listings.update_item_price(item_code)


@frappe.whitelist()
def sync_item_quantity_now(item_code: str) -> dict:
	"""Push this Item's stock count to its GunBroker listing.

	Normally automatic — every stock movement of a listed item triggers it — so
	this is the manual repair for a count that has drifted. A model's count is the
	guns free to sell less the units GunBroker has sold that no order has brought
	in yet; when GunBroker cannot be read nothing is sent.

	Args:
	    item_code: The listed Item.

	Returns:
	    {"ok": True, "quantity": <n>} — with "unchanged": True when GunBroker
	    already held that number, or the end-listing result when stock is zero
	    (GunBroker rejects a quantity of zero, so selling out ends the listing).
	"""
	frappe.only_for(STOCK_ROLES)
	return item_listings.sync_item_quantity(item_code)


@frappe.whitelist()
def end_item_listing_now(item_code: str, reason: str | None = None) -> dict:
	"""End this Item's GunBroker listing.

	Args:
	    item_code: The Item whose listing should end.
	    reason: Optional note recorded in the alert if it cannot be ended
	        automatically.

	Returns:
	    {"ok": True, "strategy": "delete_endpoint", "confirmed": True} when
	    GunBroker confirmed it, or a pending-manual result naming the listing
	    that a person still has to end there.
	"""
	frappe.only_for(STOCK_ROLES)
	return item_listings.end_item_listing(item_code, reason=reason)


@frappe.whitelist()
def item_listing_status(item_code: str) -> dict:
	"""What this Item's GunBroker listing looks like, locally and remotely.

	Args:
	    item_code: The Item to report on.

	Returns:
	    {"ok": True, "state": ..., "gb_item_id": ..., "gb_sync_error": ...,
	    "quantity_local": <n>, "quantity_pushed": <n>, "remote": {...} | None} —
	    the two quantities are what show a drift between stock and the listing.
	"""
	frappe.only_for(STOCK_ROLES)
	return item_listings.item_listing_status(item_code)


@frappe.whitelist()
def clear_sold_unknown_now(serial_no: str, note: str) -> dict:
	"""Clear the "may already be sold on GunBroker" flag after checking.

	Args:
	    serial_no: The Serial No to clear.
	    note: What was checked, recorded as a Comment on the serial. Required —
	        this flag exists to stop a gun being sold twice, and lifting it is an
	        assertion somebody should be able to stand behind afterwards.

	Returns:
	    {"ok": True, "changed": bool}.
	"""
	frappe.only_for(STOCK_ROLES)
	if not (note or "").strip():
		frappe.throw(_(
			"Say what you checked on GunBroker before clearing this flag — it is "
			"what stops the gun being sold twice."))
	return listings.clear_sold_unknown(serial_no, note.strip())


@frappe.whitelist()
def sync_orders_now() -> dict:
	"""Run the GunBroker order poll now instead of waiting for the schedule.

	Queued rather than run inline: a poll can walk many pages and fetch every
	order in full, which is far too long to hold a Desk request open. The job is
	de-duplicated against the scheduled one, so pressing the button while a poll
	is already running joins it rather than starting a second.

	Returns:
	    {"queued": True}.
	"""
	frappe.only_for(SYSTEM_ROLES)
	from ffl_integrations.gunbroker import orders

	frappe.enqueue("ffl_integrations.gunbroker.orders.run_order_sync",
		queue="long", job_id=orders.SYNC_JOB_ID, deduplicate=True)
	return {"queued": True}
