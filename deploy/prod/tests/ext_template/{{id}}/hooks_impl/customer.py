import frappe
from frappe import _

from ffl_core.extensions import extension_hook

APP = "{{id}}"


@extension_hook(APP)                       # switched off → nothing happens
def after_insert(doc, method=None):
	frappe.msgprint(_("Welcome, {0}!").format(doc.customer_name), alert=True)


def disable_notice() -> str:
	"""Shown on the Extensions page before the switch is turned off."""
	return _("New customers will no longer get the welcome message.")
