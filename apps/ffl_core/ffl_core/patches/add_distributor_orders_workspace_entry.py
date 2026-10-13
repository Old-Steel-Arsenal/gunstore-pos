"""Add "Distributor Orders" (the distributor-restock page: one cart across every
house + tracking of every open distributor order) to the Gun Shop workspace: a
tile right after "Receive Goods" and the first link in the Purchasing & FFL card.

Same two-landing-site contract as every scripted shortcut (see
ffl_core.workspace_shortcuts): gun_shop.json covers a fresh install, this patch
covers a site that already has the workspace. One-time, idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_card_link, ensure_shortcut

_LABEL = "Distributor Orders"
_PAGE = "distributor-restock"
_WORKSPACE = "Gun Shop"


def execute():
	ensure_shortcut(_LABEL, _PAGE, anchor="Receive Goods", block_id="sc_distributor_orders",
		color="Orange", shortcut_type="Page", workspace=_WORKSPACE)
	ensure_card_link(_LABEL, _PAGE, card="Purchasing & FFL", link_type="Page", workspace=_WORKSPACE)
