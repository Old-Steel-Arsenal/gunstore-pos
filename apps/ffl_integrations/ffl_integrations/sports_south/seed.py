"""after_migrate seed: the Sports South Distributor row (disabled until the
operator enables it in the hub) and the Order Fulfillment Overview's
manufacturer restrictions as Dropship Brand Policy rows SCOPED to that
Distributor — never global, so RSR's drop-ship of the same brands is
untouched. Both create-once: operator edits are never overwritten."""
from __future__ import annotations

import frappe

from ffl_integrations.distributor.policy import POLICY_TO_STORE, ensure_scoped_policies
from ffl_integrations.distributor.ss_provider import BLOCKED_BRANDS, PREAPPROVAL_BRANDS

DISTRIBUTOR_NAME = "Sports South"
REASON_BLOCKED = "Sports South does not fulfil this manufacturer"
REASON_PREAPPROVAL = "Sports South requires pre-approval for this manufacturer"


def ensure_distributor() -> bool:
	if frappe.db.exists("Distributor", DISTRIBUTOR_NAME) or frappe.db.exists(
			"Distributor", {"provider": "ss"}):
		return False
	frappe.get_doc({
		"doctype": "Distributor",
		"code": DISTRIBUTOR_NAME,
		"distributor_name": "Sports South, LLC",
		"provider": "ss",
		"enabled": 0,
		"stocking_enabled": 1,
		"dropship_enabled": 1,
		"poll_enabled": 0,
		"priority": 2,
		"safety_buffer": 3,
		"display_cap": 5,
	}).insert(ignore_permissions=True)
	return True


def ensure_brand_policies() -> dict:
	dist = frappe.db.get_value("Distributor", {"provider": "ss"}, "name")
	if not dist:
		return {"created": [], "skipped": list(BLOCKED_BRANDS + PREAPPROVAL_BRANDS)}
	entries = [(b, POLICY_TO_STORE, REASON_BLOCKED) for b in BLOCKED_BRANDS] + [
		(b, POLICY_TO_STORE, REASON_PREAPPROVAL + " — set Direct OK once your rep confirms")
		for b in PREAPPROVAL_BRANDS]
	return ensure_scoped_policies(dist, entries,
		note="Seeded from the Sports South Order Fulfillment Overview (2022-08-22).")


def run() -> dict:
	out = {"distributor_seeded": ensure_distributor(), **ensure_brand_policies()}
	frappe.db.commit()
	return out
