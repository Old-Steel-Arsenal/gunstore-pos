"""An auction with a bid is promised, not sold — and promised locks every channel.

The first bid is the moment the gun stops being the store's to sell at the
counter or on the web, and it can still come back (bids withdrawn, reserve
unmet). These pin the verdict, the lock and its release, the refusal to end
a bid-on auction from POS, and the fifteen-minute watch that finds bids.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import crosschannel, doc_events, listings, tasks
from ffl_integrations.gunbroker.client import GunBrokerClient
from ffl_integrations.tests import gunbroker_fixtures as fx

ITEM = "16087230"


class TheVerdict(FrappeTestCase):
	def test_a_live_auction_with_a_bid_is_promised_not_sold(self):
		item = fx.item_detail(ITEM, is_fixed_price=False, bids=2)
		self.assertTrue(doc_events.gb_item_bid_locked(item))
		self.assertFalse(doc_events.gb_item_sold(item))

	def test_bids_on_a_fixed_price_listing_still_read_as_sold(self):
		"""The wide net stays for fixed price: nobody has read one back after a
		Buy Now, and a false positive costs one refused sale."""
		item = fx.item_detail(ITEM, is_fixed_price=True, bids=1)
		self.assertTrue(doc_events.gb_item_sold(item))
		self.assertFalse(doc_events.gb_item_bid_locked(item))

	def test_an_ended_auction_with_a_winner_is_sold(self):
		item = fx.item_detail(ITEM, is_fixed_price=False, is_active=False,
			bids=3, has_winning_bid=True)
		self.assertTrue(doc_events.gb_item_sold(item))
		self.assertFalse(doc_events.gb_item_bid_locked(item))

	def test_an_ended_auction_with_bids_but_no_winner_is_neither(self):
		"""Reserve not met: bids were placed and nobody bought. Not sold, and
		nothing to promise any more."""
		item = fx.item_detail(ITEM, is_fixed_price=False, is_active=False, bids=3)
		self.assertFalse(doc_events.gb_item_sold(item))
		self.assertFalse(doc_events.gb_item_bid_locked(item))

	def test_a_live_auction_with_no_bids_is_clear(self):
		item = fx.item_detail(ITEM, is_fixed_price=False, bids=0)
		self.assertFalse(doc_events.gb_item_bid_locked(item))
		self.assertFalse(doc_events.gb_item_sold(item))


class TheCounterAdvisory(FrappeTestCase):
	def test_a_promised_gun_blocks_with_its_own_words(self):
		with patch.object(doc_events, "probe_gb_sale",
				return_value={"state": doc_events.STATE_BID_LOCKED, "item_id": ITEM}):
			answer = crosschannel.presale_probe("SN-1")
		self.assertTrue(answer["blocked"])
		self.assertIn("promised", answer["message"])
		self.assertIn(ITEM, answer["message"])

	def test_the_local_lock_answers_without_a_request(self):
		with patch("frappe.db.get_value", return_value={
					"item_code": "COLT", "gb_item_id": ITEM, "gb_sold_unknown": 0,
					"gb_bid_locked": 1}), \
				patch.object(doc_events, "_skip", return_value=False), \
				patch.object(doc_events, "_probe_item") as probe:
			verdict = doc_events.probe_gb_sale("SN-1")
		self.assertEqual(verdict["state"], doc_events.STATE_BID_LOCKED)
		self.assertTrue(verdict["local"])
		probe.assert_not_called()


class EndingFromPOS(FrappeTestCase):
	def test_an_auction_with_bids_is_not_ended_from_here(self):
		written = {}
		with patch.object(listings, "serial_state",
					return_value={"gb_item_id": ITEM, "gb_bid_locked": 1}), \
				patch("frappe.db.set_value",
					side_effect=lambda _d, _n, f, v=None, **_k: written.update(
						f if isinstance(f, dict) else {f: v})), \
				patch.object(listings, "end_listing_for") as ended:
			result = listings.end_listing("SN-1")
		self.assertEqual(result["skipped"], listings.AUCTION_HAS_BIDS)
		self.assertIn("GunBroker itself", result["message"])
		self.assertIn(ITEM, written["gb_sync_error"])
		ended.assert_not_called()


class _LockCase(FrappeTestCase):
	"""One in-memory Serial No row, the writes landing on it, the exits fired."""

	def setUp(self):
		self.row = {"name": "SN-1", "gb_item_id": ITEM, "gb_listing_type": "Auction",
			"gb_bid_locked": 0, "gb_bid_count": 0, "gb_gone_strikes": 0,
			"gb_gone_first_at": None}
		self.exits: list[dict] = []

		def _set_value(_doctype, _name, field, value=None, **_kw):
			self.row.update(field if isinstance(field, dict) else {field: value})

		for target, kw in (
				("frappe.db.set_value", {"side_effect": _set_value}),):
			patcher = patch(target, **kw); patcher.start(); self.addCleanup(patcher.stop)
		patcher = patch.object(tasks, "fire_serial_channel_exit",
			side_effect=lambda sn, **kw: self.exits.append({"serial": sn, **kw}))
		patcher.start(); self.addCleanup(patcher.stop)


class LockingAndReleasing(_LockCase):
	def test_the_first_bid_locks_and_tells_the_web_store_once(self):
		tasks._lock_for_bids("SN-1", self.row, bids=1, current_bid=900.0, ends_at=None)
		self.assertEqual(self.row["gb_bid_locked"], 1)
		self.assertEqual(self.row["gb_bid_count"], 1)
		self.assertEqual(self.row["gb_current_bid"], 900.0)
		self.assertEqual([e["reason"] for e in self.exits], [crosschannel.REASON_BID])
		# A second bid updates the figures and says nothing new to the web store.
		tasks._lock_for_bids("SN-1", self.row, bids=2, current_bid=950.0, ends_at=None)
		self.assertEqual(self.row["gb_bid_count"], 2)
		self.assertEqual(len(self.exits), 1)

	def test_release_clears_the_lock_and_the_figures(self):
		tasks._lock_for_bids("SN-1", self.row, bids=1, current_bid=900.0, ends_at=None)
		tasks._unlock_bids("SN-1")
		self.assertEqual(self.row["gb_bid_locked"], 0)
		self.assertEqual(self.row["gb_bid_count"], 0)
		self.assertEqual(self.row["gb_current_bid"], 0)

	def test_a_fresh_item_number_starts_unpromised(self):
		"""The reset every new listing gets clears the auction's bid state too:
		a relisted auction starts with no bids."""
		reset = listings._clear_gone_tracking()
		self.assertEqual(reset["gb_bid_locked"], 0)
		self.assertEqual(reset["gb_bid_count"], 0)
		self.assertIsNone(reset["gb_auction_ends_at"])


class TheFifteenMinuteWatch(_LockCase):
	def _watch(self, selling_rows):
		client = SimpleNamespace(iter_items_selling=lambda page_size: iter(selling_rows))
		with patch("frappe.get_all", return_value=[dict(self.row)]), \
				patch.object(tasks._Round, "page_size", new=300):
			return tasks._auction_watch_round(client=client)

	def test_a_bid_seen_in_the_selling_list_locks_the_gun(self):
		summary = self._watch([fx.selling_row(int(ITEM), is_fixed_price=False,
			bid_count=1, current_bid=875.0)])
		self.assertEqual(summary, {"auctions": 1, "locked": 1, "released": 0})
		self.assertEqual(self.row["gb_bid_locked"], 1)
		self.assertEqual(self.row["gb_current_bid"], 875.0)
		self.assertEqual([e["reason"] for e in self.exits], [crosschannel.REASON_BID])

	def test_bids_gone_releases_the_gun(self):
		self.row.update(gb_bid_locked=1, gb_bid_count=1)
		summary = self._watch([fx.selling_row(int(ITEM), is_fixed_price=False, bid_count=0)])
		self.assertEqual(summary["released"], 1)
		self.assertEqual(self.row["gb_bid_locked"], 0)

	def test_a_listing_missing_from_the_live_list_is_left_to_the_sweep(self):
		"""Ended, sold or relisted — the watch cannot tell which from an absence,
		and the hourly sweep reads the item itself."""
		self.row.update(gb_bid_locked=1, gb_bid_count=2)
		summary = self._watch([fx.selling_row(99, is_fixed_price=False, bid_count=0)])
		self.assertEqual(summary, {"auctions": 1, "locked": 0, "released": 0})
		self.assertEqual(self.row["gb_bid_locked"], 1)

	def test_an_unreadable_scan_changes_nothing(self):
		self.row.update(gb_bid_locked=1, gb_bid_count=2)
		summary = self._watch([{"title": "no sku, no id"}])
		self.assertEqual(summary["released"], 0)
		self.assertEqual(self.row["gb_bid_locked"], 1)

	def test_the_gate_is_a_no_op_with_no_auction_live(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True), \
				patch("ffl_core.utils.single_flag", return_value=True), \
				patch("frappe.db.exists", return_value=None), \
				patch("frappe.enqueue") as enqueue:
			self.assertEqual(tasks.gb_auction_watch(), {"enqueued": False})
		enqueue.assert_not_called()

	def test_the_gate_enqueues_one_deduplicated_job(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True), \
				patch("ffl_core.utils.single_flag", return_value=True), \
				patch("frappe.db.exists", return_value="SN-1"), \
				patch("frappe.enqueue") as enqueue:
			self.assertEqual(tasks.gb_auction_watch(), {"enqueued": True})
		self.assertEqual(enqueue.call_args.kwargs["job_id"], tasks.AUCTION_WATCH_JOB_ID)
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
