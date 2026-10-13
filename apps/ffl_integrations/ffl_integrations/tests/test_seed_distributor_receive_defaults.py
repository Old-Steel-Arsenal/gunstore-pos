"""The receive-default patch fills only fields with no stored row."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.utils import single_raw
from ffl_integrations.patches import seed_distributor_receive_defaults as patch


class SeedReceiveDefaults(FrappeTestCase):
	def _drop_row(self, doctype, field):
		frappe.db.delete("Singles", {"doctype": doctype, "field": field})

	def test_fills_blank_rows_and_keeps_stated_values(self):
		self._drop_row("RSR Settings", "hold_escalate_days")
		self._drop_row("Sports South Settings", "freight_variance_alert")
		frappe.db.set_single_value("RSR Settings", "freight_variance_alert", 0)   # a deliberate 0
		patch.execute()
		self.assertEqual(int(single_raw("RSR Settings", "hold_escalate_days")), 3)
		self.assertEqual(float(single_raw("Sports South Settings", "freight_variance_alert")), 5.0)
		self.assertEqual(float(single_raw("RSR Settings", "freight_variance_alert")), 0.0)
