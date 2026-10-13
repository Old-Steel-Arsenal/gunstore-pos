"""setup/test_bootstrap.py::_fastbound_test_settings
injects "TEST-API-KEY-PLACEHOLDER" into FastBound Settings.api_key with no
in_test/developer_mode guard. A "account has a real account_num, api_key not
yet set" half-configured state (e.g. mid key-rotation) would get a fake key
silently written on top if this function were ever invoked outside a
test/dev context (this module is reachable via `bench execute
ffl_core.setup.test_bootstrap.run`, not gated to CI by anything in the code
itself).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class FastBoundTestSettingsGuard(FrappeTestCase):
	def test_does_not_touch_fastbound_settings_outside_test_or_developer_mode(self):
		from ffl_core.setup import test_bootstrap

		prev_in_test = frappe.flags.in_test
		prev_dev_mode = frappe.conf.get("developer_mode")
		try:
			frappe.flags.in_test = False
			frappe.conf.developer_mode = 0
			with patch("frappe.get_single") as get_single_mock:
				test_bootstrap._fastbound_test_settings()
			get_single_mock.assert_not_called()
		finally:
			frappe.flags.in_test = prev_in_test
			frappe.conf.developer_mode = prev_dev_mode

	def test_still_seeds_placeholders_when_in_test(self):
		"""Regression pin: the guard must not break the CI/local bootstrap
		path this function exists for."""
		from ffl_core.setup import test_bootstrap

		if not frappe.db.exists("DocType", "FastBound Settings"):
			self.skipTest("ffl_integrations not installed on this site")

		self.assertTrue(frappe.flags.in_test, "test runner must set in_test")
		test_bootstrap._fastbound_test_settings()
		s = frappe.get_single("FastBound Settings")
		self.assertTrue(s.account_num)
		self.assertTrue(s.get_password("api_key", raise_exception=False))
