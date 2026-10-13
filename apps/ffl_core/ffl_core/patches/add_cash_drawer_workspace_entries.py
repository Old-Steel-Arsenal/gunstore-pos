"""Add the Cash Drawer page (a Gun Shop tile after "Inventory Count" and a link in the Sales card), the Cash
Drawer Weekly report (Sales card) and the Cash Drawer Settings link (Settings workspace, POS & Sales card) to
sites that already have those workspaces.

Same two-landing-site contract as every scripted shortcut (see ffl_core.workspace_shortcuts): the workspace JSON
covers a fresh install, this patch covers an existing site. One-time, idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_card_link, ensure_shortcut


def execute():
	ensure_shortcut("Cash Drawer", "cash-drawer", anchor="Inventory Count", block_id="sc_cash_drawer",
		color="Green", shortcut_type="Page", workspace="Gun Shop")
	# Each lands first in its card, so the report goes in before the page.
	ensure_card_link("Cash Drawer Weekly", "Cash Drawer Weekly", card="Sales", link_type="Report", workspace="Gun Shop")
	ensure_card_link("Cash Drawer", "cash-drawer", card="Sales", link_type="Page", workspace="Gun Shop")
	ensure_card_link("Cash Drawer Settings", "Cash Drawer Settings", card="POS & Sales", link_type="DocType",
		workspace="Settings")
