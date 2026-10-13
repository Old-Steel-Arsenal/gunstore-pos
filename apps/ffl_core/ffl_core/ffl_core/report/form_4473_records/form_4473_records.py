"""Form 4473 Records — every completed 4473 on file, newest first, with its PDF.

Reads the Form 4473 Log (ffl_core doctype), the durable record the
form4473.completed webhook and the paper / manager-override path write: a 4473
stays listed after its draft is deleted (Clear Abandoned Order, a Denied
buyer's draft) or re-run with Redo 4473, and whatever the NICS outcome.
FastBound's API cannot list completed 4473s, which is why the POS keeps its
own. Order Status says what became of the order since. The PDF is served by
ffl_integrations.fastbound.pos_4473.view_4473_pdf (see the .js formatter).
"""
from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import getdate

_ORDER_STATUS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return _columns(), _rows(filters)


def _columns():
	return [
		{"label": _("Completed On"), "fieldname": "completed_on", "fieldtype": "Datetime", "width": 150},
		{"label": _("Record"), "fieldname": "log", "fieldtype": "Link",
			"options": "Form 4473 Log", "width": 120},
		{"label": _("Invoice"), "fieldname": "pos_invoice", "fieldtype": "Data", "width": 170},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link",
			"options": "Customer", "width": 170},
		{"label": _("Firearms"), "fieldname": "serials", "fieldtype": "Data", "width": 180},
		{"label": _("NICS"), "fieldname": "nics_status", "fieldtype": "Data", "width": 90},
		{"label": _("Source"), "fieldname": "source", "fieldtype": "Data", "width": 130},
		{"label": _("Order Status"), "fieldname": "order_status", "fieldtype": "Data", "width": 110},
		{"label": _("4473 PDF"), "fieldname": "pdf", "fieldtype": "Data", "width": 90},
		{"label": _("PDF Copy"), "fieldname": "pdf_status", "fieldtype": "Data", "width": 90},
		{"label": _("Has PDF"), "fieldname": "has_pdf", "fieldtype": "Check", "hidden": 1},
	]


def _rows(filters):
	conditions = []
	if filters.get("from_date"):
		conditions.append(["completed_on", ">=", getdate(filters.from_date)])
	if filters.get("to_date"):
		conditions.append(["completed_on", "<", getdate(filters.to_date) + timedelta(days=1)])
	for field in ("customer", "nics_status", "source"):
		if filters.get(field):
			conditions.append([field, "=", filters[field]])
	if (filters.get("serial") or "").strip():
		conditions.append(["serials", "like", f"%{filters.serial.strip()}%"])

	# get_list, not raw SQL: the user's permissions (and User Permissions) apply.
	logs = frappe.get_list("Form 4473 Log", filters=conditions, limit_page_length=0,
		fields=["name", "completed_on", "pos_invoice", "customer", "serials", "nics_status",
			"source", "form4473_id", "fastbound_disposition_id", "pdf_file", "pdf_status"],
		order_by="completed_on desc, name desc")
	invoices = {i for log in logs for i in _invoices(log)}
	orders = {o.name: o for o in frappe.get_all("POS Invoice",
		filters={"name": ("in", list(invoices))},
		fields=["name", "docstatus", "fastbound_disposition_id"])} if invoices else {}

	out = []
	for log in logs:
		has_pdf = 1 if (log.form4473_id or log.pdf_file) else 0
		out.append({
			"completed_on": log.completed_on,
			"log": log.name,
			"pos_invoice": log.pos_invoice,
			"customer": log.customer,
			"serials": log.serials,
			"nics_status": log.nics_status,
			"source": log.source,
			"order_status": _order_status(log, orders),
			"pdf": _("View PDF") if has_pdf else "",
			"pdf_status": log.pdf_status,
			"has_pdf": has_pdf,
		})
	return out


def _invoices(log) -> list[str]:
	return [i for i in (log.pos_invoice or "").split(", ") if i]


def _order_status(log, orders: dict) -> str:
	"""What became of the order this 4473 ran on — its latest invoice."""
	invoices = _invoices(log)
	if not invoices:
		return "No POS order"
	order = orders.get(invoices[-1])
	if order is None:
		return "Deleted"
	# Redo 4473 blanks the invoice's disposition id and the next 4473 sets a new
	# one: either way it no longer points at this 4473.
	if log.fastbound_disposition_id and order.fastbound_disposition_id != log.fastbound_disposition_id:
		return "Redone"
	return _ORDER_STATUS.get(order.docstatus, "")
