# apps/ffl_core/ffl_core/patches/backfill_serial_firearm_type.py
"""Backfill Serial No.firearm_type from the model Item for existing firearm
serials that don't carry their own — so the by-serial form and the FastBound
per-gun sync see the same type the Item does. ffl_core.firearm.inherit_serial_firearm_attrs
keeps new/edited serials in sync going forward. An explicit value is never overwritten.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.firearm import FIREARM_TYPE_SELECT_OPTIONS


def execute():
    # Fixtures sync AFTER post_model_sync patches, so on the migrate that introduces
    # this field the column doesn't exist yet. Ensure it first (idempotent; the
    # fixture sync later in this same migrate fills in the full field properties).
    # Mirrors backfill_serial_model_caliber. The options come from the canonical
    # vocabulary so this seed can't drift from the fixture definition in
    # ffl_core/fixtures/custom_field.json (a drifted seed rewrites the shared
    # meta cache with the stale list whenever this runs).
    create_custom_fields(
        {
            "Serial No": [
                {"fieldname": "firearm_type", "label": "Firearm Type", "fieldtype": "Select",
                 "options": FIREARM_TYPE_SELECT_OPTIONS,
                 "insert_after": "importer", "module": "FFL Core"},
            ]
        },
        ignore_validate=True,
    )

    rows = frappe.db.sql(
        """
        SELECT sn.name, i.firearm_type AS item_ftype
        FROM `tabSerial No` sn
        INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
        WHERE NULLIF(sn.firearm_type, '') IS NULL AND NULLIF(i.firearm_type, '') IS NOT NULL
        """,
        as_dict=True,
    )
    # The JOIN already produced the non-blank Item firearm_type; set it directly
    # (mirrors backfill_serial_firearm_attrs / backfill_serial_model_caliber).
    for r in rows:
        frappe.db.set_value("Serial No", r.name, "firearm_type", r.item_ftype, update_modified=False)
