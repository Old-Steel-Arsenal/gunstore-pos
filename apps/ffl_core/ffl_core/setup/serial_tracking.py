"""Stock Settings -> "Activate Serial / Batch No for Item", the one switch a serialized firearm needs.

A fresh ERPNext v16 site has it off, and with it off a firearm Item (has_serial_no) can be neither created
nor stocked. ERPNext's own v16 patch writes it in two places and so do we: the Stock Settings single (what
the validators read) and the global default of the same name (what the Desk reads). A site with only one
of the two set looks on in one place and off in the other.

It is the only Stock Settings value a new site lacks for firearms. The rest of what a production
site has (use_serial_batch_fields, auto_create_serial_and_batch_bundle_for_outward,
do_not_update_serial_batch_on_creation_of_auto_bundle, allow_existing_serial_no, pick_serial_and_batch_based_on
FIFO) is already ERPNext's default on a fresh site, so it is not written here.
"""
from __future__ import annotations

import frappe
from frappe.utils import cint

FIELD = "enable_serial_and_batch_no_for_item"


def is_on() -> bool:
	return bool(cint(frappe.db.get_single_value("Stock Settings", FIELD)) and cint(frappe.db.get_default(FIELD)))


def ensure() -> bool:
	"""Switch it on in both places. Returns True when something was written, False when both were already on."""
	if is_on():
		return False
	frappe.db.set_single_value("Stock Settings", FIELD, 1)
	frappe.db.set_default(FIELD, 1)
	return True
