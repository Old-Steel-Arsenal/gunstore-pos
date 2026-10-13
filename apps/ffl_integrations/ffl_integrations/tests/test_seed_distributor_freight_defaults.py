"""The freight-default patch fills only fields with no stored row."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.utils import single_raw
from ffl_integrations.patches import seed_distributor_freight_defaults as patch


class SeedFreightDefaults(FrappeTestCase):
	def _drop_row(self, doctype, field):
		frappe.db.delete("Singles", {"doctype": doctype, "field": field})

	def test_fills_blank_rows_and_keeps_stated_values(self):
		self._drop_row("RSR Settings", "rate_ground")
		self._drop_row("Sports South Settings", "store_other_rate")
		frappe.db.set_single_value("RSR Settings", "rate_2day", 0)   # a deliberate 0
		patch.execute()
		self.assertEqual(float(single_raw("RSR Settings", "rate_ground")), 10.0)
		self.assertEqual(float(single_raw("Sports South Settings", "store_other_rate")), 5.0)
		self.assertEqual(float(single_raw("RSR Settings", "rate_2day")), 0.0)
