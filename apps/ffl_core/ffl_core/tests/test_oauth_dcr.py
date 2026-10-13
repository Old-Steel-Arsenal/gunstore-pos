"""Dynamic registration accepts an http://localhost callback (Claude Code) and
still refuses other plain-http redirects."""
from __future__ import annotations

from types import SimpleNamespace

from frappe.tests.utils import FrappeTestCase

from ffl_core.api import oauth_dcr


def _uri(scheme, host):
	return SimpleNamespace(scheme=scheme, host=host)


class TestLocalhostRedirect(FrappeTestCase):
	def test_localhost_is_a_loopback_redirect(self):
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("http", "localhost")))
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("http", "LOCALHOST")))
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("http", "127.0.0.1")))
		self.assertTrue(oauth_dcr._is_secure_redirect_uri(_uri("https", "claude.ai")))

	def test_other_plain_http_is_still_refused(self):
		for host in ("evil.example.com", "localhost.evil.com", "10.0.0.5"):
			self.assertFalse(oauth_dcr._is_secure_redirect_uri(_uri("http", host)), host)

	def test_frappe_validation_uses_the_widened_check(self):
		import frappe.integrations.utils as oauth_utils
		self.assertIs(oauth_utils._is_secure_redirect_uri, oauth_dcr._is_secure_redirect_uri)
