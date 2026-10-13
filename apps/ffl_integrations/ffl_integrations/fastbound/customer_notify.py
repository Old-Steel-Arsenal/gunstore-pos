"""Customer 'NICS cleared — pick up your firearm' email, fired from the FastBound
webhook when a Delayed background check later proceeds."""
from __future__ import annotations

import frappe

_TEMPLATE = "NICS Cleared Pickup"


@frappe.task(queue="long", timeout=120)
def notify_customer_nics_cleared(pos_invoice_name: str) -> None:
	"""Idempotent: email the buyer once that their background check cleared."""
	inv = frappe.db.get_value(
		"POS Invoice", pos_invoice_name,
		["customer", "customer_name", "contact_email", "custom_nics_cleared_notified", "company"],
		as_dict=True,
	)
	if not inv or inv.custom_nics_cleared_notified:
		return
	email = (inv.contact_email or "").strip()
	if not email and inv.customer:
		email = (frappe.db.get_value("Customer", inv.customer, "email_id") or "").strip()
	if not email:
		return
	template = frappe.get_doc("Email Template", _TEMPLATE)
	# `company` feeds the template's company_brand() call — the store name in the
	# body is whichever store sold the gun, not a literal (ffl_core.store_identity).
	context = {
		"customer_name": inv.customer_name or inv.customer,
		"invoice": pos_invoice_name,
		"company": inv.company,
	}
	body = template.response_html if template.use_html else template.response
	frappe.sendmail(
		recipients=[email],
		subject=frappe.render_template(template.subject, context),
		message=frappe.render_template(body, context),
		reference_doctype="POS Invoice",
		reference_name=pos_invoice_name,
		now=False,
	)
	frappe.db.set_value(
		"POS Invoice", pos_invoice_name, "custom_nics_cleared_notified", 1,
		update_modified=False)
