"""Ops-alert wiring: RSR / FastBound failures call notify_admins.

(The ATF monthly-refresh task and its alert were removed with the Data Service
migration — the national listing now lives on the shared service, whose
own sync staleness is watched by /v1/sync/status.)
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestFastBoundStuckAlert(FrappeTestCase):
	@patch("ffl_integrations.fastbound.tasks.notify_admins")
	@patch("ffl_integrations.fastbound.tasks.frappe.get_all")
	def test_alerts_on_stuck_records(self, mock_get_all, mock_notify):
		# 1st call (Acquisition) → one stuck row; 2nd (Disposition) → none.
		mock_get_all.side_effect = [
			[frappe._dict(name="ACQ-2026-00001", fastbound_sync_error="push 500")],
			[],
		]
		from ffl_integrations.fastbound.tasks import alert_stuck_fastbound
		alert_stuck_fastbound()
		mock_notify.assert_called_once()
		self.assertEqual(mock_notify.call_args.kwargs["dedup_key"], "fastbound_stuck")

	@patch("ffl_integrations.fastbound.tasks.notify_admins")
	@patch("ffl_integrations.fastbound.tasks.frappe.get_all")
	def test_no_alert_when_clean(self, mock_get_all, mock_notify):
		mock_get_all.side_effect = [[], []]
		from ffl_integrations.fastbound.tasks import alert_stuck_fastbound
		alert_stuck_fastbound()
		mock_notify.assert_not_called()
