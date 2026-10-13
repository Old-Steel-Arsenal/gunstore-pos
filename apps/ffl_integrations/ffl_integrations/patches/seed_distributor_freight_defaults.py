"""One-shot — write the freight-estimate defaults onto existing RSR / Sports South
Settings.

A Single applies docfield defaults only while it has NO stored rows (see
`seed_gunbroker_single_defaults`); both of these already have rows on every
site, so the new freight fields would read back blank — and the Settings form
would show $0.00 rates the estimator does not actually use. Only fields with no
row are written, so a value somebody has stated (a deliberate 0 included) stays.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw

FIELDS = {
	"RSR Settings": ("store_free_freight", "free_freight_threshold", "rate_ground",
		"rate_2day", "fee_signature"),
	"Sports South Settings": ("store_free_handgun_qty", "store_free_handgun_amount",
		"store_free_other_amount", "store_other_rate"),
}


def execute() -> None:
	for doctype, fieldnames in FIELDS.items():
		meta = frappe.get_meta(doctype)
		for fieldname in fieldnames:
			field = meta.get_field(fieldname)
			if not field or field.get("default") in (None, ""):
				continue
			if single_raw(doctype, fieldname) is not None:
				continue
			frappe.db.set_single_value(doctype, fieldname, field.default)
