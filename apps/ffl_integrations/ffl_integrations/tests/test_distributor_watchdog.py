"""Feed staleness watchdog (covers the case where the relay was cut).

The supply side has no relay to reconcile against any more, so the only thing
standing between a dead RSR feed and overselling is this watchdog: heartbeat
older than FEED_STALE_SECONDS (90 min) → alert, and (optionally, per
distributor) force published stock to 0 via the `distributor_stock_source`
interlock.

Pure verdict matrix here, plus a behavioural pass over the real
`check_feed_staleness` gate with its IO patched — the lesson from review is
that a pure predicate test cannot see a wiring bug, so the gate itself is
exercised (and the DB-backed field reads again in test_distributor_integration).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from ffl_integrations.distributor import tasks
from ffl_integrations.tests.catalog_stub import data_service_configured

NOW = datetime(2026, 7, 22, 12, 0, 0)


class TestFeedStaleVerdict(unittest.TestCase):
	def test_fresh_heartbeat_is_not_stale(self):
		v = tasks.feed_stale_verdict(NOW - timedelta(minutes=10), NOW, 3600)
		self.assertFalse(v["stale"])
		self.assertAlmostEqual(v["age_seconds"], 600, places=0)

	def test_heartbeat_older_than_the_threshold_is_stale(self):
		v = tasks.feed_stale_verdict(NOW - timedelta(minutes=90), NOW, 3600)
		self.assertTrue(v["stale"])

	def test_exactly_at_the_threshold_is_stale(self):
		v = tasks.feed_stale_verdict(NOW - timedelta(seconds=3600), NOW, 3600)
		self.assertTrue(v["stale"])

	def test_never_synced_is_stale_with_its_own_reason(self):
		v = tasks.feed_stale_verdict(None, NOW, 3600)
		self.assertTrue(v["stale"])
		self.assertEqual(v["reason"], "never_synced")
		self.assertIsNone(v["age_seconds"])


class TestThresholdClearsTheHourlyBoundary(unittest.TestCase):
	"""Regression for the 2026-07-30 false alerts: the hourly quantity sync and
	the watchdog share the */15 cron tick, so every hour the heartbeat age reads
	3600s ± milliseconds at the moment the watchdog checks. With the threshold
	AT the interval that is a coin flip; the threshold must clear the interval
	by real margin."""

	def test_the_pin_interval_plus_two_watchdog_ticks(self):
		# Moving FEED_STALE_SECONDS below (hourly feed + 2 × 15-min ticks) must
		# be a deliberate act — it reintroduces the boundary race.
		self.assertGreaterEqual(tasks.FEED_STALE_SECONDS, 60 * 60 + 2 * 15 * 60)

	def test_a_heartbeat_exactly_one_hour_old_is_fresh_at_the_default(self):
		v = tasks.feed_stale_verdict(NOW - timedelta(seconds=3600), NOW)
		self.assertFalse(v["stale"])

	def test_a_heartbeat_past_the_default_threshold_is_stale(self):
		v = tasks.feed_stale_verdict(
			NOW - timedelta(seconds=tasks.FEED_STALE_SECONDS), NOW)
		self.assertTrue(v["stale"])


class _Provider:
	code = "rsr"

	def __init__(self, heartbeat):
		self._hb = heartbeat

	def feed_heartbeat(self):
		return self._hb


class _NoHeartbeatProvider:
	"""A provider that doesn't publish a feed heartbeat at all (e.g. a manual,
	human-ordered distributor). The watchdog must skip it, not crash."""

	code = "manual"


class _ProviderWithContent:
	"""A provider whose CATALOG CONTENT heartbeat ages independently of its
	quantity heartbeat — RSR's content/price ingest runs on its own,
	coarser cadence than the 5-min quantity feed."""

	code = "rsr"

	def __init__(self, quantity_hb, content_hb):
		self._quantity_hb = quantity_hb
		self._content_hb = content_hb

	def feed_heartbeat(self):
		return self._quantity_hb

	def catalog_content_heartbeat(self):
		return self._content_hb


def _run_gate(distributors, provider, now=NOW):
	"""The real `check_feed_staleness` with only its IO patched. Returns a namespace:
	`result`, and the `notify` (notify_admins), `zero` (zero-stock hook) and `log`
	(safe_log_error) mocks.

	`provider` is one provider object for every house, or a callable taking the
	provider code (raise from it to model a provider that cannot be resolved).

	NOTE: patch NARROWLY. `tasks.frappe.get_all` IS `frappe.get_all`, so an
	unconditional stub blinds core's own reads too. Answer only the doctype under
	test and delegate the rest.
	"""
	real_get_all = tasks.frappe.get_all

	def _get_all(doctype=None, *a, **k):
		if doctype == "Distributor":
			return distributors
		return real_get_all(doctype, *a, **k)

	by_code = {"side_effect": provider} if callable(provider) else {"return_value": provider}
	with data_service_configured(), \
			mock.patch.object(tasks.frappe, "get_all", side_effect=_get_all), \
			mock.patch.object(tasks, "_provider_by_code", **by_code), \
			mock.patch.object(tasks, "safe_log_error") as log, \
			mock.patch.object(tasks, "notify_admins") as notify, \
			mock.patch.object(tasks.dropship_hooks, "feed_stale", return_value=False) as zero, \
			mock.patch.object(tasks, "now_datetime", return_value=now):
		result = tasks.check_feed_staleness()
	return SimpleNamespace(result=result, notify=notify, zero=zero, log=log)


class TestCheckFeedStalenessGate(unittest.TestCase):
	"""What the core gate itself decides: who is alerted, how, and what it hands
	the zero-stock hook. Zeroing is the drop-ship layer's, so it is only asserted
	here as the hook call."""

	RSR = [{"name": "RSR", "provider": "rsr"}]

	def test_a_fresh_feed_is_quiet(self):
		run = _run_gate(self.RSR, _Provider(NOW - timedelta(minutes=5)))
		self.assertEqual(run.result["stale"], [])
		run.notify.assert_not_called()
		run.zero.assert_not_called()

	def test_an_aged_feed_alerts_once_and_asks_the_hook_to_zero(self):
		run = _run_gate(self.RSR, _Provider(NOW - timedelta(hours=3)))
		self.assertEqual(run.result["stale"], ["RSR"])
		run.notify.assert_called_once()
		self.assertEqual(run.notify.call_args.kwargs["dedup_key"], "dist_feed_stale_RSR")
		run.zero.assert_called_once_with("RSR", aged=True)

	def test_a_cold_start_alerts_but_tells_the_hook_it_is_not_aged(self):
		run = _run_gate(self.RSR, _Provider(None))
		run.notify.assert_called_once()
		run.zero.assert_called_once_with("RSR", aged=False)

	def test_a_probe_failure_is_stale_and_says_so(self):
		class _Broken:
			def feed_heartbeat(self):
				raise RuntimeError("settings unreadable")

		run = _run_gate(self.RSR, _Broken())
		self.assertEqual(run.result["stale"], ["RSR"])
		self.assertIn("probe failed", run.notify.call_args[0][1])
		self.assertNotIn("never synced", run.notify.call_args[0][1])

	def test_a_house_that_cannot_be_resolved_does_not_stop_the_next_one(self):
		def by_code(code):
			if code == "broken":
				raise RuntimeError("no such provider registered")
			return _Provider(NOW - timedelta(hours=3))

		fleet = [{"name": "BROKEN-DC", "provider": "broken"}, {"name": "STALE-DC", "provider": "rsr"}]
		run = _run_gate(fleet, by_code)
		self.assertEqual(run.result["stale"], ["BROKEN-DC", "STALE-DC"])
		self.assertEqual(run.notify.call_count, 2)

	def test_a_house_without_a_feed_is_skipped(self):
		run = _run_gate([{"name": "MANUAL", "provider": "manual"}], _NoHeartbeatProvider())
		self.assertEqual((run.result["stale"], run.result["skipped"]), ([], ["MANUAL"]))
		run.notify.assert_not_called()

	def test_stale_catalog_content_alerts_without_touching_the_hook(self):
		run = _run_gate(self.RSR, _ProviderWithContent(
			quantity_hb=NOW - timedelta(minutes=5), content_hb=NOW - timedelta(hours=7)))
		self.assertEqual(run.result["stale"], [])
		self.assertEqual(run.notify.call_args.kwargs["dedup_key"], "dist_catalog_stale_RSR")
		run.zero.assert_not_called()

	def test_fresh_catalog_content_is_quiet(self):
		run = _run_gate(self.RSR, _ProviderWithContent(
			quantity_hb=NOW - timedelta(minutes=5), content_hb=NOW - timedelta(hours=1)))
		run.notify.assert_not_called()

	def test_a_broken_content_probe_is_logged_not_alerted(self):
		class _ContentBoom:
			def feed_heartbeat(self):
				return NOW - timedelta(minutes=5)

			def catalog_content_heartbeat(self):
				raise RuntimeError("content endpoint down")

		run = _run_gate(self.RSR, _ContentBoom())
		self.assertEqual(run.result["stale"], [])
		run.notify.assert_not_called()
		run.log.assert_called_once()


class TestColdStartIsNotStale(unittest.TestCase):
	"""A feed that NEVER synced (cold start / fresh install) must not
	trip the zero-stock interlock — only a feed that HAD a heartbeat and then aged
	out may zero published stock. The watchdog still ALERTS on never_synced,
	because "you enabled drop-ship and the feed has never run" is a real ops
	signal; it just must not silently blank the catalogue on day one.
	"""

	def _is_stale(self, heartbeat):
		real_get_value = tasks.frappe.db.get_value

		def _get_value(doctype, *a, **k):
			if doctype == "Distributor":
				return "rsr"
			return real_get_value(doctype, *a, **k)

		with data_service_configured(), \
				mock.patch.object(tasks.frappe.db, "get_value", side_effect=_get_value), \
				mock.patch.object(tasks, "_provider_by_code", return_value=_Provider(heartbeat)), \
				mock.patch.object(tasks, "now_datetime", return_value=NOW), \
				mock.patch.object(tasks, "_stale_cache", return_value={}):
			return tasks.is_feed_stale("RSR")

	def test_cold_start_does_not_zero_stock(self):
		self.assertFalse(self._is_stale(None))

	def test_a_heartbeat_that_aged_out_does_zero_stock(self):
		self.assertTrue(self._is_stale(NOW - timedelta(hours=3)))

	def test_a_fresh_heartbeat_does_not_zero_stock(self):
		self.assertFalse(self._is_stale(NOW - timedelta(minutes=5)))

	def test_the_watchdog_still_alerts_on_a_feed_that_never_synced(self):
		verdict = tasks.feed_stale_verdict(None, NOW)
		self.assertTrue(verdict["stale"])
		self.assertEqual(verdict["reason"], "never_synced")


if __name__ == "__main__":
	unittest.main()
