"""Abandoned-4473 watch: a parked counter order nobody ever came back for.

A Draft POS Invoice holds its firearm from the moment it is saved — off the
storefront, off the picker, out of every other channel — and nothing ages it
out. DISP-2026-02000 sat that way for eleven days: the 4473 was started at the
counter on 2026-08-29, abandoned, and the gun stayed spoken for until a web
order tripped over it.

The Pending 4473 Orders report is already the queue the operator works every
morning; this is the same rows, pushed rather than pulled, for the ones nobody
worked. It only ever ALERTS. Clearing an abandoned order deletes a draft and
touches the FastBound bound book, which is a decision a person makes with a
reason attached — ``ffl_integrations.fastbound.pos_4473_recovery
.manager_abandon_4473``, behind a manager gate and a button.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from ffl_core.ffl_core.report.pending_4473_orders.pending_4473_orders import (
	add_business_days,
	execute as pending_4473_report,
)

FORM_4473_PENDING = "Pending"

# How long past the Brady date an unfinished 4473 may sit before it is an
# abandoned order rather than a slow one. Two business days, on top of the
# Brady 3rd business day the report already computes — the
# clock is anchored to the number the operator is already reading off the
# queue rather than to a new constant nobody would keep in step with it.
GRACE_BUSINESS_DAYS = 2


def overdue_pending_4473(as_of=None) -> list[dict]:
	"""Draft POS Invoices whose 4473 has been Pending past its grace date.

	Reads the Pending 4473 Orders report rather than re-deriving the queue:
	"which drafts hold a firearm" is a definition that has already drifted once
	(antiques), and two copies of it drift again.

	Args:
	    as_of: Date to judge against (defaults to today). Testing seam.

	Returns:
	    The report's rows for the overdue orders, each with a ``due_date`` key
	    (Brady + :data:`GRACE_BUSINESS_DAYS` business days) added.
	"""
	today = getdate(as_of or nowdate())
	out: list[dict] = []
	for row in pending_4473_report({})[1]:
		if (row.get("form_4473_status") or "") != FORM_4473_PENDING:
			continue
		due = add_business_days(row["brady_date"], GRACE_BUSINESS_DAYS)
		if today <= due:
			continue
		out.append({**row, "due_date": due})
	return out


@frappe.whitelist()
def alert_overdue_pending_4473() -> dict:
	"""Raise a manager ToDo for every counter order whose 4473 was abandoned.

	Scheduled daily. Alerts only — it never deletes a draft, never releases a
	gun and never calls FastBound: an abandoned 4473 leaves a pending
	disposition in the bound book, and deleting one of those unattended is
	exactly the automation the owner refused.

	Deduped by ``ffl_core.alerts.alert_once``, so an order that stays abandoned
	is one ToDo on the invoice until a person closes it — not one per day.

	Returns:
	    ``{overdue, alerted}`` — ``alerted`` counts only the NEW alerts;
	    the difference is orders already carrying an open ToDo.

	Permissions:
	    Inventory roles (manual path; the scheduler runs as Administrator).
	"""
	from ffl_core.alerts import alert_once
	from ffl_core.roles import STOCK_ROLES

	if frappe.session.user != "Administrator":
		frappe.only_for(STOCK_ROLES)

	rows = overdue_pending_4473()
	alerted = 0
	for row in rows:
		invoice = row["pos_invoice"]
		body = _(
			"POS Invoice {0} has held {1} since {2} with its 4473 still Pending "
			"— past {3} (Brady {4} + {5} business days). The firearm is off the "
			"storefront and out of every other channel for as long as this draft "
			"lives. Finish the 4473, or clear the order with the manager cleanup "
			"button on the Pending 4473 Orders report (it removes FastBound's "
			"pending disposition and deletes the draft; the POS recycle bin keeps "
			"it restorable)."
		).format(invoice, row.get("serials") or "?", row.get("parked_on"),
			row["due_date"], row.get("brady_date"), GRACE_BUSINESS_DAYS)
		if alert_once(
			"POS Invoice", invoice,
			title=f"Abandoned 4473: POS Invoice {invoice}",
			message=body,
			description=_("Abandoned 4473 — {0} has held {1} since {2}").format(
				invoice, row.get("serials") or "?", row.get("parked_on")),
		):
			alerted += 1
	return {"overdue": len(rows), "alerted": alerted}
