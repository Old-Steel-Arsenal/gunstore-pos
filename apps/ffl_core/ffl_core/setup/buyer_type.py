"""Buyer-type marker on the Sales Invoice.

create_order records whether a shipped order's buyer is an Individual (the guns
ship to a third-party dealer; payment is required before shipping) or a Business
(an FFL buying for its own license; it may ship on account and stay in the
Pending Order queue until paid). The flag drives the shipping gate in
ffl_core.api.manual_order.dispose_order and the "Business" badge on the page.

Installed idempotently via create_custom_fields (mirrors setup/customer_tax) and
wired into after_install + after_migrate.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "ffl_buyer_type",
			"fieldtype": "Select",
			"label": "FFL Buyer Type",
			"options": "Individual\nBusiness",
			"insert_after": "ffl_destination_ffl",
			"read_only": 1,
			"in_standard_filter": 1,
			"description": "Set by Create Order: Individual ships to a dealer and must "
				"be paid before shipping; Business may ship on account.",
		},
	],
}


def ensure() -> None:
	try:
		create_custom_fields(_FIELDS, ignore_validate=True)
	except Exception:
		frappe.log_error(
			title="ffl_core.setup.buyer_type: ensure failed",
			message=frappe.get_traceback())
