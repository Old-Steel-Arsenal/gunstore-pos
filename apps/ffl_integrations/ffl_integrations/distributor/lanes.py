"""Distributor lanes — the one place that knows how goods reach their destination.

Two families:

* **drop-ship** — the distributor ships to the buyer. ``direct`` takes accessories to the
  buyer's own address; ``fds`` takes a firearm to the transfer FFL and never carries a
  consumer address. Both are placed on the house's fulfillment account.
* **store** — the distributor ships to the shop, which receives the goods into stock and,
  for a firearm, into the bound book. ``to_store`` has a sale waiting on it; ``stocking``
  is a restock with no sale behind it. Both are placed on the dealer account, and the shop
  is the account's ship-to, so the plan carries no destination at all.

Everything lane-shaped lives here: the mode the planner speaks, the Distributor Order's
``order_type``, the account, the capability a house needs to run the lane, and the
destination a plan of that lane carries. A provider states the lanes it runs through
``capabilities()`` and carries no lane table of its own.

Frappe-free, like :mod:`.base` — import it from anywhere in the distributor layer.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

STOCKING = "stocking"
TO_STORE = "to_store"
DIRECT = "direct"
FDS = "fds"

# Lane families
DROPSHIP = "dropship"
STORE = "store"

# The mode an empty / unknown ship context means. Ordering something with no lane named
# is a restock — the oldest path, and the only one that needs no destination.
DEFAULT_MODE = STOCKING


class Lane(NamedTuple):
	mode: str          # what a ship context calls it
	order_type: str    # what the Distributor Order stores
	account: str       # which account the order is placed on
	family: str        # DROPSHIP (to the buyer) or STORE (to the shop)
	capability: str    # what capabilities() must contain to run this lane


LANES: dict[str, Lane] = {
	# Both store lanes need the same thing of a house — a dealer account that ships to the
	# shop — so they share one capability.
	STOCKING: Lane(STOCKING, "Stocking", "dealer", STORE, "to_store"),
	TO_STORE: Lane(TO_STORE, "Procure To Store", "dealer", STORE, "to_store"),
	DIRECT: Lane(DIRECT, "Dropship Accessory", "fulfillment", DROPSHIP, "dropship"),
	FDS: Lane(FDS, "Dropship Firearm", "fulfillment", DROPSHIP, "fds"),
}

_BY_ORDER_TYPE = {l.order_type: l for l in LANES.values()}

ORDER_TYPES = tuple(l.order_type for l in LANES.values())


def lane(mode) -> Lane:
	"""The lane a ship-context mode names. No mode at all is a restock."""
	return LANES.get(str(mode or "").strip(), LANES[DEFAULT_MODE])


def is_lane(mode) -> bool:
	"""Does this mode name a lane? (An empty mode does not — it means "unsaid".)"""
	return str(mode or "").strip() in LANES


def lane_of(order_type) -> Optional[Lane]:
	"""The lane an order's ``order_type`` belongs to, or None if it names none."""
	return _BY_ORDER_TYPE.get(str(order_type or "").strip())


def mode_of(order_type) -> Optional[str]:
	"""The ship-context mode for a stored ``order_type``, or None."""
	found = lane_of(order_type)
	return found.mode if found else None


def stocks_goods(order_type) -> bool:
	"""True when this order's goods come to the shop and enter our inventory."""
	found = lane_of(order_type)
	return bool(found and found.family == STORE)


# Where the goods on a distributor INVOICE went (``Distributor Invoice.destination``).
DEST_STORE = "Store"
DEST_DROPSHIP = "Drop-ship"
DEST_UNKNOWN = "Unknown"


# Why a destination is Unknown (``Distributor Invoice.destination_note``): FAILED = the
# house's call failed, so ask again next run; NO_ANSWER = the house answered and it
# settles nothing (an empty ship-to, no order number, an address we cannot match) —
# asking again gets the same answer, so it is not re-asked.
NOTE_FAILED = "Lookup failed"
NOTE_NO_ANSWER = "No answer on file"


def destination_of_order_type(order_type) -> str:
	"""Where an order's goods went: the shop for a store lane, the buyer for a
	drop-ship lane, Unknown for a type that names no lane."""
	found = lane_of(order_type)
	if not found:
		return DEST_UNKNOWN
	return DEST_STORE if found.family == STORE else DEST_DROPSHIP


def destination_of_account(account) -> str:
	"""Where an invoice's goods went from the house account it was billed on: the
	dealer account ships to the shop, the fulfillment account to the buyer."""
	found = next((l for l in LANES.values() if l.account == str(account or "").strip()), None)
	return DEST_UNKNOWN if not found else (DEST_STORE if found.family == STORE else DEST_DROPSHIP)


def with_note(destination: str) -> tuple:
	"""``(destination, note)`` for an answer that is final: Unknown then carries
	NOTE_NO_ANSWER (nothing to retry), anything else no note."""
	return (destination, NOTE_NO_ANSWER if destination == DEST_UNKNOWN else "")


def ships_to_consumer(mode) -> bool:
	"""True when the order is placed on behalf of a named buyer (drop-ship)."""
	return lane(mode).family == DROPSHIP


def destination(mode, ship_context) -> dict:
	"""The destination fields a plan of this lane carries — and only those.

	``direct`` gets the buyer's own address; ``fds`` the transfer FFL and nothing else,
	because a firearm order must never carry the buyer's street address; the store lanes
	get neither. The buyer's name and phone ride along on a drop-ship order only.
	"""
	ctx = ship_context or {}
	found = lane(mode)
	ffl = ctx.get("destination_ffl") or {}
	consumer = found.family == DROPSHIP
	return {
		"ship_to": ctx.get("ship_to") if found.mode == DIRECT else None,
		"ship_ffl": (ffl.get("license") or None) if found.mode == FDS else None,
		"transfer_dealer": (dict(ffl) or None) if (found.mode == FDS and ffl) else None,
		"consumer_name": (ctx.get("consumer_name") or "") if consumer else "",
		"contact_num": (ctx.get("contact_num") or "") if consumer else "",
	}
