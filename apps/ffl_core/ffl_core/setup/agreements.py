"""Agreement fields on Item and Customer (rules in ``ffl_core.agreements``).

* **Item** — ``ffl_required_agreement`` (Link Agreement Template): the buyer must
  have signed it before a counter sale of this Item is submitted
  (``agreements.refuse_missing_required``). Next to the Firearm flags on the
  first tab.
* **Customer** — an "Agreements" section on the Profile tab, after the merged
  aliases: one HTML slot the form script fills with the customer's copies and the
  Request agreement button.

Custom fields only, so it is safe at after_install. Idempotent; never raises.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELDS = {
	"Item": [
		{"fieldname": "ffl_required_agreement", "fieldtype": "Link", "options": "Agreement Template",
			"label": "Agreement Required to Buy", "insert_after": "is_antique",
			"description": "The customer must have signed this before a counter sale of the item is submitted."},
	],
	"Customer": [
		{"fieldname": "section_customer_agreements", "fieldtype": "Section Break", "label": "Agreements",
			"insert_after": "customer_aliases"},
		{"fieldname": "customer_agreements_html", "fieldtype": "HTML", "label": "Agreements",
			"insert_after": "section_customer_agreements"},
	],
}


def ensure() -> dict:
	"""Create the agreement fields. Idempotent; never raises."""
	try:
		create_custom_fields({dt: [{**f, "module": "FFL Core"} for f in rows] for dt, rows in FIELDS.items()},
			ignore_validate=True)
		for dt in FIELDS:
			frappe.clear_cache(doctype=dt)
		return {"ok": 1}
	except Exception:
		frappe.log_error(title="agreements.ensure failed", message=frappe.get_traceback())
		return {"ok": 0}
