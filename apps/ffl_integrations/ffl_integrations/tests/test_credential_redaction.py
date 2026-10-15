"""Every integration client, driven through a real requests stack into
``frappe.get_traceback(with_context=True)``: no credential in the Error Log.

Each client gets the failure shape that reaches its credential-carrying frame:
a transport error where the client lets it propagate (requests' own frames),
an HTTP error where the client wraps transport errors ``from exc`` (frappe does
not follow the chain, so the HTTP path is the one whose frames still print).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.live_session import live_session

from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfig
from ffl_integrations.gunbroker.client import GunBrokerClient, GunBrokerConfig
from ffl_integrations.lipseys.api_client import LipseysClient, LipseysConfig
from ffl_integrations.rsr.api_client import RSRClient, RSRConfig
from ffl_integrations.shipstation.client import ShipStationClient, ShipStationConfig
from ffl_integrations.sports_south.api_client import SSConfig, SSOrdersClient


def _mark(tag: str) -> str:
	"""A credential-shaped test value built at runtime, so no secret-scanner
	sees a literal ``password="…"`` in source (GitGuardian flagged the first
	cut of this file for exactly that)."""
	return f"MARK-{tag}-must-not-leak"


def _traceback_of(fn) -> str:
	try:
		fn()
	except Exception:
		return frappe.get_traceback(with_context=True)
	raise AssertionError("expected the call to raise")


class GunBroker(FrappeTestCase):
	def _config(self):
		return GunBrokerConfig(username="seller", password=_mark("gb-password"),
			dev_key=_mark("gb-devkey"), base_url="https://api.example.invalid/",
			sandbox=True, timeout=5)

	def test_login_failure_keeps_password_and_dev_key_out(self):
		"""401 on login raises inside _request, whose `json_body` local IS the
		password payload; the dev key rides in every header set."""
		session = live_session({"/Users/AccessToken": (401, {"message": "bad creds"})})
		c = GunBrokerClient(self._config(), session=session, retry=False)
		tb = _traceback_of(c.login)
		self.assertIn("in _request", tb)
		self.assertNotIn(_mark("gb-password"), tb)
		self.assertNotIn(_mark("gb-devkey"), tb)

	def test_unwrapped_failure_inside_requests_keeps_dev_key_out(self):
		session = live_session(error=RuntimeError("adapter bug"))
		c = GunBrokerClient(self._config(), session=session, retry=False)
		tb = _traceback_of(lambda: c._request("GET", "/x", authenticated=False))
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("gb-devkey"), tb)


class FastBound(FrappeTestCase):
	def test_transport_failure_keeps_api_key_out(self):
		"""FastBound lets requests' ConnectionError propagate: Session.request's
		`auth` local is in the Error Log verbatim."""
		cfg = FastBoundConfig(account_num="1", api_key=_mark("fb-key"),
			base_url="https://cloud.example.invalid", api_version="2024-01-01",
			audit_user="tester", timeout=5)
		c = FastBoundClient(cfg, session=live_session())
		tb = _traceback_of(lambda: c._request("GET", "Items"))
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("fb-key"), tb)


class ShipStation(FrappeTestCase):
	def test_session_header_keeps_api_key_out(self):
		"""The key lives on Session.headers, which the formatter expands on
		every requests frame; an unwrapped failure inside requests shows it."""
		cfg = ShipStationConfig(api_key=_mark("ss-key"), base_url="https://api.example.invalid",
			store_id="1", ship_from_warehouse_id="1", default_parcel_weight_oz=16.0,
			timeout=5, sandbox_mode=True)
		c = ShipStationClient(cfg)
		live_session(error=RuntimeError("adapter bug"), session=c.session)
		tb = _traceback_of(lambda: c._request("GET", "/v2/carriers"))
		self.assertIn("requests/sessions.py", tb)
		self.assertNotIn(_mark("ss-key"), tb)


class SportsSouth(FrappeTestCase):
	def test_http_failure_keeps_password_out(self):
		"""HTTP 500 raises inside _post, whose `data` local carries the form
		body with Password."""
		cfg = SSConfig(customer_number="123", username="u", password=_mark("sports-password"),
			source="123", base_url="https://webservices.example.invalid")
		c = SSOrdersClient(cfg, session=live_session({".asmx/": (500, b"<boom/>")}))
		tb = _traceback_of(lambda: c._post("OrderService", "Ping", {}))
		self.assertIn("in _post", tb)
		self.assertNotIn(_mark("sports-password"), tb)


class RSR(FrappeTestCase):
	def test_http_failure_keeps_password_out(self):
		"""Every RSR call carries the password in its JSON body; a 400 raises
		with _post_raw's `payload` and _request's `json_body` on the stack."""
		cfg = RSRConfig(api_base_url="https://api.example.invalid", pos_indicator="I",
			dealer_account="acct", dealer_password=_mark("rsr-password"),
			fulfillment_account="", fulfillment_password="", api_enabled=True)
		c = RSRClient(cfg, session=live_session({"/": (400, {"StatusCode": "Bad"})}))
		tb = _traceback_of(lambda: c._post_raw("/api/rsrbridge/1.0/orders/status", {}, "dealer"))
		self.assertIn("in _post_raw", tb)
		self.assertNotIn(_mark("rsr-password"), tb)


class Lipseys(FrappeTestCase):
	def _client(self, canned):
		cfg = LipseysConfig(email="dealer@example.com", password=_mark("lipseys-password"),
			base_url="https://api.example.invalid/api/Integration")
		return LipseysClient(cfg, session=live_session(canned))

	def test_login_failure_keeps_password_out(self):
		"""HTTP 500 on login raises inside _send, whose `body` local is the JSON
		login body with Password."""
		c = self._client({"/Authentication/Login": (500, b"<boom/>")})
		tb = _traceback_of(c.login)
		self.assertIn("in _send", tb)
		self.assertNotIn(_mark("lipseys-password"), tb)

	def test_call_failure_keeps_token_out(self):
		"""The session token rides in the `headers` local of every later call."""
		c = self._client({"/Authentication/Login": (200, {"token": _mark("lipseys-token"),
			"econtact": {"success": True, "authorized": True, "data": {}}}),
			"/Items/ValidateItem": (503, b"<boom/>")})
		tb = _traceback_of(lambda: c.validate_item("ITEM-1"))
		self.assertIn("in _send", tb)
		self.assertNotIn(_mark("lipseys-token"), tb)
		self.assertNotIn(_mark("lipseys-password"), tb)
