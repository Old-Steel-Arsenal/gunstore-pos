"""Stamp pre-Ship-button, fully-disposed orders shipstation_status='Shipped'.

The Pending Order queue now retains disposed-but-unpushed Sales Invoices so
staff can buy the label via the new Ship button. Without this stamp, every
manual order completed before the feature (shipped with hand-bought labels;
status 'Not Requested', or NULL on SIs predating the ShipStation fields)
would flood back into the queue as "needs Ship". 'Shipped' is factually what
happened to those orders; no shipstation_order_id is fabricated, so legacy
rows stay distinguishable from real pushes. Idempotent.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.api.manual_order import shipstation_pushed
from ffl_core.reservations import si_pending_firearm_serials


def execute():
	if not frappe.db.has_column("Sales Invoice", "shipstation_status"):
		# Fixtures sync AFTER post_model_sync patches — on a site that has
		# never synced the ShipStation custom fields (fresh install, or prod
		# main lagging behind develop) the columns this patch reads/writes
		# don't exist yet. Create them here; the ffl_core fixture then
		# overwrites with the full definitions on sync.
		create_custom_fields({
			"Sales Invoice": [
				{"fieldname": "section_shipstation", "fieldtype": "Section Break",
					"label": "Shipping (ShipStation)",
					"insert_after": "ffl_destination_ffl"},
				{"fieldname": "shipstation_status", "fieldtype": "Select",
					"label": "ShipStation Status",
					"options": "\nNot Requested\nPending\nLabel Created\nShipped\nDelivered\nCancelled",
					"default": "Not Requested", "read_only": 1,
					"allow_on_submit": 1, "no_copy": 1,
					"insert_after": "section_shipstation"},
				{"fieldname": "shipstation_order_id", "fieldtype": "Data",
					"label": "ShipStation Order ID", "read_only": 1,
					"allow_on_submit": 1, "no_copy": 1,
					"insert_after": "shipstation_status"},
			]
		}, ignore_validate=True)

	rows = frappe.get_all(
		"Sales Invoice",
		filters={"docstatus": 1, "ffl_destination_ffl": ("is", "set")},
		fields=["name", "shipstation_status", "shipstation_order_id"])
	for r in rows:
		# Python-side (not SQL) so NULL statuses are handled; the shared
		# predicate keeps this aligned with the queue's retention rule.
		if shipstation_pushed(r.shipstation_order_id, r.shipstation_status):
			continue
		if not frappe.db.exists(
				"FFL Disposition", {"sales_invoice": r.name, "docstatus": 1}):
			# Never disposed → the retention rule won't resurrect it (it is
			# either still genuinely pending, or has no booked transfer at
			# all); leave untouched.
			continue
		si = frappe.get_doc("Sales Invoice", r.name)
		if si_pending_firearm_serials(si):
			continue  # partially disposed → genuinely still pending work
		frappe.db.set_value("Sales Invoice", r.name,
			"shipstation_status", "Shipped", update_modified=False)
