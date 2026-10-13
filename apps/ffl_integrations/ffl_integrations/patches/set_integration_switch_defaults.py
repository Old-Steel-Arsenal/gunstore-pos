"""One-shot — write the ON default for the new Payroc / FastBound `enabled`
switches on sites that already existed before they shipped.

A Single doctype stores one `tabSingles` row per field that has ever been saved.
A field added by a migrate has no row, and `frappe.db.get_single_value` casts a
missing row to 0 for a Check — so an "Enable X" switch whose docfield default is 1
reads back OFF everywhere until an operator opens the form and saves it. Reads go
through `ffl_core.utils.single_flag`, which honours the docfield default for a
missing row and is therefore already correct without this patch; what this patch
fixes is the FORM, where an unchecked box would tell the operator the integration
is off while it is demonstrably running.

Only ever writes a row that does not exist. An operator (or a later run of this
patch) must never be able to flip a deliberately-disabled integration back on.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw

SWITCHES = (
	("Payroc Settings", "enabled", "1"),
	("FastBound Settings", "enabled", "1"),
)


def execute() -> None:
	for doctype, field, value in SWITCHES:
		if single_raw(doctype, field) is not None:
			continue  # operator (or a prior run) has already stated a value
		frappe.db.set_single_value(doctype, field, value)
