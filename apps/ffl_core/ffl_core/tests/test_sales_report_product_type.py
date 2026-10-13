"""`product_type` must not fail OPEN.

A caller passed ``product_type="Accessory"`` and got every firearm row back: the report
only branches on ``"Firearms"`` / ``"Non-Firearms"``, so any other value matches
neither branch and no filtering happens. For a CPA that is the dangerous direction —
"the filter didn't run" is indistinguishable from "there are no accessory rows", and
the number gets used.

Fail-closed: an unrecognised value is refused, and the message names the accepted
ones so the next caller gets it right.
"""
from __future__ import annotations

import unittest

from ffl_core.ffl_core.report.sales_report import sales_report

_dict = sales_report.frappe._dict


def _f(**over):
	base = {"from_date": "2026-07-01", "to_date": "2026-07-31"}
	base.update(over)
	return _dict(**base)


class ProductTypeFailsClosedTest(unittest.TestCase):
	def test_unrecognised_value_is_refused(self):
		with self.assertRaises(sales_report.frappe.ValidationError):
			sales_report._validate(_f(product_type="Accessory"))

	def test_the_message_names_the_accepted_values(self):
		try:
			sales_report._validate(_f(product_type="Ammo"))
		except sales_report.frappe.ValidationError as exc:
			self.assertIn("Firearms", str(exc))
			self.assertIn("Non-Firearms", str(exc))
		else:
			self.fail("an unrecognised product_type must be refused")

	def test_accepted_values_pass(self):
		for value in ("Firearms", "Non-Firearms"):
			sales_report._validate(_f(product_type=value))

	def test_absent_or_blank_means_no_filter(self):
		sales_report._validate(_f())
		sales_report._validate(_f(product_type=""))
		sales_report._validate(_f(product_type=None))

	def test_surrounding_whitespace_is_tolerated_AND_written_back(self):
		# The write-back is half the fix: the row builders compare the filter value
		# verbatim, so a padded string that merely PASSED validation would match no
		# branch and silently return everything again. Assert the normalised value
		# actually lands in filters (without this the write-back could be
		# deleted and the test would stay green).
		f = _f(product_type="  Firearms  ")
		sales_report._validate(f)
		self.assertEqual(f["product_type"], "Firearms")

	def test_blank_is_normalised_to_empty_string(self):
		f = _f(product_type="   ")
		sales_report._validate(f)
		self.assertEqual(f["product_type"], "")

	def test_date_validation_still_fires(self):
		with self.assertRaises(sales_report.frappe.ValidationError):
			sales_report._validate(_dict(from_date="", to_date=""))


if __name__ == "__main__":
	unittest.main()
