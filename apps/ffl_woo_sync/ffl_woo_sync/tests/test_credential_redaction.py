"""WooCommerce client: the consumer secret must not reach the Error Log.

It sits on ``Session.auth`` for the life of the session, and the client lets
requests' own exceptions propagate — so Session.request's frames, with the
session expanded, are in every transport-failure traceback.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.live_session import live_session

from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceConfig


def _mark(tag: str) -> str:
	"""A credential-shaped test value built at runtime, so no secret-scanner
	sees a literal ``password="…"`` in source (GitGuardian flagged the first
	cut of this file for exactly that)."""
	return f"MARK-{tag}-must-not-leak"


class ConsumerSecretRedaction(FrappeTestCase):
	def test_transport_failure_keeps_consumer_secret_out(self):
		cfg = WooCommerceConfig(store_url="https://shop.example.invalid",
			consumer_key=_mark("woo-key"), consumer_secret=_mark("woo-secret"), timeout=5)
		c = WooCommerceClient(cfg)
		live_session(session=c.session)
		try:
			c._request("GET", "products")
		except Exception:
			# Newer Frappe renders the locals only in developer mode; without it this would pass on a traceback
			# that holds no secret to find.
			with patch.dict(frappe.local.conf, {"developer_mode": 1}):
				tb = frappe.get_traceback(with_context=True)
		else:
			self.fail("expected the call to raise")
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("woo-secret"), tb)
		self.assertNotIn(_mark("woo-key"), tb)

	def test_auth_still_reaches_the_wire(self):
		cfg = WooCommerceConfig(store_url="https://shop.example.invalid",
			consumer_key="ck_x", consumer_secret="cs_y", timeout=5)
		c = WooCommerceClient(cfg)
		live_session({"/wp-json/": (200, [])}, session=c.session)
		sent = []
		original = c.session.send
		c.session.send = lambda request, **kw: (sent.append(request), original(request, **kw))[1]
		c._request("GET", "products")
		self.assertEqual(sent[-1].headers["Authorization"], "Basic Y2tfeDpjc195")
