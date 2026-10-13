"""Posting, repricing and ending a listing — including every way they fail.

The failure paths carry most of the weight here, because the expensive mistakes
in this integration are all failures that were recorded as something other than
what they were:

* a post whose outcome was never confirmed, forgotten — the next click makes a
  SECOND live listing for one physical gun, and GunBroker enforces no
  uniqueness that would stop it;
* an end that did not happen, reported as success — the gun leaves the shelf
  while its listing stays buyable;
* a reprice that GunBroker answered 200 to and ignored — the old price stays on
  a live listing with nothing marked dirty.

Serial No writes go through an in-memory row that `serial_state` reads back
from, so "the flag survived the function returning" is a real assertion rather
than an inspection of an object the code still holds.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import listings, mapping
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerConfig,
	GunBrokerConfigError,
)
from ffl_integrations.tests import gunbroker_fixtures as fx

SKU = "COLT-PYTHON::SN-001"


def _config() -> GunBrokerConfig:
	return GunBrokerConfig(
		username="seller", password="secret", dev_key="sandbox-key",
		base_url="https://api.sandbox.gunbroker.com/v1", sandbox=True, timeout=5)


class _LifecycleCase(FrappeTestCase):
	def setUp(self):
		self.settings = fx.settings_stub()
		self.row = {
			"item_code": "COLT-PYTHON", "status": "Active", "gb_item_id": "",
			"gb_item_history": "", "gb_delisted": 0, "gb_end_pending": 0,
			"gb_push_pending": 0, "gb_push_attempted_at": None, "gb_price_dirty": 1,
			"gb_sold_unknown": 0, "gb_gone_strikes": 0, "gb_gone_first_at": None,
			"gb_reserved": 0, "gb_order": "", "gb_sync_error": "",
		}
		self.events: list[str] = []
		self.alerts: list[str] = []
		self.single_writes: dict = {}

		def _set_value(_doctype, _name, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			self.row.update(updates)

		def _set_single(_doctype, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			self.single_writes.update(updates)

		self._patch("frappe.db.set_value", side_effect=_set_value)
		self._patch("frappe.db.set_single_value", side_effect=_set_single)
		self._patch_object(listings, "_settings", side_effect=lambda: self.settings)
		self._patch_object(listings, "serial_state",
			side_effect=lambda _sn, **_kw: frappe._dict(dict(self.row)))
		self._patch_object(listings, "notify_admins",
			side_effect=lambda subject, message, **kw: self.alerts.append(kw["dedup_key"]))
		self._patch_object(listings, "commit",
			side_effect=lambda: self.events.append("commit"))

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, **kw):
		patcher = patch.object(target, attr, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _client(self, script) -> tuple[GunBrokerClient, fx.RecordingSession]:
		session = fx.RecordingSession(script)
		client = GunBrokerClient(_config(), session=session)
		patcher = patch.object(GunBrokerClient, "cached_token", return_value="tok")
		patcher.start()
		self.addCleanup(patcher.stop)
		patcher2 = patch.object(GunBrokerClient, "_store_token")
		patcher2.start()
		self.addCleanup(patcher2.stop)
		return client, session

	def _allow_push(self, pictures=None):
		self._patch_object(listings, "check_push_guards", return_value=None)
		self._patch_object(mapping, "listing_context", return_value={})
		self._patch_object(mapping, "picture_parts", return_value={
			"parts": pictures if pictures is not None else [("a.jpg", b"x", "image/jpeg")],
			"warnings": []})
		self._patch_object(mapping, "build_listing_payload", return_value={
			"payload": {"Title": "Colt Python", "SKU": SKU}, "warnings": []})


class PostingAListing(_LifecycleCase):
	def test_a_successful_post_records_the_item_number(self):
		self._allow_push()
		client, session = self._client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertTrue(result["ok"])
		self.assertEqual(result["gb_item_id"], "16087230")
		self.assertTrue(result["gb_url"].endswith("/item/16087230"),
			"the notes after Send link straight to the listing")
		self.assertEqual(self.row["gb_item_id"], "16087230")
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertIsNone(self.row["gb_push_attempted_at"])
		self.assertEqual(self.row["gb_sync_error"], "")
		self.assertEqual(session.count("POST", "/Items"), 1)

	def test_the_attempt_is_committed_before_the_request_goes_out(self):
		"""The ordering IS the protection.

		Commit first and a failed request leaves a record that something was
		attempted; commit after and the failure rolls the record back, the form
		looks untouched, and the next click posts a second listing for the same
		gun. Asserted as an order of events rather than as a final state, because
		the final state is identical either way when nothing goes wrong.
		"""
		self._allow_push()

		def _record_post(method, url, kwargs):
			self.events.append("POST /Items")
			return fx.make_response(fx.CREATE_LISTING_OK)

		client, _session = self._client([(("POST", "/Items"), _record_post)])
		listings.push_serial("SN-001", client=client)
		self.assertLess(self.events.index("commit"), self.events.index("POST /Items"),
			f"expected the commit before the post, got {self.events}")

	def test_the_number_is_recorded_from_a_fresh_snapshot(self):
		"""The row was read before a post of several seconds; a sibling job writing
		it meanwhile makes a write from that snapshot MariaDB 1020, and the listing
		then exists with its number lost. A commit must fall between the post and
		the write of the number."""
		self._allow_push()

		def _record_post(method, url, kwargs):
			self.events.append("POST /Items")
			return fx.make_response(fx.CREATE_LISTING_OK)

		client, _session = self._client([(("POST", "/Items"), _record_post)])
		original = frappe.db.set_value.side_effect

		def _set_value(doctype, name, field, value=None, **kw):
			if isinstance(field, dict) and field.get("gb_item_id"):
				self.events.append("write number")
			return original(doctype, name, field, value, **kw)

		frappe.db.set_value.side_effect = _set_value
		listings.push_serial("SN-001", client=client)
		after_post = self.events[self.events.index("POST /Items"):]
		self.assertEqual(after_post[:4], ["POST /Items", "commit", "write number", "commit"],
			"expected a commit between the post and the write, and the number "
			f"committed before anything else happens, got {self.events}")

	def test_a_timeout_leaves_the_attempt_on_record_and_does_not_raise(self):
		"""The single most consequential failure path in this module.

		An eight-photo multipart upload timing out is the ordinary case, not an
		exotic one, and GunBroker has very possibly created the listing. Returning
		a skip dict with the pending flag standing is what stops the next click
		making a second one.
		"""
		self._allow_push()

		def _timeout(method, url, kwargs):
			raise requests.Timeout("read timed out")

		client, _session = self._client([(("POST", "/Items"), _timeout, True)])
		with patch("time.sleep"):
			result = listings.push_serial("SN-001", client=client)

		self.assertFalse(result["ok"])
		self.assertEqual(result["skipped"], listings.PUSH_FAILED)
		# Read the row back, not the object the code was holding.
		self.assertEqual(self.row["gb_push_pending"], 1)
		self.assertIsNotNone(self.row["gb_push_attempted_at"])
		self.assertTrue(self.row["gb_sync_error"])
		self.assertEqual(self.row["gb_item_id"], "")

	def test_a_200_with_no_item_number_keeps_the_attempt_open(self):
		"""GunBroker almost certainly created the listing; we just cannot name
		it. Guessing is not available, so the scan is left to find it."""
		self._allow_push()
		client, _session = self._client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK_NO_ID)),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.PUSH_FAILED)
		self.assertEqual(self.row["gb_push_pending"], 1)
		self.assertEqual(self.row["gb_item_id"], "")

	def test_an_outright_refusal_clears_the_attempt_for_an_immediate_retry(self):
		"""A policy 400 means GunBroker considered the request and said no: there
		is no listing. Holding the gun in limbo for an hour over a title typo
		would be the wrong trade in the other direction."""
		self._allow_push()
		client, _session = self._client([
			(("POST", "/Items"), fx.make_response(fx.TITLE_POLICY_400, status=400)),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.PUSH_REJECTED)
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertIsNone(self.row["gb_push_attempted_at"])
		self.assertIn("policies", self.row["gb_sync_error"])

	def test_photos_beyond_the_first_eight_go_in_their_own_batches(self):
		self._allow_push(pictures=[
			(f"{i}.jpg", b"x", "image/jpeg") for i in range(13)])
		client, session = self._client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
			(("POST", "/Pictures/16087230"), fx.make_response(fx.PICTURE_UPLOAD_OK)),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertTrue(result["ok"])
		create_parts = [p for p in session.calls[0]["files"] if p[0] == "picture"]
		self.assertEqual(len(create_parts), 8)
		append_parts = [p for p in session.calls[1]["files"] if p[0] == "picture"]
		self.assertEqual(len(append_parts), 5)

	def test_a_failed_extra_photo_batch_is_a_warning_not_a_rollback(self):
		"""The listing is live. Reporting it as failed is what produces a
		duplicate, because the next click would post it again — and parking a
		created listing in PUSH_PENDING for an hour is pure self-harm."""
		self._allow_push(pictures=[
			(f"{i}.jpg", b"x", "image/jpeg") for i in range(13)])
		client, _session = self._client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
			(("POST", "/Pictures/16087230"),
				fx.make_response(fx.PICTURE_LIMIT_403, status=403), True),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertTrue(result["ok"])
		self.assertEqual(result["gb_item_id"], "16087230")
		self.assertIn("pictures_partial", result["warnings"])
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertEqual(self.row["gb_item_id"], "16087230")
		self.assertIn("photos", self.row["gb_sync_error"],
			"the operator who has to add the missing photos may be looking at "
			"this form tomorrow, long after the alert scrolled past")

	def test_an_explicit_relist_clears_the_hand_delisted_sentinel(self):
		self._allow_push()
		self.row["gb_delisted"] = 1
		client, _session = self._client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
		])
		listings.push_serial("SN-001", relist=True, client=client)
		self.assertEqual(self.row["gb_delisted"], 0)

	def test_a_refusal_leaves_the_gun_immediately_retryable(self):
		"""A rejected title is fixed in seconds; making
		the operator wait an hour for a sweep would be the wrong trade in the
		other direction. Non-3026 categories may well reject on the first
		attempt, so this is the ordinary first-day experience, not an edge."""
		self._allow_push()
		client, session = self._client([
			(("POST", "/Items"), fx.make_response(fx.TITLE_POLICY_400, status=400)),
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
		])
		first = listings.push_serial("SN-001", client=client)
		self.assertEqual(first["skipped"], listings.PUSH_REJECTED)

		# The operator fixes the title and presses again — no push_in_flight.
		second = listings.push_serial("SN-001", client=client)
		self.assertTrue(second["ok"])
		self.assertEqual(second["gb_item_id"], "16087230")
		self.assertEqual(session.count("POST", "/Items"), 2)

	def test_a_500_produces_exactly_one_post(self):
		"""`POST /Items` is not idempotent and GunBroker enforces no
		uniqueness, so the generic "retry 5xx three times" rule must not reach
		this call: a 5xx can be Cloudflare answering after the listing was
		already created, and three attempts would mean three real listings."""
		self._allow_push()
		client, session = self._client([
			(("POST", "/Items"), fx.make_response(fx.SERVER_ERROR_JSON, status=500), True),
		])
		with patch("time.sleep"):
			result = listings.push_serial("SN-001", client=client)
		self.assertEqual(session.count("POST", "/Items"), 1)
		self.assertEqual(result["skipped"], listings.PUSH_FAILED)
		self.assertEqual(self.row["gb_push_pending"], 1)

	def test_a_new_listing_resets_the_missing_listing_counters(self):
		"""Otherwise a stale `first_at` from weeks ago is already older than the
		age threshold, and the first transient hiccup against the NEW listing
		satisfies it on the spot — marking a live listing as ended."""
		self._allow_push()
		self.row.update({"gb_gone_strikes": 3, "gb_gone_first_at": "2026-07-01 00:00:00"})
		client, _session = self._client([
			(("POST", "/Items"), fx.make_response(fx.CREATE_LISTING_OK)),
		])
		listings.push_serial("SN-001", client=client)
		self.assertEqual(self.row["gb_gone_strikes"], 0)
		self.assertIsNone(self.row["gb_gone_first_at"])


class RecoveringALostResponse(_LifecycleCase):
	def _scan(self, selling_rows, sold_rows=None, **kw):
		script = [(("GET", "/ItemsSelling"), fx.make_response(fx.page(selling_rows)))]
		if sold_rows is not None:
			script.append((("GET", "/ItemsSold"), fx.make_response(fx.page(sold_rows))))
		client, session = self._client(script)
		return listings.recover_item_id("SN-001", client=client, **kw), session

	def test_one_match_is_claimed(self):
		found, _s = self._scan([fx.selling_row(item_id=16087230, sku=SKU)])
		self.assertEqual(found["state"], "claimed")
		self.assertEqual(found["item_id"], "16087230")

	def test_matching_is_on_sku_not_on_the_item_number(self):
		"""GunBroker mints a new item number on every relist and keeps the SKU.
		An item-number match would fail, plausibly, every thirty days."""
		self.row["gb_item_id"] = "16087230"
		found, _s = self._scan([fx.selling_row(item_id=99999999, sku=SKU)])
		self.assertEqual(found["state"], "claimed")
		self.assertEqual(found["item_id"], "99999999")

	def test_a_different_sku_is_not_a_match(self):
		found, _s = self._scan([fx.selling_row(sku="SOMETHING::ELSE")])
		self.assertEqual(found["state"], "miss")

	def test_two_matches_are_reported_rather_than_picked_between(self):
		"""GunBroker permits duplicate listings, so this is a real situation and
		choosing one automatically would hide it from the person who has to go
		and end the other."""
		found, _s = self._scan([
			fx.selling_row(item_id=1, sku=SKU), fx.selling_row(item_id=2, sku=SKU)])
		self.assertEqual(found["state"], "duplicate")
		self.assertEqual(sorted(found["ids"]), ["1", "2"])

	def test_the_sold_list_is_searched_only_when_asked(self):
		"""A listing bought moments ago has already left the selling list. The
		button does not look there; the sweep does."""
		found, session = self._scan([], sold_rows=[fx.selling_row(sku=SKU)],
			scan_sold=True)
		self.assertEqual(found["state"], "miss")
		self.assertTrue(found["sold"])
		self.assertEqual(session.count("GET", "/ItemsSold"), 1)

	def test_rows_with_neither_key_are_undecidable_not_absent(self):
		"""`miss` licenses a caller to clear local state. An answer that could
		not be read must not be allowed to do that."""
		found, _s = self._scan([{"title": "something", "quantity": 1}])
		self.assertEqual(found["state"], "unknown")

	def test_a_matching_row_with_no_item_number_is_undecidable(self):
		"""The major r1 found.

		In the lost-response case the local item number is empty by definition,
		so a matching row that carries none of its own leaves nothing to adopt.
		Claiming it would write an empty `gb_item_id` while clearing the pending
		flag — the gun would read as unlisted, and the next click would post a
		second listing. "Something with this SKU is listed" is not the same fact
		as "this listing is the one".
		"""
		found, _s = self._scan([{"sku": SKU, "title": "no item number here"}])
		self.assertEqual(found["state"], "unknown")

	def test_a_zero_or_junk_item_number_is_not_an_item_number(self):
		for value in (0, "", "  ", "not-a-number", None):
			with self.subTest(value):
				found, _s = self._scan([{"sku": SKU, "itemID": value}])
				self.assertEqual(found["state"], "unknown")

	def test_claiming_refuses_an_empty_id_at_the_write(self):
		"""The single write-back point guards itself, so a future caller cannot
		reintroduce this by taking a different route to it."""
		with self.assertRaises(ValueError):
			listings._claim_item_id("SN-001", "")
		self.assertEqual(self.row["gb_item_id"], "")

	def test_an_empty_account_is_a_genuine_miss(self):
		found, _s = self._scan([])
		self.assertEqual(found["state"], "miss")


class PressingTheButtonTwice(_LifecycleCase):
	def setUp(self):
		super().setUp()
		self._allow_push()
		self.row["gb_push_pending"] = 1

	def test_a_second_press_never_posts_a_second_listing(self):
		client, session = self._client([
			(("GET", "/ItemsSelling"), fx.make_response(fx.page([]))),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.PUSH_IN_FLIGHT)
		self.assertEqual(session.count("POST", "/Items"), 0)
		self.assertEqual(self.row["gb_push_pending"], 1,
			"the attempt stays on record — only the sweep may write it off")

	def test_a_second_press_adopts_the_listing_the_first_one_made(self):
		"""Returns ok, not a skip: the gun genuinely IS listed and the operator
		needs the item number. The warning is what stops them reading it as proof
		this click created it."""
		client, session = self._client([
			(("GET", "/ItemsSelling"),
				fx.make_response(fx.page([fx.selling_row(item_id=16087230, sku=SKU)]))),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertTrue(result["ok"])
		self.assertEqual(result["gb_item_id"], "16087230")
		self.assertEqual(result["warnings"], ["claimed_existing"])
		self.assertEqual(self.row["gb_item_id"], "16087230")
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertEqual(session.count("POST", "/Items"), 0)

	def test_duplicates_are_refused_and_alerted(self):
		client, _session = self._client([
			(("GET", "/ItemsSelling"), fx.make_response(fx.page([
				fx.selling_row(item_id=1, sku=SKU),
				fx.selling_row(item_id=2, sku=SKU)]))),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.DUPLICATE_LISTING)
		self.assertIn("gb_dup_listing_Serial No:SN-001", self.alerts)
		self.assertIn("1", self.row["gb_sync_error"])
		self.assertIn("2", self.row["gb_sync_error"])

	def test_an_unreadable_scan_changes_nothing_and_alerts(self):
		client, _session = self._client([
			(("GET", "/ItemsSelling"),
				fx.make_response(fx.page([{"title": "no keys here"}]))),
		])
		result = listings.push_serial("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.PUSH_IN_FLIGHT)
		self.assertEqual(self.row["gb_push_pending"], 1)
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertIn("gb_scan_unusable_SN-001", self.alerts)

	def test_an_unreachable_gunbroker_is_not_taken_as_permission_to_post(self):
		client, session = self._client([
			(("GET", "/ItemsSelling"),
				fx.make_response(fx.SERVER_ERROR_JSON, status=500), True),
		])
		with patch("time.sleep"):
			result = listings.push_serial("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.PUSH_IN_FLIGHT)
		self.assertEqual(session.count("POST", "/Items"), 0)


class Repricing(_LifecycleCase):
	def setUp(self):
		super().setUp()
		self.row["gb_item_id"] = "16087230"
		self._patch_object(listings, "effective_serial_price", return_value=1150.0)

	def test_the_price_is_confirmed_by_reading_the_listing_back(self):
		client, session = self._client([
			(("PUT", "/Items/16087230"), fx.make_response({"userMessage": "updated"})),
			(("GET", "/Items/16087230"),
				fx.make_response(fx.item_detail(fixed_price=1150.0))),
		])
		result = listings.update_price("SN-001", client=client)
		self.assertTrue(result["ok"])
		self.assertTrue(result["verified"])
		self.assertEqual(self.row["gb_price_dirty"], 0)
		self.assertEqual(session.count("GET", "/Items/16087230"), 1)

	def test_an_auction_is_never_repriced(self):
		"""The bidders set the price. No PUT goes out, and the dirty flag stays
		as a record that the POS price and the listing now disagree."""
		self.row["gb_listing_type"] = "Auction"
		client, session = self._client([])
		result = listings.update_price("SN-001", client=client)
		self.assertEqual(result["skipped"], listings.AUCTION_PRICE_LOCKED)
		self.assertEqual(session.count("PUT", "/Items/16087230"), 0)
		self.assertEqual(self.row["gb_price_dirty"], 1)

	def test_a_reprice_never_sends_a_paid_promotion(self):
		"""GunBroker takes none of them on an update, and sending one on a
		gun recorded as promoted would be a request for a charge it cannot honour."""
		self.row.update(gb_featured=1, gb_showcase=1, gb_sponsored_onsite=1, gb_view_counter=1)
		client, session = self._client([
			(("PUT", "/Items/16087230"), fx.make_response({"userMessage": "updated"})),
			(("GET", "/Items/16087230"),
				fx.make_response(fx.item_detail(fixed_price=1150.0))),
		])
		with patch.object(listings, "listing_title", return_value="Colt Python, boxed"):
			result = listings.update_price("SN-001", client=client)
		self.assertTrue(result["ok"])
		sent = session.calls[0]["json"]
		self.assertEqual(set(sent), {"FixedPrice", "Title"})
		self.assertNotIn("PremiumFeatures", sent)

	def test_a_title_of_its_own_rides_along_with_the_price(self):
		client, session = self._client([
			(("PUT", "/Items/16087230"), fx.make_response({"userMessage": "updated"})),
			(("GET", "/Items/16087230"),
				fx.make_response(fx.item_detail(fixed_price=1150.0))),
		])
		with patch.object(listings, "listing_title", return_value="Colt Python, boxed"):
			result = listings.update_price("SN-001", client=client)
		self.assertTrue(result["ok"])
		sent = session.calls[0]["json"]
		self.assertEqual(sent["FixedPrice"], 1150.0)
		self.assertEqual(sent["Title"], "Colt Python, boxed")

	def test_a_200_that_did_not_take_is_a_failure(self):
		"""This API answers 200 to updates it discards. Trusting the status would
		clear the dirty flag while the old price stayed on a live listing, and
		nothing would ever try again."""
		client, _session = self._client([
			(("PUT", "/Items/16087230"), fx.make_response({"userMessage": "updated"})),
			(("GET", "/Items/16087230"),
				fx.make_response(fx.item_detail(fixed_price=9999.99))),
		])
		result = listings.update_price("SN-001", client=client)
		self.assertFalse(result["ok"])
		self.assertEqual(self.row["gb_price_dirty"], 1,
			"still dirty, so the next sweep tries again")
		self.assertTrue(self.row["gb_sync_error"])

	def test_the_price_is_read_at_send_time_not_passed_in(self):
		"""Two edits in quick succession must publish the second one, not
		whichever job happened to win."""
		captured = {}

		def _capture(method, url, kwargs):
			captured.update(kwargs.get("json") or {})
			return fx.make_response({"userMessage": "updated"})

		client, _session = self._client([
			(("PUT", "/Items/16087230"), _capture),
			(("GET", "/Items/16087230"),
				fx.make_response(fx.item_detail(fixed_price=1150.0))),
		])
		listings.update_price("SN-001", client=client)
		self.assertEqual(captured, {"FixedPrice": 1150.0})

	def test_the_row_lock_is_not_held_across_the_reprice_calls(self):
		"""T1 avoided this lock and T2 put it straight back.

		Same row, same lock the web store's `_serial_availability` takes, and two
		calls each carrying a 30-second timeout on top of a retry ladder. Held
		across them, a checkout touching this gun waits on MariaDB until
		innodb_lock_wait_timeout and gets a database error rather than a refusal
		it can explain. Once the price doc_event is wired up this runs on every
		edit, so it is not a corner.
		"""
		events: list[str] = []
		real_state = listings.serial_state

		def _record(serial_no, **kw):
			events.append("lock" if kw.get("for_update") else "read")
			return frappe._dict(dict(self.row))

		def _http(method, url, kwargs):
			events.append(f"http {method}")
			return (fx.make_response({"userMessage": "updated"}) if method == "PUT"
				else fx.make_response(fx.item_detail(fixed_price=1150.0)))

		client, _session = self._client([
			(("PUT", "/Items/16087230"), _http, True),
			(("GET", "/Items/16087230"), _http, True),
		])
		with patch.object(listings, "serial_state", side_effect=_record), \
				patch.object(listings, "commit",
					side_effect=lambda: events.append("commit")):
			result = listings.update_price("SN-001", client=client)

		self.assertTrue(result["ok"])
		self.assertTrue(callable(real_state))
		# A lock must be released — committed — before the next HTTP call, never
		# held across one.
		for index, event in enumerate(events):
			if event != "lock":
				continue
			following = events[index + 1:]
			next_http = next((i for i, e in enumerate(following) if "http" in e), None)
			next_commit = next((i for i, e in enumerate(following) if e == "commit"), None)
			if next_http is None:
				continue
			self.assertIsNotNone(next_commit,
				f"a lock was taken and never released: {events}")
			self.assertLess(next_commit, next_http,
				f"an HTTP call ran while the row lock was held: {events}")

	def test_a_listing_that_changed_while_in_flight_is_not_marked_clean(self):
		"""GunBroker relists on its own every thirty days. Clearing the dirty
		flag against the number we started with would record "GunBroker has the
		current price" about a listing that no longer exists, and the price would
		never reach the one that replaced it."""
		row_snapshots = [dict(self.row), dict(self.row, gb_item_id="99999999")]

		def _state(serial_no, **kw):
			return frappe._dict(row_snapshots.pop(0) if row_snapshots
				else dict(self.row))

		client, _session = self._client([
			(("PUT", "/Items/16087230"), fx.make_response({"userMessage": "updated"})),
			(("GET", "/Items/16087230"),
				fx.make_response(fx.item_detail(fixed_price=1150.0))),
		])
		with patch.object(listings, "serial_state", side_effect=_state):
			result = listings.update_price("SN-001", client=client)
		self.assertFalse(result["ok"])
		self.assertEqual(result["skipped"], listings.REPRICE_UNCONFIRMED)
		self.assertEqual(self.row["gb_price_dirty"], 1)

	def test_a_hand_ended_listing_reports_that_it_was_ended(self):
		"""Reason names are the API the form, the MCP tools and the tests match
		on, so one that says the opposite of what happened is a defect even
		though the message beside it is right."""
		self.row["gb_item_id"] = ""
		self.row["gb_delisted"] = 1
		result = listings.update_price("SN-001", client=None)
		self.assertEqual(result["skipped"], listings.DELISTED)

	def test_an_unlisted_gun_has_nothing_to_reprice(self):
		self.row["gb_item_id"] = ""
		result = listings.update_price("SN-001", client=None)
		self.assertFalse(result["ok"])


class EndingAListing(_LifecycleCase):
	def setUp(self):
		super().setUp()
		self.row["gb_item_id"] = "16087239"

	def test_success_needs_both_the_delete_and_the_read_back(self):
		client, session = self._client([
			(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
			(("GET", "/Items/16087239"),
				fx.make_response(fx.item_detail("16087239", is_active=False))),
		])
		result = listings.end_listing("SN-001", client=client)
		self.assertTrue(result["confirmed"])
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1)
		self.assertEqual(self.row["gb_end_pending"], 0)
		self.assertIn("16087239", self.row["gb_item_history"])
		self.assertEqual(session.count("PUT"), 0,
			"there is no PUT that ends a listing — the one that looks like it "
			"answers 200 and does nothing")

	def test_a_listing_gunbroker_had_already_relisted_is_followed(self):
		"""Shared with the item-level end, and correct for a gun too: whatever
		put a replacement up, POS has just decided this gun comes off the market
		— so the replacement comes down with it. Reading back inactive and
		stopping there would leave a live listing for a gun nothing is watching.
		"""
		client, _session = self._client([
			(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
			(("GET", "/Items/16087239"), fx.make_response(fx.item_detail(
				"16087239", is_active=False, relisted_as=16087299))),
			(("DELETE", "/Items/16087299"),
				fx.make_response(fx.delete_ok("16087299"))),
			(("GET", "/Items/16087299"),
				fx.make_response(fx.item_detail("16087299", is_active=False))),
		])
		result = listings.end_listing("SN-001", client=client)
		self.assertTrue(result["confirmed"])
		self.assertEqual(result["gb_item_id"], "16087299")
		self.assertEqual(self.row["gb_item_id"], "")
		self.assertEqual(self.row["gb_delisted"], 1)
		for number in ("16087239", "16087299"):
			self.assertIn(number, self.row["gb_item_history"])

	def test_a_delete_that_did_not_take_is_not_success(self):
		client, _session = self._client([
			(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
			(("GET", "/Items/16087239"),
				fx.make_response(fx.item_detail("16087239", is_active=True))),
		])
		result = listings.end_listing("SN-001", client=client)
		self.assertFalse(result["confirmed"])
		self.assertEqual(self.row["gb_end_pending"], 1)
		self.assertEqual(self.row["gb_item_id"], "16087239")
		self.assertEqual(self.row["gb_delisted"], 0)

	def test_the_listing_still_appearing_in_the_selling_list_proves_nothing(self):
		"""That list lags by minutes. An ended listing sits in it for a while
		afterwards, and treating its presence as evidence would roll back a
		confirmed end and send a second delete every sweep."""
		client, _session = self._client([
			(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
			(("GET", "/Items/16087239"),
				fx.make_response(fx.item_detail("16087239", is_active=False))),
			(("GET", "/ItemsSelling"), fx.make_response(
				fx.page([fx.selling_row(item_id=16087239, sku=SKU)]))),
		])
		result = listings.end_listing("SN-001", client=client)
		self.assertTrue(result["confirmed"])
		self.assertEqual(self.row["gb_item_id"], "")

	def test_a_403_stands_the_setting_down_and_says_so_once(self):
		"""403 is a withdrawn authorisation, which no retry fixes. Standing the
		setting down means later guns raise flags for a human instead of piling
		up failed calls nobody reads."""
		client, _session = self._client([
			(("DELETE", "/Items/16087239"),
				fx.make_response(fx.DELETE_FORBIDDEN_403, status=403)),
		])
		result = listings.end_listing("SN-001", client=client)
		self.assertTrue(result["pending_manual"])
		self.assertEqual(self.single_writes.get("end_strategy"), "Manual Only")
		self.assertEqual(self.alerts.count("gb_delete_unauthorized"), 1)
		self.assertEqual(self.row["gb_end_pending"], 1)

	def test_a_server_error_does_not_stand_the_setting_down(self):
		"""A timeout is not a permissions change. Downgrading on one would take
		the whole shop off automatic ending over a blip."""
		client, _session = self._client([
			(("DELETE", "/Items/16087239"),
				fx.make_response(fx.SERVER_ERROR_JSON, status=500), True),
		])
		with patch("time.sleep"):
			result = listings.end_listing("SN-001", client=client)
		self.assertTrue(result["pending_manual"])
		self.assertNotIn("end_strategy", self.single_writes)
		self.assertEqual(self.row["gb_end_pending"], 1)

	def test_the_fallback_strategy_sends_nothing_and_raises_a_flag(self):
		self.settings = fx.settings_stub(end_strategy="Manual Only")
		client, session = self._client([])
		result = listings.end_listing("SN-001", client=client)
		self.assertEqual(result["strategy"], "manual_only")
		self.assertEqual(len(session.calls), 0)
		self.assertEqual(self.row["gb_end_pending"], 1)
		self.assertIn("gb_end_Serial No:SN-001", self.alerts)

	def test_the_alert_says_the_listing_can_still_be_bought(self):
		"""The whole content of that message. An unended listing on a gun that
		has left the shelf is a sale waiting to happen that cannot be filled."""
		self.settings = fx.settings_stub(end_strategy="Manual Only")
		captured = {}

		def _capture(subject, message, **kw):
			captured["message"] = message
			self.alerts.append(kw["dedup_key"])

		with patch.object(listings, "notify_admins", side_effect=_capture):
			listings.end_listing("SN-001", client=None)
		self.assertIn("can still be bought", captured["message"])

	def test_every_unfinished_outcome_hands_back_the_same_actionable_shape(self):
		"""The Manual Only setting, a failed call and a read-back that still says
		active are three different stories in the log and ONE situation at the
		counter: the gun is off the shelf and its listing is still buyable. So
		they return the same keys, and the button can show one dialog."""
		cases = {
			"manual_only_setting": [],
			"delete_failed": [
				(("DELETE", "/Items/16087239"),
					fx.make_response(fx.SERVER_ERROR_JSON, status=500), True)],
			"still_active_on_readback": [
				(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
				(("GET", "/Items/16087239"),
					fx.make_response(fx.item_detail("16087239", is_active=True)))],
		}
		for name, script in cases.items():
			with self.subTest(name):
				self.row["gb_item_id"] = "16087239"
				self.row["gb_end_pending"] = 0
				self.settings = fx.settings_stub(
					end_strategy="Manual Only" if not script else "Delete Endpoint")
				client, _session = self._client(script)
				with patch("time.sleep"):
					result = listings.end_listing("SN-001", client=client)
				self.assertIs(result["confirmed"], False)
				self.assertIs(result["pending_manual"], True)
				self.assertIn("16087239", result["gb_url"])
				self.assertIn("can still be bought", result["message"])
				self.assertEqual(self.row["gb_end_pending"], 1)

	def test_the_strategy_says_whether_anything_will_retry(self):
		"""The one thing the three cases genuinely differ on. Telling somebody
		"the system will keep trying" under Manual Only would be a plain lie —
		nothing happens there until a person acts."""
		self.settings = fx.settings_stub(end_strategy="Manual Only")
		manual = listings.end_listing("SN-001", client=None)
		self.assertEqual(manual["strategy"], "manual_only")
		self.assertNotIn("downgraded", manual)

		self.row["gb_item_id"] = "16087239"
		self.settings = fx.settings_stub(end_strategy="Delete Endpoint")
		client, _session = self._client([
			(("DELETE", "/Items/16087239"),
				fx.make_response(fx.SERVER_ERROR_JSON, status=500), True)])
		with patch("time.sleep"):
			auto = listings.end_listing("SN-001", client=client)
		self.assertEqual(auto["strategy"], "delete_endpoint")

	def test_a_withdrawn_permission_says_the_setting_was_changed(self):
		"""Otherwise the shop discovers the strategy moved only when somebody
		next opens the Settings form and wonders who changed it."""
		client, _session = self._client([
			(("DELETE", "/Items/16087239"),
				fx.make_response(fx.DELETE_FORBIDDEN_403, status=403)),
		])
		result = listings.end_listing("SN-001", client=client)
		self.assertIs(result["downgraded"], True)
		self.assertEqual(result["strategy"], "manual_only")
		self.assertIn("gb_url", result)

	def test_a_switched_off_integration_never_promises_a_retry(self):
		"""The hourly sweep is gated on the same switch that is off, so
		`delete_endpoint` here would have the form tell the operator to wait for
		something that is never going to run."""
		client, _session = self._client([])
		with patch.object(GunBrokerClient, "from_settings",
				side_effect=GunBrokerConfigError(
					"Set GunBroker Settings › Seller name to the name your DevKey is registered under.")):
			result = listings.end_listing("SN-001", client=None)
		self.assertEqual(result["strategy"], "manual_only")
		self.assertIs(result["pending_manual"], True)
		# The alert quotes the actual config error, not a generic "switched off".
		self.assertIn("Seller name", result["detail"])
		self.assertNotIn("switched off", result["detail"])
		self.assertEqual(len(client.session.calls), 0)

	def test_the_integration_going_off_mid_call_reports_the_same_way(self):
		"""There are two places the integration can turn out to be off: building
		the client, and the read-back that confirms the delete. They are the same
		situation to the operator and must answer the same way — the second one
		had no test."""
		client, session = self._client([
			(("DELETE", "/Items/16087239"), fx.make_response(fx.delete_ok())),
		])
		with patch.object(client, "get_item",
				side_effect=GunBrokerConfigError("GunBroker integration is off")):
			result = listings.end_listing("SN-001", client=client)
		self.assertEqual(result["strategy"], "manual_only")
		self.assertIs(result["pending_manual"], True)
		self.assertIn("integration is off", result["detail"])
		self.assertEqual(self.row["gb_end_pending"], 1)
		self.assertEqual(session.count("DELETE", "/Items/16087239"), 1)

	def test_the_listing_link_follows_the_environment(self):
		"""A sandbox link sends staff to a listing that does not exist on the
		live site, and a live link during testing sends them somewhere they must
		not touch."""
		with patch.object(listings, "sandbox_mode", return_value=True):
			self.assertIn("sandbox.gunbroker.com", listings.gb_item_url("1"))
		with patch.object(listings, "sandbox_mode", return_value=False):
			self.assertIn("www.gunbroker.com", listings.gb_item_url("1"))

	def test_an_unlisted_gun_has_nothing_to_end(self):
		self.row["gb_item_id"] = ""
		result = listings.end_listing("SN-001", client=None)
		self.assertEqual(result["skipped"], "unlisted")


class DerivedState(FrappeTestCase):
	"""State is derived from the flags, never stored. A status column would be a
	second copy of the same facts, and the two would drift the first time a job
	died between writing one and the other."""

	def _state(self, **row):
		base = {"gb_item_id": "", "gb_delisted": 0, "gb_end_pending": 0,
			"gb_push_pending": 0, "gb_sold_unknown": 0, "gb_reserved": 0,
			"gb_order": ""}
		base.update(row)
		return listings.listing_state(base)

	def test_every_state_is_reachable(self):
		self.assertEqual(self._state(), "unlisted")
		self.assertEqual(self._state(gb_push_pending=1), "push_pending")
		self.assertEqual(self._state(gb_item_id="1"), "listed")
		self.assertEqual(self._state(gb_item_id="1", gb_end_pending=1), "end_pending")
		self.assertEqual(self._state(gb_reserved=1, gb_order="GB-ORD-00001"), "sold")
		self.assertEqual(self._state(gb_sold_unknown=1), "sold_unknown")
		self.assertEqual(self._state(gb_delisted=1), "ended")

	def test_possibly_sold_outranks_everything(self):
		"""It is the fail-closed flag: whatever else is true, a gun that may
		already be somebody else's must not read as available."""
		self.assertEqual(
			self._state(gb_item_id="1", gb_sold_unknown=1), "sold_unknown")

	def test_an_ended_listing_is_not_unlisted(self):
		"""`unlisted` means a machine may list it. `ended` means a person took it
		down and only a person puts it back."""
		self.assertEqual(self._state(gb_delisted=1), "ended")


class ItemHistory(FrappeTestCase):
	def test_the_most_recent_twenty_are_kept(self):
		"""GunBroker relists every thirty days under a new number, so this grows
		on its own."""
		history = ""
		for i in range(30):
			history = listings._push_history(history, str(i))
		ids = history.split(",")
		self.assertEqual(len(ids), listings.ITEM_HISTORY_LIMIT)
		self.assertEqual(ids[-1], "29")
		self.assertEqual(ids[0], "10")

	def test_the_same_number_is_not_appended_twice(self):
		history = listings._push_history("1,2", "2")
		self.assertEqual(history, "1,2")


class ConnectionTest(_LifecycleCase):
	def test_it_reports_which_gunbroker_it_reached(self):
		client, _session = self._client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
			(("GET", "/Users/AccountInfo"), fx.make_response(fx.ACCOUNT_INFO_OK)),
			(("GET", "/Listing/Defaults"), fx.make_response(fx.LISTING_DEFAULTS_OK)),
		])
		with patch.object(GunBrokerClient, "from_settings", return_value=client):
			result = listings.test_connection()
		self.assertTrue(result["ok"])
		self.assertEqual(result["account"], "store-sandbox")
		self.assertIn("sandbox", result["base_url"])

	def test_the_unapproved_caller_403_says_what_it_actually_means(self):
		"""The message GunBroker sends blames the DevKey; the header is at fault.

		"Please register at …/DevKey/Create" is what came back on a DevKey that
		was registered, approved, and mid-scrape from another service at that
		moment — this store's User-Agent simply did not carry the name the key
		was registered under. Reporting that sentence alone sends the reader to
		create a second key.
		"""
		client, _session = self._client([
			(("GET", "/GunBrokerTime"), fx.make_response(
				"Unapproved User Parameter\nPlease register at:\n"
				"api.gunbroker.com/User/DevKey/Create",
				status=403, content_type="text/plain")),
		])
		with patch.object(GunBrokerClient, "from_settings", return_value=client):
			result = listings.test_connection()
		self.assertFalse(result["ok"])
		self.assertIn("Unapproved User Parameter", result["error"])
		self.assertIn("User-Agent", result["error"])
		self.assertIn("This store sent: GunStorePOS/", result["error"])

	def test_an_ordinary_failure_is_reported_unadorned(self):
		"""The hint is for one 403. Pasting it under every error would make the
		next reader stare at a header that is fine."""
		client, _session = self._client([
			(("GET", "/GunBrokerTime"), fx.make_response(
				"Bad Request", status=400, content_type="text/plain")),
		])
		with patch.object(GunBrokerClient, "from_settings", return_value=client):
			result = listings.test_connection()
		self.assertNotIn("User-Agent", result["error"])

	def _account(self, body):
		client, _session = self._client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
			(("GET", "/Users/AccountInfo"), fx.make_response(body)),
			(("GET", "/Listing/Defaults"), fx.make_response(fx.LISTING_DEFAULTS_OK)),
		])
		with patch.object(GunBrokerClient, "from_settings", return_value=client):
			return listings.test_connection()["account"]

	def test_the_account_name_comes_from_user_summary(self):
		"""The sandbox puts the username in ``userSummary``, not on top.

		The whole point of echoing the account is the moment somebody presses
		Test Connection on the production account to check WHICH account they
		just reached. An empty string answers that question wrongly and silently,
		so the three places a name can live are pinned here rather than left to
		whatever the next response shape happens to be.
		"""
		self.assertEqual(self._account(fx.ACCOUNT_INFO_OK), "store-sandbox")

	def test_a_flat_user_name_still_works(self):
		"""The earlier reading, kept as a fallback rather than replaced.

		Nothing observed returns this shape today, but it is what the code read
		for the whole of PR-1 and dropping it outright would be a second guess
		layered on the first."""
		self.assertEqual(self._account(fx.ACCOUNT_INFO_FLAT), "store-flat")

	def test_a_person_name_is_better_than_an_empty_box(self):
		"""Last resort. ``firstName``/``lastName`` are on the sandbox response
		too, so an account with no username still identifies itself."""
		self.assertEqual(self._account(fx.ACCOUNT_INFO_NAME_ONLY), "Test Seller")

	def test_an_unrecognisable_body_reports_empty_rather_than_raising(self):
		"""Test Connection's job is to report, and the rest of the report —
		``sandbox``, ``base_url``, ``server_time`` — is worth more than an
		exception thrown over a cosmetic field."""
		self.assertEqual(self._account({"somethingElse": 1}), "")

	def test_no_gateway_and_no_card_checkout_is_not_a_warning(self):
		"""It is the intended configuration for this phase. A warning that fires
		on the correct setup teaches people to skip warnings."""
		client, _session = self._client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
			(("GET", "/Users/AccountInfo"), fx.make_response(fx.ACCOUNT_INFO_OK)),
			(("GET", "/Listing/Defaults"), fx.make_response(fx.LISTING_DEFAULTS_OK)),
		])
		with patch.object(GunBrokerClient, "from_settings", return_value=client):
			result = listings.test_connection()
		self.assertEqual(result["warnings"], [])

	def test_card_checkout_without_a_gateway_is_a_warning(self):
		self.settings = fx.settings_stub(card_checkout_enabled=1)
		client, _session = self._client([
			(("GET", "/GunBrokerTime"), fx.make_response(fx.GUNBROKER_TIME_OK)),
			(("GET", "/Users/AccountInfo"), fx.make_response(fx.ACCOUNT_INFO_OK)),
			(("GET", "/Listing/Defaults"), fx.make_response(fx.LISTING_DEFAULTS_OK)),
		])
		with patch.object(GunBrokerClient, "from_settings", return_value=client):
			result = listings.test_connection()
		self.assertEqual(len(result["warnings"]), 1)
		self.assertIn("card", result["warnings"][0].lower())


class NothingLeavesTheMachine(_LifecycleCase):
	"""Failures BEFORE the request is sent must not record an attempt.

	Bad credentials, an unbuildable client and unreadable photos all mean
	GunBroker certainly has no listing. Recording a pending attempt for a request
	that never left the building locks the gun out for an hour and tells the
	operator "the last attempt is unconfirmed" instead of "your password is
	wrong" — a message that sends them looking in exactly the wrong place.
	"""

	def setUp(self):
		super().setUp()
		self._allow_push()

	def _assert_untouched(self, result, reason, session):
		self.assertFalse(result["ok"])
		self.assertEqual(result["skipped"], reason)
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertIsNone(self.row["gb_push_attempted_at"])
		self.assertEqual(session.count("POST", "/Items"), 0)

	def test_bad_credentials_report_themselves(self):
		session = fx.RecordingSession([
			(("POST", "/Users/AccessToken"),
				fx.make_response({"userMessage": "Invalid credentials."}, status=400)),
		])
		client = GunBrokerClient(_config(), session=session)
		with patch.object(GunBrokerClient, "cached_token", return_value=""):
			result = listings.push_serial("SN-001", client=client)
		self._assert_untouched(result, listings.AUTH_FAILED, session)

	def test_an_unconfigured_client_reports_itself(self):
		session = fx.RecordingSession([])
		with patch.object(GunBrokerClient, "from_settings",
				side_effect=GunBrokerConfigError("sandbox DevKey is empty")):
			result = listings.push_serial("SN-001", client=None)
		self.assertEqual(result["skipped"], listings.NOT_CONFIGURED)
		self.assertIn("DevKey", result["message"])
		self.assertEqual(self.row["gb_push_pending"], 0)
		self.assertIsNone(self.row["gb_push_attempted_at"])
		self.assertEqual(session.count("POST", "/Items"), 0)

	def test_unreadable_photos_report_themselves(self):
		session = fx.RecordingSession([])
		client = GunBrokerClient(_config(), session=session)
		with patch.object(mapping, "picture_parts",
				return_value={"parts": [], "warnings": ["image_unreadable"]}), \
				patch.object(GunBrokerClient, "cached_token", return_value="tok"):
			result = listings.push_serial("SN-001", client=client)
		self._assert_untouched(result, listings.NO_IMAGES, session)


class TheRowLockIsNotHeldAcrossHttp(_LifecycleCase):
	"""The recovery scan pages through up to fifty requests.

	Holding the Serial No row lock across that deadlocks against the web store's
	own `for_update` on the same row when a shopper checks out mid-scan, and what
	the second click then gets back is a MariaDB lock-wait error rather than a
	skip dict.
	"""

	def setUp(self):
		super().setUp()
		self._allow_push()
		self.row["gb_push_pending"] = 1
		self.locked_reads: list[bool] = []
		real_state = listings.serial_state

		def _record(serial_no, **kw):
			self.locked_reads.append(bool(kw.get("for_update")))
			return frappe._dict(dict(self.row))

		self._patch_object(listings, "serial_state", side_effect=_record)
		self.assertTrue(callable(real_state))

	def test_no_locking_read_happens_before_the_scan(self):
		scanned_at: list[int] = []

		def _scan(method, url, kwargs):
			# How many locking reads had happened by the time the scan ran.
			scanned_at.append(sum(self.locked_reads))
			return fx.make_response(fx.page([]))

		client, _session = self._client([(("GET", "/ItemsSelling"), _scan, True)])
		result = listings.push_serial("SN-001", client=client)

		self.assertEqual(result["skipped"], listings.PUSH_IN_FLIGHT)
		self.assertEqual(scanned_at, [0],
			"a for_update read happened before the recovery scan — the scan would "
			"then be paging through GunBroker while holding the row lock")
		self.assertNotIn(True, self.locked_reads,
			"the pending path must not take the row lock at all")
