import hashlib
import hmac as hmac_mod
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.data_service import client


class TestSignedHeaders(FrappeTestCase):
	def test_signature_matches_reference_scheme(self):
		with patch("ffl_integrations.data_service.client.time") as fake_time:
			fake_time.time.return_value = 1753900000
			headers = client.signed_headers(
				"gunstore-pos", "s3cret", "POST", "/v1/catalog/rows", b'{"a":1}')
		msg = b"gunstore-pos" + b"1753900000" + b"POST" + b"/v1/catalog/rows" + b'{"a":1}'
		expected = hmac_mod.new(b"s3cret", msg, hashlib.sha256).hexdigest()
		self.assertEqual(headers["x-signature"], expected)
		self.assertEqual(headers["x-client-id"], "gunstore-pos")
		self.assertEqual(headers["x-timestamp"], "1753900000")

	def test_unconfigured_raises_unavailable(self):
		settings = frappe.get_single("Data Service Settings")
		old = settings.endpoint
		try:
			frappe.db.set_single_value("Data Service Settings", "endpoint", "")
			frappe.get_single("Data Service Settings")  # reload cache
			with self.assertRaises(client.DataServiceUnavailable):
				client.get_connection()
		finally:
			frappe.db.set_single_value("Data Service Settings", "endpoint", old or "")


class TestPerDistributorConnection(FrappeTestCase):
	"""docs/catalog-api: a Distributor row may name its own catalog service;
	everything else (and every cross-house read) uses Data Service Settings."""

	def setUp(self):
		if not frappe.db.exists("Distributor", "CONTRACT-TEST"):
			frappe.get_doc({"doctype": "Distributor", "code": "CONTRACT-TEST",
				"distributor_name": "Contract test house", "provider": "contract-test",
				"enabled": 0}).insert(ignore_permissions=True)
		# tests in this class share the transaction: start each from "no override"
		frappe.db.set_value("Distributor", "CONTRACT-TEST", {"catalog_endpoint": "",
			"catalog_client_id": "", "catalog_secret": ""}, update_modified=False)
		frappe.clear_document_cache("Distributor", "CONTRACT-TEST")
		frappe.db.set_single_value("Data Service Settings", {
			"endpoint": "https://site.example/", "client_id": "site-id"})
		frappe.get_single("Data Service Settings").db_set("secret", "site-secret")  # plain in tests
		frappe.get_single("Data Service Settings")

	def test_house_without_its_own_endpoint_uses_the_site_service(self):
		endpoint, client_id, _ = client.get_connection("contract-test")
		self.assertEqual((endpoint, client_id), ("https://site.example", "site-id"))
		self.assertEqual(client.get_connection()[0], "https://site.example")

	def test_house_with_its_own_endpoint_uses_it(self):
		doc = frappe.get_doc("Distributor", "CONTRACT-TEST")
		doc.catalog_endpoint = "https://house.example/"
		doc.catalog_client_id = "house-id"
		doc.catalog_secret = "house-secret"
		doc.save(ignore_permissions=True)
		frappe.clear_document_cache("Distributor", "CONTRACT-TEST")
		endpoint, client_id, secret = client.get_connection("contract-test")
		self.assertEqual((endpoint, client_id, secret), ("https://house.example", "house-id", "house-secret"))
		# the site-wide service is untouched for unscoped reads
		self.assertEqual(client.get_connection()[1], "site-id")
		# half-configured house fails closed rather than silently falling back
		doc.catalog_client_id = ""
		doc.save(ignore_permissions=True)
		frappe.clear_document_cache("Distributor", "CONTRACT-TEST")
		with self.assertRaises(client.DataServiceUnavailable):
			client.get_connection("contract-test")


class TestMovePatch(FrappeTestCase):
	"""patches.move_data_service_to_own_settings: the three FFL Settings values
	land on Data Service Settings once, the old rows go, the provider choice is
	renamed — and an operator's explicit new setting is never overwritten."""

	def setUp(self):
		from ffl_integrations.tests.site_fixtures import snapshot_single_values
		self.addCleanup(snapshot_single_values("FFL Settings", ("live_check_provider",)))
		self.addCleanup(snapshot_single_values(
			"Data Service Settings", ("endpoint", "client_id"), passwords=("secret",)))

	@staticmethod
	def _old_row(field):
		return frappe.db.sql("select value from `tabSingles` where doctype='FFL Settings' and field=%s", (field,))

	def _seed_old(self):
		from frappe.utils.password import set_encrypted_password
		for field, value in (("osa_api_endpoint", "https://old.example/"),
				("osa_api_client_id", "old-id"), ("live_check_provider", "OSA API")):
			frappe.db.delete("Singles", {"doctype": "FFL Settings", "field": field})
			frappe.db.sql("insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)",
				("FFL Settings", field, value))
		set_encrypted_password("FFL Settings", "FFL Settings", "old-secret", "osa_api_secret")

	def test_moves_then_leaves_an_explicit_setting_alone(self):
		from ffl_integrations.patches import move_data_service_to_own_settings as patch_mod
		frappe.db.set_single_value("Data Service Settings", {"endpoint": "", "client_id": ""})
		self._seed_old()
		self.assertEqual(patch_mod.execute(), "moved")
		doc = frappe.get_single("Data Service Settings")
		self.assertEqual((doc.endpoint, doc.client_id), ("https://old.example", "old-id"))
		self.assertEqual(doc.get_password("secret"), "old-secret")
		self.assertEqual(frappe.db.get_single_value("FFL Settings", "live_check_provider"), "Data Service")
		self.assertFalse(self._old_row("osa_api_endpoint"))
		# second store's upgrade on an already-hand-wired new Single: kept, old rows still cleaned
		self._seed_old()
		self.assertEqual(patch_mod.execute(), "kept")
		self.assertEqual(frappe.get_single("Data Service Settings").client_id, "old-id")
		self.assertFalse(self._old_row("osa_api_client_id"))

	def test_nothing_to_move(self):
		from ffl_integrations.patches import move_data_service_to_own_settings as patch_mod
		frappe.db.delete("Singles", {"doctype": "FFL Settings",
			"field": ["in", ["osa_api_endpoint", "osa_api_client_id"]]})
		self.assertEqual(patch_mod.execute(), "absent")


class TestConnectionButton(FrappeTestCase):
	def test_reports_per_house_and_records_contract(self):
		from ffl_integrations.ffl_integrations.doctype.data_service_settings import (
			data_service_settings as mod)
		health = {"contract_version": "1.0", "last_catalog_sync_at": "t1", "last_quantity_sync_at": "t2"}
		with patch("ffl_integrations.data_service.client.get", return_value=health):
			out = mod.test_connection()
		self.assertTrue(out["checks"] and all(c["ok"] for c in out["checks"]))
		self.assertEqual(frappe.db.get_single_value("Data Service Settings", "contract_version"), "1.0")
		with patch("ffl_integrations.data_service.client.get",
				side_effect=client.DataServiceUnavailable("Data service unreachable: boom")):
			out = mod.test_connection()
		self.assertFalse(out["checks"][0]["ok"])
		self.assertIn("boom", frappe.db.get_single_value("Data Service Settings", "last_error"))
