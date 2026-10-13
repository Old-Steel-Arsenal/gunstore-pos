"""One-shot — the two settings changes that landed AFTER the first review round.

A separate patch rather than extra steps inside
``set_integration_switch_defaults``, because Frappe records a patch by module
name and runs it exactly once: editing one that has already executed is a silent
no-op on every site that ran the earlier build. Caught on the dev site — the
rename below simply did not happen there until this became its own entry.

Carries ``manual_4473_mode`` → ``paper_4473_mode``. Renamed before it ever
shipped: every reader said "paper" (``paper_4473_mode()``, the boot key, the POS
bundle, ``attest_paper_4473``), while ``manual_`` was already taken on the very
same bound-book record by ``FFL Disposition.manual_override``, meaning something
else entirely. No production site has the old row; dev sites that migrated an
earlier build of this branch do, and silently resetting an operator's setting
because a field was renamed under them is exactly the kind of surprise that
costs somebody an afternoon.

Only ever writes a value nobody has stated, so re-running this can never
overturn a deliberate choice.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw

SETTINGS = "FFL Settings"
OLD_FIELD = "manual_4473_mode"
NEW_FIELD = "paper_4473_mode"


def execute() -> None:
	old = single_raw(SETTINGS, OLD_FIELD)
	if old is None:
		return
	if single_raw(SETTINGS, NEW_FIELD) is None:
		frappe.db.set_single_value(SETTINGS, NEW_FIELD, old)
	frappe.db.sql(
		"DELETE FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
		(SETTINGS, OLD_FIELD))
