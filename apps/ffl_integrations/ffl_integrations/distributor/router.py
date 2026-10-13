"""Web order → Distributor Order routing (Flow C).

A paid Woo order is split into *lanes*: the lines we ship ourselves (``local``)
and the lines a distributor fulfils (``direct`` accessory / ``fds`` firearm /
``to_store`` procure-to-store). Each drop-ship group becomes its own **Draft**
Distributor Order — v1 never places automatically; a human confirms from
the queue.

PURE core (harness-tested, no DB): :func:`classify_lane`, :func:`route_verdict`,
:func:`build_ship_context`, :func:`plan_route` — the rules the counter shares.
Reading web orders and routing them (``route_source``, its triggers and the
scan) is the website drop-ship layer's (the osa_dropship extension); only
the roll-up of a web order's distributor orders stays here, for completion.

Two gates stand in front of DO creation:

* **paid** — an unpaid order must never reach RSR (there is no cancel API, and
  we would be buying stock against money we do not hold);
* **H6 FFL-captured** — an FDS order ships a firearm to the buyer's transfer
  licence, so that licence must already be on the order. Missing → the order
  parks at ``Pending Route`` and operations are alerted, rather than a gun
  going out against a blank destination.

**Layering.** This module reads Woo Online Order — a
``ffl_woo_sync`` doctype — but never *imports* that app, in either direction:
``ffl_integrations`` must stay installable and usable on a POS-only site that has
no webshop and therefore no ``ffl_woo_sync`` package on disk. Even a
function-local import would be a latent ImportError on that supported
configuration, so the two web-fulfilment call sites late-bind by dotted path
(``tasks.woo_attr``) and everything reachable from the scheduler guards on the
doctype existing.

The cost is that two rules necessarily exist on both sides — the paid-status set
(:data:`PAID_WOO_STATUSES`) and the lane classification (:func:`classify_lane`).
Both copies are pinned against each other by
``tests/test_distributor_layering.py``; neither may be edited alone.
"""
from __future__ import annotations

import frappe

# `lanes` is taken in this module: route_verdict's keyword argument and plan_route's
# local are both LISTS of lane names, and either would shadow the module.
from ffl_integrations.distributor import lanes as lane_table
from ffl_integrations.distributor import policy

# A route lane names the same thing a plan's mode does (see lanes.py), plus "local"
# — the one lane no distributor runs.
LANE_LOCAL = "local"
LANE_TO_STORE = lane_table.TO_STORE
LANE_FDS = lane_table.FDS
LANE_DIRECT = lane_table.DIRECT

DROPSHIP_LANES = frozenset({LANE_TO_STORE, LANE_FDS, LANE_DIRECT})

# route_verdict reasons.
REASON_NO_DROPSHIP = "no_dropship"
REASON_UNPAID = "unpaid"
REASON_FFL_MISSING = "ffl_missing"
REASON_FFL_UNRESOLVED = "ffl_unresolved"
REASON_FFL_EXPIRED = "ffl_expired"
REASON_FFL_INCOMPLETE = "ffl_incomplete"
REASON_SHIP_TO_MISSING = "ship_to_missing"

# The FFL reasons that hold an FDS order — all routed through the same park+alert
# channel (canonical validation: don't downgrade).
FFL_HOLD_REASONS = frozenset({
	REASON_FFL_MISSING, REASON_FFL_UNRESOLVED, REASON_FFL_EXPIRED, REASON_FFL_INCOMPLETE})

# Human-readable reasons for the confirmation queue / alerts.
REASON_TEXT = {
	REASON_UNPAID: "not paid on WooCommerce yet",
	REASON_FFL_MISSING: (
		"the buyer's transfer FFL has not been captured — a drop-shipped firearm "
		"ships straight to that licence and the distributor has no cancel API"),
	REASON_FFL_UNRESOLVED: (
		"the buyer's transfer FFL could not be resolved to an ATF record — run "
		"Verify FFL before this firearm can be ordered"),
	REASON_FFL_EXPIRED: (
		"the buyer's transfer FFL has no valid (unexpired) licence on file — a gun "
		"must not ship to an expired FFL, and the distributor has no cancel API"),
	REASON_FFL_INCOMPLETE: (
		"the buyer's transfer FFL is missing a complete premise address (street + a "
		"valid state) — required before a firearm ships"),
	REASON_SHIP_TO_MISSING: (
		"the buyer's shipping address is incomplete — a direct shipment needs a "
		"full destination"),
}

# Deterministic group order. A shuffling plan makes idempotency untestable, and
# firearms first matches the plan_fulfillment convention.
_LANE_SEQUENCE = (LANE_FDS, LANE_TO_STORE, LANE_DIRECT)

# Woo Online Order.dropship_status values.
DS_PENDING_ROUTE = "Pending Route"
DS_ORDERED = "Ordered"
DS_COMPLETE = "Complete"
DS_ERROR = "Error"
# Anything in here means "this order still has distributor work outstanding" —
# the #3 guard in _auto_clear_non_firearm_if_completed keys off the same set.
UNSETTLED_DROPSHIP_STATUSES = frozenset({DS_PENDING_ROUTE, DS_ORDERED, DS_ERROR})

MODE_TO_STORE = "To Store"

# WooCommerce's own `wc_get_is_paid_statuses()` default — the SECOND copy of
# ffl_woo_sync's `fulfillment._PAID_STATUSES`. Duplicated because this app may not
# import that one (see the module docstring); the two are pinned equal by
# tests/test_distributor_layering.py::TestPaidStatusAgreement. **Edit both or
# neither** — getting this wrong means either ordering goods against an unpaid
# order, or never routing a paid one.
PAID_WOO_STATUSES = frozenset({"processing", "completed",
	# the storefront plugin's Awaiting Stock: a paid order whose ship-to-store goods have not
	# arrived (ShipStation flow).
	"awaiting-stock"})

# lane → the `mode` key plan_fulfillment reads out of ship_context.


# ==================================================================== pure core

def classify_lane(*, dropship_distributor=None, is_firearm=False,
		fulfillment_mode="Direct") -> str:
	"""The fulfilment lane for one order line.

	A firearm can never be shipped to a consumer's door, so a drop-shipped gun in
	``Direct`` mode is RSR's **FDS** lane (ship-to = the buyer's transfer FFL). An
	accessory in ``Direct`` mode ships straight to the buyer.

	A blank/unknown ``fulfillment_mode`` on an item that DOES carry a distributor
	resolves as Direct, never as local: the item is sourced from the distributor
	whatever the listing row says, and treating it as local would try to pick a
	serial we do not own.
	"""
	if not str(dropship_distributor or "").strip():
		return LANE_LOCAL
	if str(fulfillment_mode or "").strip() == MODE_TO_STORE:
		return LANE_TO_STORE
	return LANE_FDS if is_firearm else LANE_DIRECT


def accessory_mode(listing_mode, *, brand_policy=None, dropship_blocked=False,
		vendor_approval_required=False) -> str:
	"""Fulfilment mode (``To Store`` / ``Direct``) for an accessory web line.

	A materialised ``Distributor Listing.fulfillment_mode`` wins — it is what the
	item was listed/priced/pushed to Woo under. When it is blank (a data gap: the
	item carries a ``dropship_distributor`` but the listing row lost or never got a
	mode), fall back to the brand-policy chain (brand table → RSR per-item flag → default
	Direct) rather than defaulting a blocked brand to a direct-to-buyer shipment RSR
	would refuse. Firearm lanes are NOT resolved here — their recorded serial-row
	lane is authoritative (the webhook stamped it at order time).
	"""
	set_mode = str(listing_mode or "").strip()
	if set_mode:
		return set_mode
	return policy.resolve_mode(
		brand_policy=brand_policy,
		dropship_blocked=dropship_blocked,
		vendor_approval_required=vendor_approval_required)


def is_dropship_lane(lane) -> bool:
	return lane in DROPSHIP_LANES


def is_paid_woo_status(woo_status) -> bool:
	"""True when a Woo order's live status means the customer has paid.

	Mirror of ``ffl_woo_sync.woocommerce.fulfillment._is_paid_woo_status``,
	including the strip/lower normalisation — the agreement test compares the two
	across a status matrix, not just the constant.
	"""
	return str(woo_status or "").strip().lower() in PAID_WOO_STATUSES


def route_verdict(*, woo_status=None, lanes, ffl_captured, is_paid=None) -> dict:
	"""Should this order be routed to a distributor now? ``{route, reason}``.

	Reason ordering is deliberate: *unpaid* is reported before *ffl_missing* so a
	pending/on-hold order never raises the H6 operations alert — it is not ready
	for placement for a much more ordinary reason.

	``is_paid`` overrides the Woo-status paid check: a counter (Sales Invoice) order
	has no ``woo_status``, and its Draft DO is created regardless (the paid gate is
	at Queue), so it passes ``is_paid=True``. Web callers omit it and keep deriving
	paid from ``woo_status``.
	"""
	lanes = list(lanes or [])
	if not any(is_dropship_lane(l) for l in lanes):
		return {"route": False, "reason": REASON_NO_DROPSHIP}
	paid = is_paid if is_paid is not None else _is_paid(woo_status)
	if not paid:
		return {"route": False, "reason": REASON_UNPAID}
	if LANE_FDS in lanes and not ffl_captured:
		return {"route": False, "reason": REASON_FFL_MISSING}
	return {"route": True, "reason": ""}


def build_ship_context(lane, *, ship_to=None, destination_ffl=None,
		consumer_name=None, contact_num=None, fill_or_kill=False) -> dict:
	"""The ``ship_context`` handed to ``provider.plan_fulfillment`` for one lane.

	Each lane carries exactly the destination it is allowed to know:

	* ``direct`` — the buyer's own address (the ORIGINAL payload ship-to,
	  never re-derived from an Address doc);
	* ``fds`` — the transfer FFL only. The buyer's street address must not leak
	  onto a firearm order;
	* ``to_store`` — neither; the shop is the dealer account's default ship-to.
	"""
	return {
		# A route lane IS a plan mode (lanes.py owns both names); "local" is the one
		# lane no distributor runs, and it never reaches a ship context.
		"mode": lane if lane_table.is_lane(lane) else lane_table.DEFAULT_MODE,
		"ship_to": ship_to if lane == LANE_DIRECT else None,
		"destination_ffl": destination_ffl if lane == LANE_FDS else None,
		"consumer_name": consumer_name or "",
		"contact_num": contact_num or "",
		"buyer_type": "individual",
		# Counter orders are FillOrKill=Y (the buyer is standing there — a short-ship
		# is re-quoted on the spot). Web orders stay N: a short-ship alerts and is
		# partially refunded rather than killing the line.
		"fill_or_kill": bool(fill_or_kill),
	}


def plan_route(snapshot) -> dict:
	"""Turn one order snapshot into the distributor orders it needs.

	``{"route": bool, "reason": str, "groups": [...]}``. A group is one Draft
	Distributor Order to be: ``{lane, distributor, lines, ship_context}``.

	Grouped by **(lane, distributor)** because each combination is a genuinely
	different order: the lanes use different RSR accounts and different
	destinations, and one DO names exactly one distributor.

	A gate failure produces NO groups at all, not a partial plan. Splitting off
	the accessory leg of an order whose firearm leg is blocked would ship half an
	order the buyer may well cancel — and there is no cancel API to undo the half
	that went.
	"""
	snapshot = snapshot or {}
	lines = list(snapshot.get("lines") or [])
	lanes = [l.get("lane") for l in lines]

	verdict = route_verdict(
		woo_status=snapshot.get("woo_status"),
		lanes=lanes,
		ffl_captured=bool(snapshot.get("destination_ffl")),
		is_paid=snapshot.get("is_paid"))
	if not verdict["route"]:
		return {"route": False, "reason": verdict["reason"], "groups": []}

	# A direct shipment needs a complete destination; the snapshot builder returns
	# None for a partial one rather than letting a half address through.
	if LANE_DIRECT in lanes and not snapshot.get("ship_to"):
		return {"route": False, "reason": REASON_SHIP_TO_MISSING, "groups": []}

	grouped: dict = {}
	for line in lines:
		lane = line.get("lane")
		if not is_dropship_lane(lane):
			continue
		grouped.setdefault((lane, line.get("distributor")), []).append(line)

	groups = []
	for lane in _LANE_SEQUENCE:
		keys = sorted((k for k in grouped if k[0] == lane), key=lambda k: str(k[1]))
		for key in keys:
			groups.append({
				"lane": lane,
				"distributor": key[1],
				"lines": grouped[key],
				"ship_context": build_ship_context(
					lane,
					ship_to=snapshot.get("ship_to"),
					destination_ffl=snapshot.get("destination_ffl"),
					consumer_name=snapshot.get("consumer_name"),
					contact_num=snapshot.get("contact_num"),
					fill_or_kill=snapshot.get("fill_or_kill", False)),
			})
	return {"route": True, "reason": "", "groups": groups}


def groups_to_create(planned_groups, existing_orders) -> list:
	"""The planned groups that have no live Distributor Order yet.

	The idempotency key is ``(lane, distributor)``. Both triggers — the doc_event
	and the cron sweep — can fire for one order, and a WooCommerce webhook
	redelivery can fire either again; ordering the same goods twice from RSR is
	not recoverable.

	A **Draft** order counts as existing: it is already sitting in the human
	confirmation queue, and a duplicate would become a duplicate purchase the
	moment both were confirmed. A **Cancelled** one does not — the operator
	withdrew it, so re-routing is a deliberate retry.
	"""
	from ffl_integrations.distributor import order_state

	live = {
		(o.get("lane"), o.get("distributor"))
		for o in (existing_orders or [])
		if o.get("status") != order_state.CANCELLED
	}
	return [g for g in (planned_groups or [])
		if (g.get("lane"), g.get("distributor")) not in live]


def rollup_status(order_statuses) -> str:
	"""One ``dropship_status`` summarising every distributor order on a web order.

	``order_status`` keeps its three values, so all distributor progress is
	expressed here.

	An all-cancelled order rolls up to **Error**, not Complete: nothing shipped,
	and reading it as done would let the completion path invoice a dead order and
	let auto-clear mark it Transferred.
	"""
	from ffl_integrations.distributor import order_state

	statuses = list(order_statuses or [])
	if not statuses:
		return DS_PENDING_ROUTE
	if any(s == order_state.ERROR for s in statuses):
		return DS_ERROR
	if any(s not in order_state.TERMINAL_STATES for s in statuses):
		return DS_ORDERED
	if any(s == order_state.COMPLETE for s in statuses):
		return DS_COMPLETE
	return DS_ERROR


def _is_paid(woo_status) -> bool:
	return is_paid_woo_status(woo_status)


# ================================================================ frappe shells

WOO_ORDER_DT = "Woo Online Order"


def existing_orders(doctype: str, name: str) -> list:
	rows = frappe.get_all(
		"Distributor Order",
		filters={"source_doctype": doctype, "source_name": name},
		fields=["name", "distributor", "status"])
	out = []
	for row in rows:
		# n3: one DO carries exactly one lane in this flow (route_source stamps every
		# line of a group with the group's lane), so the first line's fulfillment IS
		# the order's lane. A future P4b counter mixed-order that mixed lanes on one DO
		# would break this assumption — revisit here if that ships.
		lanes = frappe.get_all(
			"Distributor Order Line", filters={"parent": row["name"]},
			fields=["fulfillment"], limit=1)
		out.append({
			"lane": (lanes[0]["fulfillment"] if lanes else "") or "",
			"distributor": row["distributor"],
			"status": row["status"],
			"name": row["name"],
		})
	return out


def validate_fds_destination(order):
	"""Resolve + validate an FDS order's transfer FFL through the canonical chain.

	Returns ``(atf_record_name, None)`` when the licence resolves and the ATF
	record is unexpired with a complete address, else ``(None, reason)`` — one of
	:data:`FFL_HOLD_REASONS`. B3: the SAME resolve
	(``dealer_transfer._resolve_destination_ffl``) and gate
	(``dealer_transfer.atf_record_block``) the local firearm-disposition path uses,
	so a gun can never drop-ship to a licence the counter would refuse. There is no
	cancel API once RSR ships, which is exactly why this runs before placement.
	"""
	from ffl_core.compliance import dealer_transfer

	license_no = (order.get("ffl_license") or "").strip()
	if not license_no:
		return None, REASON_FFL_MISSING
	try:
		record = dealer_transfer._resolve_destination_ffl(license_no)
	except Exception:  # noqa: BLE001 — resolve throws when it cannot find/create the record
		return None, REASON_FFL_UNRESOLVED
	block = dealer_transfer.atf_record_block(record, require_address=True)
	if not block:
		return record, None
	code = block[0]
	if code == dealer_transfer.FFL_BLOCK_EXPIRED:
		return None, REASON_FFL_EXPIRED
	if code == dealer_transfer.FFL_BLOCK_INCOMPLETE_ADDRESS:
		return None, REASON_FFL_INCOMPLETE
	# NOT_LINKED / NOT_ON_FILE — resolve returned a name the gate can't verify.
	return None, REASON_FFL_UNRESOLVED


def sync_dropship_status(order_name: str, *, routed: bool = False) -> str:
	"""Roll the order's distributor orders up onto ``dropship_status``.

	A drop-ship line that no live distributor order covers — its group was held
	for a human (no house, a planning refusal), or it was never selected — makes
	the order Error, whatever the orders that do exist say: completing it would
	invoice goods nobody ordered. Checked once some order exists, or right after
	a routing attempt (``routed``); before routing has run, no orders is simply
	Pending Route."""
	statuses = frappe.get_all(
		"Distributor Order",
		filters={"source_doctype": WOO_ORDER_DT, "source_name": order_name},
		pluck="status")
	status = rollup_status(statuses)
	if status != DS_ERROR and (statuses or routed) and uncovered_dropship_items(order_name):
		status = DS_ERROR
	frappe.db.set_value(WOO_ORDER_DT, order_name, "dropship_status", status,
		update_modified=False)
	return status


def uncovered_dropship_items(order_name: str) -> list:
	"""Items of the order's drop-ship lines (the lane recorded at checkout, else
	the serial rows') whose quantity the live distributor orders don't cover —
	by item and quantity, since a distributor order line carries no Woo line id.
	A line from before lanes were recorded with no serial rows is not judged."""
	from collections import defaultdict

	from ffl_integrations.distributor import order_state

	order = frappe.get_doc(WOO_ORDER_DT, order_name)
	serial_lane = {}
	for sr in order.serials or []:
		serial_lane.setdefault(str(sr.get("woo_line_id")), sr.get("fulfillment") or "")
	need = defaultdict(int)
	for ln in order.lines or []:
		lane = ln.get("lane") or serial_lane.get(str(ln.get("woo_line_id"))) or ""
		if is_dropship_lane(lane):
			need[ln.get("item_code")] += int(ln.get("qty") or 1)
	if not need:
		return []
	have = dict(frappe.db.sql(
		"""SELECT l.item_code, SUM(l.wish_qty) FROM `tabDistributor Order Line` l
		   JOIN `tabDistributor Order` d ON d.name = l.parent
		   WHERE d.source_doctype = %s AND d.source_name = %s AND d.status != %s
		   GROUP BY l.item_code""", (WOO_ORDER_DT, order_name, order_state.CANCELLED)))
	return sorted(ic for ic, qty in need.items() if int(have.get(ic) or 0) < qty)


# ------------------------------------------------------------- the human queue

@frappe.whitelist()
def precheck_fds(do_name: str) -> dict:
	"""Pre-check whether the transfer dealer accepts FDS shipments.

	Asked of the distributor BEFORE the order is placed — the cheapest way
	to avoid an FDS Hold.

	Args:
	    do_name: Distributor Order name (carries the transfer FFL).

	Returns:
	    {"supported": False} when the provider can't verify dealers, else
	    {"supported": True, "result": <provider verification>}.

	Permissions:
	    Distributor API roles.
	"""
	frappe.only_for(api_roles())
	from ffl_integrations.distributor import api

	do = frappe.get_doc("Distributor Order", do_name)
	provider = api.get_provider(
		frappe.db.get_value("Distributor", do.distributor, "provider") or "rsr")
	if not api.provider_supports(provider, "verify_dealer"):
		return {"supported": False}
	verify = getattr(provider, "verify_transfer_dealer", None)
	if not callable(verify):
		return {"supported": False}
	return {"supported": True, "result": verify(do.transfer_ffl_license)}


def _do_provider(do):
	"""The provider for a Distributor Order (own helper so update_order_ffl is
	unit-testable by patching just this)."""
	from ffl_integrations.distributor import api

	return api.get_provider(
		frappe.db.get_value("Distributor", do.distributor, "provider") or "rsr")


@frappe.whitelist()
def update_order_ffl(do_name: str, ffl_license: str) -> dict:
	"""Attach/replace the transfer FFL on a placed FDS order to release an FDS Hold
	(the standard remedy the completion path calls for).

	Re-validates the NEW licence through the SAME canonical chain routing uses
	BEFORE calling the distributor: releasing a hold onto an expired or
	address-incomplete FFL would defeat the very gate that held it, on a channel
	with no cancel API. The resolved ATF record is pinned so the queue's expiry
	display and any later disposition read one destination.

	Args:
	    do_name: Distributor Order name (a placed FDS order on hold).
	    ffl_license: The new transfer FFL licence number.

	Returns:
	    {"name": do_name, "ffl": ffl_license, "result": <distributor
	    response>}.

	Permissions:
	    Distributor order/console roles (this gate is deliberately widened
	    to the console role).
	"""
	from ffl_integrations.distributor import api

	# The metabox drives the FDS-hold remedy, so the console role reaches
	# THIS gate. Only update_order_ffl is widened — the other api_roles() gates
	# (route_queue / precheck_fds / reroute / scan) stay on ORDER_ROLES.
	frappe.only_for(api.ORDER_ROLES_WITH_CONSOLE)

	do = frappe.get_doc("Distributor Order", do_name)
	record, reason = validate_fds_destination(
		frappe._dict(name=do_name, ffl_license=ffl_license))
	if reason:
		frappe.throw(frappe._("Cannot update the FFL — {0}.").format(REASON_TEXT[reason]))

	provider = _do_provider(do)
	if not api.provider_supports(provider, "update_ffl"):
		frappe.throw(frappe._("This distributor does not support updating the order FFL."))

	result = provider.update_order_ffl(do_name, ffl_license)
	do.transfer_ffl_license = ffl_license
	do.transfer_ffl_record = record
	do.save(ignore_permissions=True)
	return {"name": do_name, "ffl": ffl_license, "result": result}


def api_roles():
	from ffl_integrations.distributor import api

	return api.ORDER_ROLES
