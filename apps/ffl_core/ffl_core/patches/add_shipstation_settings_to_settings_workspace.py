"""ShipStation Settings moves from the Gun Shop workspace to the Settings one.

The Gun Shop home page no longer carries setup links (rewrite_gun_shop_workspace); Settings has
every other one already. Two-landing-site contract as in workspace_shortcuts.py: a fresh
install reads workspace/settings/settings.json, an existing site gets this. Idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_card_link


def execute():
	ensure_card_link("ShipStation Settings", "ShipStation Settings", card="POS & Sales",
		link_type="DocType", workspace="Settings")
