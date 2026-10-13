"""The POS Special Order flow is retired (neither store ever created
one). Ordering goes through Create Order.

The code is gone; this removes what it left on a site: the "Open Special Orders" report, the
"Special Orders" number card (off every workspace first), the four runtime link fields, and the
SPECIAL-ORDER-DEPOSIT item with its Item Prices (the locked-line tombstones). An item that any
record still references (a test or dev site) is disabled instead of deleted, so no row is left
pointing at nothing.

Not touched: the Customer Deposits account (setup/customer_deposits keeps it; the GunBroker
cash-basis report looks it up by name) and the Service item group.

Idempotent: on a second run, or on a site that never had any of it, nothing is found.
"""
from __future__ import annotations

import frappe

from ffl_core.workspace_shortcuts import remove_number_card

REPORT = "Open Special Orders"
NUMBER_CARD = "Special Orders"
ITEM = "SPECIAL-ORDER-DEPOSIT"
CUSTOM_FIELDS = (
	"Sales Order-ffl_special_order",
	"Sales Order-ffl_deposit_invoice",
	"POS Invoice Item-ffl_special_order",
	"Sales Invoice Item-ffl_special_order",
)


def execute() -> None:
	if frappe.db.exists("Report", REPORT):
		frappe.delete_doc("Report", REPORT, force=True, ignore_permissions=True)

	for workspace in frappe.get_all("Workspace Number Card",
			filters={"number_card_name": NUMBER_CARD, "parenttype": "Workspace"}, pluck="parent", distinct=True):
		remove_number_card(NUMBER_CARD, workspace=workspace)
	if frappe.db.exists("Number Card", NUMBER_CARD):
		frappe.delete_doc("Number Card", NUMBER_CARD, force=True, ignore_permissions=True)

	for name in CUSTOM_FIELDS:
		frappe.delete_doc_if_exists("Custom Field", name)

	_retire_item()


def _retire_item() -> None:
	for price in frappe.get_all("Item Price", filters={"item_code": ITEM}, pluck="name"):
		frappe.delete_doc("Item Price", price, force=True, ignore_permissions=True)
	if not frappe.db.exists("Item", ITEM):
		return
	if _referenced(ITEM):
		frappe.db.set_value("Item", ITEM, "disabled", 1, update_modified=False)
	else:
		frappe.delete_doc("Item", ITEM, force=True, ignore_permissions=True)


def _referenced(item_code: str) -> bool:
	"""Does any record outside the item itself point at it? Every Link field to Item on the site
	(standard, custom and property-setter ones). An unreadable table counts as a reference: a
	disabled item is harmless, a deleted one under a live row is not."""
	from frappe.model.rename_doc import get_link_fields

	for link in get_link_fields("Item"):
		doctype, field = link["parent"], link["fieldname"]
		try:
			if link.get("issingle"):
				if frappe.db.get_single_value(doctype, field) == item_code:
					return True
			elif frappe.db.exists(doctype, {field: item_code}):
				return True
		except Exception:
			return True
	return False
