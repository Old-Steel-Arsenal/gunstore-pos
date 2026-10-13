"""Tests for the migrate_condition_grade_to_osa_tiers patch.

Verifies:
  - NIB → New on both tabItem and tabSerial No
  - Poor → Fair on both tabItem and tabSerial No
  - Valid grades (Excellent, Very Good, Good, Fair) are left untouched
  - Blank values are left untouched
  - Patch is idempotent (re-running is a no-op)
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches.migrate_condition_grade_to_osa_tiers import execute


class MigrateConditionGrade(FrappeTestCase):
	"""Unit-test the condition_grade migration patch directly."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _make_item(self, item_code: str, condition_grade: str | None = None) -> str:
		frappe.db.sql("DELETE FROM `tabItem` WHERE name=%s", (item_code,))
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": "Rifle",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
		}).insert(ignore_permissions=True)
		if condition_grade is not None:
			# Bypass validation — the field may not accept the old value post-migrate
			frappe.db.sql(
				"UPDATE `tabItem` SET condition_grade=%s WHERE name=%s",
				(condition_grade, item_code),
			)
		return item_code

	def _make_serial(self, serial_no: str, item_code: str, condition_grade: str | None = None) -> str:
		frappe.db.sql("DELETE FROM `tabSerial No` WHERE name=%s", (serial_no,))
		frappe.get_doc({
			"doctype": "Serial No",
			"serial_no": serial_no,
			"item_code": item_code,
		}).insert(ignore_permissions=True)
		if condition_grade is not None:
			frappe.db.sql(
				"UPDATE `tabSerial No` SET condition_grade=%s WHERE name=%s",
				(condition_grade, serial_no),
			)
		return serial_no

	def setUp(self):
		# Clean up test rows from any previous run
		for code in (
			"CG-PATCH-NIB", "CG-PATCH-POOR", "CG-PATCH-GOOD",
			"CG-PATCH-BLANK", "CG-PATCH-SER-BLANK", "CG-PATCH-IDEM",
		):
			frappe.db.sql("DELETE FROM `tabSerial No` WHERE item_code=%s", (code,))
			frappe.db.sql("DELETE FROM `tabItem` WHERE name=%s", (code,))
		frappe.db.commit()

	# ------------------------------------------------------------------
	# tabItem tests
	# ------------------------------------------------------------------

	def test_item_nib_becomes_new(self):
		self._make_item("CG-PATCH-NIB", condition_grade="NIB")
		execute()
		self.assertEqual(
			frappe.db.get_value("Item", "CG-PATCH-NIB", "condition_grade"),
			"New",
		)

	def test_item_poor_becomes_fair(self):
		self._make_item("CG-PATCH-POOR", condition_grade="Poor")
		execute()
		self.assertEqual(
			frappe.db.get_value("Item", "CG-PATCH-POOR", "condition_grade"),
			"Fair",
		)

	def test_item_valid_grade_unchanged(self):
		self._make_item("CG-PATCH-GOOD", condition_grade="Good")
		execute()
		self.assertEqual(
			frappe.db.get_value("Item", "CG-PATCH-GOOD", "condition_grade"),
			"Good",
		)

	def test_item_blank_grade_unchanged(self):
		self._make_item("CG-PATCH-BLANK", condition_grade="")
		execute()
		val = frappe.db.get_value("Item", "CG-PATCH-BLANK", "condition_grade")
		self.assertIn(val, (None, ""))

	# ------------------------------------------------------------------
	# tabSerial No tests
	# ------------------------------------------------------------------

	def test_serial_nib_becomes_new(self):
		self._make_item("CG-PATCH-NIB")
		self._make_serial("SN-CG-NIB", "CG-PATCH-NIB", condition_grade="NIB")
		execute()
		self.assertEqual(
			frappe.db.get_value("Serial No", "SN-CG-NIB", "condition_grade"),
			"New",
		)

	def test_serial_poor_becomes_fair(self):
		self._make_item("CG-PATCH-POOR")
		self._make_serial("SN-CG-POOR", "CG-PATCH-POOR", condition_grade="Poor")
		execute()
		self.assertEqual(
			frappe.db.get_value("Serial No", "SN-CG-POOR", "condition_grade"),
			"Fair",
		)

	def test_serial_valid_grade_unchanged(self):
		self._make_item("CG-PATCH-GOOD")
		self._make_serial("SN-CG-GOOD", "CG-PATCH-GOOD", condition_grade="Very Good")
		execute()
		self.assertEqual(
			frappe.db.get_value("Serial No", "SN-CG-GOOD", "condition_grade"),
			"Very Good",
		)

	def test_serial_blank_grade_unchanged(self):
		self._make_item("CG-PATCH-SER-BLANK")
		self._make_serial("SN-CG-BLANK", "CG-PATCH-SER-BLANK", condition_grade="")
		execute()
		val = frappe.db.get_value("Serial No", "SN-CG-BLANK", "condition_grade")
		self.assertIn(val, (None, ""))

	# ------------------------------------------------------------------
	# Idempotency
	# ------------------------------------------------------------------

	def test_patch_is_idempotent(self):
		"""Running execute() twice must produce the same result as running it once."""
		self._make_item("CG-PATCH-IDEM", condition_grade="NIB")
		self._make_serial("SN-CG-IDEM", "CG-PATCH-IDEM", condition_grade="Poor")

		execute()  # first run
		execute()  # second run — must be a no-op

		self.assertEqual(
			frappe.db.get_value("Item", "CG-PATCH-IDEM", "condition_grade"),
			"New",
		)
		self.assertEqual(
			frappe.db.get_value("Serial No", "SN-CG-IDEM", "condition_grade"),
			"Fair",
		)
