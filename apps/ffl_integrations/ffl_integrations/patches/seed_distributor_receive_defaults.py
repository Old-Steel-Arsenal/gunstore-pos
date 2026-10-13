"""One-shot — write the scan-first receiving defaults onto existing RSR /
Sports South Settings.

Same reason as `seed_distributor_freight_defaults`: a Single applies docfield
defaults only while it has NO stored rows, and both Settings already have rows on
every site, so `hold_escalate_days` (when a held order shows as trouble on the
queue) and `freight_variance_alert` (the invoiced-vs-booked freight gap that
alerts) would read back blank. Only fields with no row are written, so a value
somebody has stated (a deliberate 0 included) stays.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw

FIELDS = {
	"RSR Settings": ("hold_escalate_days", "freight_variance_alert"),
	"Sports South Settings": ("hold_escalate_days", "freight_variance_alert"),
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
