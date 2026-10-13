"""One-shot: write Payroc Settings.require_card_reference = 1 where no row exists.

Same defence set_integration_switch_defaults gave the two enabled switches:
``get_single_value`` casts a missing tabSingles row to 0 for a Check, so a
docfield default of 1 reads back OFF on every site that predates the field.
Reads go through ``single_flag`` (which honours the default), and this writes
the row so the FORM also shows the truth. Its own patch because the earlier two
have already executed and an edited patch never re-runs (that lesson is
finish_integration_switch_defaults' docstring).

Only ever writes where nothing has been stated, so re-running can never
overturn a deliberate choice.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw


def execute() -> None:
	if single_raw("Payroc Settings", "require_card_reference") is None:
		frappe.db.set_single_value("Payroc Settings", "require_card_reference", 1)
