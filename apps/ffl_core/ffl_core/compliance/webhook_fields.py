"""Server-side protection for the webhook-owned 4473 fields on POS Invoice.

A past incident: the FastBound webhook stamps
form_4473_status / nics_status / fastbound_disposition_id / fastbound_4473_pdf_url
with ``frappe.db.set_value(update_modified=False)`` — so Frappe's modified-timestamp
conflict check never fires. A cashier's browser that loaded the draft BEFORE the
webhook still holds the pre-completion values; its next full-doc save (e.g. adding a
Zelle split) writes those stale values back and silently reverts the order to
Pending, blocking Complete Order at "Awaiting 4473".

We treat the four as ONE group. Once the DB shows the group completed
(``form_4473_status == "Completed"``), a Draft POS Invoice save may not alter any of
them — they are restored from the DB before the row is written. The webhook itself
writes via ``db.set_value`` (which never runs doc-event hooks) so it is unaffected,
and a legitimate server-side reset (re-opening a 4473) opts out by setting
``doc.flags.ignore_webhook_4473_guard = True`` before it saves.

Hooked on ``before_save`` of POS Invoice.
"""
from __future__ import annotations

import re

import frappe

# Written together by the webhook (ffl_integrations.fastbound.webhook); protected
# together here — a stale client can revert them only as a set, so we restore the set.
WEBHOOK_OWNED_4473_FIELDS = (
	"form_4473_status",
	"nics_status",
	"fastbound_disposition_id",
	"fastbound_4473_pdf_url",
)

# fastbound_4473_pdf_url holds FastBound's apiDownloadUrl,
# ".../api/Form4473s/Download/<form id>" (a trailing slash / query tolerated).
_FORM_4473_ID = re.compile(
	r"/Form4473s/Download/([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})/?(?:[?#].*)?$")


def form_4473_id(pdf_url: str | None) -> str | None:
	"""The FastBound 4473 id in a stored fastbound_4473_pdf_url, or None when the
	value is not a 4473 download link. One parser for the report's "View PDF"
	and the viewer that serves it, so the two can never disagree."""
	match = _FORM_4473_ID.search((pdf_url or "").strip())
	return match.group(1) if match else None


def protect_webhook_4473_fields(doc, method: str | None = None) -> None:
	"""Restore the webhook-owned 4473 group from the DB on a Draft POS Invoice
	save that would regress it. Surgical: only fires once the DB group is
	Completed, and only rewrites fields whose incoming value differs."""
	if getattr(doc, "doctype", "") != "POS Invoice":
		return
	# Only Draft saves race the webhook. A submitted invoice's group is frozen and
	# already governed by the submit gate (require_4473_and_nics_for_firearm_sale).
	if int(getattr(doc, "docstatus", 0) or 0) != 0:
		return
	# Explicit, sanctioned server-side reset (e.g. pos_4473 re-opening a 4473).
	if getattr(doc.flags, "ignore_webhook_4473_guard", False):
		return
	# A brand-new invoice has no persisted truth to protect.
	is_new = getattr(doc, "is_new", None)
	if callable(is_new) and is_new():
		return

	db = frappe.db.get_value(
		"POS Invoice", doc.name, WEBHOOK_OWNED_4473_FIELDS, as_dict=True)
	if not db:
		return
	# The group is authoritative only once the webhook stamped it Completed; before
	# that a Pending/empty draft saves normally (the common case — cheap early out).
	if (db.get("form_4473_status") or "") != "Completed":
		return

	# Force the group back to the DB truth. A fresh (post-webhook) client carries the
	# same values → no-op; a stale (pre-webhook) client is corrected.
	regressed = []
	for field in WEBHOOK_OWNED_4473_FIELDS:
		db_val = db.get(field)
		if (getattr(doc, field, None) or "") != (db_val or ""):
			regressed.append(field)
			setattr(doc, field, db_val)
	if regressed:
		_log_blocked_regression(doc.name, regressed, db)


def _log_blocked_regression(name: str, fields: list, db: dict) -> None:
	"""Best-effort audit trail of a blocked stale save; never blocks the save."""
	try:
		frappe.log_error(
			title=f"POS Invoice {name}: blocked stale 4473 field revert",
			message=(
				f"A Draft save tried to revert webhook-owned field(s) {fields}; "
				f"restored from DB (form_4473_status={db.get('form_4473_status')}, "
				f"nics_status={db.get('nics_status')}, "
				f"fastbound_disposition_id={db.get('fastbound_disposition_id')}). "
				f"Stale-browser overwrite."
			),
		)
	except Exception:  # noqa: BLE001 — logging must never block the save
		pass
