"""The Data Service extension update source (core hook gunshop_extension_updates):
what it asks for, how it signs a download, and when a site has none."""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.data_service import client, extension_updates

CONN = ("https://api.example", "store-id", "store-secret")


class OsaApiSource(FrappeTestCase):
	def test_it_is_the_registered_source(self):
		self.assertIn("ffl_integrations.data_service.extension_updates.get_source",
			frappe.get_hooks("gunshop_extension_updates"))

	def test_no_data_service_no_source(self):
		from ffl_integrations.tests.catalog_stub import data_service_configured

		with data_service_configured(False):
			self.assertIsNone(extension_updates.get_source())
		with data_service_configured(True):
			self.assertIsInstance(extension_updates.get_source(), extension_updates.OsaApiSource)

	def test_latest_asks_for_what_this_core_can_run_and_keys_by_id(self):
		from ffl_core.extensions.registry import core_version

		body = {"releases": [{"ext_id": "osa_consignment", "version": "1.3.1", "sha256": "ab" * 32, "size": 9,
			"requires_core": ">=0.1.0,<2"}]}
		with patch.object(client, "get", return_value=body) as get:
			latest = extension_updates.OsaApiSource().latest()
		get.assert_called_once_with("/v1/extensions/latest", {"core": core_version()},
			timeout=extension_updates.CHECK_TIMEOUT_SECONDS)                # the page's Refresh waits on it
		self.assertEqual(extension_updates.OsaApiSource.publisher_id, "osa")
		self.assertEqual(list(latest), ["osa_consignment"])
		self.assertEqual(latest["osa_consignment"]["version"], "1.3.1")

	def test_a_store_not_granted_updates_has_none_offered(self):
		with patch.object(client, "get", side_effect=client.DataServiceForbidden("403")):
			self.assertEqual(extension_updates.OsaApiSource().latest(), {})

	def _response(self, status=200, chunks=(b"PK-", b"zip")):
		response = MagicMock(status_code=status)
		response.iter_content.return_value = list(chunks)
		return response

	def test_fetch_signs_the_path_streams_and_does_not_follow_redirects(self):
		with patch.object(client, "get_connection", return_value=CONN), \
				patch("ffl_integrations.data_service.client.time") as fake_time, \
				patch.object(client.requests, "get", return_value=self._response()) as get:
			fake_time.time.return_value = 1753900000
			fake_time.monotonic.return_value = 0
			self.assertEqual(extension_updates.OsaApiSource().fetch("osa_consignment", "1.3.1"), b"PK-zip")
			path = "/v1/extensions/osa_consignment/1.3.1/package"
			expected = client.signed_headers(CONN[1], CONN[2], "GET", path, b"")
		self.assertEqual(get.call_args.args[0], CONN[0] + path)
		self.assertEqual(get.call_args.kwargs["headers"], expected)
		self.assertEqual((get.call_args.kwargs["stream"], get.call_args.kwargs["allow_redirects"]), (True, False))

	def test_fetch_failures_are_data_service_errors(self):
		with patch.object(client, "get_connection", return_value=CONN):
			for response, error in ((self._response(404), client.DataServiceUnavailable),
					(self._response(403), client.DataServiceForbidden),
					(self._response(302), client.DataServiceUnavailable)):
				with patch.object(client.requests, "get", return_value=response), self.assertRaises(error):
					extension_updates.OsaApiSource().fetch("osa_consignment", "9.9.9")
			with patch.object(client.requests, "get", side_effect=client.requests.ConnectionError("down")), \
					self.assertRaises(client.DataServiceUnavailable):
				extension_updates.OsaApiSource().fetch("osa_consignment", "1.3.1")

	def test_a_download_past_the_cap_is_cut_off(self):
		with patch.object(client, "get_connection", return_value=CONN), \
				patch.object(client.requests, "get", return_value=self._response(chunks=(b"x" * 10, b"y" * 10))):
			with self.assertRaises(client.DataServiceUnavailable) as cm:
				client.get_bytes("/v1/extensions/a/1/package", max_bytes=15)
		self.assertIn("more than 15 bytes", str(cm.exception))

	def test_a_download_that_takes_too_long_in_all_is_cut_off(self):
		with patch.object(client, "get_connection", return_value=CONN), \
				patch("ffl_integrations.data_service.client.time") as fake_time, \
				patch.object(client.requests, "get", return_value=self._response(chunks=(b"a", b"b", b"c"))):
			fake_time.time.return_value = 1753900000
			fake_time.monotonic.side_effect = [0, 1, 100]            # start, after chunk 1, after chunk 2
			with self.assertRaises(client.DataServiceUnavailable) as cm:
				client.get_bytes("/v1/extensions/a/1/package", max_bytes=100, timeout=60)
		self.assertIn("more than 60s", str(cm.exception))
