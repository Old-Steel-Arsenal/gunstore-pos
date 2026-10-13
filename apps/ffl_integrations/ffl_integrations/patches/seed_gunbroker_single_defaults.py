"""One-shot — write every GunBroker Settings docfield default that has no row yet.

`set_gunbroker_sandbox_default` writes one field. That turns out to be the
dangerous number of fields to write, because of how a Single loads:

1. a Single keeps one `tabSingles` row per field that has ever been saved;
2. `Document.load_from_db` applies the docfield defaults only when a Single has
   NO stored rows at all — once any row exists, every field without one reads
   back as `None`, not as its default;
3. so the moment that earlier patch writes `sandbox_mode`, every other field on
   an existing site starts reading `None`;
4. and the first `save()` after that — `after_migrate` seeding the category map
   — writes the whole valid_dict back, freezing those `None`s into real rows.

The result on an upgraded site is a listing payload built from blanks:
`IsFFLRequired` false on a firearm, `UseDefaultExcludeStates` false so the
account's shipping restrictions stop applying, and no payment methods at all —
none of which raises anything, and all of which are visibly wrong only to
someone who already knows what the defaults were meant to be.

A new patch rather than an edit to the earlier one: Frappe records a patch by
module name and runs it exactly once, so editing one that has already executed
is a silent no-op on every site that ran the earlier build. That lesson is
written up in `finish_integration_switch_defaults`' docstring and it cost
somebody an afternoon.

Only ever writes a field with no row, so it can never overturn a value somebody
has stated — including a deliberate zero.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw

SETTINGS = "GunBroker Settings"

# Fieldtypes whose docfield default is meaningful to persist. Data/Text fields
# are left alone: their default is almost always blank, and a blank row and no
# row read back identically.
_DEFAULTED_TYPES = {"Check", "Int", "Float", "Currency", "Select", "Data"}


def execute() -> None:
	meta = frappe.get_meta(SETTINGS)
	for field in meta.fields:
		if field.fieldtype not in _DEFAULTED_TYPES:
			continue
		default = field.get("default")
		if default is None or str(default).strip() == "":
			continue
		if single_raw(SETTINGS, field.fieldname) is not None:
			continue  # already stated (by an operator, or by an earlier patch)
		frappe.db.set_single_value(SETTINGS, field.fieldname, default)
