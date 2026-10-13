"""Used Firearm listing_status state machine.

Hooks fire from Sales Order / Sales Invoice doc_events. Transitions:

	Draft   ─(Intake Tool publish)──>  Listed
	Listed  ─(SO/SI submit)─────────>  Reserved
	Reserved ─(SI on_submit fulfilled)──> Sold
	Reserved ─(SO/SI cancel)────────>  Listed   # (back to available)
	Sold    ─(SI cancel)────────────>  Listed   # (revert; rare, manual)

Only Item where item_group ∈ {Used Firearm} flips status; new firearms (qty>1)
ignore listing_status entirely. Idempotent — re-running on the same SO is a
no-op when target state matches.
"""
from __future__ import annotations

import frappe

USED_FIREARM_GROUPS: frozenset[str] = frozenset({"Used Firearm"})


def on_sales_order_submit(doc, method: str | None = None) -> None:
	"""SO Submit → Reserve every Used Firearm line."""
	for code in _iter_used_firearm_codes(doc):
		_set_listing_status(code, "Reserved")


def on_sales_order_cancel(doc, method: str | None = None) -> None:
	"""SO Cancel → put it back on the shelf.

	Only a Reserved gun goes back to Listed: a used gun on an order that was
	never published (Draft) must not be promoted onto the shelf by a
	cancelled order.
	"""
	for code in _iter_used_firearm_codes(doc):
		if frappe.db.get_value("Item", code, "listing_status") == "Reserved":
			_set_listing_status(code, "Listed")


def on_sales_invoice_submit(doc, method: str | None = None) -> None:
	"""SI Submit → Sold (only when stock is actually issued, not draft POS).

	Sales Invoice with `update_stock=1` (POS or Direct Sales) ships the firearm
	now; without it, fulfillment happens via Delivery Note and the SO hook covers
	the reserve step. The SI submit is the "money plus stock leaves" event.
	"""
	if not getattr(doc, "update_stock", 0):
		# Pure invoice (Delivery Note ships separately) — keep as Reserved if SO
		# already moved it; otherwise mark Reserved as a soft lock.
		for code in _iter_used_firearm_codes(doc):
			_set_listing_status(code, "Reserved")
		return

	for code in _iter_used_firearm_codes(doc):
		_set_listing_status(code, "Sold")


def on_sales_invoice_cancel(doc, method: str | None = None) -> None:
	"""SI Cancel → revert to Listed (firearm back in stock)."""
	for code in _iter_used_firearm_codes(doc):
		_set_listing_status(code, "Listed")


def _iter_used_firearm_codes(doc):
	codes = [c for c in (getattr(r, "item_code", None)
		for r in (getattr(doc, "items", None) or [])) if c]
	if not codes:
		return
	# One query for the whole cart instead of a get_value per line.
	info = {r.name: r for r in frappe.get_all(
		"Item", filters={"name": ("in", list(set(codes)))},
		fields=["name", "item_group", "is_used"])}
	for code in codes:
		row = info.get(code)
		# A used firearm carries is_used=1. Phase 2 files used guns under the
		# TYPE group (Handgun / Rifle / Shotgun / ...), not the legacy "Used
		# Firearm" group — so match on the flag, with the old group as fallback
		# for any pre-Phase-2 rows that never got is_used set.
		if row and (row.is_used or row.item_group in USED_FIREARM_GROUPS):
			yield code


def _set_listing_status(item_code: str, target: str) -> None:
	current = frappe.db.get_value("Item", item_code, "listing_status")
	if current == target:
		return
	# Avoid kicking the full Item validate chain — flip the column directly.
	frappe.db.set_value("Item", item_code, "listing_status", target, update_modified=False)
