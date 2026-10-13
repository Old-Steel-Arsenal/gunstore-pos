"""P4 — the FastBound webhook runs as Guest
(allow_guest=True). Taking a directly-disposed firearm out of ERPNext stock
submits a Stock Entry whose auto-created Serial and Batch Bundle runs its OWN
permission check (it does NOT inherit the Stock Entry's ignore_permissions), so
a Guest hits PermissionError and the whole direct-disposal stock-sync path dies.

The HMAC-verified path must elevate to a system user for this trusted
server-to-server stock move — WITHOUT weakening issue_serial itself (it is the
sole stock-out exit, shared by the consignment G guard and the reconcile).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


_DATA = {"items": [{"id": "FB-IT-1", "serial": "FBX-SYNC-1"}]}
_TARGET = {"serial": "FBX-SYNC-1", "item_code": "GUN-X", "warehouse": "WH-1", "fb_id": "FB-IT-1"}


class TestFbDirectDisposalGuestElevation(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._prev_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._prev_user)
		super().tearDown()

	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	def test_issue_serial_runs_elevated_under_guest(self, _targets, _onpos):
		"""Under a Guest session, issue_serial must be invoked with an elevated
		(system) user so the auto-created Serial and Batch Bundle passes its own
		permission check — and the Guest context is restored afterward."""
		from ffl_integrations.fastbound import webhook as fb
		frappe.set_user("Guest")
		seen = {}

		def _capture_user(*a, **k):
			seen["user"] = frappe.session.user
			return "STE-CAPTURE"

		with patch("ffl_core.firearm.issue_serial", side_effect=_capture_user) as mock_issue:
			fb._sync_fb_direct_disposal_stock("FB-DISP-9", _DATA)

		mock_issue.assert_called_once()
		self.assertNotEqual(seen.get("user"), "Guest",
			"issue_serial ran as Guest — the Serial and Batch Bundle insert will PermissionError")
		self.assertEqual(seen.get("user"), "Administrator")
		self.assertEqual(frappe.session.user, "Guest", "Guest context must be restored")

	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	def test_user_restored_even_if_issue_serial_raises(self, _targets, _onpos):
		"""The elevation must be unwound even when the stock move fails (the error
		is swallowed + logged per-target); the session must not leak Administrator."""
		from ffl_integrations.fastbound import webhook as fb
		frappe.set_user("Guest")
		with patch("ffl_core.firearm.issue_serial",
				side_effect=frappe.PermissionError("boom")), \
		     patch.object(fb, "_log_error"):
			fb._sync_fb_direct_disposal_stock("FB-DISP-9", _DATA)  # must not raise
		self.assertEqual(frappe.session.user, "Guest",
			"session.user leaked — elevation was not unwound on error")
