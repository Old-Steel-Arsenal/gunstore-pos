"""One-shot — seed FFL Settings.receive_sources with the default list so the
Source dropdown on Receive Goods / FFL Acquisition / Purchase Receipt has values
immediately after install. Frappe's `default` on a Single doctype field isn't
materialised until someone saves the form; this patch writes it once so the
dropdown isn't empty out-of-the-box.

Idempotence: tracked by a Singles 'default' marker `ffl_receive_sources_seeded`,
NOT by inferring 'is the field empty?'. Inferring fails the case where an admin
deliberately CLEARS receive_sources to force free-text per PR — without the
marker, every subsequent `bench migrate` re-applies the canned six-source list
and silently undoes the admin's intent.
"""
from __future__ import annotations

import frappe


DEFAULT = "RSR\nDistributor\nIndividual (trade-in)\nManufacturer\nConsignment\nOther"
SEEDED_MARKER = "ffl_receive_sources_seeded"


def execute():
	if frappe.db.get_default(SEEDED_MARKER):
		# Patch has already run on this site — respect whatever the admin has
		# configured since, including a deliberate-empty list.
		return
	frappe.db.set_value(
		"FFL Settings", "FFL Settings",
		"receive_sources", DEFAULT, update_modified=False,
	)
	frappe.db.set_default(SEEDED_MARKER, "1")
