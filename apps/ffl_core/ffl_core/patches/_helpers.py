"""Shared helpers for one-shot migration patches."""
from __future__ import annotations

import frappe


def ensure_field_column(doctype: str, fieldname: str, df: dict) -> bool:
	"""Ensure a Custom Field's DB column exists before a patch reads/writes it.

	`bench migrate` can run patches before fixtures finish syncing the Custom
	Field on a fresh clone — create it synchronously if the column is missing
	so the ALTER TABLE happens up-front (the later fixtures pass no-ops on the
	existing definition). `df` is the field definition minus doctype/dt/
	fieldname/module. Returns True if the column is present afterwards.
	"""
	if fieldname in frappe.db.get_table_columns(doctype):
		return True
	if not frappe.db.exists("Custom Field", f"{doctype}-{fieldname}"):
		cf = frappe.get_doc({
			"doctype": "Custom Field",
			"dt": doctype,
			"fieldname": fieldname,
			"module": "FFL Core",
			**df,
		})
		cf.flags.ignore_permissions = True
		cf.insert()
	frappe.clear_cache(doctype=doctype)
	return fieldname in frappe.db.get_table_columns(doctype)
