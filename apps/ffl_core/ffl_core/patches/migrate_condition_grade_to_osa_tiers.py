"""Migrate condition_grade values from the old 6-tier set to the 5-tier set.

Old tiers: (blank) / NIB / Excellent / Very Good / Good / Fair / Poor
New tiers: New / Excellent / Very Good / Good / Fair

Mapping:
  NIB  → New   (New-In-Box becomes the canonical "New" grade)
  Poor → Fair  (lowest kept tier; "Poor" is retired)

Everything else (Excellent, Very Good, Good, Fair, blank) is already valid and
left untouched. The patch is idempotent: after it runs "NIB" and "Poor" no
longer appear, so re-running is a no-op.
"""
from __future__ import annotations

import frappe


def execute():
	# Item.condition_grade: NIB → New, Poor → Fair
	frappe.db.sql(
		"UPDATE `tabItem` SET condition_grade = 'New' WHERE condition_grade = 'NIB'"
	)
	frappe.db.sql(
		"UPDATE `tabItem` SET condition_grade = 'Fair' WHERE condition_grade = 'Poor'"
	)

	# Serial No.condition_grade: NIB → New, Poor → Fair
	frappe.db.sql(
		"UPDATE `tabSerial No` SET condition_grade = 'New' WHERE condition_grade = 'NIB'"
	)
	frappe.db.sql(
		"UPDATE `tabSerial No` SET condition_grade = 'Fair' WHERE condition_grade = 'Poor'"
	)

	frappe.logger(__name__).info(
		"migrate_condition_grade_to_osa_tiers: NIB→New and Poor→Fair applied "
		"on tabItem and tabSerial No."
	)
