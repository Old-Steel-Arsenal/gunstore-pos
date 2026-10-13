"""Source-order cancellation — the ONE routine both cancel paths run.

A Woo order cancelled by the shop and a counter Sales Invoice being cancelled (or
amended, which cancels first) ask their Distributor Orders the same three questions:

* what can still be **withdrawn** (Draft / Queued — nothing has left our hands);
* what is already **live** at the distributor (:data:`order_state.LIVE_AT_DISTRIBUTOR`
  — there is no cancel API, so this is a phone call, not a status change);
* what has already **shipped** (Complete — the goods are with the customer).

Only the terminal action differs — the counter REFUSES the cancel, the web path lets
it through and alerts — so the classification, the withdrawal and the shipped alert
live here exactly once. Before this, the two paths had drifted: one treated ERROR as
withdrawable and neither said anything when a COMPLETE order's sale was undone.
"""
from __future__ import annotations

import frappe

from ffl_integrations.distributor import order_state
from ffl_integrations.notifications import notify_admins


def classify(orders) -> dict:
	"""PURE. Split ``[{name, status}]`` into ``{withdraw, live, shipped}``.

	An already-Cancelled order appears in none of them — there is nothing left to do
	to it.
	"""
	withdraw, live, shipped = [], [], []
	for o in orders or []:
		status = (o or {}).get("status")
		name = (o or {}).get("name")
		if status in (order_state.DRAFT, order_state.QUEUED):
			withdraw.append(name)
		elif status == order_state.COMPLETE:
			shipped.append(name)
		elif status in order_state.LIVE_AT_DISTRIBUTOR:
			live.append(name)
	return {"withdraw": withdraw, "live": live, "shipped": shipped}


def withdraw_orders(names, reason: str) -> list:
	"""Cancel the not-yet-placed orders. Returns the names actually withdrawn.

	Each order is re-read under ``for_update`` and re-checked before the write:
	the classification came from an unlocked read, and a placement worker can land
	between the two. Without the re-check a cancel would happily stamp Cancelled over
	an order that is, by then, live at the distributor — the worst possible outcome,
	since the goods still ship and nothing in the system expects them.
	"""
	withdrawn = []
	for name in names or []:
		status = frappe.db.get_value(
			"Distributor Order", name, "status", for_update=True)
		if status not in (order_state.DRAFT, order_state.QUEUED):
			continue  # it moved while we were deciding — leave it to the live path
		do = frappe.get_doc("Distributor Order", name)
		do.status = order_state.CANCELLED
		do.hold_reason = reason
		do.save(ignore_permissions=True)
		withdrawn.append(name)
	return withdrawn


def alert_shipped(shipped, *, source_label: str, source_name: str) -> None:
	"""A Complete order means the goods are already with the customer.

	The cancel is ALLOWED — the sale is being undone for a real reason — but it must
	never be silent: somebody has to get the parcel back. Cancelling the paperwork
	while a shipped order quietly drops off the books is the exact failure this
	alert exists to prevent.
	"""
	if not shipped:
		return
	joined = ", ".join(shipped)
	notify_admins(
		f"Cancelled a {source_label} whose goods already SHIPPED: {source_name}",
		f"{source_name} was cancelled, but distributor order(s) {joined} are already "
		f"Complete — the goods shipped to the customer. Arrange the return/RMA per "
		f"docs/rsr-fds-after-sale-sop.md; the sale has been reversed in the books.",
		dedup_key=f"dist_cancel_shipped_{source_name}")


def alert_live(live, *, source_label: str, source_name: str) -> None:
	"""A live order cannot be recalled through the API — say so, loudly."""
	if not live:
		return
	joined = ", ".join(live)
	notify_admins(
		f"{source_label} cancelled with LIVE distributor orders: {source_name}",
		f"{source_name} was cancelled, but these distributor orders are already placed "
		f"and cannot be recalled through the API: {joined}.\n\nContact the distributor "
		f"to arrange a return, and follow docs/rsr-fds-after-sale-sop.md.",
		dedup_key=f"dist_cancel_live_{source_name}")
