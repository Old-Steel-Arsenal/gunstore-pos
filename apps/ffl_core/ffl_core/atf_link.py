"""Customer/Supplier ↔ ATF FFL Record linkage.

The ATF directory record (name == FFL number) is the source of truth for a
dealer party's license facts. `atf_ffl_record` is the formal Link; on every
party save the license fields are synced FROM the record, so a typo in the
free-text ffl_number can no longer desync the Search-ATF fill, the shipping
gate and the disposition destination. Parties created before the link existed
heal automatically: a bare ffl_number that matches a directory record
backfills the link on the next save.
"""
from __future__ import annotations

import re

import frappe
from frappe import _


def sync_party_atf_link(doc, method: str | None = None) -> None:
	"""Customer + Supplier validate hook."""
	if not int(doc.get("is_ffl_dealer") or 0):
		return
	record = (doc.get("atf_ffl_record") or "").strip()
	if not record:
		# Old data heals: bare FFL number that exists in the directory →
		# adopt it as the formal link (then sync from it below).
		number = (doc.get("ffl_number") or "").strip()
		if not number or not frappe.db.exists("ATF FFL Record", number):
			return
		record = number
		doc.atf_ffl_record = record

	rec = frappe.db.get_value(
		"ATF FFL Record", record,
		["ffl_number", "license_type", "licensee_name", "licensee_dba",
			"street", "street2", "city", "state", "zip", "expiry_date"],
		as_dict=True,
	)
	if not rec:
		frappe.throw(_("ATF FFL Record {0} not found.").format(record))

	# The directory is authoritative for everything it knows.
	doc.ffl_number = rec.ffl_number
	if rec.licensee_name:
		doc.licensee_name = rec.licensee_name
	address = ", ".join(p for p in (
		rec.street, rec.street2, rec.city, " ".join(p for p in (rec.state, rec.zip) if p),
	) if p)
	if address:
		doc.licensee_address = address
	if rec.expiry_date:
		doc.ffl_expiry_date = rec.expiry_date
	code = _license_code(rec.license_type)
	if code:
		doc.ffl_license_type = code


def _license_code(license_type: str | None) -> str | None:
	"""'01 - Dealer in Firearms' → '01' (the party field is a 2-digit Select)."""
	m = re.match(r"\s*(\d{2})", license_type or "")
	return m.group(1) if m else None
