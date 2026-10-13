"""Add "4473 Records" (the Form 4473 Records report) to the Gun Shop workspace:
a tile right after "Pending 4473 Orders" and the first link in the
Compliance (A&D) card.

Same two-landing-site contract as every scripted shortcut (see
ffl_core.workspace_shortcuts): gun_shop.json covers a fresh install, this patch
covers a site that already has the workspace. One-time, idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_card_link, ensure_shortcut

_LABEL = "4473 Records"
_REPORT = "Form 4473 Records"
_WORKSPACE = "Gun Shop"


def execute():
	ensure_shortcut(_LABEL, _REPORT, anchor="Pending 4473 Orders", block_id="sc_4473_records",
		color="Pink", shortcut_type="Report", workspace=_WORKSPACE,
		report_ref_doctype="POS Invoice")
	ensure_card_link(_LABEL, _REPORT, card="Compliance (A&D)", workspace=_WORKSPACE)
