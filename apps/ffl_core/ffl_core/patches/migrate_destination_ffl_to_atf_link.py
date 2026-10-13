"""Migrate FFL Disposition.destination_ffl from free-text Data to Link → ATF FFL Record.

FFL Disposition's `destination_ffl` field was Data; this branch flips it to Link
pointing at ATF FFL Record. Existing rows whose destination_ffl was a raw FFL
number (or a free-text label) would otherwise fail Link validation on any
subsequent save / amend / cancel.

For each distinct legacy value:
  - If it parses as a plausible FFL number AND no ATF FFL Record exists for it,
    create a stub (loaded_from='legacy-migration', no licensee data) — the
    operator can fill it in later via EZ Check or the desk form.
  - Leave the disposition's destination_ffl value as-is; the Link now resolves
    against the stub.
  - If the legacy value doesn't look like an FFL number (e.g. a free-text label
    like 'Joe Gun Shop, TX'), clear the field to NULL — better to surface "no
    destination set" than to fabricate an FFL Record with a non-FFL name.
"""
from __future__ import annotations

import re

import frappe


# Matches the canonical ATF FFL format `1-99-999-99-9X-99999`. Hyphens are
# optional in inputs; we normalise to lowercase-letter-allowed for the
# license-issue-letter slot.
_FFL_RE = re.compile(r"^\d{1,2}-?\d{2}-?\d{3}-?\d{2}-?\d[A-Za-z]-?\d{5}$")


def execute():
	rows = frappe.get_all(
		"FFL Disposition",
		filters={"destination_ffl": ("is", "set")},
		fields=["name", "destination_ffl"],
	)
	migrated = 0
	cleared = 0
	for row in rows:
		value = (row.destination_ffl or "").strip()
		if not value:
			continue
		# Already a Link target? Then nothing to do.
		if frappe.db.exists("ATF FFL Record", value):
			continue
		if _FFL_RE.match(value):
			# Looks like an FFL number — create a stub ATF FFL Record so the
			# Link validates. Operator can fill licensee data later via EZ Check.
			try:
				doc = frappe.get_doc({
					"doctype": "ATF FFL Record",
					"ffl_number": value,
					"loaded_from": "legacy-migration",
				})
				doc.flags.ignore_permissions = True
				doc.insert()
				migrated += 1
			except Exception as exc:
				frappe.log_error(
					f"migrate_destination_ffl_to_atf_link: failed to create stub "
					f"ATF FFL Record for {value!r}: {exc}",
				)
		else:
			# Non-FFL-shaped free text — clear it; fabricating a stub with a
			# nonsense name would be worse than a NULL destination.
			frappe.db.set_value(
				"FFL Disposition", row.name,
				"destination_ffl", None, update_modified=False,
			)
			cleared += 1
	print(
		f"migrate_destination_ffl_to_atf_link: created {migrated} stub ATF FFL "
		f"Record(s); cleared {cleared} non-FFL-shaped value(s) to NULL."
	)
