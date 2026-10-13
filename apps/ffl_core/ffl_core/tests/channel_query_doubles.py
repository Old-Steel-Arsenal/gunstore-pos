"""Query doubles for the cross-channel reservation read points.

`ffl_core.reservations.channel_reserved_serials` was wired into hot paths that a
dozen existing tests already covered with blanket `frappe.get_all` and
`frappe.db.get_value` stubs — precisely the shape docs/testing.md rule 4 warns
about. A blanket answer reaches the new read point too, and that read point asks
a differently-shaped question:

    free_units / the serial pickers
        filters={…, "woo_reserved": 0, …}, pluck="name"   →  a list of NAMES
    reservations.channel_reserved_serials
        filters={"item_code": …, "<flag>": 1},
        fields=["name", "<ref>"]                          →  a list of ROWS

Hand the second one a list of names and `row["name"]` raises
`TypeError: string indices must be integers`. Separately,
`_channel_fields_present` calls `frappe.get_meta("Serial No")`, and frappe builds
Meta by reading through `frappe.db.get_value` — so a blanket get_value answer
becomes the Meta, and fails later as
`AttributeError: 'Meta' object has no attribute 'istable'`.

Both are test-side: the product code is correct against real Frappe. So
`get_all_double` answers ONLY the channel query and passes everything else
through untouched, and `warm_channel_meta` takes the Meta build out of the
stub's reach instead of narrowing the stub — narrowing it breaks the tests that
legitimately depend on a broad answer for the reads they are actually about.

Live in ffl_core because the predicate does, and because ffl_woo_sync's tests
and ffl_core's own both need them.
"""
from __future__ import annotations

import frappe

from ffl_core.reservations import CHANNEL_RESERVE_FIELDS

#: The flag columns, e.g. ``{"woo_reserved", "gb_reserved"}``. Derived, so a
#: third channel needs no edit here.
CHANNEL_FLAGS = frozenset(spec[0] for spec in CHANNEL_RESERVE_FIELDS)


def is_channel_reservation_query(filters) -> bool:
    """Is this the SELECT `channel_reserved_serials` issues?

    Keyed on a channel flag being asked for as **1**. `free_units` also names
    `woo_reserved` in its filters, but as **0** ("not spoken for"), so the value
    is what separates the two — not the presence of the column.
    """
    if not isinstance(filters, dict):
        return False
    return any(filters.get(flag) == 1 for flag in CHANNEL_FLAGS)


def get_all_double(answer, *, held: dict | None = None):
    """A `frappe.get_all` side_effect that keeps a blanket *answer* working.

    Args:
        answer: what the test was already returning — a list of serial names for
            a pluck-style query, or a list of rows.
        held: ``{serial_no: ref}`` to report as channel-reserved, when a test
            wants to say so outright.

    With *held* left out the channel query is answered FROM *answer*, the way
    the database would: rows carrying the flag come back, rows without it do
    not, and a list of bare names yields nothing (a name cannot be reserved by
    itself). That keeps a fixture as the single description of the world, so a
    test that already writes `_sn("B283116", woo_reserved=1, woo_order="…")`
    needs no second statement of the same fact — and one that says nothing about
    channels goes on meaning "no channel holds anything", which is what the
    counter-hold, custody and picker suites all mean. The channel dimension has
    its own suite in `test_channel_exclusion`.
    """

    def _get_all(doctype=None, *a, **kw):
        filters = kw.get("filters")
        if not is_channel_reservation_query(filters):
            return answer
        flag = next(f for f in CHANNEL_FLAGS if filters.get(f) == 1)
        ref_field = next(spec[1] for spec in CHANNEL_RESERVE_FIELDS
                         if spec[0] == flag)
        if held is not None:
            return [{"name": sn, ref_field: ref} for sn, ref in held.items()]
        return [{"name": row.get("name"), ref_field: row.get(ref_field) or ""}
                for row in answer
                if hasattr(row, "get") and row.get(flag)]

    return _get_all


def warm_channel_meta(doctype: str = "Serial No") -> None:
    """Build this doctype's Meta NOW, before a blanket `get_value` stub lands.

    `_channel_fields_present` asks `frappe.get_meta("Serial No")`, and frappe
    builds Meta by READING THE DOCTYPE through `frappe.db.get_value`. Inside a
    test that has stubbed that call with a fixture row, the Meta is built out of
    the fixture and then fails on the first attribute nobody thought to put
    there:

        AttributeError: 'Meta' object has no attribute 'istable'

    Called before entering the patch, this puts the real Meta in frappe's
    per-request cache, so the read point finds it there and never reaches the
    stub. It leaves the test's own stub exactly as broad as it was — which
    matters, because several of these tests genuinely rely on that breadth for
    the reads they ARE about.
    """
    frappe.get_meta(doctype)
