"""DataServiceProvider + remote-first search tests. No real network."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.atf import ez_check
from ffl_integrations.atf.ez_check import EZCheckUnavailable
from ffl_integrations.atf.data_service_provider import DataServiceProvider, search_remote

FFL = "1-99-888-01-7K-27553"

VERIFY_PAYLOAD = {
	"ffl_number": FFL,
	"found": True,
	"status": "Verified",
	"licensee_name": "HUNTERS HEAVEN INC",
	"licensee_dba": "HUNTERS HEAVEN",
	"license_type": "01 - Dealer in Firearms Other Than Destructive Devices",
	"expiry_date": "2027-10-01",
	"address": "3530 GEORGE WASHINGTON MEMORIAL HIGHWAY HAYES VA - 23072",
	"street": "3530 GEORGE WASHINGTON MEMORIAL HIGHWAY",
	"street2": None,
	"city": "HAYES",
	"state": "VA",
	"zip": "23072",
	"fetched_at": "2026-07-30T22:49:18Z",
	"cached": False,
}

SEARCH_PAYLOAD = {
	"results": [
		{
			"license_number": "1-99-888-01-8L-12345",
			"type_code": "01",
			"expiration_date": "8L",
			"license_name": "SMITH JOHN",
			"trade_name": "JOHN'S GUNS",
			"premise_street": "123 MAIN ST",
			"premise_city": "ANYTOWN",
			"premise_state": "TX",
			"premise_zip": "75001",
			"voice_phone": "5551234567",
			"distance_miles": 0.0,
		},
		{
			"license_number": "1-99-888-07-7A-67890",
			"type_code": "07",
			"expiration_date": "7A",
			"license_name": "ACME GUNSMITHING LLC",
			"trade_name": "ACME",
			"premise_street": "500 OAK AVE",
			"premise_city": "DALLAS",
			"premise_state": "TX",
			"premise_zip": "75002",
			"voice_phone": "5559876543",
			"distance_miles": 0.0,
		},
	],
	"stale": False,
}


def _response(status: int, payload: dict | None = None) -> MagicMock:
	resp = MagicMock()
	resp.status_code = status
	resp.json.return_value = payload if payload is not None else {}
	return resp


class _ConfiguredBase(FrappeTestCase):
	"""FFL Settings + Data Service Settings wired for the Data Service provider — and restored afterwards:
	without the cleanup, every test run left the shared dev site pointing at
	the fake endpoint with a burned secret (see snapshot_single_values)."""

	def setUp(self):
		from ffl_integrations.tests.site_fixtures import snapshot_single_values
		self.addCleanup(snapshot_single_values("FFL Settings", ("live_check_provider",)))
		self.addCleanup(snapshot_single_values(
			"Data Service Settings", ("endpoint", "client_id"), passwords=("secret",)))
		frappe.db.set_single_value("FFL Settings", "live_check_provider", "Data Service")
		doc = frappe.get_single("Data Service Settings")
		doc.endpoint = "https://api.example.test"
		doc.client_id = "gunstore-pos"
		doc.secret = "unit-secret"
		doc.save(ignore_permissions=True)


class TestVerify(_ConfiguredBase):
	def test_maps_response_onto_result(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(200, VERIFY_PAYLOAD)) as mock_post:
			result = DataServiceProvider().verify(FFL)

		self.assertTrue(result.found)
		self.assertEqual(result.status, "Verified")
		self.assertEqual(result.licensee_name, "HUNTERS HEAVEN INC")
		self.assertEqual(result.expiry_date, date(2027, 10, 1))
		# Structured address rides along — no local re-parsing needed.
		self.assertEqual(result.city, "HAYES")
		self.assertEqual(result.zip, "23072")

		url = mock_post.call_args.args[0]
		self.assertEqual(url, f"https://api.example.test/v1/ffl/{FFL}/verify")

	def test_signs_like_the_wp_client(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(200, VERIFY_PAYLOAD)) as mock_post:
			DataServiceProvider().verify(FFL)

		kwargs = mock_post.call_args.kwargs
		headers, body = kwargs["headers"], kwargs["data"]
		msg = (
			f"{headers['x-client-id']}{headers['x-timestamp']}POST/v1/ffl/{FFL}/verify"
		).encode() + body
		expected = hmac.new(b"unit-secret", msg, hashlib.sha256).hexdigest()
		self.assertEqual(headers["x-signature"], expected)
		self.assertEqual(headers["x-client-id"], "gunstore-pos")
		self.assertEqual(json.loads(body), {"force": False})

	def test_429_maps_to_unavailable(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(429)):
			with self.assertRaises(EZCheckUnavailable):
				DataServiceProvider().verify(FFL)

	def test_5xx_maps_to_unavailable(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(503)):
			with self.assertRaises(EZCheckUnavailable):
				DataServiceProvider().verify(FFL)

	def test_network_error_maps_to_unavailable(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				side_effect=requests.ConnectionError("down")):
			with self.assertRaises(EZCheckUnavailable):
				DataServiceProvider().verify(FFL)

	def test_get_provider_resolves_by_settings(self):
		provider = ez_check.get_provider()
		self.assertIsInstance(provider, DataServiceProvider)

	def test_verify_flow_caches_structured_address(self):
		"""End to end through ez_check.verify(): the provider's structured
		address lands on the ATF FFL Record without local re-parsing."""
		frappe.db.delete("ATF FFL Record", {"ffl_number": FFL})
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(200, VERIFY_PAYLOAD)):
			result = ez_check.verify(FFL, force_live=True)

		self.assertTrue(result.found)
		row = frappe.db.get_value("ATF FFL Record", FFL,
			["licensee_name", "street", "city", "state", "zip"], as_dict=True)
		self.assertEqual(row.licensee_name, "HUNTERS HEAVEN INC")
		self.assertEqual(row.street, "3530 GEORGE WASHINGTON MEMORIAL HIGHWAY")
		self.assertEqual(row.city, "HAYES")
		self.assertEqual(row.state, "VA")
		self.assertEqual(row.zip, "23072")


class TestUnconfigured(FrappeTestCase):
	def setUp(self):
		# Restored afterwards — this one BLANKS the endpoint (see _ConfiguredBase).
		from ffl_integrations.tests.site_fixtures import snapshot_single_values
		self.addCleanup(snapshot_single_values("FFL Settings", ("live_check_provider",)))
		self.addCleanup(snapshot_single_values("Data Service Settings", ("endpoint", "client_id")))
		frappe.db.set_single_value("FFL Settings", "live_check_provider", "Data Service")
		doc = frappe.get_single("Data Service Settings")
		doc.endpoint = ""
		doc.client_id = "gunstore-pos"
		doc.save(ignore_permissions=True)

	def test_missing_config_is_unavailable_not_a_crash(self):
		with self.assertRaises(EZCheckUnavailable):
			DataServiceProvider().verify(FFL)


class TestSearchRemote(_ConfiguredBase):
	def test_name_query_and_row_shape(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(200, SEARCH_PAYLOAD)) as mock_post:
			rows = search_remote("ACME", limit=10)

		body = json.loads(mock_post.call_args.kwargs["data"])
		self.assertEqual(body, {"limit": 10, "name": "ACME"})
		self.assertEqual(len(rows), 2)
		row = rows[1]
		self.assertEqual(row["ffl_number"], "1-99-888-07-7A-67890")
		self.assertEqual(row["licensee_name"], "ACME GUNSMITHING LLC")
		self.assertEqual(row["city"], "DALLAS")
		self.assertTrue(row["_remote"])

	def test_numberish_query_routes_to_license_number(self):
		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(200, {"results": []})) as mock_post:
			search_remote("1-99-888-01-8L-12345")
		body = json.loads(mock_post.call_args.kwargs["data"])
		self.assertEqual(body.get("license_number"), "1-99-888-01-8L-12345")
		self.assertNotIn("name", body)


class TestSearchMerge(_ConfiguredBase):
	LOCAL_FFL = "1-99-888-01-XX-12345"  # placeholder-code twin of remote 8L row
	# Rows other suites (test_atf_loader) may have left on a shared dev site —
	# same licences under other spellings would pollute the LIKE results.
	STRAY_FFLS = (
		"1-99-888-01-XX-12345", "1-99-888-01-8L-12345",
		"1-99-888-07-7A-67890", "1-99-888-07-XX-67890",
	)

	def setUp(self):
		super().setUp()
		for ffl in self.STRAY_FFLS:
			frappe.db.delete("ATF FFL Record", {"ffl_number": ffl})
		frappe.get_doc({
			"doctype": "ATF FFL Record",
			"ffl_number": self.LOCAL_FFL,
			"licensee_name": "SMITH JOHN",
			"licensee_dba": "JOHN'S GUNS",
			"state": "TX",
		}).insert(ignore_permissions=True)

	def tearDown(self):
		for ffl in self.STRAY_FFLS:
			frappe.db.delete("ATF FFL Record", {"ffl_number": ffl})
		super().tearDown()

	def test_merges_and_dedupes_across_expiration_codes(self):
		from ffl_integrations.atf.search import search

		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				return_value=_response(200, SEARCH_PAYLOAD)):
			rows = search("JOHN")

		numbers = [r.get("ffl_number") for r in rows]
		# The landed local row wins over its remote twin (XX vs 8L is the
		# same licence); the genuinely new remote row is appended.
		self.assertIn(self.LOCAL_FFL, numbers)
		self.assertNotIn("1-99-888-01-8L-12345", numbers)
		self.assertIn("1-99-888-07-7A-67890", numbers)
		remote_rows = [r for r in rows if r.get("_remote")]
		self.assertTrue(remote_rows)

	def test_remote_failure_degrades_to_local_only(self):
		from ffl_integrations.atf.search import search

		with patch("ffl_integrations.atf.data_service_provider.requests.post",
				side_effect=requests.ConnectionError("down")):
			rows = search("JOHN")

		self.assertEqual([r["ffl_number"] for r in rows], [self.LOCAL_FFL])

	def test_non_api_provider_stays_local(self):
		from ffl_integrations.atf.search import search

		frappe.db.set_single_value("FFL Settings", "live_check_provider", "ATF Form Scraper")
		with patch("ffl_integrations.atf.data_service_provider.requests.post") as mock_post:
			rows = search("JOHN")
		mock_post.assert_not_called()
		self.assertEqual([r["ffl_number"] for r in rows], [self.LOCAL_FFL])
