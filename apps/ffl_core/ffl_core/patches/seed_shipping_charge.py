"""Back-fill the native shipping-charge setup onto existing sites.

Runs setup.shipping_charge.ensure (the ffl_shipping_amount custom field on Sales
Order / Sales Invoice + the per-company Shipping Income account), and retires the
stray stock-tracked "Shipping" item some sites created by hand to charge for
shipping. The item is DISABLED (not deleted) — reversible, drops out of the item
picker, and won't break any draft that referenced it. Only an unused, still-stock
item is touched, so a site that already fixed it is left alone. Idempotent.
"""
from __future__ import annotations

import frappe

STRAY_ITEM = "Shipping"


def execute():
	from ffl_core.setup import shipping_charge
	# Back-fill on existing sites; after_migrate re-runs ensure() on every migrate henceforth.
	shipping_charge.ensure()
	item = frappe.db.get_value(
		"Item", STRAY_ITEM, ["is_stock_item", "is_used", "disabled"], as_dict=True)
	if item and item.is_stock_item and not item.is_used and not item.disabled:
		frappe.db.set_value("Item", STRAY_ITEM, "disabled", 1)
