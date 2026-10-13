"""Sports South invoices mirrored before the fee split: strip the fee lines' "0"
from ``order_ref`` and move the mirrored ``*PROCESSING FEE ...`` service-charge
lines out of the merchandise lines into ``fees``. Display-only data, no GL.

Scope: only invoices of Distributors whose provider is Sports South, and only
lines whose description starts with the fee prefix. Live ``normalize_invoice``
classifies by ORDNO "0", but stored lines have no ORDNO column, and
``*PROCESSING FEE`` is the only fee shape ever mirrored (a ``*SHIPPING`` line
was never stored as a line — it is the ``freight`` total). One-time, idempotent
(a second run finds nothing left to change)."""
import frappe

from ffl_integrations.distributor.ss_provider import DISTRIBUTOR_CODE, FEE_ORDNO, STORED_FEE_PREFIX

DT, LINE_DT = "Distributor Invoice", "Distributor Invoice Line"


def execute():
	houses = frappe.get_all("Distributor", filters={"provider": DISTRIBUTOR_CODE}, pluck="name")
	if not houses:
		return
	invoices = frappe.get_all(DT, filters={"distributor": ["in", houses]},
		fields=["name", "order_ref", "fees"])
	if not invoices:
		return
	fee_by_parent: dict = {}
	for ln in frappe.get_all(LINE_DT, filters={"parenttype": DT,
			"parent": ["in", [i.name for i in invoices]],
			"description": ["like", STORED_FEE_PREFIX + "%"]},
			fields=["name", "parent", "description", "amount"]):
		if (ln.description or "").upper().startswith(STORED_FEE_PREFIX):
			fee_by_parent.setdefault(ln.parent, []).append(ln)
	for inv in invoices:
		refs = [r.strip() for r in (inv.order_ref or "").split(",") if r.strip()]
		clean = ",".join(r for r in refs if r != FEE_ORDNO)
		fee_lines = fee_by_parent.get(inv.name) or []
		values = {}
		if clean != (inv.order_ref or ""):
			values["order_ref"] = clean
		if fee_lines:
			values["fees"] = round((inv.fees or 0) + sum(ln.amount or 0 for ln in fee_lines), 2)
			frappe.db.delete(LINE_DT, {"name": ["in", [ln.name for ln in fee_lines]]})
		if values:
			frappe.db.set_value(DT, inv.name, values, update_modified=False)
