"""Add "Inventory Count" (the stocktake page: scan firearm labels and UPCs, compare
with the system, adjust the non-serialized differences) to the Gun Shop workspace: a
tile right after "Stock On Hand" and the first link in the Inventory card.

Same two-landing-site contract as every scripted shortcut (see
ffl_core.workspace_shortcuts): gun_shop.json covers a fresh install, this patch
covers a site that already has the workspace. One-time, idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_card_link, ensure_shortcut

_LABEL = "Inventory Count"
_PAGE = "stocktake"
_WORKSPACE = "Gun Shop"


def execute():
	ensure_shortcut(_LABEL, _PAGE, anchor="Stock On Hand", block_id="sc_inventory_count",
		color="Green", shortcut_type="Page", workspace=_WORKSPACE)
	ensure_card_link(_LABEL, _PAGE, card="Inventory", link_type="Page", workspace=_WORKSPACE)
