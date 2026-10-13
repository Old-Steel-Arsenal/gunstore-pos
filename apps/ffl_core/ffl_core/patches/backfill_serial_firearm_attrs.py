"""Backfill Serial No.manufacturer from the model Item for existing firearm
serials that don't carry their own — so the by-serial form shows the same
maker the Firearms In Stock report does (the report COALESCEs a blank serial
manufacturer to the Item). Going forward, ffl_core.firearm.inherit_serial_firearm_attrs
(Serial No before_save) keeps new/edited serials in sync. An explicit per-serial
value is never overwritten.

importer was removed from this backfill (2026-08-20): an Item-level importer
can be one gun's marking seeded onto a generic bucket, so copying it to blank
serials manufactured exactly the wrong per-gun data (three such serials had to
be cleaned against FastBound). Nothing treats Item.importer as a per-gun
default any more — a blank serial importer stays blank.
"""
from __future__ import annotations

import frappe


def execute():
	rows = frappe.db.sql(
		"""
		SELECT sn.name,
		       NULLIF(sn.manufacturer, '') AS sn_mfg,
		       NULLIF(i.manufacturer, '') AS item_mfg
		FROM `tabSerial No` sn
		INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
		WHERE NULLIF(sn.manufacturer, '') IS NULL AND NULLIF(i.manufacturer, '') IS NOT NULL
		""",
		as_dict=True,
	)
	for r in rows:
		if not r.sn_mfg and r.item_mfg:
			frappe.db.set_value(
				"Serial No", r.name, {"manufacturer": r.item_mfg}, update_modified=False)
