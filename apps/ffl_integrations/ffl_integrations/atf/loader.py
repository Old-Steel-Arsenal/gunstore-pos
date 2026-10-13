"""ATF FFL CSV loader.

Parses the public ATF FFL listing (https://www.atf.gov/firearms/listing-federal-firearms-licensees)
into the local ATF FFL Record cache. Idempotent: running twice with the
same CSV updates rather than duplicates.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from os.path import basename
from typing import Any

import frappe
from frappe.utils import now_datetime


# ATF CSV column → our doctype field. LICENSE_NAME is resolved separately
# (see _name_column) because ATF has shipped that header blank.
COLUMN_MAP = {
	"LIC_TYPE": "license_type",
	"LICENSE_NAME": "licensee_name",
	"BUSINESS_NAME": "licensee_dba",
	"PREMISE_STREET": "street",
	"PREMISE_CITY": "city",
	"PREMISE_STATE": "state",
	"PREMISE_ZIP_CODE": "zip",
	"VOICE_PHONE": "phone",
}

# Position of the licensee-name column in the national file, used when the
# header itself is unusable.
NAME_COLUMN_INDEX = 6


def load_ffl_csv(path: str) -> dict[str, int]:
	"""Idempotent upsert. Returns {created, updated, total}."""
	loaded_from = basename(path)
	loaded_at = now_datetime()

	created = 0
	updated = 0
	total = 0
	# utf-8-sig: the national file carries a BOM, which under plain utf-8 fuses
	# onto the LIC_REGN header and costs every licence number its first segment.
	with open(path, newline="", encoding="utf-8-sig") as fh:
		reader = csv.DictReader(fh)
		name_col = _name_column(reader.fieldnames)
		for row in reader:
			total += 1
			ffl_number = _format_ffl(row)
			values = {COLUMN_MAP[csv_col]: (row.get(csv_col) or "").strip()
				for csv_col in COLUMN_MAP}
			values["licensee_name"] = (row.get(name_col) or "").strip()
			# A blank cell means "this file doesn't carry the value", not "clear
			# it" — an EZ Check lookup may have filled it in with better data.
			values = {k: v for k, v in values.items() if v != ""}
			expiry = _parse_yyyymm(row.get("LIC_XPRDTE"))
			if expiry:
				values["expiry_date"] = expiry
			values["ffl_number"] = ffl_number
			values["loaded_from"] = loaded_from
			values["loaded_at"] = loaded_at

			if frappe.db.exists("ATF FFL Record", ffl_number):
				frappe.db.set_value("ATF FFL Record", ffl_number, values, update_modified=False)
				updated += 1
			else:
				doc = frappe.get_doc({"doctype": "ATF FFL Record", **values})
				doc.flags.ignore_permissions = True
				doc.insert()
				created += 1

	frappe.db.commit()
	return {"created": created, "updated": updated, "total": total}


def _name_column(fieldnames: list[str] | None) -> str:
	"""Which column carries the licensee name.

	The 0726 national file shipped that header as a bare space (0326–0526 all
	said LICENSE_NAME). 32k of the 77k licensees have no BUSINESS_NAME, so a
	dropped column leaves them with no name at all — fall back to position."""
	if not fieldnames:
		return "LICENSE_NAME"
	if "LICENSE_NAME" in fieldnames:
		return "LICENSE_NAME"
	if len(fieldnames) > NAME_COLUMN_INDEX:
		return fieldnames[NAME_COLUMN_INDEX]
	return "LICENSE_NAME"


def _format_ffl(row: dict[str, str]) -> str:
	"""Compose the canonical 15-char FFL string '1-XX-XXX-XX-XX-XXXXX' from
	the raw ATF columns."""
	regn = (row.get("LIC_REGN") or "").strip().zfill(1)
	dist = (row.get("LIC_DIST") or "").strip().zfill(2)
	cnty = (row.get("LIC_CNTY") or "").strip().zfill(3)
	ltype = (row.get("LIC_TYPE") or "").strip().zfill(2)
	seqn = (row.get("LIC_SEQN") or "").strip().zfill(5)
	# Segment 5 is the 2-char expiration code, which the current CSV publishes
	# in LIC_XPRDTE — and which EZ Check hands back as part of the number. Keep
	# the "XX" placeholder only for legacy files that put a YYYYMM date there:
	# inventing a placeholder while the real code is on hand forks every dealer
	# into two records that never reconcile.
	xprdte = (row.get("LIC_XPRDTE") or "").strip().upper()
	expcode = xprdte if len(xprdte) == 2 else "XX"
	return f"{regn}-{dist}-{cnty}-{ltype}-{expcode}-{seqn}"


def _parse_yyyymm(s: str | None) -> date | None:
	"""Legacy ATF expiry is YYYYMM (e.g. '202812') → end-of-month date. The
	current CSV instead carries the 2-char licence-number code here, which is
	no date at all — return None so the caller leaves expiry_date alone."""
	if not s:
		return None
	s = s.strip()
	if len(s) != 6 or not s.isdigit():
		return None
	year, month = int(s[:4]), int(s[4:6])
	# Last day of month (no need to be exact; use day=28 as a safe lower bound).
	return date(year, month, 28)
