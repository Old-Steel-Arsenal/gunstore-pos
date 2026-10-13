"""One-shot — seed FFL Settings.transfer_fee_amount so the POS transfer dialog
has a sensible default fee out of the box.

Frappe doesn't materialise a Single field's `default` into the DB until the form
is first saved, so get_transfer_config (a direct DB read) would otherwise see
fee_amount=0 — the same reason seed_receive_sources exists.

The TRANSFER-FEE *item* is created lazily on first use (ffl_core.firearm.
get_transfer_config → setup.transfer_fee.ensure), NOT here: a fresh install runs
patches/hooks before ERPNext's setup wizard seeds the default UOM and Item Group
masters the item needs, so creating it at migrate time fails. We also don't seed
transfer_fee_item (a Link) — get_transfer_config falls back to TRANSFER-FEE, and
seeding a link to a not-yet-created item would re-break a later FFL Settings save.

Idempotence: a Singles marker, so an admin who later sets a $0 transfer fee
isn't overwritten on the next migrate.
"""
from __future__ import annotations

import frappe

SEEDED_MARKER = "ffl_transfer_fee_seeded"
DEFAULT_FEE = 25


def execute():
	if frappe.db.get_default(SEEDED_MARKER):
		# Already seeded on this site — respect whatever the admin set since,
		# including a deliberate $0 transfer fee.
		return
	frappe.db.set_single_value("FFL Settings", "transfer_fee_amount", DEFAULT_FEE)
	frappe.db.set_default(SEEDED_MARKER, "1")
