import frappe
from frappe import _

from ffl_core.extensions import require_enabled
from ffl_core.roles import COUNTER_ROLES

APP = "{{id}}"


@frappe.whitelist(methods=["POST"])
def greet(customer: str):
	"""Say hello to a customer.

	Args:
	    customer: Customer name (the record id).

	Returns:
	    dict: {"message": str}
	"""
	require_enabled(APP, roles=COUNTER_ROLES)
	name = frappe.get_cached_value("Customer", customer, "customer_name") or customer
	return {"message": _("Hello {0}").format(name)}


@frappe.whitelist(methods=["GET"])
def ping():
	"""Prove the extension's Python is importable and switched on.

	Returns:
	    str: "pong"
	"""
	require_enabled(APP, roles=COUNTER_ROLES)
	return "pong"
