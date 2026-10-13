"""Phase 1 hotfix: ERPNext v16 dropped Item.serial_no, so we add our own
Custom Field `firearm_serial` on Item to keep a canonical, human-visible
serial reference on each Used Firearm.

Run once after migrate:
    bench --site dev.localhost execute scripts.add_firearm_serial_field.run
"""
from __future__ import annotations

import frappe


def run() -> None:
	if frappe.db.exists("Custom Field", "Item-firearm_serial"):
		print("Custom Field Item-firearm_serial already exists")
		return

	frappe.get_doc({
		"doctype": "Custom Field",
		"name": "Item-firearm_serial",
		"dt": "Item",
		"fieldname": "firearm_serial",
		"fieldtype": "Data",
		"label": "Firearm Serial",
		"description": (
			"Canonical serial for one-and-done Used Firearm Items. "
			"ERPNext's Serial No doctype tracks stock movements; this field is the human-visible reference."
		),
		"insert_after": "section_firearm",
		"depends_on": "eval:['Used Firearm','Firearm'].includes(doc.item_group)",
		"in_list_view": 1,
		"module": "FFL Core",
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	print("created Custom Field Item-firearm_serial")
