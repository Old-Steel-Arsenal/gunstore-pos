"""Tests for the backfill_serial_purchase_rate_from_acquisition patch.

Recovers Serial No.purchase_rate for guns taken in via firearm_intake before
the _stamp_serial fix, sourcing the cost from the authoritative FFL Acquisition
(cost > 0 only — consignment / opening-migration guns with no recorded cost
stay at 0 so they remain flagged as missing on the Sales Report).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import trade_in
from ffl_core.patches import backfill_serial_purchase_rate_from_acquisition as bf


class BackfillSerialPurchaseRateFromAcquisition(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)
		# A pay-now buy pays a seller on file (api/trade_in._buy_now).
		if not frappe.db.exists("Customer", "_Test Backfill Seller"):
			frappe.get_doc({"doctype": "Customer", "customer_name": "_Test Backfill Seller",
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)

	def setUp(self) -> None:
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; these tests are not about FastBound.
		# A site where another module saved credentials (they persist) would otherwise reach the live client.
		from unittest.mock import patch

		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in backfill tests"))
		p.start()
		self.addCleanup(p.stop)

	def _payload(self, suffix: str, **over) -> dict:
		base = {
			"serial_no": f"BF-{suffix}", "manufacturer": "Glock", "model_name": "19",
			"caliber": "9mm", "firearm_type": "Pistol", "condition_grade": "Good",
			"id_type": "Driver License", "id_number": f"DL-{suffix}",
			"source_name": "_Test Backfill Seller",
			"source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 655,
			"payout_method": "Cash",  # a pure buy says how the seller was paid (ignored with apply_credit)
		}
		base.update(over)
		return base

	def test_backfill_recovers_purchase_rate_from_acquisition(self):
		suffix = self._testMethodName
		trade_in.create_trade_in_intake(self._payload(suffix))
		serial = f"BF-{suffix}"
		# Simulate a gun taken in before the intake fix: cost is on the
		# acquisition, but purchase_rate was never stamped onto the Serial No.
		frappe.db.set_value("Serial No", serial, "purchase_rate", 0, update_modified=False)
		bf.execute()
		self.assertEqual(
			float(frappe.db.get_value("Serial No", serial, "purchase_rate") or 0), 655.0)

	def test_backfill_leaves_serial_without_costed_acquisition_untouched(self):
		# A gun with no cost > 0 acquisition (consignment / opening migration)
		# must stay at 0 so it remains flagged as missing, not given a fake cost.
		if not frappe.db.exists("Item Group", "Used Firearm"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Used Firearm",
				"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
		code = "BF-NOACQ-ITEM"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Used Firearm", "is_firearm": 1, "is_stock_item": 1,
				"has_serial_no": 1}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Serial No", "serial_no": "BF-NOACQ-1",
			"item_code": code}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", "BF-NOACQ-1", "purchase_rate", 0, update_modified=False)
		bf.execute()
		self.assertEqual(
			float(frappe.db.get_value("Serial No", "BF-NOACQ-1", "purchase_rate") or 0), 0.0)
