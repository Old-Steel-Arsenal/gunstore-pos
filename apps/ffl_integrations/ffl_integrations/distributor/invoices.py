"""Distributor invoices — a read-only mirror of every invoice on each house's
account, whoever placed the order (this POS, the distributor's website, the
phone). Display only: nothing here builds or touches a Purchase Invoice or the
GL (the AP/COGS reconciliation waits on an accounting decision).

A provider opts in by overriding ``DistributorProvider.list_invoices`` and, for
houses that can list their orders, ``list_open_orders`` (both default to []). Matching to
a Distributor Order, in order: PO number = the order's name, the house's order
number = the order's ``web_ref``, or the invoice number already recorded on one
of the order's lines by the status poll. No match → ``outside_pos``.

Each invoice also records where its goods went (``destination``: Store /
Drop-ship / Unknown — the matched order's lane, else the provider's answer from
the account or the house's ship-to call). Only a Store invoice no POS order
matches can be received on Receive Goods (``receive_bridge.list_incoming`` /
``receive_invoice_lines``); ``received`` is set once every line is booked.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, nowdate

from ffl_core.utils import commit
from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.api import ORDER_ROLES, get_providers
from ffl_integrations.distributor.queue import enqueue_distributor

DOCTYPE = "Distributor Invoice"
# lazy: fixed 30-day window; an invoice older than that on first sync is never
# fetched. Add a backfill `since` argument if the shop wants history.
LOOKBACK_DAYS = 30
SYNC_JOB_ID = "distributor-invoice-sync"
OPEN_ORDERS_KEY = "distributor_open_orders"
# lazy: orders untouched for 90 days are not indexed for invoice matching; widen
# if a house ever invoices an order that long after its last status change.
LINE_INDEX_DAYS = 90
# Site default (DB, survives a cache flush): the day invoice receiving shipped.
# An invoice dated on or before it is mirrored as already received — the goods were
# handled the old way — even when it is re-pulled after a patch dropped its row.
RECEIVE_FROM_KEY = "distributor_invoice_receive_from"
# lazy: at most this many tracking-less invoices are re-asked per hourly run (one
# call each, newest first); raise it if the shop ever has more waiting than that.
TRACKING_REFRESH_LIMIT = 50
# lazy: same bound for re-asking a failed ship-to lookup (one call each per run).
DESTINATION_RETRY_LIMIT = 50


def enqueue_sync() -> None:
	"""Scheduler entry (hourly) and the page button: the sync makes one or two
	HTTP calls per new invoice, so it always runs on the worker — never inside a
	web request — and never twice at once."""
	enqueue_distributor("ffl_integrations.distributor.invoices.sync_invoices",
		timeout=1800, job_id=SYNC_JOB_ID, deduplicate=True)


def sync_invoices() -> dict:
	"""Pull new invoices from every enabled house, cache each house's order
	headers for the page's outside-POS list, then retry matching the unmatched
	invoices (the status poll may have recorded an invoice number since).
	Committed per house and each invoice inserted under its own savepoint: one
	bad invoice or one house down never loses the rest, and a run killed midway
	keeps what it saved (the next run skips those)."""
	since = add_days(nowdate(), -LOOKBACK_DAYS)
	providers = get_providers()
	result = {}
	fresh: set = set()  # inserted this run: their tracking was just read
	failed: set = set()  # houses whose invoice list could not be read this run
	for d in frappe.get_all("Distributor", filters={"enabled": 1}, fields=["name", "provider"]):
		provider = providers.get(d.provider)
		if provider is None:
			continue
		_cache_open_orders(d.name, provider)
		known = set(frappe.get_all(DOCTYPE, filters={"distributor": d.name},
			pluck="invoice_number"))
		try:
			rows = provider.list_invoices(since, known)
		except Exception:  # noqa: BLE001 — one house down must not stop the rest
			frappe.log_error(title=f"Distributor invoice sync failed: {d.name}",
				message=frappe.get_traceback())
			result[d.name] = "error"
			failed.add(d.name)
			continue
		index = line_invoice_index(d.name) if rows else {}
		added = 0
		for row in rows:
			if not row.get("invoice_number") or row["invoice_number"] in known:
				continue
			frappe.db.savepoint("dist_invoice")
			try:
				_insert(d.name, row, index, provider)
			except Exception:  # noqa: BLE001 — skip this one, keep the rest
				frappe.db.rollback(save_point="dist_invoice")
				frappe.log_error(title=f"Distributor invoice not saved: {d.name} "
					f"{row['invoice_number']}", message=frappe.get_traceback())
				continue
			known.add(row["invoice_number"])
			fresh.add((d.name, row["invoice_number"]))
			added += 1
		commit()
		result[d.name] = added
	_redecide_destinations(providers, failed)
	_refresh_tracking(providers, fresh, failed)
	commit()
	_rematch()
	commit()
	return result


def _recent_unreceived(extra: dict, limit: int) -> list:
	return frappe.get_all(DOCTYPE, filters={
		"outside_pos": 1, "received": 0, "creation": [">=", add_days(nowdate(), -LOOKBACK_DAYS)],
		**extra}, fields=["name", "distributor", "invoice_number", "account", "order_ref", "po_number"],
		order_by="creation desc", limit=limit)


def _provider_of(providers: dict) -> dict:
	return {d.name: providers.get(d.provider) for d in frappe.get_all(
		"Distributor", filters={"enabled": 1}, fields=["name", "provider"])}


def _redecide_destinations(providers: dict, failed=()) -> None:
	"""A destination the house could not be asked for (``NOTE_FAILED``: the call
	failed) stays Unknown — never receivable — but is asked again each hourly run while
	the invoice is unreceived and inside the window. An answer the house gave (an empty
	ship-to, an address that matches nothing) is settled and is not re-asked. Bounded by
	DESTINATION_RETRY_LIMIT; a house whose list failed this run is skipped."""
	provider_of = _provider_of(providers)
	for inv in _recent_unreceived({"destination": lanes.DEST_UNKNOWN,
			"destination_note": lanes.NOTE_FAILED}, DESTINATION_RETRY_LIMIT):
		provider = provider_of.get(inv.distributor)
		if provider is None or inv.distributor in failed:
			continue
		destination, note = destination_for(provider, inv, None)
		if (destination, note) != (lanes.DEST_UNKNOWN, lanes.NOTE_FAILED):
			frappe.db.set_value(DOCTYPE, inv.name,
				{"destination": destination, "destination_note": note}, update_modified=False)


def _refresh_tracking(providers: dict, fresh=(), failed=()) -> None:
	"""Tracking is read once, when an invoice is first mirrored — but a parcel can
	ship after its invoice posts, and Receive Goods only offers a parcel that has
	tracking. Re-ask, hourly, for the recent unreceived invoices still showing none:
	Store ones first, then Unknown ones that might still turn out to be Store (a
	confirmed-empty ship-to is a drop-ship: it would only burn the budget). Bounded by
	TRACKING_REFRESH_LIMIT; a house whose list failed this run is skipped; one invoice
	failing is logged and skipped."""
	no_tracking = {"tracking_numbers": ["is", "not set"]}
	rows = _recent_unreceived({**no_tracking, "destination": lanes.DEST_STORE},
		TRACKING_REFRESH_LIMIT)
	if len(rows) < TRACKING_REFRESH_LIMIT:
		rows += _recent_unreceived({**no_tracking, "destination": lanes.DEST_UNKNOWN,
			"destination_note": ["!=", lanes.NOTE_NO_ANSWER]}, TRACKING_REFRESH_LIMIT - len(rows))
	provider_of = _provider_of(providers)
	for inv in rows:
		provider = provider_of.get(inv.distributor)
		if provider is None or inv.distributor in failed or (inv.distributor, inv.invoice_number) in fresh:
			continue
		try:
			numbers = provider.invoice_tracking(inv)
		except Exception:  # noqa: BLE001 — retried next run
			frappe.log_error(title=f"Distributor invoice tracking refresh failed: {inv.distributor} "
				f"{inv.invoice_number}", message=frappe.get_traceback())
			continue
		if numbers:
			frappe.db.set_value(DOCTYPE, inv.name, "tracking_numbers", ",".join(numbers),
				update_modified=False)


def _cache_open_orders(distributor: str, provider) -> None:
	"""The house's order headers, read here on the worker so the page never makes
	the live call. Kept two hours (two sync runs) — stale beats a hung page."""
	try:
		orders = provider.list_open_orders()
	except Exception:  # noqa: BLE001 — the invoices still sync
		frappe.log_error(title=f"Distributor open-order read failed: {distributor}",
			message=frappe.get_traceback())
		return
	frappe.cache.set_value(f"{OPEN_ORDERS_KEY}:{distributor}", orders, expires_in_sec=7200)


def destination_for(provider, row: dict, order: str | None) -> tuple:
	"""``(destination, note)`` (``lanes.DEST_*``): the matched order's lane, else the
	provider's answer for an invoice no POS order matches. A provider that fails
	answers Unknown noted ``NOTE_FAILED`` — never Store on doubt, and asked again."""
	if order:
		return lanes.destination_of_order_type(
			frappe.db.get_value("Distributor Order", order, "order_type")), ""
	try:
		return provider.invoice_destination(row)
	except Exception:  # noqa: BLE001 — Unknown is the safe answer
		frappe.log_error(title=f"Distributor invoice destination failed: {row.get('invoice_number')}",
			message=frappe.get_traceback())
		return lanes.DEST_UNKNOWN, lanes.NOTE_FAILED


def _predates_receiving(invoice_date) -> bool:
	since = frappe.db.get_default(RECEIVE_FROM_KEY)
	# On the day itself too: a same-day parcel the old flow already handled, re-mirrored
	# after the upgrade, must not turn up as new.
	return bool(since and invoice_date and str(invoice_date)[:10] <= str(since)[:10])


def _insert(distributor: str, row: dict, index: dict, provider) -> None:
	order = match_order(distributor, row, index)
	destination, note = destination_for(provider, row, order)
	frappe.get_doc({
		"doctype": DOCTYPE, "distributor": distributor,
		"invoice_number": row["invoice_number"], "invoice_date": row.get("invoice_date"),
		"account": row.get("account") or "", "po_number": row.get("po_number") or "",
		"order_ref": row.get("order_ref") or "", "total": row.get("total") or 0,
		"freight": row.get("freight") or 0, "fees": row.get("fees") or 0,
		"tracking_numbers": ",".join(row.get("tracking_numbers") or []),
		"distributor_order": order, "outside_pos": 0 if order else 1,
		"destination": destination, "destination_note": note,
		"received": 1 if _predates_receiving(row.get("invoice_date")) else 0,
		"lines": [{k: ln.get(k) for k in ("sku", "description", "qty", "unit_cost", "amount",
			"tracking_number", "serials", "upc")} for ln in row.get("lines") or []],
	}).insert(ignore_permissions=True)


def line_invoice_index(distributor: str) -> dict:
	"""{invoice number: Distributor Order} from the invoice numbers the status poll
	recorded on this house's order lines — one read per sync instead of a LIKE
	scan per invoice. Orders touched in the last LINE_INDEX_DAYS only (every poll
	bumps ``modified``, and an invoice is at most LOOKBACK_DAYS old)."""
	orders = frappe.get_all("Distributor Order", filters={"distributor": distributor,
		"modified": [">=", add_days(nowdate(), -LINE_INDEX_DAYS)]}, pluck="name")
	index = {}
	if orders:
		for ln in frappe.get_all("Distributor Order Line", filters={
				"parenttype": "Distributor Order", "parent": ["in", orders],
				"invoices": ["is", "set"]}, fields=["parent", "invoices"]):
			for inv in (ln.invoices or "").split(","):
				if inv.strip():
					index.setdefault(inv.strip(), ln.parent)
	return index


def order_with_invoice(distributor: str, number: str) -> str | None:
	"""The Distributor Order of this house whose lines record this invoice number
	(the status poll writes them), by a direct read — the same answer
	``line_invoice_index`` gives for one invoice, without building the whole index."""
	number = (number or "").strip()
	if not number:
		return None
	for ln in frappe.get_all("Distributor Order Line", filters={
			"parenttype": "Distributor Order", "invoices": ["like", f"%{number}%"]},
			fields=["parent", "invoices"]):
		# LIKE also matches "12345" inside "123456" (and `_`); confirm the exact token.
		if number in [i.strip() for i in (ln.invoices or "").split(",")] and \
				frappe.db.get_value("Distributor Order", ln.parent, "distributor") == distributor:
			return ln.parent
	return None


def match_order(distributor: str, row: dict, index: dict) -> str | None:
	"""The Distributor Order this invoice (or order header) bills, or None: PO =
	the order's name, the house's order number = its ``web_ref``, or the invoice
	number in ``index`` (``line_invoice_index``)."""
	po = (row.get("po_number") or "").strip()
	if po and frappe.db.get_value("Distributor Order", po, "distributor") == distributor:
		return po
	refs = [r.strip() for r in (row.get("order_ref") or "").split(",") if r.strip()]
	if refs:
		name = frappe.db.get_value("Distributor Order",
			{"distributor": distributor, "web_ref": ["in", refs]}, "name")
		if name:
			return name
	return index.get((row.get("invoice_number") or "").strip()) or None


def _rematch() -> None:
	# By creation, not invoice_date: an RSR invoice can land with no ship date.
	since = add_days(nowdate(), -LOOKBACK_DAYS)
	indexes: dict = {}
	for inv in frappe.get_all(DOCTYPE, filters={"outside_pos": 1, "creation": [">=", since]},
			fields=["name", "distributor", "invoice_number", "po_number", "order_ref"]):
		if inv.distributor not in indexes:
			indexes[inv.distributor] = line_invoice_index(inv.distributor)
		order = match_order(inv.distributor, inv, indexes[inv.distributor])
		if order:
			# The order's lane now decides where the goods went.
			frappe.db.set_value(DOCTYPE, inv.name, {"distributor_order": order, "outside_pos": 0,
				"destination": destination_for(None, inv, order)[0], "destination_note": ""},
				update_modified=False)
			_warn_if_already_received(inv, order)


def _warn_if_already_received(inv, order: str) -> None:
	"""An invoice booked through Receive Goods as "outside POS" that a POS order now
	claims: that order's lines still read as awaiting receipt, so a second receive
	would double-book the goods. Tell the office; nothing is changed here."""
	# A locking read: the sync's connection is REPEATABLE READ, and a receive that
	# committed while _rematch ran must be seen.
	booked = frappe.db.get_value("Distributor Invoice Line",
		{"parent": inv.name, "parenttype": DOCTYPE, "received_qty": [">", 0]}, "name",
		for_update=True)
	if booked:
		from ffl_integrations.notifications import notify_admins

		notify_admins(
			f"Invoice {inv.invoice_number} was received, then matched to {order}",
			f"Distributor invoice {inv.invoice_number} ({inv.distributor}) was received on Receive "
			f"Goods as an outside-POS parcel and has now been matched to {order}. Do not receive "
			f"{order} again for these goods: mark its lines received by hand.",
			dedup_key=f"dist_invoice_rematch_{inv.name}")


# ----------------------------------------------------------------- whitelisted

@frappe.whitelist()
def sync_invoices_now() -> dict:
	"""Queue a pull of every house's recent invoices now (the hourly job, on
	demand). Runs on the worker; a pull already queued or running is not doubled.

	Returns:
	    {queued: True}.

	Permissions:
	    Distributor order roles.
	"""
	frappe.only_for(ORDER_ROLES)
	enqueue_sync()
	return {"queued": True}


@frappe.whitelist()
def list_invoices(distributor: str | None = None, outside_pos=0, limit: int = 100) -> list:
	"""Mirrored distributor invoices, newest first, with their lines.

	Args:
	    distributor: Optional Distributor filter.
	    outside_pos: 1 = only invoices no POS order matches.
	    limit: Row cap (default 100, max 500).

	Returns:
	    [{name, distributor, invoice_number, invoice_date, account, po_number,
	    order_ref, distributor_order, outside_pos, destination, received,
	    likely_order, total, freight, fees, tracking_numbers, lines: [{sku,
	    description, qty, unit_cost, amount, tracking_number, serials, upc,
	    item_code, received_qty}]}]. ``likely_order``: for an unreceived Store
	    invoice no order is linked to, the open store-bound POS order that
	    probably is this parcel (awaiting the status poll's link), else "".

	Permissions:
	    Distributor order roles.
	"""
	frappe.only_for(ORDER_ROLES)
	filters = {}
	if distributor:
		filters["distributor"] = distributor
	if cint(outside_pos):
		filters["outside_pos"] = 1
	rows = frappe.get_all(DOCTYPE, filters=filters,
		fields=["name", "distributor", "invoice_number", "invoice_date", "account", "po_number",
			"order_ref", "distributor_order", "outside_pos", "destination", "received", "total",
			"freight", "fees", "tracking_numbers"],
		order_by="invoice_date desc, creation desc", limit=min(max(1, cint(limit) or 100), 500))
	by_parent: dict = {}
	if rows:
		for ln in frappe.get_all("Distributor Invoice Line",
				filters={"parent": ["in", [r.name for r in rows]], "parenttype": DOCTYPE},
				fields=["parent", "sku", "description", "qty", "unit_cost", "amount",
					"tracking_number", "serials", "upc", "item_code", "received_qty"],
				order_by="parent asc, idx asc"):
			by_parent.setdefault(ln.pop("parent"), []).append(ln)
	from ffl_integrations.distributor import receive_bridge

	open_orders = receive_bridge.open_order_index() if any(
		r.outside_pos and r.destination == lanes.DEST_STORE and not r.received for r in rows) else {}
	for r in rows:
		r["lines"] = by_parent.get(r.name, [])
		r["likely_order"] = receive_bridge.likely_order(open_orders, r.distributor, r["lines"]) \
			if r.outside_pos and r.destination == lanes.DEST_STORE and not r.received else ""
	return rows


@frappe.whitelist()
def outside_open_orders() -> list:
	"""Orders on a house's account that were NOT placed from this POS and are not
	invoiced yet, from the order headers the hourly sync cached (houses whose
	provider can list orders; no live distributor call here). Only orders dated
	inside the invoice mirror's window are judged: an older order's invoice may
	predate the mirror, so "no invoice on file" would not mean "not invoiced".

	Returns:
	    [{distributor, order_ref, po_number, order_date, status}].

	Permissions:
	    Distributor order roles.
	"""
	frappe.only_for(ORDER_ROLES)
	since = add_days(nowdate(), -LOOKBACK_DAYS)
	out = []
	for d in frappe.get_all("Distributor", filters={"enabled": 1}, pluck="name"):
		orders = [o for o in frappe.cache.get_value(f"{OPEN_ORDERS_KEY}:{d}") or []
			if o.get("order_date") and o["order_date"] >= since]
		if not orders:
			continue
		invoiced = {ref.strip() for refs in frappe.get_all(DOCTYPE,
			filters={"distributor": d, "order_ref": ["!=", ""]}, pluck="order_ref")
			for ref in refs.split(",") if ref.strip()}
		index = line_invoice_index(d)
		# lazy: an order cancelled at the house (never invoiced) stays listed for
		# the window; filter on its STATUS once the house's status codes are known.
		for o in orders:
			if o["order_ref"] in invoiced or match_order(d, o, index):
				continue
			out.append({"distributor": d, **o})
	return out
