"""Distributor invoices mirrored before invoice receiving shipped were handled the
old way (received by hand, or not at all): mark every existing row received so none
of them turns up in Receive Goods → Incoming shipments, and fill ``destination``
best effort with no distributor call — the matched order's lane, else the account
(dealer = Store, fulfillment = Drop-ship), else Unknown. Also records the day
receiving shipped, so an invoice re-pulled later (``refetch_rsr_invoices`` drops
rows the hourly sync then mirrors again) that predates it is born received too.
Also fills the new line ``upc`` from the "UPC <code>" description RSR lines were
mirrored with. Display-only data, no GL. One-time."""
import re

import frappe
from frappe.utils import nowdate

from ffl_integrations.distributor import lanes
from ffl_integrations.distributor.invoices import DOCTYPE, RECEIVE_FROM_KEY


LINE_DT = "Distributor Invoice Line"
_UPC = re.compile(r"^UPC\s+(\d{6,14})$", re.I)


def execute():
	frappe.db.set_default(RECEIVE_FROM_KEY, nowdate())
	for ln in frappe.get_all(LINE_DT, filters={"parenttype": DOCTYPE, "upc": ["is", "not set"],
			"description": ["like", "UPC %"]}, fields=["name", "description"]):
		m = _UPC.match((ln.description or "").strip())
		if m:
			frappe.db.set_value(LINE_DT, ln.name, "upc", m.group(1), update_modified=False)
	order_types = dict(frappe.get_all("Distributor Order", fields=["name", "order_type"], as_list=True))
	for inv in frappe.get_all(DOCTYPE, fields=["name", "account", "distributor_order", "destination"]):
		values = {"received": 1}
		if not inv.destination or inv.destination == lanes.DEST_UNKNOWN:
			values["destination"] = (
				lanes.destination_of_order_type(order_types.get(inv.distributor_order))
				if inv.distributor_order else lanes.destination_of_account(inv.account))
		frappe.db.set_value(DOCTYPE, inv.name, values, update_modified=False)
