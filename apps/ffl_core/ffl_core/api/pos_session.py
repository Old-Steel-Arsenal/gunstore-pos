"""POS opening-entry resolution for a shared register.

ERPNext pairs two rules that deadlock a multi-clerk store:
  - POS page load resumes only an open entry whose `user` == session user
    (point_of_sale.check_opening_entry);
  - POS Opening Entry validation allows only ONE open entry per POS Profile
    (check_open_pos_exists).
So the moment clerk A opens the shift, clerk B (or A on another login) gets
the "create" dialog and then "{profile} is open. Close the POS or cancel..."
— they can neither resume nor create.

This override (wired via override_whitelisted_methods) keeps the native
behavior when the user has their own open entry, and otherwise falls back to
ANY open entry — one register, one shift, many clerks.
"""
from __future__ import annotations

import frappe
from frappe.utils import add_to_date

_FIELDS = ["name", "company", "pos_profile", "period_start_date"]


@frappe.whitelist()
def check_opening_entry(user):
	"""Resolve the POS opening entry for a shared register (override).

	Replaces ERPNext's point_of_sale.check_opening_entry (wired via
	override_whitelisted_methods): keeps the native behavior when the user
	has their own open entry, and otherwise falls back to ANY open entry —
	one register, one shift, many clerks (see module docstring for the
	deadlock this solves).

	Args:
	    user: Session user the POS page is resolving a shift for.

	Returns:
	    List of open POS Opening Entry rows ({name, company, pos_profile,
	    period_start_date}), the caller's own first.

	Permissions:
	    Open by design (endpoint-gates adjudication group A): ERPNext's
	    own check_opening_entry is gateless, and this override must keep
	    upstream's semantics — any clerk resuming the shared register
	    calls it before a shift/role context exists.
	"""
	own = frappe.db.get_all(
		"POS Opening Entry",
		# status="Open" is required, not just an empty pos_closing_entry: if a
		# Closing Entry is deleted, its opening is left status="Closed" with an
		# empty link, and trusting the link alone would resurface that closed
		# shift as "open" — then ERPNext's Close Shift throws "Selected POS
		# Opening Entry should be open." and the register is stuck. Mirror the
		# status filter the fallback below already uses.
		filters={"user": user, "status": "Open", "pos_closing_entry": ["in", ["", None]], "docstatus": 1},
		fields=_FIELDS,
		order_by="period_start_date desc",
	)
	if own:
		return own
	# Fall back to the register's open shift regardless of who opened it.
	return frappe.db.get_all(
		"POS Opening Entry",
		filters={"status": "Open", "docstatus": 1},
		fields=_FIELDS,
		order_by="period_start_date desc",
	)


def open_shift_for_profile(pos_profile: str, *, at=None, dead_after_days: int | None = None):
	"""The register's live shift that covers a sale at ``at`` (default: any time):
	the newest Open, submitted POS Opening Entry on ``pos_profile`` that started
	at or before ``at`` — and, when ``dead_after_days`` is given, not earlier
	than that many days before ``at`` (an opening left Open that long is
	abandoned and holds nothing). Returns ``{name, user, period_start_date}`` or
	None.

	The Python twin of the covering-shift rule the stuck-invoice sweep writes in
	SQL (``pos_consolidate._find_stuck_pos_invoices``); change the two together.
	"""
	filters = {"docstatus": 1, "status": "Open", "pos_profile": pos_profile}
	if at is not None:
		filters["period_start_date"] = ["<=", at]
	rows = frappe.db.get_all(
		"POS Opening Entry", filters=filters,
		fields=["name", "user", "period_start_date"],
		order_by="period_start_date desc", limit=1,
	)
	if not rows:
		return None
	shift = rows[0]
	if dead_after_days is not None and at is not None:
		if shift.period_start_date < add_to_date(at, days=-dead_after_days):
			return None
	return shift
