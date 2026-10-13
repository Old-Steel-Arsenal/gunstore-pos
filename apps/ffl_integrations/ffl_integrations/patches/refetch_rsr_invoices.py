"""RSR invoices mirrored before the per-slot fix billed only the FIRST slot of an
item that sits on one invoice several times (one slot per serialized unit), so
e.g. two NFA units showed as qty 1. Drop the RSR mirror rows the hourly
``distributor.invoices.sync_invoices`` can pull again (invoice dated inside its
LOOKBACK_DAYS window) so they come back through the fixed parser; older rows are
left as they are rather than lost. Display-only data, no GL, nothing links to
these rows. One-time."""
import frappe

from frappe.utils import add_days, nowdate

from ffl_integrations.distributor.invoices import LOOKBACK_DAYS
from ffl_integrations.distributor.rsr_provider import DISTRIBUTOR_CODE

DT, LINE_DT = "Distributor Invoice", "Distributor Invoice Line"


def execute():
	houses = frappe.get_all("Distributor", filters={"provider": DISTRIBUTOR_CODE}, pluck="name")
	if not houses:
		return
	names = frappe.get_all(DT, filters={"distributor": ["in", houses],
		"invoice_date": [">=", add_days(nowdate(), -LOOKBACK_DAYS)]}, pluck="name")
	if names:
		frappe.db.delete(LINE_DT, {"parenttype": DT, "parent": ["in", names]})
		frappe.db.delete(DT, {"name": ["in", names]})
