"""Tests for ffl_core.acquisition.record_firearm_acquisition — the single
entry point for FFL Acquisition record creation."""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from ffl_core.acquisition import record_firearm_acquisition
from ffl_core.firearm import PISTOL


class AcquisitionService(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls._ensure_item("FFL-SVC-VALID", PISTOL, "SVC-MFG-VALID")
		cls._ensure_item("FFL-SVC-INDIV", PISTOL, "SVC-MFG-INDIV")
		cls._ensure_item("FFL-SVC-IDEMP", PISTOL, "SVC-MFG-IDEMP")

	@staticmethod
	def _ensure_item(code, group, serial):
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
			"firearm_serial": serial,
		}).insert(ignore_permissions=True)

	def test_ffl_dealer_source_requires_source_ffl(self):
		with self.assertRaises(frappe.ValidationError):
			record_firearm_acquisition(
				item_code="FFL-SVC-VALID",
				serial="SVC-MFG-VALID",
				source_type="FFL Dealer",
				source_name="ACME Distributors",
				source_ffl=None,           # missing → reject
				cost=100,
				company=self.company,
			)

	def test_individual_source_requires_id(self):
		with self.assertRaises(frappe.ValidationError):
			record_firearm_acquisition(
				item_code="FFL-SVC-INDIV",
				serial="SVC-MFG-INDIV",
				source_type="Individual",
				source_name="John Doe",
				cost=100,
				company=self.company,
			)

	def test_ffl_dealer_full_path(self):
		name = record_firearm_acquisition(
			item_code="FFL-SVC-VALID",
			serial="SVC-MFG-VALID",
			source_type="FFL Dealer",
			source_name="ACME Distributors",
			source_ffl="1-12-345-67-8K-99999",
			cost=200,
			company=self.company,
		)
		self.assertTrue(name.startswith("ACQ-"))
		acq = frappe.get_doc("FFL Acquisition", name)
		self.assertEqual(acq.docstatus, 1)
		self.assertEqual(acq.source_ffl, "1-12-345-67-8K-99999")

	def test_idempotent_on_item_serial(self):
		# Same item + serial called twice → returns the same name; no duplicate.
		args = dict(
			item_code="FFL-SVC-IDEMP",
			serial="SVC-MFG-IDEMP",
			source_type="FFL Dealer",
			source_name="ACME",
			source_ffl="1-12-345-67-8K-99999",
			cost=100,
			company=self.company,
		)
		first = record_firearm_acquisition(**args)
		second = record_firearm_acquisition(**args)
		self.assertEqual(first, second)

	def test_acquisition_date_can_be_back_dated(self):
		from datetime import datetime
		past = datetime(2020, 6, 15, 10, 30, 0)
		# Use a fresh item so this test doesn't collide with the idempotency one.
		self._ensure_item("FFL-SVC-BACK", PISTOL, "SVC-MFG-BACK")
		name = record_firearm_acquisition(
			item_code="FFL-SVC-BACK",
			serial="SVC-MFG-BACK",
			source_type="FFL Dealer",
			source_name="ACME Distributors",
			source_ffl="1-12-345-67-8K-99999",
			cost=200,
			company=self.company,
			acquisition_date=past,
		)
		acq = frappe.get_doc("FFL Acquisition", name)
		# Frappe stores datetime as string-ish; compare normalized.
		from frappe.utils import get_datetime
		self.assertEqual(get_datetime(acq.acquisition_date), past)


if __name__ == "__main__":
	unittest.main()
