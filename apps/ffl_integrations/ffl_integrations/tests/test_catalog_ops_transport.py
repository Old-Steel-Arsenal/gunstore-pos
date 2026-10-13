"""`/v1/catalog/ops/*` transport + its test double.

Two things are pinned here and nothing else:

1. **The wire contract**, against `tests/catalog_stub.py`. The stub is the only
   description of the ops face that lives in this repo, so if it drifts from the
   service the page reads garbage and no test notices. Field NAMES and the fixed
   5-feed list are asserted exactly, not `assertIn`-style, so an added or renamed
   key fails loudly.
2. **The 60s cache**, which is the entire mitigation for "3 extra service calls
   per page open, times a page that refreshes itself every 60s, times N staff".
   Exercising it means turning `in_test` off for the call, because the stub short
   -circuits before the cache by design.
"""
from __future__ import annotations

import unittest
from unittest import mock

import frappe

from ffl_integrations.distributor import remote_catalog
from ffl_integrations.data_service import client
from ffl_integrations.tests import catalog_stub

STATUS_KEYS = {"distributor", "known", "ingest_enabled", "server_time", "feeds",
	"offers_total", "offers_active", "products_total", "changed_24h",
	"change_seq_max", "next_catalog_due_at", "failures_24h", "last_failure",
	"cursor_lag"}
FEED_KEYS = {"feed", "last_success_at", "age_seconds", "threshold_seconds",
	"stale", "reason"}
RUN_KEYS = {"finished_at", "source", "status", "rows_total", "duration_seconds",
	"error_class"}
FLAGS_KEYS = {"distributor", "active", "in_stock", "discontinued", "deleted",
	"upc_have", "upc_orphan", "flags", "departments"}


class TestOpsStubContract(unittest.TestCase):
	def setUp(self):
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "GLK19", "upc": "764503037082", "quantity": 12,
				"department_number": "01", "ffl_required": 1, "map_price": 499.0,
				"dealer_price": 445.5, "image_url": "https://x/y.jpg"},
			{"rsr_stock_number": "NOMAP", "upc": "111111111111", "quantity": 0,
				"department_number": "01", "map_price": 0.0},
			{"rsr_stock_number": "ORPHAN", "quantity": 3, "department_number": "05",
				"discontinued": 1},
		])

	def tearDown(self):
		catalog_stub.uninstall()

	def test_status_shape_is_exactly_the_contract(self):
		out = remote_catalog.ops_status("rsr")
		self.assertEqual(set(out), STATUS_KEYS)
		self.assertEqual([f["feed"] for f in out["feeds"]],
			list(catalog_stub.OPS_FEEDS))
		self.assertEqual(len(out["feeds"]), 5)
		for feed in out["feeds"]:
			self.assertEqual(set(feed), FEED_KEYS)
			self.assertEqual(feed["threshold_seconds"],
				catalog_stub.OPS_FEED_THRESHOLD_SECONDS[feed["feed"]])
		self.assertTrue(out["known"])
		self.assertEqual(out["offers_total"], 3)
		self.assertEqual(out["offers_active"], 2)      # ORPHAN is discontinued
		self.assertEqual(out["products_total"], 2)     # ORPHAN has no UPC
		self.assertIsNone(out["cursor_lag"])           # no `since` asked

	def test_feed_verdicts_follow_age_against_the_service_threshold(self):
		self.stub.set_ops_feed("quantity", 5399)
		self.assertFalse(self._feed("quantity")["stale"])
		self.stub.set_ops_feed("quantity", 5400)
		aged = self._feed("quantity")
		self.assertTrue(aged["stale"])
		self.assertEqual(aged["reason"], "stale")
		self.stub.set_ops_feed("quantity", None)
		never = self._feed("quantity")
		self.assertTrue(never["stale"])
		self.assertEqual(never["reason"], "never_synced")
		self.assertIsNone(never["last_success_at"])
		self.assertIsNone(never["age_seconds"])

	def test_unknown_distributor_is_all_zero_not_an_error(self):
		"""The service answers 200 with an empty body for a code it has never
		heard of — the page must never 404 on a typo."""
		out = remote_catalog.ops_status("zzz")
		self.assertEqual(set(out), STATUS_KEYS)
		self.assertFalse(out["known"])
		self.assertEqual(out["offers_total"], 0)
		self.assertEqual(out["offers_active"], 0)
		self.assertEqual({f["reason"] for f in out["feeds"]}, {"never_synced"})

	def test_cursor_lag_counts_offers_not_seq_distance(self):
		"""`change_seq` is a global sequence with holes, so the difference between
		two seq values is NOT a row count. The contract is "how many offers are
		behind us", and the stub must answer it the same way."""
		seqs = sorted(self.stub.change_seq.values())
		out = remote_catalog.ops_status("rsr", since=seqs[0])
		self.assertEqual(out["cursor_lag"], {"since": seqs[0], "offers_pending": 2})
		caught_up = remote_catalog.ops_status("rsr", since=seqs[-1])
		self.assertEqual(caught_up["cursor_lag"]["offers_pending"], 0)

	def test_history_shape_and_limit(self):
		self.stub.seed_sync_runs([
			{"status": "error", "source": "rsr_quantity", "error_class": "SSHException"},
			{"status": "success", "source": "rsr_catalog", "rows_total": 31245},
			{"status": "success", "source": "rsr_quantity", "rows_total": 31245},
		])
		out = remote_catalog.ops_history("rsr", limit=2)
		self.assertEqual(set(out), {"distributor", "runs"})
		self.assertEqual(len(out["runs"]), 2)
		for run in out["runs"]:
			self.assertEqual(set(run), RUN_KEYS)
		status = remote_catalog.ops_status("rsr")
		self.assertEqual(status["failures_24h"], 1)
		self.assertEqual(status["last_failure"]["error_class"], "SSHException")
		# Only the class name ever crosses the wire — never the raw message, which
		# can carry an FTP host/path/account.
		self.assertRegex(status["last_failure"]["error_class"],
			r"^[A-Za-z_][A-Za-z0-9_.]*$")

	def test_flags_shape_and_derivation(self):
		out = remote_catalog.ops_flags("rsr")
		self.assertEqual(set(out), FLAGS_KEYS)
		self.assertEqual(set(out["flags"]),
			{"map_zero", "cost_over_map", "no_image", "orphan_upc"})
		self.assertEqual(out["active"], 2)
		self.assertEqual(out["in_stock"], 1)          # GLK19 qty 12, NOMAP qty 0
		self.assertEqual(out["discontinued"], 1)
		self.assertEqual(out["upc_orphan"], 1)
		self.assertEqual(out["flags"]["orphan_upc"], out["upc_orphan"])
		self.assertEqual(out["flags"]["map_zero"], 1)  # NOMAP
		self.assertLessEqual(len(out["departments"]), 10)
		self.assertEqual(out["departments"][0]["number"], "01")

	def test_ops_error_propagates_so_tests_can_drive_degradation(self):
		self.stub.set_ops_error(remote_catalog.DataServiceForbidden("nope"))
		for call in (lambda: remote_catalog.ops_status("rsr"),
				lambda: remote_catalog.ops_history("rsr"),
				lambda: remote_catalog.ops_flags("rsr")):
			with self.assertRaises(remote_catalog.DataServiceForbidden):
				call()

	def test_missing_stub_raises_loudly_for_the_ops_face_too(self):
		catalog_stub.uninstall()
		for call in (lambda: remote_catalog.ops_status("rsr"),
				lambda: remote_catalog.ops_history("rsr"),
				lambda: remote_catalog.ops_flags("rsr")):
			with catalog_stub.data_service_configured(), \
					self.assertRaises(remote_catalog.CatalogStubMissing):
				call()

	def _feed(self, name):
		return next(f for f in remote_catalog.ops_status("rsr")["feeds"]
			if f["feed"] == name)


class TestOpsTransportCaching(unittest.TestCase):
	"""The real (non-stub) branch. `in_test` is flipped off for the duration
	because `_stub()` short-circuits before the cache by design."""

	DIST = "ops-cache-probe"

	def setUp(self):
		self.keys = [f"rc_ops_status:{self.DIST}:None", f"rc_ops_status:{self.DIST}:7",
			f"rc_ops_history:{self.DIST}:20", f"rc_ops_flags:{self.DIST}"]
		self._clear()
		self.addCleanup(self._clear)
		self.enterContext(catalog_stub.data_service_configured())

	def _clear(self):
		for key in self.keys:
			frappe.cache().delete_value(key)

	def _live(self, fn, *args, **kwargs):
		was = getattr(frappe.flags, "in_test", False)
		frappe.flags.in_test = False
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.flags.in_test = was

	def test_second_call_inside_the_ttl_does_not_hit_the_service(self):
		body = {"distributor": self.DIST, "known": True, "feeds": []}
		with mock.patch.object(client, "get", return_value=body) as get:
			first = self._live(remote_catalog.ops_status, self.DIST)
			second = self._live(remote_catalog.ops_status, self.DIST)
		self.assertEqual(first, body)
		self.assertEqual(second, body)
		self.assertEqual(get.call_count, 1)
		get.assert_called_with("/v1/catalog/ops/status", {"distributor": self.DIST}, distributor=self.DIST)

	def test_a_different_cursor_is_a_different_cache_entry(self):
		"""`since` changes the answer (`cursor_lag`), so it must be part of the
		key — otherwise the first viewer's lag figure is served to everyone."""
		with mock.patch.object(client, "get", return_value={"ok": 1}) as get:
			self._live(remote_catalog.ops_status, self.DIST)
			self._live(remote_catalog.ops_status, self.DIST, since=7)
		self.assertEqual(get.call_count, 2)
		self.assertEqual(get.call_args_list[-1][0][1],
			{"distributor": self.DIST, "since": 7})

	def test_a_403_is_cached_so_the_page_stops_re_asking(self):
		"""Since the scope is optional, "this client has no `catalog:ops`" is
		the STEADY STATE for every tenant that did not buy the scope — a page that
		refreshes itself every 60s would keep firing requests the service is
		certain to refuse, one per viewer, forever. The negative entry expires on
		the same TTL, so granting the scope still takes effect within a minute."""
		forbidden = client.DataServiceForbidden("Data Service returned 403 for /v1/catalog/ops/status")
		with mock.patch.object(client, "get", side_effect=forbidden) as get:
			for _ in range(3):
				with self.assertRaises(client.DataServiceForbidden):
					self._live(remote_catalog.ops_status, self.DIST)
		self.assertEqual(get.call_count, 1)

	def test_other_failures_are_not_cached(self):
		"""Only 403 is a stable fact. An outage must be retried on the next
		refresh — caching it would keep the panel dark after recovery."""
		with mock.patch.object(client, "get",
				side_effect=client.DataServiceUnavailable("unreachable")) as get:
			for _ in range(2):
				with self.assertRaises(client.DataServiceUnavailable):
					self._live(remote_catalog.ops_flags, self.DIST)
		self.assertEqual(get.call_count, 2)

	def test_history_and_flags_cache_and_pass_their_params(self):
		with mock.patch.object(client, "get", return_value={"runs": []}) as get:
			self._live(remote_catalog.ops_history, self.DIST)
			self._live(remote_catalog.ops_history, self.DIST)
		self.assertEqual(get.call_count, 1)
		get.assert_called_with("/v1/catalog/ops/history",
			{"distributor": self.DIST, "limit": 20}, distributor=self.DIST)
		with mock.patch.object(client, "get", return_value={"flags": {}}) as get:
			self._live(remote_catalog.ops_flags, self.DIST)
			self._live(remote_catalog.ops_flags, self.DIST)
		self.assertEqual(get.call_count, 1)
		get.assert_called_with("/v1/catalog/ops/flags", {"distributor": self.DIST}, distributor=self.DIST)


class TestForbiddenIsItsOwnType(unittest.TestCase):
	"""403 must be distinguishable from "service down" WITHOUT string matching:
	`catalog:ops` is opt-in per tenant, so a 403 is a
	provisioning fact the page states plainly, not an outage."""

	def _response(self, status_code):
		response = mock.Mock()
		response.status_code = status_code
		response.json.return_value = {}
		return response

	def _call(self, status_code):
		with mock.patch.object(client, "get_connection",
					return_value=("https://api.example", "cid", "sec")), \
				mock.patch.object(client.requests, "get",
					return_value=self._response(status_code)):
			return client.get("/v1/catalog/ops/status")

	def test_403_raises_forbidden(self):
		with self.assertRaises(client.DataServiceForbidden):
			self._call(403)

	def test_forbidden_is_still_an_unavailable_for_existing_handlers(self):
		self.assertTrue(issubclass(client.DataServiceForbidden, client.DataServiceUnavailable))
		with self.assertRaises(client.DataServiceUnavailable):
			self._call(403)

	def test_other_failures_stay_generic(self):
		for code in (401, 500, 502):
			with self.assertRaises(client.DataServiceUnavailable) as caught:
				self._call(code)
			self.assertNotIsInstance(caught.exception, client.DataServiceForbidden)
