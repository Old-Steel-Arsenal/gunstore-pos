"""after_migrate seed: the Lipsey's Distributor row, disabled until the operator
enables it in the hub. Create-once: operator edits are never overwritten.

Polling is on: a Lipsey's order status is worked out on this site from the
placement answer and the mirrored shipping rows (no call to Lipsey's), so the
30-minute poll costs nothing at the house."""
from __future__ import annotations

import frappe

DISTRIBUTOR_NAME = "Lipsey's"
PROVIDER = "lipseys"


def ensure_distributor() -> bool:
	if frappe.db.exists("Distributor", DISTRIBUTOR_NAME) or frappe.db.exists(
			"Distributor", {"provider": PROVIDER}):
		return False
	frappe.get_doc({
		"doctype": "Distributor",
		"code": DISTRIBUTOR_NAME,
		"distributor_name": "Lipsey's, LLC",
		"provider": PROVIDER,
		"enabled": 0,
		"stocking_enabled": 1,
		"dropship_enabled": 1,
		"poll_enabled": 1,
		"priority": 3,
		"safety_buffer": 3,
		"display_cap": 5,
	}).insert(ignore_permissions=True)
	return True


def run() -> dict:
	out = {"distributor_seeded": ensure_distributor()}
	frappe.db.commit()
	return out
