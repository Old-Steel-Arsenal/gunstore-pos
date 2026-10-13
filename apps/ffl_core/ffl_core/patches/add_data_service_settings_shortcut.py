"""Add the "Data Service (FFL & Catalog)" shortcut to the Settings workspace.

Data Service Settings holds the connection every distributor
catalog read and the EZ Check provider share; the fields used to sit on FFL
Settings, which is reachable from the FFL & Compliance card. Its own Single
is not, so — like GunBroker — it needs a shortcut, with the same
two-landing-site contract (see ffl_core.workspace_shortcuts). One-time,
idempotent.
"""
from ffl_core.workspace_shortcuts import ensure_shortcut

_LABEL = "Data Service (FFL & Catalog)"
_DOCTYPE = "Data Service Settings"
_ANCHOR = "GunBroker (Marketplace)"  # last integration tile → fills the second row


def execute():
	ensure_shortcut(_LABEL, _DOCTYPE, anchor=_ANCHOR, block_id="sc_data_service_settings",
		color="Gray")
