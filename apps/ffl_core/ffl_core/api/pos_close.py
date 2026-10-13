"""Nightly POS auto-close.

Finds every submitted POS Opening Entry that has no corresponding POS
Closing Entry yet, builds the closing via ERPNext's
`make_closing_entry_from_opening`, copies `expected_amount → closing_amount`
on each payment row (treating the till as matched with zero variance),
and submits — which consolidates the day's POS Invoices into Sales Invoices.

Scheduled once nightly at 23:00 store-local (America/Chicago; Frappe evaluates
cron in the site timezone — see hooks.scheduler_events) so the day's shifts
close the same night and the books are clean by morning. By 11 PM the store is
closed, so any still-open opening is a finished shift the operator forgot to
close. The small MIN_AGE_HOURS guard only skips an opening created in the last
hour (a fluke late open just before the cron) — the next night's run catches it.

The shift is consolidated, but the drawer is NOT counted here: nobody has counted it
at 23:00. Every POS Closing Entry gets a Cash row built from the cash ledger
(ffl_core.cash_drawer.stamp_closing_entry, flagged "not counted", difference 0), and
each company with a POS Profile gets an uncounted Cash Drawer Close for the day unless
staff already counted it. Nothing is booked for an uncounted day: the next real count
(Cash Drawer page, ffl_core.cash_drawer.close_day) absorbs the difference for every day
since the last one. Other tenders (card, Zelle...) are still trusted as expected.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt, get_datetime, now_datetime

from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
	make_closing_entry_from_opening,
)

from ffl_core.cash_drawer import ensure_uncounted_close


# Minimum age (hours) between opening creation and auto-close — a thin safety
# margin so the nightly 23:00 run won't close an opening created in the last
# hour (the store is already shut, so this is just belt-and-suspenders). It is
# the nightly cron timing, NOT this guard, that makes auto-close safe; keep it
# small so the day's shifts (opened any time before ~22:00) all close tonight.
MIN_AGE_HOURS = 1


@frappe.whitelist()
def auto_close_open_pos(min_age_hours: int | None = None) -> dict:
	"""Auto-close every stale open POS shift, WITHOUT counting the drawer.

	Finds every unclosed POS Opening Entry older than min_age_hours and
	submits a matching POS Closing Entry (card and other tenders trusted:
	closing amount = expected amount; the Cash row repeats the cash ledger,
	flagged not counted). Then leaves an uncounted Cash Drawer Close for the
	day in every company that has a POS Profile, unless staff already counted
	today. Nothing is booked for the drawer. Runs nightly from the scheduler;
	whitelisted for a manual force-close.

	Args:
	    min_age_hours: Minimum opening age to close (default 1).

	Returns:
	    {closed: [{opening, closing, grand_total}], skipped: [...],
	    errors: [...], cash_drawer: [Cash Drawer Close names left uncounted],
	    total_open_at_start, min_age_hours, ran_at}.

	Permissions:
	    Sales Manager / Stock Manager / System Manager.
	"""
	# Whitelisted force-close: managers only (the nightly cron runs as
	# Administrator, which only_for bypasses).
	frappe.only_for(("Sales Manager", "Stock Manager", "System Manager"))
	min_age = int(min_age_hours if min_age_hours is not None else MIN_AGE_HOURS)
	cutoff = frappe.utils.add_to_date(now_datetime(), hours=-min_age)

	# Submitted openings with no closing referencing them.
	openings = frappe.db.sql(
		"""
		SELECT poe.name, poe.period_start_date, poe.company
		FROM `tabPOS Opening Entry` poe
		LEFT JOIN `tabPOS Closing Entry` pce
			ON pce.pos_opening_entry = poe.name AND pce.docstatus IN (0, 1)
		WHERE poe.docstatus = 1
			AND poe.status = 'Open'
			AND pce.name IS NULL
			AND poe.period_start_date <= %s
		ORDER BY poe.period_start_date, poe.creation
		""",
		(cutoff,),
		as_dict=True,
	)
	last_per_company: dict = {}
	for row in openings:
		last_per_company[row["company"]] = row["name"]

	closed: list[dict] = []
	skipped: list[dict] = []
	errors: list[str] = []

	for row in openings:
		try:
			opening = frappe.get_doc("POS Opening Entry", row["name"])
			closing = make_closing_entry_from_opening(opening)
			# Card and other tenders: trust the till (closing = expected). The
			# Cash row is stamped on insert from the cash ledger, not counted;
			# only the company's last shift carries the drawer's figures.
			if row["name"] == last_per_company.get(row["company"]):
				closing.flags.ffl_cash_drawer = True
			else:
				closing.flags.ffl_cash_zero = True
			for p in closing.payment_reconciliation:
				if p.mode_of_payment == "Cash":
					continue
				p.closing_amount = flt(p.expected_amount)
				p.difference = 0
			# Also fold the cashier-was-here audit trail.
			closing.flags.ignore_permissions = True
			closing.insert()
			closing.submit()
			closed.append({
				"opening": opening.name,
				"closing": closing.name,
				"grand_total": flt(closing.grand_total),
			})
		except Exception as e:
			errors.append(f"{row['name']}: {e}")
			frappe.log_error(
				title=f"POS auto-close failed for {row['name']}",
				message=frappe.get_traceback(),
			)

	# The drawer: a day nobody counted is left as such, once per company.
	uncounted: list[str] = []
	for company in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="company", distinct=True):
		try:
			name = ensure_uncounted_close(company)
			if name:
				uncounted.append(name)
		except Exception as e:
			errors.append(f"cash drawer {company}: {e}")
			frappe.log_error(
				title=f"Uncounted cash close failed for {company}", message=frappe.get_traceback())

	return {
		"closed": closed,
		"skipped": skipped,
		"errors": errors,
		"cash_drawer": uncounted,
		"total_open_at_start": len(openings),
		"min_age_hours": min_age,
		"ran_at": str(now_datetime()),
	}
