"""One-shot — write GunBroker Settings.sandbox_mode = 1 where no row exists.

Same defence the earlier switch patches gave their Checks: a Single stores one
`tabSingles` row per field that has ever been saved, and `get_single_value`
casts a missing row to 0 — so a docfield default of 1 displays as UNCHECKED on
every site until somebody opens the form and saves it.

Reads already go through `ffl_core.utils.single_flag(..., default=1)`, which
honours the docfield default, so the integration is pointed at the sandbox
whether or not this patch has run. What the patch fixes is the FORM: an
unchecked Sandbox Mode box reading back at an operator who is deciding whether
it is safe to press Test Connection is the single worst thing this form could
get wrong.

Its own patch file, not an extra tuple entry in
`set_integration_switch_defaults`: Frappe records a patch by module name and
runs it exactly once, so editing one that has already executed is a silent
no-op on every site that ran the earlier build. That lesson is written up in
`finish_integration_switch_defaults`' docstring and cost somebody an afternoon.

Only ever writes where nothing has been stated, so re-running can never
overturn a deliberate choice — including the deliberate choice to go live.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw


def execute() -> None:
	if single_raw("GunBroker Settings", "sandbox_mode") is None:
		frappe.db.set_single_value("GunBroker Settings", "sandbox_mode", 1)
