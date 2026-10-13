"""Backfill Serial No.model_name/caliber from the model Item for existing firearm
serials that don't carry their own — so the by-serial form, the Firearms In Stock
report and the FastBound push all read each gun's true model/caliber from the Serial
No (the report COALESCEs a blank serial field to the Item). Going forward,
ffl_core.firearm.inherit_serial_firearm_attrs keeps new/edited serials in sync and the
FastBound import/sync stamps them from the bound book. An explicit per-serial value is
never overwritten.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	# Fixtures sync AFTER post_model_sync patches, so on the migrate that introduces
	# these fields the columns don't exist yet. Ensure them first (idempotent; the
	# fixture sync later in this same migrate fills in the full field properties).
	create_custom_fields(
		{
			"Serial No": [
				{"fieldname": "model_name", "label": "Model", "fieldtype": "Data",
				 "insert_after": "manufacturer", "module": "FFL Core"},
				{"fieldname": "caliber", "label": "Caliber", "fieldtype": "Link",
				 "options": "Caliber", "insert_after": "model_name", "module": "FFL Core"},
			]
		},
		ignore_validate=True,
	)

	rows = frappe.db.sql(
		"""
		SELECT sn.name,
		       NULLIF(sn.model_name, '') AS sn_model, NULLIF(sn.caliber, '') AS sn_cal,
		       NULLIF(i.model_name, '') AS item_model, NULLIF(i.caliber, '') AS item_cal
		FROM `tabSerial No` sn
		INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
		WHERE (NULLIF(sn.model_name, '') IS NULL AND NULLIF(i.model_name, '') IS NOT NULL)
		   OR (NULLIF(sn.caliber, '') IS NULL AND NULLIF(i.caliber, '') IS NOT NULL)
		""",
		as_dict=True,
	)
	for r in rows:
		updates = {}
		if not r.sn_model and r.item_model:
			updates["model_name"] = r.item_model
		if not r.sn_cal and r.item_cal:
			updates["caliber"] = r.item_cal
		if updates:
			frappe.db.set_value("Serial No", r.name, updates, update_modified=False)
