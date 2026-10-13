"""How many guns of a model GunBroker is told it has (R3-19, R3-22 and the pool end of 23).

The number is the shared pool's, less the units GunBroker sold that no order has
brought in yet::

    unseen = max(0, basis - ingested - remote)
    push   = max(0, target - unseen)

and each class here is one way that goes wrong while looking right:

* ``TheThreeRaces`` — GunBroker decrements its own count at the moment of purchase
  but the order reaches us on the next poll, so for a while the pool still counts a
  gun that is already sold. A push of the pool's figure would put it straight back.
  The races: a GunBroker sale and a web order in the same window, a gun received in
  the same window, and a sale that made GunBroker end and relist the listing under
  a new number.
* ``WhenTheRemoteCannotBeRead`` — the one thing worse than a stale number is a
  stale number that is too HIGH: nothing is sent, except an end when nothing at all
  is sellable.
* ``TheWriteIsACompareAndSet`` — a write addressed to a listing that sold in the
  meantime is discarded by GunBroker with a 200, so it is read back.
* ``SellingOutAndComingBack`` — sold out ends (without the "a person did this"
  sentinel), a restock relists it after the cool-off and never with the paid
  promotions, a person pressing Send does.

Every test runs against real Items, guns, claims and GunBroker orders; only
GunBroker's own answers are faked.
"""
from __future__ import annotations

from unittest.mock import call, patch

import frappe
from frappe.utils import add_to_date, now_datetime

from ffl_integrations.gunbroker import item_listings, listings
from ffl_integrations.tests.gb_model_fixtures import MODEL
from ffl_integrations.tests.gb_model_listing_fixtures import (
	LISTED,
	REPLACEMENT,
	FakeGB,
	ModelListingCase,
)


class TheThreeRaces(ModelListingCase):
	def test_a_gunbroker_sale_not_in_yet_and_a_web_order_in_the_same_window(self):
		"""Three guns listed as 3; GunBroker sells one (its count reads 2, no order yet)
		while the web store reserves another. The pool says 2 are free, but one of
		those is the gun GunBroker already sold — so 1, not 2."""
		guns = self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		frappe.db.set_value("Serial No", guns[0], {"woo_reserved": 1, "woo_order": "WOO-9"})

		result = self.sync()

		self.assertEqual(self.gb.puts, [(LISTED, {"Quantity": 1})])
		self.assertEqual(result["quantity"], 1)
		self.assertEqual((self.item().gb_qty_basis, self.item().gb_qty_basis_id), (1, LISTED))
		self.assertIn("request_order_sync", self.enqueued_methods(),
			"an unseen sale asks for the order poll rather than waiting for the cron")

	def test_a_gun_received_while_a_gunbroker_sale_is_still_unseen_does_not_raise_it(self):
		"""The restock lands in the unseen window. Three listed, one sold on GunBroker
		(count 2), a fourth gun arrives: the pool is 4 but the listing may only go to
		3. Without the baseline this pushes 4 — and sells a gun that is not there."""
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self.stock("RESTOCK")

		self.sync()

		self.assertEqual(self.gb.puts, [(LISTED, {"Quantity": 3})])

	def test_once_the_order_is_in_the_pool_and_the_baseline_agree(self):
		"""The same sale after the poll: the order is a claim (pool 4 - 1 = 3), and
		it is `ingested`, so nothing is unseen and the push is the pool's 3 — not 3 - 1."""
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self.stock("RESTOCK")
		self.ingest(self.detail(qty=1))

		self.sync()

		self.assertEqual(self.gb.puts, [(LISTED, {"Quantity": 3})])
		self.assertNotIn("request_order_sync", self.enqueued_methods())

	def test_a_relist_chain_is_followed_to_the_live_listing(self):
		"""A partial sale ended the listing and GunBroker relisted the remainder under
		a new number. The write goes to the NEW number, the record moves onto it, and
		the baseline is re-anchored there."""
		self.stock_n(3)
		self.stock("RESTOCK")
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.gb.sold(LISTED, 1, relist_as=REPLACEMENT)

		self.sync()

		self.assertEqual(self.gb.puts, [(REPLACEMENT, {"Quantity": 3})])
		item = self.item()
		self.assertEqual(item.gb_item_id, REPLACEMENT)
		self.assertIn(LISTED, item.gb_item_history)
		self.assertEqual((item.gb_qty_basis, item.gb_qty_basis_id), (3, REPLACEMENT))

	def test_the_sale_on_the_old_number_counts_as_ingested_once_its_order_is_in(self):
		"""The order for that sale names the OLD listing. It is found because the chain
		is walked from the baseline's number — and then nothing is unseen."""
		self.stock_n(4)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.gb.sold(LISTED, 1, relist_as=REPLACEMENT)
		self.ingest(self.detail(qty=1))  # sold from LISTED

		self.sync()

		# Pool: 4 free - 1 claim = 3; nothing unseen (3 - 1 - 2): the listing holds 2
		# but should hold 3 -> raised, to the replacement.
		self.assertEqual(self.gb.puts, [(REPLACEMENT, {"Quantity": 3})])
		self.assertNotIn("request_order_sync", self.enqueued_methods())

	def test_a_sale_decremented_in_place_is_not_counted_twice(self):
		"""Review r1: if GunBroker decrements a listing in place instead of ending and
		relisting it, an order ingested BEFORE the baseline was re-anchored on that same
		number is already inside the basis. Counting it again would hide the next
		unseen sale and push a sold gun back on the listing."""
		self.stock_n(5)
		self.listed(basis=5)
		# First sale, decremented in place (5 -> 4), its order already in.
		self.gb = FakeGB(**{LISTED: {"quantity": 4}})
		self.ingest(self.detail(qty=1))
		self.sync()  # pool 4, nothing unseen: re-anchors on (4, LISTED)
		self.assertEqual(self.gb.puts, [])
		self.assertEqual((self.item().gb_qty_basis, self.item().gb_qty_basis_ingested), (4, 1))
		# Second sale in place (4 -> 3), order NOT in yet: one unit is unseen.
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})

		result = self.sync()

		self.assertEqual(self.gb.puts, [], "the pool's 4 must not be pushed over the unseen sale")
		self.assertEqual(result["quantity"], 3)

	def test_nothing_is_sent_when_the_listing_already_holds_the_number(self):
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})

		result = self.sync()

		self.assertEqual(self.gb.puts, [])
		self.assertTrue(result["unchanged"])

	def test_an_unchanged_count_re_anchors_the_baseline_when_nothing_is_unseen(self):
		"""So a long-lived listing's chain does not grow without bound."""
		self.stock_n(2)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self.ingest(self.detail(qty=1))  # 3 - 1 ingested = 2: nothing unseen
		self.stock("MORE")
		# pool: 3 free - 1 claim = 2 == remote

		self.sync()

		self.assertEqual(self.gb.puts, [])
		self.assertEqual(self.item().gb_qty_basis, 2)

	def test_the_baseline_is_not_re_anchored_while_a_sale_is_unseen(self):
		"""Three guns still on the shelf, GunBroker sold one (count 2, no order yet): the
		push is 3 - 1 = 2, which GunBroker already holds. Nothing is sent — and the
		baseline stays at 3, because re-anchoring it on 2 would forget the sale that is
		still owed and let the order, when it lands, be taken off twice."""
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})

		result = self.sync()

		self.assertEqual(self.gb.puts, [])
		self.assertTrue(result["unchanged"])
		self.assertEqual(self.item().gb_qty_basis, 3)
		self.assertIn("request_order_sync", self.enqueued_methods())

	def test_the_sale_is_taken_off_once_when_its_order_lands(self):
		"""The same listing after the poll: the order is a claim (pool 3 - 1 = 2) and
		is ingested, so nothing is unseen and the push is still 2 — not 1."""
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self.ingest(self.detail(qty=1))

		result = self.sync()

		self.assertEqual(self.gb.puts, [])
		self.assertEqual(result["quantity"], 2)
		self.assertNotIn("request_order_sync", self.enqueued_methods())

	def test_when_every_gun_left_is_one_gunbroker_already_sold_the_listing_ends(self):
		"""One gun on the shelf; GunBroker's count fell from 2 to 1 with no order yet.
		The pool says 1, but that gun is the one GunBroker sold: nothing to promise."""
		self.stock_n(1)
		self.listed(basis=2)
		self.gb = FakeGB(**{LISTED: {"quantity": 1}})

		result = self.sync()

		self.assertEqual(self.gb.deletes, [LISTED])
		self.assertEqual(self.gb.puts, [])
		self.assertEqual(result["quantity"], 0)
		self.assertIn("request_order_sync", self.enqueued_methods())

	def test_a_listing_with_no_baseline_has_nothing_unseen(self):
		"""One adopted by the sweep rather than posted by us records no baseline, so
		there is nothing to measure GunBroker's own sales against: the pool's figure
		goes, and the PUT writes the first baseline."""
		self.stock_n(3)
		self.listed(basis=3)
		frappe.db.set_value("Item", MODEL, {"gb_qty_basis": 0, "gb_qty_basis_id": ""},
			update_modified=False)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})

		self.sync()

		self.assertEqual(self.gb.puts, [(LISTED, {"Quantity": 3})])
		self.assertEqual((self.item().gb_qty_basis, self.item().gb_qty_basis_id), (3, LISTED))

	def test_the_drift_check_uses_the_same_formula(self):
		"""What the hourly round compares a scan against: with a sale unseen the
		pool's 3 is NOT what the listing should hold."""
		self.stock_n(3)
		self.listed(basis=3)
		row = item_listings.item_state(MODEL)
		self.assertEqual(item_listings.expected_model_quantity(MODEL, row, 2, 3), 2)
		self.assertEqual(item_listings.expected_model_quantity(MODEL, row, 3, 3), 3)
		self.ingest(self.detail(qty=1))
		self.assertEqual(item_listings.expected_model_quantity(MODEL, row, 2, 2), 2)


class WhenTheRemoteCannotBeRead(ModelListingCase):
	def test_an_unreadable_listing_sends_nothing(self):
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.gb.errors.add(LISTED)
		self.stock("RESTOCK")

		result = self.sync()

		self.assertEqual(self.gb.puts, [])
		self.assertEqual(self.gb.deletes, [])
		self.assertEqual(result["skipped"], listings.QUANTITY_UNREADABLE)
		self.assertIn("not sent", self.item().gb_sync_error)

	def test_a_pool_that_cannot_be_read_sends_nothing(self):
		"""A channel that cannot say what it owes would read as owing nothing — a
		bigger number, which on a marketplace is a gun that is not there."""
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		with patch.object(item_listings, "uniform_sellable_units",
				side_effect=RuntimeError("woo is down")):
			result = self.sync()
		self.assertEqual(self.gb.puts, [])
		self.assertEqual(self.gb.deletes, [])
		self.assertEqual(result["skipped"], listings.QUANTITY_UNREADABLE)

	def test_a_chain_longer_than_the_limit_is_unreadable_not_guessed(self):
		self.stock_n(3)
		self.listed(basis=3)
		chain = {str(1000 + i): {"quantity": 0, "active": False, "relisted_as": 1001 + i}
			for i in range(item_listings.MODEL_CHAIN_LIMIT + 2)}
		chain[LISTED] = {"quantity": 0, "active": False, "relisted_as": 1000}
		self.gb = FakeGB(**chain)
		result = self.sync()
		self.assertEqual(result["skipped"], listings.QUANTITY_UNREADABLE)
		self.assertEqual(self.gb.puts, [])

	def test_nothing_sellable_still_ends_the_listing_with_the_remote_unreadable(self):
		"""The one write that is safe unread: the pool says there is nothing at all.
		The end is attempted; that GunBroker cannot be read to confirm it is the end's
		own business (it stays pending and an alert goes out)."""
		self.listed(basis=3)  # no guns on the shelf
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.gb.errors.add(LISTED)

		result = self.sync()

		self.assertEqual(self.gb.deletes, [LISTED])
		self.assertEqual(self.gb.puts, [])
		self.assertEqual(result["quantity"], 0)


class TheWriteIsACompareAndSet(ModelListingCase):
	def test_a_sale_between_the_read_and_the_write_records_nothing(self):
		"""GunBroker ends the listing the moment it sells a unit; a PUT addressed to the
		dead number is a 200 that did nothing. Read back, it is not recorded — the record
		moves to the replacement and the count is queued again."""
		self.stock_n(4)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.gb.on_put = lambda gb: gb.sold(LISTED, 1, relist_as=REPLACEMENT)

		result = self.sync()

		self.assertEqual(result["skipped"], listings.REPRICE_UNCONFIRMED)
		item = self.item()
		self.assertEqual(item.gb_item_id, REPLACEMENT)
		self.assertEqual((item.gb_qty_basis, item.gb_qty_basis_id), (3, LISTED),
			"the baseline still names the number the sale happened on")
		self.assertIn("gb_sync_item_quantity_task", self.enqueued_methods())

	def test_a_landed_write_records_count_and_listing_as_the_new_baseline(self):
		self.stock_n(4)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.sync()
		item = self.item()
		self.assertEqual((item.gb_qty_pushed, item.gb_qty_basis, item.gb_qty_basis_id),
			(4, 4, LISTED))

	def test_a_count_that_moved_during_the_write_is_queued_again(self):
		self.stock_n(3)
		self.listed(basis=3)
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self.stock("MORE")
		self.gb.on_put = lambda gb: self.stock("ARRIVED-DURING")

		self.sync()

		self.assertIn("gb_sync_item_quantity_task", self.enqueued_methods())


class WhenTheListingHasEnded(ModelListingCase):
	def test_every_unit_sold_and_in_leaves_the_listing_over_and_asks_for_a_relist(self):
		self.stock_n(3)
		self.listed(basis=2)
		self.gb = FakeGB(**{LISTED: {"quantity": 0, "active": False}})
		self.ingest(self.detail(qty=2))  # both units sold from LISTED, orders in
		# pool: 3 free - 2 claims = 1 -> something to list again, so the ended
		# listing is closed out locally rather than ended by a call

		result = self.sync()

		self.assertTrue(result["ended"])
		item = self.item()
		self.assertEqual((item.gb_item_id, item.gb_qty_basis, item.gb_qty_basis_id), ("", 0, ""))
		self.assertIn(LISTED, item.gb_item_history)
		self.assertEqual(item.gb_delisted, 0)
		self.assertIn("gb_relist_item_task", self.enqueued_methods())

	def test_an_ended_listing_with_sales_not_in_yet_is_left_alone(self):
		"""Ended with no replacement named could be sold out — or a partial sale whose
		remainder GunBroker has not told us about. Calling it over now would let a
		restock post a SECOND live listing."""
		self.stock_n(2)
		self.listed(basis=2)
		self.gb = FakeGB(**{LISTED: {"quantity": 0, "active": False}})

		result = self.sync()

		self.assertEqual(result["skipped"], listings.REPRICE_UNCONFIRMED)
		self.assertEqual(self.item().gb_item_id, LISTED)
		self.assertEqual(self.gb.puts, [])
		self.assertIn("request_order_sync", self.enqueued_methods())


class SellingOutAndComingBack(ModelListingCase):
	def setUp(self):
		super().setUp()
		self._start(patch.object(item_listings.mapping, "item_candidate_image_paths",
			return_value=["/files/cz.jpg"]))
		self._start(patch.object(item_listings.mapping, "item_picture_parts",
			return_value={"parts": [("cz.jpg", b"x", "image/jpeg")], "warnings": []}))
		self.posts: list[dict] = []

		self.post_kwargs: list[dict] = []

		def _post(doctype, name, payload, pictures, **kw):
			self.posts.append(payload)
			self.post_kwargs.append(kw)
			frappe.db.set_value(doctype, name, {"gb_item_id": "16087500", "gb_push_pending": 0})
			return {"ok": True, "gb_item_id": "16087500", "warnings": kw.get("warnings", [])}

		self._start(patch.object(listings, "post_listing", side_effect=_post))
		self._start(patch.object(item_listings.GunBrokerClient, "from_settings",
			return_value=self.gb))
		self._start(patch.object(item_listings, "_dropship_priced", return_value=False))
		self._start(patch("ffl_integrations.integration_gate.gunbroker_enabled",
			return_value=True))
		self._start(patch.object(listings, "_config_gap", return_value=""))
		frappe.db.set_value("Item", MODEL, {
			"gb_featured": 1, "gb_view_counter": 1, "gb_ship_cost": 0}, update_modified=False)

	def _ended(self, *, delisted=0, hours_ago=8):
		frappe.db.set_value("Item", MODEL, {
			"gb_item_id": "", "gb_item_history": LISTED, "gb_delisted": delisted,
			"gb_synced_at": add_to_date(now_datetime(), hours=-hours_ago)},
			update_modified=False)

	def test_selling_out_ends_the_listing_without_the_hand_taken_down_sentinel(self):
		self.listed(basis=2)  # nothing on the shelf
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self.sync()
		self.assertEqual(self.gb.deletes, [LISTED])
		item = self.item()
		self.assertEqual((item.gb_item_id, item.gb_delisted), ("", 0))

	def test_a_restock_after_the_cool_off_relists_with_no_paid_promotion(self):
		self._ended()
		self.stock_n(2)

		result = item_listings.relist_on_restock(MODEL, client=self.gb)

		self.assertTrue(result["ok"])
		self.assertEqual(self.posts[0]["Quantity"], 2)
		self.assertNotIn("PremiumFeatures", self.posts[0],
			"the Item still carries Featured + View counter from the last human Send")
		item = self.item()
		self.assertEqual((item.gb_qty_basis, item.gb_qty_basis_id), (2, "16087500"))

	def test_a_restock_inside_the_cool_off_waits(self):
		self._ended(hours_ago=1)
		self.stock_n(2)
		self.assertEqual(item_listings.relist_on_restock(MODEL, client=self.gb)["skipped"],
			"cooling_off")
		self.assertEqual(self.posts, [])

	def test_a_model_taken_down_by_hand_is_never_put_back_by_a_machine(self):
		self._ended(delisted=1)
		self.stock_n(2)
		self.assertEqual(item_listings.relist_on_restock(MODEL, client=self.gb)["skipped"],
			"not_eligible")

	def test_a_person_pressing_send_buys_the_recorded_promotions(self):
		self.stock_n(2)
		result = item_listings.push_item(MODEL, premium=True, client=self.gb)
		self.assertTrue(result["ok"])
		self.assertEqual(self.posts[0]["PremiumFeatures"],
			{"IsFeaturedItem": True, "HasViewCounter": True})

	def test_a_person_relisting_by_hand_lifts_the_sentinel_and_buys_them_too(self):
		self._ended(delisted=1)
		self.stock_n(2)
		result = item_listings.push_item(MODEL, relist=True, premium=True, client=self.gb)
		self.assertTrue(result["ok"])
		self.assertIn("PremiumFeatures", self.posts[0])
		self.assertIs(self.post_kwargs[0]["relist"], True,
			"post_listing is what clears the sentinel on success")

	def test_the_hourly_round_relists_what_the_stock_event_was_too_early_for(self):
		"""Inside the cool-off the restock event is refused and nothing asks again."""
		from ffl_integrations.gunbroker import tasks

		self._ended(hours_ago=8)
		self.stock_n(2)
		with patch.object(item_listings, "relist_on_restock") as relist:
			tasks._relist_ended_models()
		# (other models on the site are asked too; only this one's inclusion matters)
		self.assertIn(call(MODEL), relist.call_args_list)

	def test_the_hourly_round_leaves_a_hand_ended_model_alone(self):
		from ffl_integrations.gunbroker import tasks

		self._ended(delisted=1)
		with patch.object(item_listings, "relist_on_restock") as relist:
			tasks._relist_ended_models()
		self.assertNotIn(call(MODEL), relist.call_args_list)
