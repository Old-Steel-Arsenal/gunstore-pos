"""alert_stale_trade_in_drafts — the §478.125(e) watchdog for parked trade-in
drafts (deferred booking's boundary condition).

A draft acquisition whose possession date (acquisition_date) has slipped past
its calendar day must surface in the admin digest — the bound-book entry is
due by the close of the NEXT business day, independent of the buyer's 4473.
A same-day draft must NOT be flagged (checkout is presumably in flight).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from ffl_integrations.fastbound import tasks


class StaleTradeInDraftAlert(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)
		if not frappe.db.exists("Customer", "_Test Trade-in Seller"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "_Test Trade-in Seller",
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)

	def _draft(self, serial: str) -> str:
		from ffl_core.api import trade_in
		res = trade_in.create_trade_in_intake({
			"serial_no": serial,
			"manufacturer": "Glock", "model_name": "19", "caliber": "9mm",
			"firearm_type": "Pistol", "condition_grade": "Good",
			"id_type": "Driver License", "id_number": f"DL-{serial}",
			"source_name": "_Test Trade-in Seller",
			"source_first_name": "Testy", "source_last_name": "Seller",
			"source_address": "123 Main St, Springfield TX 75024",
			"acquisition_cost": 100, "apply_credit": 1,
		})
		return res["acquisition"]

	def test_yesterday_draft_flagged_same_day_draft_not(self):
		suffix = self._testMethodName
		stale = self._draft(f"STALE-{suffix}")
		fresh = self._draft(f"FRESH-{suffix}")
		frappe.db.set_value("FFL Acquisition", stale, "acquisition_date",
			add_days(now_datetime(), -1), update_modified=False)

		with patch.object(tasks, "notify_admins") as spy:
			tasks.alert_stale_trade_in_drafts()

		self.assertTrue(spy.called, "a day-old unbooked draft must alert")
		body = spy.call_args[0][1]
		self.assertIn(stale, body)
		self.assertNotIn(fresh, body,
			"a same-day draft is presumably mid-checkout — no alert yet")


if __name__ == "__main__":
	unittest.main()
