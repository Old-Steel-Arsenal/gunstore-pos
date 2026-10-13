"""Stale-`reservation_error` retry (webhook `_apply_serials_from_woo` + the
`tasks.retry_stuck_serial_reservations` sweep).

A serial rejection is a SNAPSHOT of the moment it was written ("that gun is held
by WOO-ORD-x"). The only thing that ever retried it was the next `order.updated`
webhook — so once the obstacle cleared (the other order cancelled, or swapped to
a different gun) nothing re-ran, and a Woo order already `completed` sends no
further webhook. The row sat in the Pending Order queue forever quoting a reason
that had stopped being true.

The sweep that fixes this is an UNATTENDED WRITER that calls an external store,
so most of what is pinned here are its guards rather than its happy path:

- _apply_serials_from_woo: the webhook's serial loop, now runnable without a
  webhook body; refuses non-pending orders, drop-ship rows (a gun that is not
  ours), and orders that are already dead on Woo.
- retry_stuck_serial_reservations: per-site sync gate, age cut-off with a
  give-up report, batch ceiling, per-order isolation, flag restoration.
- The candidate query itself is exercised AGAINST A REAL DATABASE — it is the
  only thing standing between this sweep and re-pinning a gun onto an order that
  must not have it, so asserting on its text would be worth nothing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"
TASKS = "ffl_woo_sync.woocommerce.tasks"


def _srow(item_code="GLOCK-19", woo_line_id="7", unit_index=0,
		confirmed_serial="", reservation_error="", fulfillment="local"):
	return frappe._dict({
		"item_code": item_code, "woo_line_id": woo_line_id,
		"unit_index": unit_index, "confirmed_serial": confirmed_serial,
		"reservation_error": reservation_error, "fulfillment": fulfillment,
	})


def _order_doc(order_status="Pending Fulfillment", serials=None,
		woo_order_id="1893", name="WOO-ORD-01488"):
	return frappe._dict({
		"name": name, "woo_order_id": woo_order_id,
		"order_status": order_status,
		"serials": [] if serials is None else serials,
	})


def setUpModule():
	# These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
	# the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
	from ffl_core.tests.channel_query_doubles import warm_channel_meta

	warm_channel_meta()


# ---------------------------------------------------------------------------
# _apply_serials_from_woo — the webhook loop, now usable without a webhook
# ---------------------------------------------------------------------------

class TestApplySerialsFromWoo(FrappeTestCase):
	def test_non_pending_order_is_never_re_pinned_and_costs_no_rest_call(self):
		"""A disposed/cancelled order's serials are already out of stock — never
		re-pin one, and don't spend a REST call discovering that."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch, \
			 patch(f"{WEBHOOK}._apply_dealer_serial") as mock_apply:
			changed = _apply_serials_from_woo(
				_order_doc(order_status="Cancelled", serials=[_srow()]))
		self.assertFalse(changed)
		mock_fetch.assert_not_called()
		mock_apply.assert_not_called()

	def test_order_without_serial_rows_is_a_noop(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch:
			self.assertFalse(_apply_serials_from_woo(_order_doc(serials=[])))
		mock_fetch.assert_not_called()

	def test_serials_in_the_webhook_body_are_used_without_a_rest_refetch(self):
		"""The webhook path keeps its fast lane: serials present in the body are
		applied directly."""
		from ffl_woo_sync.woocommerce.webhook import (
			_SERIAL_META_KEY, _apply_serials_from_woo)
		row = _srow(woo_line_id="575")
		line_items = [{"id": 575, "meta_data": [
			{"key": _SERIAL_META_KEY, "value": ["C2322"]}]}]
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch, \
			 patch(f"{WEBHOOK}._apply_dealer_serial", return_value=True) as mock_apply:
			changed = _apply_serials_from_woo(_order_doc(serials=[row]), line_items)
		self.assertTrue(changed)
		mock_fetch.assert_not_called()
		self.assertEqual(mock_apply.call_args[0][2], "C2322")

	def test_no_body_falls_back_to_the_rest_refetch(self):
		"""The sweep has no webhook body at all — the serial Woo still holds is
		read straight from the REST API."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		row = _srow(woo_line_id="575", reservation_error="already reserved (WOO-ORD-01491)")
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status",
				return_value=({"575": ["C2322"]}, "processing")) as mock_fetch, \
			 patch(f"{WEBHOOK}._apply_dealer_serial", return_value=True) as mock_apply:
			changed = _apply_serials_from_woo(_order_doc(serials=[row]))
		self.assertTrue(changed)
		mock_fetch.assert_called_once_with("1893")
		self.assertEqual(mock_apply.call_args[0][2], "C2322")

	def test_order_already_terminal_on_woo_is_skipped_and_reported(self):
		"""POS only learns an order died THROUGH A WEBHOOK, and this sweep exists
		because that webhook may never arrive. So a locally-pending order can be
		long dead on Woo — re-pinning it would reserve a sellable gun to a refunded
		order and delist it, with nothing left to release it."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		row = _srow(woo_line_id="575", reservation_error="already reserved (WOO-ORD-01491)")
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status",
				return_value=({"575": ["C2322"]}, "refunded")), \
			 patch(f"{WEBHOOK}._apply_dealer_serial") as mock_apply, \
			 patch(f"{WEBHOOK}._log_error") as mock_log:
			changed = _apply_serials_from_woo(_order_doc(serials=[row]))
		self.assertFalse(changed)
		mock_apply.assert_not_called()
		mock_log.assert_called_once()  # never silent — staff must go release it

	def test_unreachable_store_does_not_look_like_a_dead_order(self):
		"""A failed REST read returns an EMPTY status. Empty must mean 'unknown',
		never 'terminal' — otherwise a flaky store would silently look like a wave
		of cancellations."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		row = _srow(woo_line_id="575")
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "")), \
			 patch(f"{WEBHOOK}._apply_dealer_serial") as mock_apply, \
			 patch(f"{WEBHOOK}._log_error") as mock_log:
			changed = _apply_serials_from_woo(_order_doc(serials=[row]))
		self.assertFalse(changed)      # nothing to apply — but not because it "died"
		mock_apply.assert_not_called()
		mock_log.assert_not_called()   # no false cancellation alarm

	def test_dropship_row_is_never_re_pinned(self):
		"""A drop-shipped unit ships from the distributor's warehouse: a dealer-typed
		serial there names a gun that is not ours. Re-applying it would reserve OUR
		stock against a unit we never handle."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		row = _srow(woo_line_id="575", fulfillment="direct")
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch, \
			 patch(f"{WEBHOOK}._apply_dealer_serial") as mock_apply:
			changed = _apply_serials_from_woo(_order_doc(serials=[row]))
		self.assertFalse(changed)
		mock_apply.assert_not_called()
		mock_fetch.assert_not_called()  # not even worth a REST call

	def test_row_whose_unit_index_has_no_serial_on_woo_is_left_alone(self):
		"""Woo holds one serial for a two-gun line — unit 1 must not borrow unit 0's."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		rows = [_srow(woo_line_id="575", unit_index=0),
				_srow(woo_line_id="575", unit_index=1)]
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status",
				return_value=({"575": ["C2322"]}, "processing")), \
			 patch(f"{WEBHOOK}._apply_dealer_serial", return_value=True) as mock_apply:
			_apply_serials_from_woo(_order_doc(serials=rows))
		self.assertEqual(mock_apply.call_count, 1)
		self.assertEqual(mock_apply.call_args[0][1], rows[0])

	def test_woo_holding_no_serial_at_all_changes_nothing(self):
		"""Dealer removed the serial on Woo — nothing to apply, no error raised."""
		from ffl_woo_sync.woocommerce.webhook import _apply_serials_from_woo
		with patch(f"{WEBHOOK}._fetch_order_serials_and_status",
				return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_dealer_serial") as mock_apply:
			self.assertFalse(_apply_serials_from_woo(_order_doc(serials=[_srow()])))
		mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# retry_stuck_serial_reservations — the sweep's guards
# ---------------------------------------------------------------------------

def _pinned(n: int):
	"""An order doc whose serial rows already carry `n` confirmed serials."""
	order = MagicMock()
	order.serials = [frappe._dict({"confirmed_serial": f"SN-{i}"}) for i in range(n)]
	return order


class TestRetryStuckSerialReservations(FrappeTestCase):
	"""The sweep is an unattended writer that calls an external store, so nearly
	all of these pin its GUARDS rather than its happy path."""

	def setUp(self):
		super().setUp()
		from ffl_woo_sync.woocommerce.tasks import _AGED_ALERT_KEY
		frappe.cache().delete_value(_AGED_ALERT_KEY)

	def _rows(self, name="WOO-ORD-01488", site="retail", woo_order_id="4503"):
		return [{"name": name, "site": site, "woo_order_id": woo_order_id}]

	def _sql(self, stuck=None, aged=None):
		"""frappe.db.sql runs TWICE — fresh candidates, then the aged-out set.
		They differ by the HAVING direction, which is what we key on here."""
		def _run(query, *args, **kwargs):
			return list(aged or []) if "MAX(s.modified) <" in query else list(stuck or [])
		return _run

	def test_nothing_stuck_costs_nothing(self):
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		with patch("frappe.db.sql", side_effect=self._sql()), \
			 patch("frappe.get_doc") as mock_get, \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch:
			out = retry_stuck_serial_reservations()
		mock_get.assert_not_called()
		mock_fetch.assert_not_called()
		self.assertEqual(out["checked"], 0)

	def test_cleared_conflict_pins_the_serial_and_saves(self):
		"""The 2026-08-07 case: C2322 was released hours ago — the retry re-applies
		it and the order leaves 'awaiting serial'."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		order = _pinned(0)

		def _apply(o, fetched=None):
			o.serials = [frappe._dict({"confirmed_serial": "C2322"})]
			return True

		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=order), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", side_effect=_apply):
			out = retry_stuck_serial_reservations()
		order.save.assert_called_once()
		self.assertEqual(out["pinned"], 1)
		self.assertEqual(out["re_rejected"], 0)

	def test_a_fresh_rejection_is_not_counted_as_a_fix(self):
		"""_apply_serials_from_woo returns True for a NEW rejection too. Counting
		that as success would tell whoever ran this by hand that it was fixed,
		when in fact nothing got pinned and a new note went out."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=True):
			out = retry_stuck_serial_reservations()
		self.assertEqual(out["pinned"], 0)
		self.assertEqual(out["re_rejected"], 1)

	def test_conflict_that_still_stands_saves_nothing(self):
		"""Re-rejection is idempotent upstream, so the retry must not dirty the
		order — no save means no Version row, no needless write every 2 hours."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		order = _pinned(0)
		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=order), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			out = retry_stuck_serial_reservations()
		order.save.assert_not_called()
		self.assertEqual(out["pinned"], 0)

	def test_the_rest_read_happens_before_any_row_lock_is_taken(self):
		"""THE ordering that matters. That REST call can burn ~90s against a slow
		store; holding an order-row X lock across it stalls a concurrent webhook,
		and a stalled webhook is a LOST webhook — woo_event returns HTTP 200 even
		when its handler fails, so WooCommerce never retries it."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		calls: list[str] = []

		def _fetch(_woo_order_id):
			calls.append("rest")
			return ({}, "processing")

		def _get_value(*_a, **kwargs):
			if kwargs.get("for_update"):
				calls.append("lock")
			return "Pending Fulfillment"

		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", side_effect=_fetch), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			retry_stuck_serial_reservations()
		self.assertEqual(calls, ["rest", "lock"])

	def test_the_status_recheck_takes_a_row_lock(self):
		"""Without for_update the re-check is worthless: a cancel could land
		between the read and the reservation write. Deleting that kwarg must
		fail a test, not slip through."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment") as mock_get_value, \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			retry_stuck_serial_reservations()
		self.assertTrue(mock_get_value.call_args.kwargs.get("for_update"))

	def test_order_cancelled_between_query_and_processing_is_dropped(self):
		"""A cancel that landed while we were on the network must win — otherwise
		the gun ends up reserved to a dead order AND delisted, with nothing left
		to release it."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.db.get_value", return_value="Cancelled"), \
			 patch("frappe.get_doc") as mock_get, \
			 patch(f"{TASKS}.safe_commit") as mock_commit, \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo") as mock_apply:
			out = retry_stuck_serial_reservations()
		mock_get.assert_not_called()
		mock_apply.assert_not_called()
		self.assertEqual(out["not_pending"], 1)
		# The lock we just took must be released before moving to the next order.
		mock_commit.assert_called()

	def test_site_with_sync_switched_off_is_never_called(self):
		"""auto_sync_enabled is the master switch every other outbound task honours.
		Ignoring it would hammer a store the owner deliberately disconnected — and
		the REST helper logs an Error Log on every config failure, so it would also
		spam the log forever."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		with patch("frappe.db.sql", side_effect=self._sql(self._rows())), \
			 patch("frappe.get_doc") as mock_get, \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=False), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch:
			out = retry_stuck_serial_reservations()
		mock_get.assert_not_called()
		mock_fetch.assert_not_called()  # not even a REST call to a disabled store
		self.assertEqual(out["site_disabled"], 1)

	def test_the_site_gate_is_read_once_per_site_not_once_per_order(self):
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		rows = [{"name": f"WOO-ORD-{i}", "site": "retail", "woo_order_id": str(i)}
				for i in range(4)]
		with patch("frappe.db.sql", side_effect=self._sql(rows)), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True) as mock_gate, \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			retry_stuck_serial_reservations()
		mock_gate.assert_called_once_with("retail")

	def test_aged_out_orders_are_reported_and_never_retried(self):
		"""A rejection nothing can clear must not be retried to the end of time —
		but going quiet about it is worse. Report, then stop."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		aged = [{"name": "WOO-ORD-OLD"}]
		with patch("frappe.db.sql", side_effect=self._sql(stuck=[], aged=aged)), \
			 patch("frappe.get_doc") as mock_get, \
			 patch(f"{TASKS}.safe_commit"), \
			 patch("frappe.log_error") as mock_log, \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status") as mock_fetch:
			out = retry_stuck_serial_reservations()
		mock_fetch.assert_not_called()   # aged-out orders cost no REST call
		mock_get.assert_not_called()
		self.assertEqual(out["aged_out"], 1)
		mock_log.assert_called_once()
		self.assertIn("WOO-ORD-OLD", mock_log.call_args.kwargs["message"])

	def test_the_give_up_report_is_throttled_across_runs(self):
		"""Aged-out rows stay aged out BY CONSTRUCTION, so an unthrottled report is
		twelve identical Error Logs a day for the rest of time — the very
		"permanent failure becomes permanent noise" this cut-off exists to stop.
		The name says 'once'; this is what makes that true across runs."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		aged = [{"name": "WOO-ORD-OLD"}]
		with patch("frappe.db.sql", side_effect=self._sql(stuck=[], aged=aged)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch("frappe.log_error") as mock_log, \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status"):
			retry_stuck_serial_reservations()
			retry_stuck_serial_reservations()
			retry_stuck_serial_reservations()
		self.assertEqual(mock_log.call_count, 1)

	def test_the_order_is_read_with_its_own_store_and_the_loop_guard_set(self):
		"""The REST refetch reads frappe.flags.woo_sync_site, and the save must not
		echo back to Woo."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		seen = {}

		def _capture(_woo_order_id):
			seen["site"] = frappe.flags.get("woo_sync_site")
			seen["inbound"] = frappe.flags.get("woo_sync_inbound")
			return ({}, "processing")

		with patch("frappe.db.sql",
				side_effect=self._sql(self._rows(name="WOO-ORD-R1", site="retail"))), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", side_effect=_capture), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			retry_stuck_serial_reservations()
		self.assertEqual(seen["site"], "retail")
		self.assertTrue(seen["inbound"])

	def test_both_flags_are_restored_afterwards(self):
		"""The sweep shares its worker with everything else — a leaked flag would
		silently mis-route the NEXT job's Woo calls."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		frappe.flags.woo_sync_site = None
		frappe.flags.woo_sync_inbound = None
		with patch("frappe.db.sql", side_effect=self._sql(self._rows(site="retail"))), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			retry_stuck_serial_reservations()
		self.assertIsNone(frappe.flags.get("woo_sync_site"))
		self.assertIsNone(frappe.flags.get("woo_sync_inbound"))

	def test_one_failing_order_does_not_strand_the_rest_of_the_batch(self):
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		rows = [{"name": "WOO-ORD-BAD", "site": "retail", "woo_order_id": "1"},
				{"name": "WOO-ORD-GOOD", "site": "retail", "woo_order_id": "2"}]
		good = _pinned(0)

		def _get_doc(_doctype, name):
			if name == "WOO-ORD-BAD":
				raise RuntimeError("boom")
			return good

		with patch("frappe.db.sql", side_effect=self._sql(rows)), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", side_effect=_get_doc), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch("frappe.db.rollback") as mock_rollback, \
			 patch("frappe.log_error") as mock_log, \
			 patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=True):
			out = retry_stuck_serial_reservations()
		good.save.assert_called_once()
		mock_rollback.assert_called_once()
		mock_log.assert_called_once()
		self.assertEqual(out["failed"], 1)


	def test_a_store_whose_settings_cannot_be_read_does_not_strand_the_batch(self):
		"""The site gate reads that store's Settings and is allowed to raise. One
		unreadable store must not take every remaining order down with it —
		including the ones sitting on a perfectly healthy site."""
		from ffl_woo_sync.woocommerce.tasks import retry_stuck_serial_reservations
		rows = [{"name": "WOO-ORD-X", "site": "other", "woo_order_id": "1"},
				{"name": "WOO-ORD-R", "site": "retail", "woo_order_id": "2"}]

		def _gate(site):
			if site == "other":
				raise RuntimeError("settings unreadable")
			return True

		with patch("frappe.db.sql", side_effect=self._sql(rows)), \
			 patch("frappe.db.get_value", return_value="Pending Fulfillment"), \
			 patch("frappe.get_doc", return_value=_pinned(0)), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch("frappe.db.rollback"), \
			 patch("frappe.log_error"), \
			 patch(f"{TASKS}._site_sync_enabled", side_effect=_gate), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status", return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=False):
			out = retry_stuck_serial_reservations()
		self.assertEqual(out["failed"], 1)   # the unreadable store's order
		self.assertEqual(out["checked"], 1)  # the retail one still got its turn

	def test_the_give_up_report_is_persisted_before_the_gate_shuts(self):
		"""The gate lives in redis and does not roll back; the Error Log insert
		does. Shutting the gate first meant a failure on the first order could roll
		the report away while the gate stayed shut for 24h — that day's give-up
		list gone with nothing to show it ever existed."""
		from ffl_woo_sync.woocommerce.tasks import (
			_AGED_ALERT_KEY, retry_stuck_serial_reservations)
		calls: list[str] = []
		# NB: patching frappe.cache itself is not an option — it is global, and
		# frappe pickles whatever it holds (System Settings etc.), so a MagicMock
		# there blows up unrelated code. Use the real cache and pin the ordering
		# through the two calls that bracket it.
		with patch("frappe.db.sql", side_effect=self._sql(stuck=[], aged=[{"name": "OLD"}])), \
			 patch("frappe.log_error", side_effect=lambda **kw: calls.append("log")), \
			 patch(f"{TASKS}.safe_commit", side_effect=lambda: calls.append("commit")):
			retry_stuck_serial_reservations()
		self.assertEqual(calls, ["log", "commit"])   # report persisted first…
		self.assertTrue(frappe.cache().get_value(_AGED_ALERT_KEY))  # …gate only after


class TestSerialReadIsRowLocked(FrappeTestCase):
	def test_apply_dealer_serial_locks_the_serial_row(self):
		"""Read-then-reserve without a lock lets a concurrent writer take the same
		gun in between, and both orders then believe they hold it. Deleting
		for_update=True must fail a test rather than slip through unnoticed."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", return_value=None) as mock_get, \
			 patch(f"{WEBHOOK}._suggest_instock_serial", return_value=None), \
			 patch(f"{WEBHOOK}._queue_wc_order_note"), \
			 patch(f"{WEBHOOK}._log_error"):
			_apply_dealer_serial(MagicMock(), row, "SN-X")
		self.assertTrue(mock_get.call_args.kwargs.get("for_update"))


# ---------------------------------------------------------------------------
# The candidate query — against a REAL database, not against its own text
# ---------------------------------------------------------------------------

class TestStuckQueryAgainstTheDatabase(FrappeTestCase):
	"""This query is the only thing standing between an unattended writer and
	re-pinning a gun onto an order that must not hold it. Asserting on the SQL
	string would pass just as happily for `WHERE 1=1`, so it is exercised for
	real: five orders go in, exactly one must come out."""

	def _make(self, order_status, reservation_error, confirmed_serial="",
			fulfillment="local"):
		doc = frappe.get_doc({
			"doctype": "Woo Online Order",
			"woo_order_id": f"RS-{frappe.generate_hash(length=8)}",
			"order_status": order_status, "site": "retail",
			"customer_name": "QA Retry",
			"serials": [{
				"woo_line_id": "1", "unit_index": 0,
				"confirmed_serial": confirmed_serial,
				"reservation_error": reservation_error,
				"fulfillment": fulfillment,
			}],
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

	def test_only_genuinely_stuck_local_rows_are_picked_up(self):
		from ffl_woo_sync.woocommerce import tasks
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON

		conflict = "Serial 'C2322' is already reserved for another web order (WOO-ORD-X)"
		want = self._make("Pending Fulfillment", conflict)
		disposed = self._make("Transferred", conflict)
		has_serial = self._make("Pending Fulfillment", conflict, confirmed_serial="C2150")
		dropship = self._make("Pending Fulfillment", conflict, fulfillment="direct")
		out_of_stock = self._make(
			"Pending Fulfillment", f"{_NO_STOCK_REASON} for 'CZ85' to hold stock")
		clean = self._make("Pending Fulfillment", "")

		seen: list[str] = []

		def _capture(order, fetched=None):
			seen.append(order.name)
			return False

		with patch(f"{TASKS}._site_sync_enabled", return_value=True), \
			 patch(f"{TASKS}.safe_commit"), \
			 patch(f"{WEBHOOK}._fetch_order_serials_and_status",
				 return_value=({}, "processing")), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", side_effect=_capture):
			tasks.retry_stuck_serial_reservations()

		self.assertIn(want, seen)
		# Each of these would be a distinct way to hand a gun to the wrong order,
		# or to burn a REST call on something no retry can fix.
		self.assertNotIn(disposed, seen)      # already disposed → guns are gone
		self.assertNotIn(has_serial, seen)    # nothing left to retry
		self.assertNotIn(dropship, seen)      # not our gun to reserve
		self.assertNotIn(out_of_stock, seen)  # no dealer serial on Woo to re-apply
		self.assertNotIn(clean, seen)         # not stuck at all
