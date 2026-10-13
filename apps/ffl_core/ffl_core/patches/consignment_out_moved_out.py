"""Consignment Out left the core for the osa_consignment extension (September 2026).

What the core owned and no longer ships: three Desk Page records and their
two Gun Shop shortcuts. The DocTypes, the four link fields on core records and
the Consignment Dealer role are NOT touched — the extension adopts them
(renamed) the moment it is installed on this site, and until then they are
inert: no page, no hook, nothing reads them. Deleting them here would destroy
a store's consignment history on the migrate that merely upgrades the core.
"""
from __future__ import annotations

import frappe

from ffl_core.workspace_shortcuts import remove_shortcut

PAGES = ("ship-consignment", "dealer-orders", "dealer-onboarding")
SHORTCUTS = ("Consignment Out", "Dealer Orders")


def execute() -> None:
	for page in PAGES:
		if frappe.db.exists("Page", page):
			frappe.delete_doc("Page", page, force=True, ignore_permissions=True)
	for label in SHORTCUTS:
		remove_shortcut(label, workspace="Gun Shop")
