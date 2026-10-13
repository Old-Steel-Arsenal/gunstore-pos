"""Tests: webhook enqueues the NICS-cleared email on a Delayed->Proceed transition."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestWebhookEnqueuesNicsCleared(FrappeTestCase):
	def _data(self):
		return {"dispositionId": "FB-DISP-1"}

	@patch("ffl_integrations.fastbound.webhook._sync_fb_direct_disposal_stock")
	@patch("ffl_integrations.fastbound.webhook._disposition_id_from_event", return_value="FB-DISP-1")
	@patch("ffl_integrations.fastbound.webhook.frappe.enqueue")
	@patch("ffl_integrations.fastbound.webhook.frappe.publish_realtime")
	@patch("ffl_integrations.fastbound.webhook.frappe.db.set_value")
	@patch("ffl_integrations.fastbound.webhook.frappe.db.get_value", return_value="Delayed")
	@patch("ffl_integrations.fastbound.webhook.frappe.get_all")
	@patch("ffl_integrations.fastbound.webhook.now_datetime")
	def test_enqueues_on_delayed_to_proceed(self, _now, mock_get_all, _gv, _sv, _pr, mock_enqueue, _did, _sync):
		from ffl_integrations.fastbound import webhook as fb
		# 1st get_all → FFL Disposition rows (none); 2nd → POS Invoice names;
		# 3rd → POS Invoice Item lines for the P5 serial-mismatch alarm ([] = no
		# firearm line → no alarm). rows=[] → the retail-gated backstop is skipped.
		mock_get_all.side_effect = [[], ["POS-1"], []]
		fb._handle_disposition_committed(self._data())
		paths = [c.args[0] for c in mock_enqueue.call_args_list]
		self.assertIn(
			"ffl_integrations.fastbound.customer_notify.notify_customer_nics_cleared", paths)

	@patch("ffl_integrations.fastbound.webhook._sync_fb_direct_disposal_stock")
	@patch("ffl_integrations.fastbound.webhook._disposition_id_from_event", return_value="FB-DISP-1")
	@patch("ffl_integrations.fastbound.webhook.frappe.enqueue")
	@patch("ffl_integrations.fastbound.webhook.frappe.publish_realtime")
	@patch("ffl_integrations.fastbound.webhook.frappe.db.set_value")
	@patch("ffl_integrations.fastbound.webhook.frappe.db.get_value", return_value="N/A")
	@patch("ffl_integrations.fastbound.webhook.frappe.get_all")
	@patch("ffl_integrations.fastbound.webhook.now_datetime")
	def test_no_enqueue_when_not_previously_delayed(self, _now, mock_get_all, _gv, _sv, _pr, mock_enqueue, _did, _sync):
		from ffl_integrations.fastbound import webhook as fb
		# +3rd get_all → POS Invoice Item lines for the P5 alarm ([] = no firearm line).
		mock_get_all.side_effect = [[], ["POS-1"], []]
		fb._handle_disposition_committed(self._data())
		paths = [c.args[0] for c in mock_enqueue.call_args_list]
		self.assertNotIn(
			"ffl_integrations.fastbound.customer_notify.notify_customer_nics_cleared", paths)
