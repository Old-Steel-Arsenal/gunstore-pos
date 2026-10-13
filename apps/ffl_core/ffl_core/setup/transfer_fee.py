"""Idempotent creation of the customer-transfer service item.

A transfer-in charges a service fee (the "Transfer Fee"), NOT a firearm sale:
the customer already owns the gun shipped to the store, which only runs the
4473 + NICS and hands it over. The fee item must therefore be a plain
non-firearm service line so it never trips the 4473 gate or stock moves.

Mirrors setup/item_groups.py's ensure-pattern; wired into hooks.after_install /
after_migrate (after item_groups, so the "Accessory" group already exists) and
back-filled onto existing sites by patches/seed_transfer_fee.py.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import commit
from ffl_core.setup.tax import SERVICE_GROUP

TRANSFER_FEE_ITEM = "TRANSFER-FEE"

# "Service" is a non-firearm group (∉ ALL_FIREARM_GROUPS, see firearm.py) that
# carries a default 0% Item Tax Template (see setup/tax.py). Filing the fee here
# means it (a) never trips the firearm gate — auto_set_is_firearm leaves
# is_firearm=0 — and (b) is sales-tax-exempt, inheriting the group's exemption
# immediately on creation (before tax.ensure tags it explicitly). The group is
# part of the canonical taxonomy (item_groups.py), created on install/migrate.
_FEE_ITEM_GROUP = SERVICE_GROUP


def ensure() -> dict:
	"""Create the TRANSFER-FEE service item if missing. Idempotent."""
	if frappe.db.exists("Item", TRANSFER_FEE_ITEM):
		return {"created": 0}
	frappe.get_doc({
		"doctype": "Item",
		"item_code": TRANSFER_FEE_ITEM,
		"item_name": "FFL Transfer Fee",
		"item_group": _FEE_ITEM_GROUP,
		"stock_uom": "Nos",
		"is_stock_item": 0,
		"has_serial_no": 0,
		"is_sales_item": 1,
		"is_purchase_item": 0,
		"is_firearm": 0,
		"description": "Service fee for a customer firearm transfer-in (4473). Non-firearm, non-stock.",
	}).insert(ignore_permissions=True)
	commit()
	return {"created": 1}
