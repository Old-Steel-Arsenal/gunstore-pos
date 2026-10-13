"""Sports South scheduled work.

``poll_credits`` — the post-invoice short: warehouse errors are
credited back as credit memos, issued overnight, readable with
GetCreditsByDate(previous day). Each credit is matched to its Distributor
Order by PONBR (= DO name); the credited lines lose their invoiced quantity
(a 0 leaves the line CANCELLED) and an alert goes out so the consumer refund
/ re-source happens — the DO may already be terminal (Complete), which is
why this writes lines directly instead of waiting for a poll that will never
come.
"""
from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe.utils import getdate, now_datetime, nowdate

from ffl_integrations.notifications import notify_admins

SETTINGS = "Sports South Settings"
MAX_CATCHUP_DAYS = 7


def apply_credit(do_lines: list, credit: dict) -> list:
	"""Pure: fold one credit memo onto DO line dicts. Returns the changed lines
	``[{sku, before, after, item_status}]``; unknown SKUs are reported with
	before=after=None so the alert can say so."""
	by_sku = {l["sku"]: l for l in do_lines}
	changes = []
	for cl in credit.get("lines") or []:
		qty = abs(int(cl.get("qty") or 0))
		l = by_sku.get(cl.get("sku"))
		if not l or not qty:
			changes.append({"sku": cl.get("sku"), "before": None, "after": None,
				"item_status": "unknown line"})
			continue
		before = int(l.get("invoiced_qty") or 0)
		after = max(0, before - qty)
		l["invoiced_qty"] = after
		l["item_status"] = "CANCELLED" if after == 0 else "SHORT"
		l["line_status"] = "Killed" if after == 0 else l.get("line_status") or "Shipped"
		changes.append({"sku": l["sku"], "before": before, "after": after,
			"item_status": l["item_status"]})
	return changes


def dates_to_poll(polled_through, today: date) -> list[str]:
	"""YYYYMMDD strings from the day after ``polled_through`` up to yesterday,
	capped at MAX_CATCHUP_DAYS (credits post overnight → never today)."""
	yesterday = today - timedelta(days=1)
	start = (getdate(polled_through) + timedelta(days=1)) if polled_through else yesterday
	start = max(start, yesterday - timedelta(days=MAX_CATCHUP_DAYS - 1))
	out = []
	d = start
	while d <= yesterday:
		out.append(d.strftime("%Y%m%d"))
		d += timedelta(days=1)
	return out


def poll_credits() -> dict:
	s = frappe.get_cached_doc(SETTINGS)
	if not s.ordering_enabled:
		return {"skipped": "disabled"}
	from ffl_integrations.sports_south.api_client import SSOrdersClient

	client = SSOrdersClient.from_settings()
	applied, unmatched = [], []
	for day in dates_to_poll(s.credits_polled_through, getdate(nowdate())):
		try:
			credits = client.get_credits_by_date(day)
		except Exception as exc:  # noqa: BLE001 — alert, keep the cursor, retry tomorrow
			frappe.db.set_single_value(SETTINGS, "last_error", f"{day}: {exc}")
			notify_admins("Sports South credit poll failed", f"{day}: {exc}",
				dedup_key="ss_credits_poll")
			return {"failed": day, "applied": applied}
		day_ok = True
		for credit in credits:
			po = (credit.get("po") or "").strip()
			if not (po and frappe.db.exists("Distributor Order", po)):
				unmatched.append(credit)
				continue
			try:
				if _apply_to_do(po, credit):
					applied.append(credit["credit_no"])
			except Exception as exc:  # noqa: BLE001 — one bad DO must not block the day
				day_ok = False
				frappe.log_error(title=f"SS credit {credit['credit_no']} failed on {po}",
					message=frappe.get_traceback())
				notify_admins(f"Sports South credit {credit['credit_no']} not applied to {po}",
					str(exc), dedup_key=f"ss_credit_fail_{credit['credit_no']}")
		if not day_ok:
			# Cursor stays: the day replays tomorrow; applied credits are
			# idempotent (marked on the DO) so nothing is subtracted twice.
			return {"failed": day, "applied": applied}
		frappe.db.set_single_value(SETTINGS, {
			"credits_polled_through": getdate(f"{day[:4]}-{day[4:6]}-{day[6:]}"),
			"last_credits_poll_at": now_datetime()})
	if unmatched:
		notify_admins("Sports South credit memos with no matching order",
			"\n".join(f"credit {c['credit_no']} PO {c.get('po')!r} {c.get('reason')} "
				f"total {c.get('total')}" for c in unmatched),
			dedup_key="ss_credits_unmatched")
	return {"applied": applied, "unmatched": len(unmatched)}


def _credit_mark(credit_no: str) -> str:
	return f"ss_credit:{credit_no}"


def _apply_to_do(do_name: str, credit: dict) -> bool:
	"""Fold one credit onto its DO once (marked on ``notes_posted``). Returns
	False when it was already applied."""
	do = frappe.get_doc("Distributor Order", do_name)
	marks = {s for s in str(do.get("notes_posted") or "").split(",") if s}
	mark = _credit_mark(credit["credit_no"])
	if mark in marks:
		return False
	# lazy: a DO still in POLLABLE_STATES will have these lines re-read from
	# GetDetail by the next poll; the comment + alert carry the credit. Upgrade
	# path: feed credited qty into get_order_status so compute_do_updates sees it.
	rows = [{"sku": l.sku, "invoiced_qty": l.invoiced_qty, "item_status": l.item_status,
		"line_status": l.line_status} for l in do.lines]
	changes = apply_credit(rows, credit)
	by_sku = {r["sku"]: r for r in rows}
	for l in do.lines:
		r = by_sku.get(l.sku)
		if r:
			l.invoiced_qty, l.item_status, l.line_status = (
				r["invoiced_qty"], r["item_status"], r["line_status"])
	marks.add(mark)
	do.notes_posted = ",".join(sorted(marks))
	do.flags.ignore_validate_update_after_submit = True
	do.save(ignore_permissions=True)
	summary = "\n".join(
		f"  {c['sku']}: shipped {c['before']} → {c['after']} ({c['item_status']})"
		for c in changes)
	note = (f"Sports South credit memo {credit['credit_no']} ({credit.get('reason') or 'no reason'}) "
		f"on invoice {credit.get('invoice_no')}, total {credit.get('total')}:\n{summary}")
	do.add_comment("Comment", note.replace("\n", "<br>"))
	notify_admins(f"Sports South credited {do_name} — refund / re-source needed",
		note + "\n\nThe consumer's order needs a refund or a re-source for the credited units.",
		dedup_key=f"ss_credit_{credit['credit_no']}")
	return True
