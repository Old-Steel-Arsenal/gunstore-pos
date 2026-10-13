"""Add "Storage Locations" (where things sit in the shop: the map, moving things, what is
not in a location yet) to the Gun Shop workspace: a tile right after "Inventory Count"
and the second link in the Inventory card.

Same two-landing-site contract as every scripted shortcut (see
ffl_core.workspace_shortcuts): gun_shop.json covers a fresh install, this patch
covers a site that already has the workspace. One-time, idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_card_link, ensure_shortcut

_LABEL = "Storage Locations"
_PAGE = "storage-map"
_WORKSPACE = "Gun Shop"


def execute():
	ensure_shortcut(_LABEL, _PAGE, anchor="Inventory Count", block_id="sc_storage_locations",
		color="Cyan", shortcut_type="Page", workspace=_WORKSPACE)
	ensure_card_link(_LABEL, _PAGE, card="Inventory", link_type="Page", workspace=_WORKSPACE)
