"""Customer sales-tax-exempt fields.

A dealer/business with a resale or exemption certificate on file is exempt from
sales tax on all of their orders. The certificate is attached to the Customer
(retrievable any time) and `is_tax_exempt` drives the 0% tax in
ffl_core.api.manual_order.create_order.

Installed idempotently via create_custom_fields (mirrors setup/shipstation_fields)
and wired into after_install + after_migrate.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

_FIELDS = {
	"Customer": [
		{
			"fieldname": "is_tax_exempt",
			"fieldtype": "Check",
			"label": "Tax Exempt",
			"insert_after": "is_ffl_dealer",
			"in_standard_filter": 1,
			"description": "Sales-tax exempt — a resale/exemption certificate is on "
				"file. Exempts every order for this customer from sales tax.",
		},
		{
			"fieldname": "tax_exemption_certificate",
			"fieldtype": "Attach",
			"label": "Tax Exemption Certificate",
			"insert_after": "is_tax_exempt",
			"depends_on": "is_tax_exempt",
			"description": "The resale/exemption certificate kept on file.",
		},
	],
}


def ensure() -> None:
	try:
		create_custom_fields(_FIELDS, ignore_validate=True)
	except Exception:
		frappe.log_error(
			title="ffl_core.setup.customer_tax: ensure failed",
			message=frappe.get_traceback())
