"""Unit tests for the RSR Direct Connect ordering API client.

Every test runs WITHOUT a live RSR account: HTTP is intercepted at the session
level (an injected MagicMock session) or at `_request` (patch.object). No real
network traffic and no real credentials. Fixtures mirror the shapes documented
in the RSR Direct Connect manual (POST + JSON, body-carried Username/Password/POS,
line-level AckQty/Cost/ItemStatus/HoldStatus, check-order SerialNum[], StatusCode).
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.rsr.api_client import (
	API_PREFIX,
	NOT_FOUND_STATUS_CODE,
	RETRYABLE_STATUS_CODES,
	RSRClient,
	RSRConfig,
	RSRConfigError,
	RSRError,
	RSROrderNotFound,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(
	api_base_url: str = "https://www.rsrgroup.com",
	pos_indicator: str = "POS1",
	dealer_account: str = "99001",
	dealer_password: str = "dealer-secret",
	fulfillment_account: str = "88002",
	fulfillment_password: str = "fulfillment-secret",
	api_enabled: bool = True,
	timeout: int = 5,
) -> RSRConfig:
	return RSRConfig(
		api_base_url=api_base_url,
		pos_indicator=pos_indicator,
		dealer_account=dealer_account,
		dealer_password=dealer_password,
		fulfillment_account=fulfillment_account,
		fulfillment_password=fulfillment_password,
		api_enabled=api_enabled,
		timeout=timeout,
	)


def _client(cfg: RSRConfig | None = None) -> tuple[RSRClient, MagicMock]:
	"""Return (client, mock_session). HTTP calls go through mock_session.request."""
	cfg = cfg or _cfg()
	session = MagicMock()
	return RSRClient(cfg, session=session), session


def _ok(body=None, status: int = 200) -> MagicMock:
	resp = MagicMock()
	resp.status_code = status
	resp.content = b"{}" if body is None else b"body"
	resp.json.return_value = {} if body is None else body
	resp.text = ""
	return resp


def _err(status: int, text: str = "error") -> MagicMock:
	resp = MagicMock()
	resp.status_code = status
	resp.content = text.encode()
	resp.text = text
	return resp


def _et(hour: int, minute: int = 0) -> datetime:
	"""An America/New_York aware datetime at the given wall-clock time."""
	return datetime(2026, 7, 22, hour, minute, tzinfo=ZoneInfo("America/New_York"))


def _mock_settings(
	api_base_url="https://www.rsrgroup.com",
	pos_indicator="POS1",
	dealer_account="99001",
	dealer_password="dealer-secret",
	fulfillment_account="88002",
	fulfillment_password="fulfillment-secret",
	api_enabled=1,
	order_email="orders@example.com",
):
	m = MagicMock()
	m.order_email = order_email
	m.api_base_url = api_base_url
	m.pos_indicator = pos_indicator
	m.dealer_account = dealer_account
	m.fulfillment_account = fulfillment_account
	m.api_enabled = api_enabled
	m.get_password.side_effect = lambda field, **_: {
		"dealer_password": dealer_password,
		"fulfillment_password": fulfillment_password,
	}.get(field, "")
	return m


def _last_json(session: MagicMock) -> dict:
	"""The JSON body of the most recent session.request call."""
	return session.request.call_args.kwargs["json"]


def _called_endpoints(session: MagicMock) -> list[str]:
	"""The endpoint (last path segment) of every session.request call, in order."""
	return [c.args[1].rsplit("/", 1)[-1] for c in session.request.call_args_list]


# ---------------------------------------------------------------------------
# RSRConfig.from_settings
# ---------------------------------------------------------------------------

class TestConfigFromSettings(FrappeTestCase):
	def test_reads_api_region_and_both_credential_pairs(self):
		s = _mock_settings()
		with patch("frappe.get_single", return_value=s):
			cfg = RSRConfig.from_settings()
		self.assertEqual(cfg.api_base_url, "https://www.rsrgroup.com")
		self.assertEqual(cfg.pos_indicator, "POS1")
		self.assertEqual(cfg.dealer_account, "99001")
		self.assertEqual(cfg.dealer_password, "dealer-secret")
		self.assertEqual(cfg.fulfillment_account, "88002")
		self.assertEqual(cfg.fulfillment_password, "fulfillment-secret")
		self.assertEqual(cfg.order_email, "orders@example.com")
		self.assertTrue(cfg.api_enabled)

	def test_trailing_slash_stripped_from_base_url(self):
		s = _mock_settings(api_base_url="https://www.rsrgroup.com/")
		with patch("frappe.get_single", return_value=s):
			cfg = RSRConfig.from_settings()
		self.assertEqual(cfg.api_base_url, "https://www.rsrgroup.com")

	def test_api_enabled_reflects_setting(self):
		s = _mock_settings(api_enabled=0)
		with patch("frappe.get_single", return_value=s):
			cfg = RSRConfig.from_settings()
		self.assertFalse(cfg.api_enabled)


# ---------------------------------------------------------------------------
# URL construction + session policy
# ---------------------------------------------------------------------------

class TestUrlAndSession(FrappeTestCase):
	def test_url_uses_rsrbridge_pos_prefix(self):
		client, _ = _client()
		self.assertEqual(
			client._url("place-order"),
			f"https://www.rsrgroup.com/{API_PREFIX}/place-order",
		)
		self.assertTrue(API_PREFIX.startswith("api/rsrbridge/"))

	def test_retry_policy_excludes_post(self):
		"""Every RSR endpoint is a POST, several of which create/mutate an order.
		A blind urllib3 retry could double-submit, so POST must never auto-retry —
		place_order_idempotent handles the ambiguous case explicitly instead."""
		client = RSRClient(_cfg())
		session = client._build_session()
		retry = session.get_adapter("https://www.rsrgroup.com").max_retries
		self.assertNotIn("POST", retry.allowed_methods)

	def test_no_auth_header_set(self):
		"""RSR auth rides in the JSON body (Username/Password/POS), not a header."""
		client = RSRClient(_cfg())
		session = client._build_session()
		self.assertNotIn("Authorization", session.headers)


# ---------------------------------------------------------------------------
# _post: credential injection, POS, api_enabled guard, account selection
# ---------------------------------------------------------------------------

class TestPostEnvelope(FrappeTestCase):
	def test_injects_username_password_pos(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00"})
		client.check_catalog(["ABC"], account="dealer")
		body = _last_json(session)
		self.assertEqual(body["Username"], "99001")
		self.assertEqual(body["Password"], "dealer-secret")
		self.assertEqual(body["POS"], "POS1")

	def test_dealer_account_selects_dealer_credentials(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00"})
		client.check_catalog(["ABC"], account="dealer")
		body = _last_json(session)
		self.assertEqual(body["Username"], "99001")
		self.assertEqual(body["Password"], "dealer-secret")

	def test_fulfillment_account_selects_fulfillment_credentials(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00"})
		client.get_transfer_dealers(account="fulfillment")
		body = _last_json(session)
		self.assertEqual(body["Username"], "88002")
		self.assertEqual(body["Password"], "fulfillment-secret")

	def test_unknown_account_raises(self):
		client, session = _client()
		with self.assertRaises(RSRError):
			client.check_catalog(["ABC"], account="nope")
		session.request.assert_not_called()

	def test_disabled_api_refuses_calls(self):
		client, session = _client(_cfg(api_enabled=False))
		with self.assertRaises(RSRConfigError):
			client.check_catalog(["ABC"], account="dealer")
		session.request.assert_not_called()

	def test_missing_selected_credentials_raise_config_error(self):
		client, session = _client(_cfg(fulfillment_account="", fulfillment_password=""))
		with self.assertRaises(RSRConfigError):
			client.get_transfer_dealers(account="fulfillment")
		session.request.assert_not_called()


# ---------------------------------------------------------------------------
# _request: HTTP-layer RSRError terminal split
# ---------------------------------------------------------------------------

class TestHttpErrorSplit(FrappeTestCase):
	def test_http_4xx_is_terminal(self):
		client, session = _client()
		session.request.return_value = _err(400, "bad request")
		with self.assertRaises(RSRError) as cm:
			client.check_catalog(["ABC"])
		self.assertEqual(cm.exception.status_code, 400)
		self.assertTrue(cm.exception.terminal)

	def test_http_5xx_is_retryable(self):
		client, session = _client()
		session.request.return_value = _err(503, "unavailable")
		with self.assertRaises(RSRError) as cm:
			client.check_catalog(["ABC"])
		self.assertEqual(cm.exception.status_code, 503)
		self.assertFalse(cm.exception.terminal)

	def test_http_429_is_retryable(self):
		client, session = _client()
		session.request.return_value = _err(429, "slow down")
		with self.assertRaises(RSRError) as cm:
			client.check_catalog(["ABC"])
		self.assertFalse(cm.exception.terminal)

	def test_network_error_is_retryable_status_none(self):
		client, session = _client()
		for exc in (requests.exceptions.ConnectTimeout("t"),
				requests.exceptions.ConnectionError("down"),
				requests.exceptions.ReadTimeout("slow")):
			session.request.side_effect = exc
			with self.assertRaises(RSRError) as cm:
				client.check_catalog(["ABC"])
			self.assertIsNone(cm.exception.status_code)
			self.assertFalse(cm.exception.terminal)

	def test_non_json_body_raises(self):
		client, session = _client()
		resp = MagicMock()
		resp.status_code = 200
		resp.content = b"<html>nope</html>"
		resp.json.side_effect = ValueError("no json")
		resp.text = "<html>nope</html>"
		session.request.return_value = resp
		with self.assertRaises(RSRError):
			client.check_catalog(["ABC"])


# ---------------------------------------------------------------------------
# Business StatusCode terminal split
# ---------------------------------------------------------------------------

class TestStatusCodeSplit(FrappeTestCase):
	def test_success_status_returns_body(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00", "WebRef": "W-1"})
		out = client.place_order({"PONum": "PO-1"}, account="dealer")
		self.assertEqual(out["WebRef"], "W-1")

	def test_absent_status_treated_as_success(self):
		client, session = _client()
		session.request.return_value = _ok({"Dealers": []})
		self.assertEqual(client.get_transfer_dealers(), [])

	def test_unknown_error_status_is_terminal(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "12", "Message": "Invalid item"})
		with self.assertRaises(RSRError) as cm:
			client.place_order({"PONum": "PO-1"}, account="dealer")
		self.assertTrue(cm.exception.terminal)
		self.assertNotIsInstance(cm.exception, RSROrderNotFound)

	def test_retryable_status_is_not_terminal(self):
		retryable_code = next(iter(RETRYABLE_STATUS_CODES))
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": retryable_code})
		with self.assertRaises(RSRError) as cm:
			client.place_order({"PONum": "PO-1"}, account="dealer")
		self.assertFalse(cm.exception.terminal)


# ---------------------------------------------------------------------------
# place_order — response passthrough (AckQty / Cost / Hold / ItemStatus)
# ---------------------------------------------------------------------------

class TestPlaceOrder(FrappeTestCase):
	def _place(self, body):
		client, session = _client()
		session.request.return_value = _ok(body)
		with patch.object(client, "_now_et", return_value=_et(12)):   # outside maint window
			return client.place_order({"PONum": "PO-1", "Items": []}, account="fulfillment")

	def test_success_full_ack(self):
		out = self._place({
			"StatusCode": "00", "WebRef": "12345", "OrderTotal": 419.97,
			"Items": [{"SKU": "GLK19", "AckQty": 2, "Cost": 399.99, "ItemStatus": "P"}],
		})
		self.assertEqual(out["WebRef"], "12345")
		self.assertEqual(out["Items"][0]["AckQty"], 2)
		self.assertEqual(out["Items"][0]["Cost"], 399.99)

	def test_partial_ack_passed_through(self):
		out = self._place({
			"StatusCode": "00", "WebRef": "12346",
			"Items": [{"SKU": "GLK19", "AckQty": 1, "ItemStatus": "P"}],   # wanted 2, got 1
		})
		self.assertEqual(out["Items"][0]["AckQty"], 1)

	def test_hold_status_passed_through(self):
		out = self._place({
			"StatusCode": "00", "WebRef": "12347", "HoldStatus": "FDS",
			"Items": [{"SKU": "SIGP320", "AckQty": 1}],
		})
		self.assertEqual(out["HoldStatus"], "FDS")

	def test_item_status_branch_passed_through(self):
		out = self._place({
			"StatusCode": "00", "WebRef": "12348",
			"Items": [{"SKU": "OUT1", "AckQty": 0, "ItemStatus": "O"}],   # out of stock
		})
		self.assertEqual(out["Items"][0]["ItemStatus"], "O")


# ---------------------------------------------------------------------------
# Maintenance window: Queue=Y (America/New_York 3–5am ET)
# ---------------------------------------------------------------------------

class TestMaintenanceWindow(FrappeTestCase):
	def test_queue_injected_during_window(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00"})
		with patch.object(client, "_now_et", return_value=_et(4)):
			client.place_order({"PONum": "PO-1"}, account="fulfillment")
		self.assertEqual(_last_json(session).get("Queue"), "Y")

	def test_no_queue_outside_window(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00"})
		with patch.object(client, "_now_et", return_value=_et(12)):
			client.place_order({"PONum": "PO-1"}, account="fulfillment")
		self.assertNotIn("Queue", _last_json(session))

	def test_queue_only_applies_to_place_order(self):
		"""A read call in the window must NOT get a Queue flag — it's a place-order
		concept only."""
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00", "Items": []})
		with patch.object(client, "_now_et", return_value=_et(4)):
			client.check_catalog(["ABC"], account="dealer")
		self.assertNotIn("Queue", _last_json(session))

	def test_window_boundaries(self):
		client, _ = _client()
		self.assertTrue(client._in_maintenance_window(_et(3, 0)))
		self.assertTrue(client._in_maintenance_window(_et(4, 59)))
		self.assertFalse(client._in_maintenance_window(_et(2, 59)))
		self.assertFalse(client._in_maintenance_window(_et(5, 0)))
		self.assertFalse(client._in_maintenance_window(_et(12, 0)))


# ---------------------------------------------------------------------------
# place_order_idempotent — timeout/5xx disambiguation, never blind-retry
# ---------------------------------------------------------------------------

class TestIdempotentPlaceOrder(FrappeTestCase):
	def _found_order(self):
		return _ok({"StatusCode": "00", "PONum": "PO-9", "Items": [{"SKU": "GLK19", "AckQty": 1}]})

	def test_timeout_disambiguates_via_check_order_without_resubmitting(self):
		client, session = _client()
		session.request.side_effect = [
			requests.exceptions.ReadTimeout("timeout"),   # place-order: unclear if it landed
			self._found_order(),                            # check-order: it DID land
		]
		with patch.object(client, "_now_et", return_value=_et(12)):
			out = client.place_order_idempotent("PO-9", {"Items": []}, account="fulfillment")
		self.assertEqual(out["PONum"], "PO-9")
		endpoints = _called_endpoints(session)
		self.assertEqual(endpoints, ["place-order", "check-order"])
		self.assertEqual(endpoints.count("place-order"), 1)   # never blind-resubmitted

	def test_5xx_disambiguates_via_check_order(self):
		client, session = _client()
		session.request.side_effect = [
			_err(502, "bad gateway"),      # place-order 5xx: ambiguous
			self._found_order(),           # check-order: found
		]
		with patch.object(client, "_now_et", return_value=_et(12)):
			out = client.place_order_idempotent("PO-9", {"Items": []}, account="fulfillment")
		self.assertEqual(out["PONum"], "PO-9")
		self.assertEqual(_called_endpoints(session).count("place-order"), 1)

	def test_confirmed_not_placed_raises_retryable_without_resubmitting(self):
		client, session = _client()
		session.request.side_effect = [
			requests.exceptions.ReadTimeout("timeout"),   # place-order
			_ok({"StatusCode": "98"}),                     # check-order: not found
			_ok({"StatusCode": "98"}),                     # check-combined-order: also not found
		]
		with patch.object(client, "_now_et", return_value=_et(12)):
			with self.assertRaises(RSRError) as cm:
				client.place_order_idempotent("PO-9", {"Items": []}, account="fulfillment")
		self.assertFalse(cm.exception.terminal)              # safe to re-drive at a higher level
		self.assertEqual(_called_endpoints(session).count("place-order"), 1)

	def test_terminal_4xx_does_not_disambiguate(self):
		"""A definite client-side rejection (4xx) is terminal — nothing landed, so
		there's nothing to disambiguate and check-order must not be called."""
		client, session = _client()
		session.request.return_value = _err(400, "invalid PONum")
		with patch.object(client, "_now_et", return_value=_et(12)):
			with self.assertRaises(RSRError):
				client.place_order_idempotent("PO-9", {"Items": []}, account="fulfillment")
		self.assertEqual(session.request.call_count, 1)

	def test_terminal_business_status_reraised_without_disambiguation(self):
		"""A place-order the RSR *business layer* definitively rejected (e.g.
		StatusCode 81 Duplicate PO Num: status_code=None but rsr_status set) is
		terminal, NOT transport-ambiguous. Idempotent must re-raise the original
		rejection as-is and never run check-order — disambiguating here would mask
		the real reason as retryable, and on a reused PONum could adopt a prior,
		unrelated order (the exact failure this method exists to prevent)."""
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "81", "Message": "Duplicate PO Num"})
		with patch.object(client, "_now_et", return_value=_et(12)):
			with self.assertRaises(RSRError) as cm:
				client.place_order_idempotent("PO-9", {"Items": []}, account="fulfillment")
		self.assertTrue(cm.exception.terminal)                         # stays terminal, not downgraded
		self.assertEqual(cm.exception.rsr_status, "81")                # original rejection preserved
		self.assertNotIsInstance(cm.exception, RSROrderNotFound)
		self.assertIn("place-order", str(cm.exception))                # the ORIGINAL error, not a check-order re-derivation
		self.assertEqual(_called_endpoints(session), ["place-order"])  # never disambiguated


# ---------------------------------------------------------------------------
# check_order — 98 / combined-signature fallback to check_combined_order (H2)
# ---------------------------------------------------------------------------

class TestCheckOrderCombinedFallback(FrappeTestCase):
	def test_normal_response_returns_directly(self):
		client, session = _client()
		session.request.return_value = _ok({
			"StatusCode": "00", "PONum": "PO-1",
			"Items": [{"SKU": "GLK19", "Invoiced": 1, "SerialNum": ["ABC123"], "TrackingNum": ["1Z"]}],
		})
		out = client.check_order("PO-1", account="dealer")
		self.assertEqual(out["Items"][0]["SerialNum"], ["ABC123"])
		self.assertEqual(_called_endpoints(session), ["check-order"])

	def test_status_98_falls_back_to_combined(self):
		client, session = _client()
		session.request.side_effect = [
			_ok({"StatusCode": "98"}),                                   # check-order: not found
			_ok({"StatusCode": "00", "PONum": "PO-1", "Combined": "Y"}),  # combined: found
		]
		out = client.check_order("PO-1", account="dealer")
		self.assertEqual(out["PONum"], "PO-1")
		self.assertEqual(_called_endpoints(session), ["check-order", "check-combined-order"])

	def test_combined_signature_falls_back(self):
		client, session = _client()
		session.request.side_effect = [
			_ok({"StatusCode": "00", "PONum": "PO-1", "Combined": "Y"}),  # signals a combine
			_ok({"StatusCode": "00", "PONum": "PO-1", "Items": [{"SKU": "X"}]}),
		]
		out = client.check_order("PO-1", account="dealer")
		self.assertEqual(_called_endpoints(session), ["check-order", "check-combined-order"])
		self.assertIn("Items", out)

	def test_combined_also_missing_raises_not_found(self):
		client, session = _client()
		session.request.side_effect = [
			_ok({"StatusCode": "98"}),   # check-order
			_ok({"StatusCode": "98"}),   # combined
		]
		with self.assertRaises(RSROrderNotFound):
			client.check_order("PO-1", account="dealer")


# ---------------------------------------------------------------------------
# check_catalog + probe_connection
# ---------------------------------------------------------------------------

class TestCatalogAndProbe(FrappeTestCase):
	def test_check_catalog_returns_list(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00", "Items": [
			{"SKU": "GLK19", "Qty": 5, "Cost": 399.99, "Map": 449.99}]})
		out = client.check_catalog(["GLK19"], account="dealer")
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["SKU"], "GLK19")

	def test_check_catalog_rejects_over_100_skus(self):
		client, session = _client()
		with self.assertRaises(RSRError) as cm:
			client.check_catalog([f"SKU{i}" for i in range(101)], account="dealer")
		self.assertTrue(cm.exception.terminal)
		session.request.assert_not_called()

	def test_check_catalog_defaults_to_dealer_account(self):
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "00", "Items": []})
		client.check_catalog(["GLK19"])
		self.assertEqual(_last_json(session)["Username"], "99001")

	def test_probe_connection_is_read_only_and_returns_response(self):
		"""A credentials canary — returns the raw response even when RSR reports a
		non-success StatusCode (so it never masks an auth problem as a crash)."""
		client, session = _client()
		session.request.return_value = _ok({"StatusCode": "13", "Message": "bad creds"})
		out = client.probe_connection(account="dealer")
		self.assertIsInstance(out, dict)
		# read-only: hits the catalog endpoint, never place-order
		self.assertNotIn("place-order", _called_endpoints(session))


# ---------------------------------------------------------------------------
# Live-verified request shapes (2026-08-18, test.rsrgroup.com sweep)
#
# Every assertion below pins a request body to the contract the REAL bridge
# accepts. The original shapes (SKUList / SearchZip / PONum-keyed FFL update /
# ShipmentId / bare-PONum check-order) were built from assumptions, passed 25
# PRs of recorded-fixture tests, and failed 100% against the live endpoint.
# If one of these breaks, the fix is a wire capture, not an edit here.
# ---------------------------------------------------------------------------

class TestLiveContractShapes(FrappeTestCase):
	def _ok_client(self, body=None):
		client, session = _client()
		session.request.return_value = _ok(body or {"StatusCode": "00", "Items": []})
		return client, session

	def test_check_catalog_sends_lookupby_items_batch(self):
		"""Batch shape always — the single-item form answers in the topmost
		schema where a per-item miss becomes a raised top-level error."""
		client, session = self._ok_client()
		client.check_catalog(["GLK19", "BD100"], account="dealer")
		body = _last_json(session)
		self.assertEqual(body["LookupBy"], "S")
		self.assertEqual(body["Items"], ["GLK19", "BD100"])
		self.assertNotIn("SKUList", body)

	def test_check_catalog_merges_fulfillment_ship_context(self):
		client, session = self._ok_client()
		client.check_catalog(["GLK19"], account="fulfillment",
			ship_context={"Storename": "Jane", "ShipFFL": "9-99-999-07-7E-17760"})
		body = _last_json(session)
		self.assertEqual(body["Storename"], "Jane")
		self.assertEqual(body["ShipFFL"], "9-99-999-07-7E-17760")

	def test_check_order_always_reports_all(self):
		client, session = self._ok_client({"StatusCode": "00", "PONum": "PO-1", "Items": []})
		client.check_order("PO-1", account="dealer")
		body = _last_json(session)
		self.assertEqual(body["ReportAll"], "Y")
		self.assertEqual(body["PONum"], "PO-1")

	def test_check_combined_order_also_reports_all(self):
		client, session = self._ok_client({"StatusCode": "00", "PONum": "PO-1"})
		client.check_combined_order("PO-1", account="dealer")
		self.assertEqual(_last_json(session)["ReportAll"], "Y")

	def test_get_item_attributes_is_single_sku_lookup(self):
		client, session = self._ok_client({"StatusCode": "00",
			"Attributes": [{"Name": "Caliber", "Value": "9MM"}]})
		out = client.get_item_attributes("MGMPI571BLK", account="dealer")
		body = _last_json(session)
		self.assertEqual(body["LookupBy"], "S")
		self.assertEqual(body["PartNum"], "MGMPI571BLK")
		self.assertNotIn("Items", body)
		self.assertEqual(out, [{"Name": "Caliber", "Value": "9MM"}])

	def test_get_items_passes_search_filters_verbatim(self):
		client, session = self._ok_client({"StatusCode": "00", "TotalFound": "1",
			"RowCount": 1, "Items": [{"SKU": "GLK19"}]})
		out = client.get_items({"Departments": "1", "Limit": 500}, account="dealer")
		body = _last_json(session)
		self.assertEqual(body["Departments"], "1")
		self.assertEqual(body["Limit"], 500)
		self.assertEqual(out["TotalFound"], "1")     # raw envelope for pagination

	def test_get_transfer_dealers_sends_no_filter(self):
		client, session = self._ok_client({"StatusCode": 0, "Dealers": [
			{"Name": "EXAMPLE FIREARMS", "FFL": "9-99-999-07-7E-17760"}]})
		out = client.get_transfer_dealers(account="fulfillment")
		body = _last_json(session)
		self.assertNotIn("SearchZip", body)
		self.assertEqual(out[0]["Name"], "EXAMPLE FIREARMS")

	def test_dealer_accept_transfers_keys_on_ffl(self):
		client, session = self._ok_client({"StatusCode": "00",
			"FFL": "9-99-999-07-7E-17760", "AcceptTransfers": "Y"})
		out = client.dealer_accept_transfers("9-99-999-07-7E-17760")
		body = _last_json(session)
		self.assertEqual(body["FFL"], "9-99-999-07-7E-17760")
		self.assertNotIn("PONum", body)
		self.assertEqual(out["AcceptTransfers"], "Y")
		self.assertTrue(out["on_file"])

	def test_dealer_accept_transfers_maps_unknown_ffl_to_not_on_file(self):
		"""96 "FFL does not exist" is an ANSWER (not on file), never a raise."""
		client, session = self._ok_client(
			{"StatusCode": "96", "StatusMssg": "FFL does not exist"})
		out = client.dealer_accept_transfers("1-11-111-11-1A-11111")
		self.assertFalse(out["on_file"])
		self.assertEqual(out["AcceptTransfers"], "N")

	def test_dealer_accept_transfers_other_96_still_raises(self):
		client, session = self._ok_client(
			{"StatusCode": "96", "StatusMssg": "FFL: Required field"})
		with self.assertRaises(RSRError):
			client.dealer_accept_transfers("")

	def test_update_order_ffl_keys_on_webref(self):
		client, session = self._ok_client({"StatusCode": "00", "ShipFFL": "9-99-999-07-8K-16950"})
		client.update_order_ffl("1000001", "9-99-999-07-8K-16950")
		body = _last_json(session)
		self.assertEqual(body["WebRef"], "1000001")
		self.assertEqual(body["ShipFFL"], "9-99-999-07-8K-16950")
		self.assertNotIn("PONum", body)

	def test_get_shipments_is_a_date_range_invoice_feed(self):
		client, session = self._ok_client({"StatusCode": "00",
			"Invoices": ["4021361234"], "RecordCount": 1})
		out = client.get_shipments("2026-08-01", account="dealer")
		body = _last_json(session)
		self.assertEqual(body["DatePosted"], "2026-08-01")
		self.assertNotIn("PONum", body)
		self.assertEqual(out, ["4021361234"])

	def test_get_shipment_keys_on_invoice_number(self):
		client, session = self._ok_client({"StatusCode": "00", "Items": []})
		client.get_shipment("4021361234", account="dealer")
		body = _last_json(session)
		self.assertEqual(body["InvoiceNumber"], "4021361234")
		self.assertNotIn("ShipmentId", body)

	def test_probe_sends_batch_shape_and_fulfillment_ship_context(self):
		client, session = self._ok_client({"Status": "OK", "Items": []})
		client.probe_connection(account="dealer")
		body = _last_json(session)
		self.assertEqual(body["Items"], ["POS-PING"])
		self.assertEqual(body["LookupBy"], "S")
		self.assertNotIn("Storename", body)
		client.probe_connection(account="fulfillment")
		body = _last_json(session)
		# the fulfillment account geolocates a ship-to on every call (97 without)
		self.assertEqual(body["Storename"], "OpenGunstore Connection Probe")
		self.assertEqual(body["ShipZip"], "32792")

	def test_place_order_status_60_queued_is_not_an_error(self):
		"""60 = accepted + queued for after the maintenance window (manual p.94).
		Only place-order defines it — anywhere else it must still raise."""
		client, session = self._ok_client({"StatusCode": "60", "StatusMssg": "Order queued"})
		out = client.place_order({"PONum": "PO-1", "Items": []}, account="dealer")
		self.assertEqual(out["StatusCode"], "60")
		with self.assertRaises(RSRError):
			client.check_order("PO-1", account="dealer")
