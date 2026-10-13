"""Add the "Extensions" shortcut (the /app/extensions page) to the Settings workspace.

Same two-landing-site contract as every Settings shortcut (see
ffl_core.workspace_shortcuts): workspace/settings/settings.json covers a fresh
install, this patch covers a site that already saved the workspace. One-time,
idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_shortcut

_LABEL = "Extensions"
_PAGE = "extensions"
_ANCHOR = "Data Service (FFL & Catalog)"   # last integration tile


def execute():
	ensure_shortcut(_LABEL, _PAGE, anchor=_ANCHOR, block_id="sc_extensions", color="Gray", shortcut_type="Page")
