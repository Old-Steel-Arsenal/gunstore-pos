"""Distributor Order state machine.

The DO is NON-submittable — docstatus stays 0 for its whole life; progression is
a ``status`` field walk, not a submit/cancel. This module is the single, pure
source of transition legality (no frappe import), used by both the DO controller
``validate`` (backstop on any write) and the whitelisted transition endpoints
(which add the role gate). Same-state writes are no-ops (idempotent polling).
"""
from __future__ import annotations

DRAFT = "Draft"
QUEUED = "Queued"
PLACED = "Placed"
ACKNOWLEDGED = "Acknowledged"
HOLD = "Hold"
PARTIALLY_SHIPPED = "Partially Shipped"
COMPLETE = "Complete"
CANCELLED = "Cancelled"
ERROR = "Error"

# Directed transition graph. A status maps to the set it may move to. Same-state
# is always allowed (handled in can_transition) so idempotent re-saves pass.
ALLOWED_TRANSITIONS: dict = {
	DRAFT: {QUEUED, CANCELLED},
	# Placement can land Placed, or immediately Held (an FDS order RSR holds on
	# arrival), or fail terminally (Error) — all reachable directly from Queued.
	QUEUED: {PLACED, HOLD, ERROR, CANCELLED},
	PLACED: {ACKNOWLEDGED, HOLD, PARTIALLY_SHIPPED, COMPLETE, ERROR, CANCELLED},
	ACKNOWLEDGED: {HOLD, PARTIALLY_SHIPPED, COMPLETE, ERROR, CANCELLED},
	HOLD: {ACKNOWLEDGED, PARTIALLY_SHIPPED, COMPLETE, CANCELLED, ERROR},
	PARTIALLY_SHIPPED: {PARTIALLY_SHIPPED, COMPLETE, ERROR, CANCELLED},
	COMPLETE: set(),
	CANCELLED: set(),
	ERROR: {QUEUED, CANCELLED},
}

# The poller only touches live orders; terminal orders are left alone.
POLLABLE_STATES = {PLACED, ACKNOWLEDGED, HOLD, PARTIALLY_SHIPPED}
TERMINAL_STATES = {COMPLETE, CANCELLED}

# "The distributor may already be acting on this order" — the ONE set every
# cancellation path asks. Deliberately NOT POLLABLE_STATES:
# polling is a *scheduling* concept and excludes ERROR, but an ERROR order may well
# have reached RSR before it failed, so a cancel must treat it as potentially live
# rather than silently withdraw it. COMPLETE is excluded on purpose — it is not
# "live", it is *shipped*, which the cancel paths handle as its own case.
LIVE_AT_DISTRIBUTOR = {PLACED, ACKNOWLEDGED, HOLD, PARTIALLY_SHIPPED, ERROR}

ALL_STATES = set(ALLOWED_TRANSITIONS.keys())


def can_transition(current: str, new: str) -> bool:
	if current == new:
		return True
	return new in ALLOWED_TRANSITIONS.get(current, set())


def assert_transition(current: str, new: str) -> None:
	"""Raise ValueError on an illegal status jump (pure — the controller wraps it
	in frappe.throw so the desk sees a friendly message)."""
	if not can_transition(current, new):
		raise ValueError(f"Illegal Distributor Order transition: {current!r} → {new!r}.")
