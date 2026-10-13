"""Keeping a model's listing true after it is up (R3-20, 21, 23, 24, 25).

Four things move a live model listing, and each class is one of them:

* ``EveryTriggerQueuesTheCount`` — anything that changes the pool of free guns
  (a receipt, a counter hold, a web order, a GunBroker claim) queues the ONE quantity
  task for the model, deduplicated per Item, and an unrelated product costs nothing.
* ``ThePriceFollowsTheWebStore`` — a promotion starting, ending or being edited
  reprices (read back), midnight is covered by the sweep because the register's own
  refresh writes with no document event, and an Item that stops being a uniform-price
  gun ends its listing and says so.
* ``TheHourlySweepKnowsModels`` — candidates include them, a successor the GunBroker
  relisted is adopted by SKU, and "drift" is judged by the push's own formula so a
  GunBroker sale awaiting its order is never "fixed" by putting the unit back.
* ``TheCounterAsksGunBroker`` — a till sale of a gun the model listing has already
  sold is refused on a reply that was understood, and only then.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from ffl_core.firearm import current_price, item_current_price
from ffl_integrations.gunbroker import (
	crosschannel,
	doc_events,
	item_listings,
	listings,
	tasks,
)
from ffl_integrations.tests import gunbroker_fixtures as fx
from ffl_integrations.tests.gb_model_fixtures import AMMO, MODEL, SINGLE
from ffl_integrations.tests.gb_model_listing_fixtures import (
	LISTED,
	REPLACEMENT,
	FakeGB,
	ModelListingCase,
)


class EveryTriggerQueuesTheCount(ModelListingCase):
	def setUp(self):
		super().setUp()
		self._start(patch.object(doc_events, "_skip", return_value=False))

	def assert_queued_once(self, method="gb_sync_item_quantity_task"):
		jobs = [(m, kw) for m, kw in self.enqueued if m.endswith(method)]
		self.assertEqual(len(jobs), 1, f"expected one {method}, got {self.enqueued_methods()}")
		kw = jobs[0][1]
		self.assertEqual((kw["item_code"], kw["deduplicate"], kw["enqueue_after_commit"]),
			(MODEL, True, True))
		self.assertEqual(kw["job_id"], f"{'gb-qty' if 'quantity' in method else 'gb-relist'}:{MODEL}")

	def assert_nothing_queued(self):
		self.assertEqual(self.enqueued, [])

	def test_another_channels_commitment_moving(self):
		self.listed(basis=3)
		crosschannel.channel_commitments_changed(MODEL, source="woo")
		self.assert_queued_once()

	def test_gunbrokers_own_claim_moving(self):
		"""A cancelled GunBroker order returns its units to the pool, and GunBroker
		does not put them back by itself — so the channel's own change is not skipped."""
		self.listed(basis=3)
		crosschannel.channel_commitments_changed(MODEL, source="gunbroker")
		self.assert_queued_once()

	def test_a_sold_out_model_is_relisted_when_units_come_back(self):
		frappe.db.set_value("Item", MODEL, "gb_item_history", LISTED, update_modified=False)
		crosschannel.channel_commitments_changed(MODEL, source="gunbroker")
		self.assert_queued_once("gb_relist_item_task")

	def test_a_model_taken_down_by_hand_is_never_relisted(self):
		frappe.db.set_value("Item", MODEL, {"gb_item_history": LISTED, "gb_delisted": 1},
			update_modified=False)
		crosschannel.channel_commitments_changed(MODEL, source="woo")
		self.assert_nothing_queued()

	def test_a_model_never_on_gunbroker_costs_nothing(self):
		crosschannel.channel_commitments_changed(MODEL, source="woo")
		self.assert_nothing_queued()

	def test_a_gun_priced_one_by_one_is_still_left_alone(self):
		frappe.db.set_value("Item", SINGLE, "gb_item_id", "16087001", update_modified=False)
		crosschannel.channel_commitments_changed(SINGLE, source="woo")
		self.assert_nothing_queued()

	def test_a_receipt_of_a_gun_into_stock(self):
		"""A real Material Receipt: the ledger entry is what the hook sees."""
		self.listed(basis=3)
		self.stock("T-RECEIPT")
		self.assert_queued_once()

	def test_the_ledger_entry_of_an_unlisted_model_queues_nothing(self):
		self.stock("T-QUIET")
		self.assert_nothing_queued()

	def test_a_counter_ticket_submitting_or_cancelling(self):
		self.listed(basis=3)
		ticket = frappe._dict(items=[frappe._dict(item_code=MODEL, serial_no="G-1"),
			frappe._dict(item_code=AMMO)], is_return=0, is_consolidated=0)
		with patch.object(doc_events, "_listed_serials", return_value=[]):
			doc_events.on_invoice_change(ticket)
		self.assert_queued_once()

	def test_a_return_or_a_consolidation_is_not_a_new_hold(self):
		self.listed(basis=3)
		for flag in ("is_return", "is_consolidated"):
			ticket = frappe._dict(items=[frappe._dict(item_code=MODEL)], **{flag: 1})
			doc_events.on_invoice_change(ticket)
		self.assert_nothing_queued()

	def test_a_draft_ticket_saved_holds_its_guns(self):
		self.listed(basis=3)
		draft = frappe._dict(items=[frappe._dict(item_code=MODEL)], docstatus=0,
			is_return=0, is_consolidated=0)
		doc_events.on_invoice_draft_change(draft)
		self.assert_queued_once()

	def test_a_submitted_ticket_is_the_submit_hooks_not_the_drafts(self):
		self.listed(basis=3)
		submitted = frappe._dict(items=[frappe._dict(item_code=MODEL)], docstatus=1,
			is_return=0, is_consolidated=0)
		doc_events.on_invoice_draft_change(submitted)
		self.assert_nothing_queued()

	def test_a_ticket_of_accessories_costs_one_read(self):
		self.listed(basis=3)
		draft = frappe._dict(items=[frappe._dict(item_code=AMMO)], docstatus=0,
			is_return=0, is_consolidated=0)
		doc_events.on_invoice_draft_change(draft)
		self.assert_nothing_queued()

	def test_a_web_order_reserving_a_gun(self):
		self.listed(basis=3)
		order = frappe._dict(serials=[frappe._dict(item_code=MODEL, confirmed_serial="",
			stock_hold_serial="")])
		doc_events.on_woo_order_change(order)
		self.assert_queued_once()

	def test_many_triggers_for_one_model_are_one_job(self):
		"""All use the same job id and `deduplicate`, which is what makes a receipt, a
		hold and a claim arriving together one round trip."""
		self.listed(basis=3)
		crosschannel.channel_commitments_changed(MODEL, source="woo")
		doc_events.on_invoice_draft_change(frappe._dict(
			items=[frappe._dict(item_code=MODEL)], docstatus=0, is_return=0, is_consolidated=0))
		ids = {kw["job_id"] for m, kw in self.enqueued if m.endswith("gb_sync_item_quantity_task")}
		self.assertEqual(ids, {f"gb-qty:{MODEL}"})
		self.assertTrue(all(kw["deduplicate"] for _m, kw in self.enqueued))

	def test_the_task_routes_a_model_to_the_model_formula(self):
		self.listed(basis=3)
		with patch.object(item_listings, "sync_model_quantity", return_value={"ok": True}) as sync:
			tasks.gb_sync_item_quantity_task(MODEL)
		sync.assert_called_once()


class ThePriceFollowsTheWebStore(ModelListingCase):
	def setUp(self):
		super().setUp()
		self._start(patch.object(doc_events, "_skip", return_value=False))
		self._start(patch.object(doc_events, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"])))
		self.stock_n(2)
		self.listed(basis=2, price=499)

	def edit(self, **fields):
		doc = frappe.get_doc("Item", MODEL)
		doc.update(fields)
		doc.save(ignore_permissions=True)
		return doc

	def assert_repriced(self):
		self.assertEqual(frappe.db.get_value("Item", MODEL, "gb_price_dirty"), 1)
		self.assertIn("gb_reprice_item_task", self.enqueued_methods())

	def assert_left_alone(self):
		self.assertEqual(frappe.db.get_value("Item", MODEL, "gb_price_dirty"), 0)
		self.assertNotIn("gb_reprice_item_task", self.enqueued_methods())
		self.assertNotIn("gb_end_item_listing_task", self.enqueued_methods())

	# ---------------------------------------------------------------- the rule

	def test_one_rule_for_the_register_and_the_listing(self):
		window = (add_days(nowdate(), -1), add_days(nowdate(), 1))
		self.assertEqual(current_price(499, 399, *window), 399.0)
		self.assertEqual(current_price(499, 399, add_days(nowdate(), 1), None), 499.0)
		self.assertEqual(current_price(499, 399, None, add_days(nowdate(), -1)), 499.0)
		self.assertEqual(current_price(499, 0, None, None), 499.0)
		self.assertEqual(current_price(499, 600, None, None), 499.0,
			"a 'sale' above the regular price is not one — WooCommerce drops it too")
		frappe.db.set_value("Item", MODEL, {"sale_price": 399, "sale_from": window[0],
			"sale_to": window[1]}, update_modified=False)
		self.assertEqual(item_current_price(MODEL), 399.0)
		self.assertEqual(item_current_price(MODEL, on=add_days(nowdate(), 2)), 499.0)

	def test_the_registers_item_price_still_follows_the_same_rule(self):
		"""`sync_standard_selling_price` now asks `item_current_price`: the Item Price a
		uniform-priced Item carries is unchanged by the extraction."""
		self.edit(sale_price=399, sale_from=add_days(nowdate(), -1), sale_to=add_days(nowdate(), 3))
		rate = frappe.db.get_value("Item Price", {"item_code": MODEL, "selling": 1},
			"price_list_rate")
		self.assertEqual(float(rate), 399.0)

	# ------------------------------------------------------------- the hook

	def test_a_price_edit_reprices(self):
		self.edit(standard_rate=549)
		self.assert_repriced()

	def test_a_promotion_starting_reprices(self):
		self.edit(sale_price=399, sale_from=add_days(nowdate(), -1), sale_to=add_days(nowdate(), 5))
		self.assert_repriced()

	def test_a_promotion_ending_by_edit_reprices(self):
		frappe.db.set_value("Item", MODEL, "gb_price_pushed", 399, update_modified=False)
		self.edit(sale_price=399, sale_from=add_days(nowdate(), -9), sale_to=add_days(nowdate(), -1))
		self.assert_repriced()  # 499 now, 399 on GunBroker

	def test_a_promotion_in_the_future_changes_nothing_yet(self):
		self.edit(sale_price=399, sale_from=add_days(nowdate(), 4), sale_to=add_days(nowdate(), 9))
		self.assert_left_alone()

	def test_an_unrelated_edit_costs_nothing(self):
		self.edit(item_name="CZ 75 Grade A (surplus)")
		self.assert_left_alone()

	def test_an_unlisted_model_is_not_repriced(self):
		frappe.db.set_value("Item", MODEL, "gb_item_id", "", update_modified=False)
		self.edit(standard_rate=549)
		self.assert_left_alone()

	def test_a_non_firearm_listing_is_untouched(self):
		"""Ammunition and parts keep their old behaviour: no automatic reprice."""
		frappe.db.set_value("Item", AMMO, {"gb_item_id": "16087002", "standard_rate": 20},
			update_modified=False)
		doc = frappe.get_doc("Item", AMMO)
		doc.standard_rate = 25
		doc.save(ignore_permissions=True)
		self.assertNotIn("gb_reprice_item_task", self.enqueued_methods())
		self.assertNotIn("gb_end_item_listing_task", self.enqueued_methods())

	def test_a_gun_priced_one_by_one_is_not_repriced_from_its_item(self):
		"""Per-serial listings keep their rule: a serial's own price edit reprices it
		(`on_serial_update`), and a price on the Item means nothing to it."""
		frappe.db.set_value("Item", SINGLE, {"gb_item_id": "16087004", "standard_rate": 700},
			update_modified=False)
		doc = frappe.get_doc("Item", SINGLE)
		doc.standard_rate = 750
		doc.save(ignore_permissions=True)
		self.assertNotIn("gb_reprice_item_task", self.enqueued_methods())
		self.assertNotIn("gb_end_item_listing_task", self.enqueued_methods())

	# ------------------------------------------------------- no longer a model

	def test_an_item_that_stops_being_a_uniform_price_gun_ends_its_listing(self):
		self.edit(uniform_sell_price=0)
		self.assertIn("gb_end_item_listing_task", self.enqueued_methods())
		end = next(kw for m, kw in self.enqueued if m.endswith("gb_end_item_listing_task"))
		self.assertEqual(end["item_code"], MODEL)
		self.assertIn(f"gb_model_ended_{MODEL}", self.alerts)
		self.assertIn("listing ended", frappe.db.get_value("Item", MODEL, "gb_sync_error"))
		self.assertNotIn("gb_reprice_item_task", self.enqueued_methods())

	def test_a_disabled_item_ends_its_listing(self):
		self.edit(disabled=1)
		self.assertIn("gb_end_item_listing_task", self.enqueued_methods())
		self.assertIn(f"gb_model_ended_{MODEL}", self.alerts)

	def test_an_item_left_without_a_price_ends_its_listing(self):
		"""Review r1: GunBroker would otherwise go on selling at the last price sent."""
		self.edit(standard_rate=0, sale_price=0)
		self.assertIn("gb_end_item_listing_task", self.enqueued_methods())
		end = next(kw for m, kw in self.enqueued if m.endswith("gb_end_item_listing_task"))
		self.assertIn("no price", end["reason"])
		self.assertNotIn("gb_reprice_item_task", self.enqueued_methods())

	def test_an_item_that_stops_being_a_firearm_ends_its_listing(self):
		frappe.db.set_value("Item", MODEL, {"has_serial_no": 0}, update_modified=False)
		with patch("ffl_core.firearm.auto_set_is_firearm"):  # the save would re-derive it
			self.edit(is_firearm=0, uniform_sell_price=0)
		self.assertIn("gb_end_item_listing_task", self.enqueued_methods())

	def test_the_end_is_by_a_person_so_a_machine_never_puts_it_back(self):
		with patch.object(item_listings, "end_item_listing") as end:
			tasks.gb_end_item_listing_task(MODEL, "the Item was disabled")
		end.assert_called_once_with(MODEL, reason="the Item was disabled", delisted=True)

	# --------------------------------------------------------------- the push

	def test_the_reprice_reads_the_price_when_it_runs_and_checks_it_back(self):
		self.gb.listings[LISTED] = {"quantity": 2, "active": True, "relisted_as": 0, "price": 499.0}
		frappe.db.set_value("Item", MODEL, {"sale_price": 399, "sale_from": add_days(nowdate(), -1),
			"sale_to": add_days(nowdate(), 5), "gb_price_dirty": 1}, update_modified=False)
		with patch.object(listings, "commit"):
			result = item_listings.update_item_price(MODEL, client=self.gb)
		self.assertEqual(self.gb.puts, [(LISTED, {"FixedPrice": 399.0})])
		self.assertTrue(result["verified"])
		item = self.item()
		self.assertEqual((item.gb_price_pushed, item.gb_price_dirty), (399.0, 0))

	def test_a_reprice_that_does_not_read_back_is_not_recorded(self):
		class Stubborn(FakeGB):
			def update_listing(self, item_id, patch_):  # GunBroker's 200 that did nothing
				self.puts.append((str(item_id), dict(patch_)))

		self.gb = Stubborn(**{LISTED: {"quantity": 2, "price": 499.0}})
		frappe.db.set_value("Item", MODEL, {"standard_rate": 549, "gb_price_dirty": 1},
			update_modified=False)
		with patch.object(listings, "commit"):
			result = item_listings.update_item_price(MODEL, client=self.gb)
		self.assertEqual(result["skipped"], listings.REPRICE_UNCONFIRMED)
		item = self.item()
		self.assertEqual((item.gb_price_pushed, item.gb_price_dirty), (499.0, 1),
			"still dirty, so the next sweep tries again")

	def test_the_task_does_nothing_for_a_model_that_is_not_dirty(self):
		with patch.object(item_listings, "update_item_price") as reprice:
			tasks.gb_reprice_item_task(MODEL)
			frappe.db.set_value("Item", MODEL, "gb_price_dirty", 1, update_modified=False)
			tasks.gb_reprice_item_task(MODEL)
		reprice.assert_called_once_with(MODEL)

	# ---------------------------------------------------------- the midnight sweep

	def test_midnight_reprices_a_window_that_opened_with_no_save(self):
		frappe.db.set_value("Item", MODEL, {"sale_price": 399, "sale_from": nowdate(),
			"sale_to": add_days(nowdate(), 5)}, update_modified=False)  # what the refresh does
		tasks._run_model_price_sweep()
		self.assert_repriced()

	def test_midnight_reprices_a_window_that_closed(self):
		frappe.db.set_value("Item", MODEL, {"gb_price_pushed": 399, "sale_price": 399,
			"sale_from": add_days(nowdate(), -5), "sale_to": add_days(nowdate(), -1)},
			update_modified=False)
		tasks._run_model_price_sweep()
		self.assert_repriced()

	def test_midnight_leaves_a_model_at_the_right_price_alone(self):
		tasks._run_model_price_sweep()
		self.assert_left_alone()

	def test_the_sweep_gate_only_enqueues_and_obeys_both_switches(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=False):
			tasks.gb_model_price_sweep()
		self.assertEqual(self.enqueued, [])
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True), \
				patch.object(tasks, "scheduled_sync_enabled", return_value=False):
			tasks.gb_model_price_sweep()
		self.assertEqual(self.enqueued, [])
		with patch("ffl_integrations.integration_gate.gunbroker_enabled", return_value=True), \
				patch.object(tasks, "scheduled_sync_enabled", return_value=True):
			tasks.gb_model_price_sweep()
		self.assertEqual(self.enqueued_methods(), ["_run_model_price_sweep"])


class TheHourlySweepKnowsModels(ModelListingCase):
	def setUp(self):
		super().setUp()
		self.stock_n(3)
		self.listed(basis=3)

	def _round(self, *rows):
		rnd = tasks._Round(self.gb)
		rnd._selling = tasks._Scan(list(rows))
		rnd._sold = tasks._Scan([])
		return rnd

	def _row(self, item_id=LISTED, quantity=3, price=499.0):
		return {**fx.selling_row(int(item_id), sku=MODEL, serial=""),
			"quantity": quantity, "fixedPrice": price}

	def _sweep(self, rnd):
		row = next(r for r in tasks.item_sweep_candidates() if r["item_code"] == MODEL)
		tasks._sweep_one_item(rnd, row)

	def test_a_model_is_a_candidate_and_a_gun_priced_one_by_one_is_not(self):
		frappe.db.set_value("Item", SINGLE, "gb_item_id", "16087003", update_modified=False)
		names = {r["item_code"] for r in tasks.item_sweep_candidates()}
		self.assertIn(MODEL, names)
		self.assertNotIn(SINGLE, names)

	def test_a_listing_holding_the_right_number_and_price_is_left_alone(self):
		with patch.object(item_listings, "sync_item_quantity") as sync:
			self._sweep(self._round(self._row()))
		sync.assert_not_called()
		self.assertNotIn("gb_reprice_item_task", self.enqueued_methods())

	def test_a_gunbroker_sale_waiting_for_its_order_is_not_drift(self):
		"""GunBroker holds 2 of the 3 because it sold one; the pool is still 3. Judged
		against the pool alone this would 'fix' it by putting the sold unit back."""
		with patch.object(item_listings, "sync_item_quantity") as sync:
			self._sweep(self._round(self._row(quantity=2)))
		sync.assert_not_called()

	def test_a_real_drift_is_corrected_with_the_pushes_own_logic(self):
		"""GunBroker holds 5 against a pool of 3 — nothing a sale explains."""
		with patch.object(item_listings, "sync_item_quantity") as sync:
			self._sweep(self._round(self._row(quantity=5)))
		sync.assert_called_once()
		self.assertTrue(sync.call_args.kwargs["force"])

	def test_a_price_that_is_not_the_web_stores_is_marked_and_sent(self):
		with patch.object(item_listings, "sync_item_quantity"):
			self._sweep(self._round(self._row(price=449.0)))
		self.assertEqual(frappe.db.get_value("Item", MODEL, "gb_price_dirty"), 1)
		self.assertIn("gb_reprice_item_task", self.enqueued_methods())

	def test_a_scan_that_does_not_carry_the_listing_says_nothing(self):
		with patch.object(item_listings, "sync_item_quantity") as sync:
			self._sweep(self._round())
		sync.assert_not_called()
		self.assertEqual(frappe.db.get_value("Item", MODEL, "gb_price_dirty"), 0)

	def test_a_replacement_listing_is_adopted_by_sku_and_the_baseline_survives(self):
		self.gb.listings[LISTED] = {"quantity": 0, "active": False}
		with patch.object(item_listings, "sync_item_quantity"):
			self._sweep(self._round(self._row(item_id=REPLACEMENT, quantity=2)))
		item = self.item()
		self.assertEqual(item.gb_item_id, REPLACEMENT)
		self.assertIn(LISTED, item.gb_item_history)
		self.assertEqual((item.gb_qty_basis, item.gb_qty_basis_id), (3, LISTED),
			"the baseline is where OUR last write landed, not where GunBroker moved it")

	def test_a_push_nobody_heard_back_from_is_claimed_by_sku(self):
		frappe.db.set_value("Item", MODEL, {"gb_item_id": "", "gb_push_pending": 1,
			"gb_push_attempted_at": add_to_date(now_datetime(), hours=-3)}, update_modified=False)
		with patch.object(item_listings, "sync_item_quantity"):
			self._sweep(self._round(self._row(item_id=REPLACEMENT)))
		item = self.item()
		self.assertEqual((item.gb_item_id, item.gb_push_pending), (REPLACEMENT, 0))

	def test_an_end_that_was_never_confirmed_is_retried(self):
		frappe.db.set_value("Item", MODEL, "gb_end_pending", 1, update_modified=False)
		with patch.object(item_listings, "end_item_listing") as end:
			self._sweep(self._round(self._row()))
		end.assert_called_once()


class TheCounterAsksGunBroker(ModelListingCase):
	"""``model_sale_verdict`` and the two places that use it: the hard gate on
	``before_submit`` and the scan-time advisory, one probe between them."""

	def setUp(self):
		super().setUp()
		self.guns = self.stock_n(2)
		self.listed(basis=3)  # one more than the shelf: GunBroker sold one, order not in
		self.gb = FakeGB(**{LISTED: {"quantity": 2}})
		self._start(patch.object(doc_events, "_skip", return_value=False))
		self._start(patch.object(doc_events, "_fast_client", side_effect=lambda _b: (self.gb, "")))
		self._start(patch.object(doc_events, "_settings",
			side_effect=lambda: fx.settings_stub(probe_timeout_seconds=3)))

	def ticket(self, *guns):
		return frappe._dict(
			name="POS-T-1", is_return=0, is_consolidated=0, consolidated_invoice=None,
			items=[frappe._dict(item_code=MODEL, serial_no=g) for g in guns])

	def gate(self, *guns):
		with patch.object(doc_events, "serials_for_line",
				side_effect=lambda row: [row["serial_no"]] if row.get("serial_no") else []):
			return doc_events.block_sale_of_sold_gb_listing(self.ticket(*guns))

	def test_selling_the_gun_gunbroker_already_sold_is_refused(self):
		"""Two on the shelf, one already gone on GunBroker (unseen): the till may sell
		one, not two."""
		self.gate(self.guns[0])  # leaves one for the GunBroker buyer: fine
		with self.assertRaises(frappe.ValidationError) as caught:
			self.gate(*self.guns)
		self.assertIn("GunBroker has already sold", str(caught.exception))

	def test_the_refusal_asks_for_the_order_to_be_brought_in(self):
		with self.assertRaises(frappe.ValidationError):
			self.gate(*self.guns)
		self.assertIn("request_order_sync", self.enqueued_methods())

	def test_the_verdict_is_the_pushs_formula(self):
		verdict = doc_events.model_sale_verdict(MODEL, selling=2, budget=3)
		self.assertEqual((verdict["state"], verdict["unseen"]), ("refuse", 1))
		self.assertIsNone(doc_events.model_sale_verdict(MODEL, selling=1, budget=3))

	def test_nothing_unseen_nothing_to_say(self):
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.assertIsNone(doc_events.model_sale_verdict(MODEL, selling=2, budget=3))

	def test_once_the_order_is_in_the_claim_gate_not_this_one_decides(self):
		self.ingest(self.detail(qty=1))
		self.assertIsNone(doc_events.model_sale_verdict(MODEL, selling=1, budget=3),
			"1 claim leaves 1 free, nothing unseen")

	def test_an_unreadable_listing_warns_and_lets_the_sale_through(self):
		self.gb.errors.add(LISTED)
		self.assertEqual(doc_events.model_sale_verdict(MODEL, selling=2, budget=3)["state"],
			"unresolved")
		with patch("frappe.msgprint") as warn, patch.object(doc_events, "safe_log_error"):
			self.gate(*self.guns)  # does not raise
		self.assertIn("model listing", warn.call_args.args[0])

	def test_no_session_warms_one_and_does_not_block(self):
		with patch.object(doc_events, "_fast_client", return_value=(None, "no cached session")):
			verdict = doc_events.model_sale_verdict(MODEL, selling=2, budget=3)
		self.assertEqual(verdict["state"], "unresolved")

	def test_a_spent_budget_asks_nothing(self):
		self.assertEqual(doc_events.model_sale_verdict(MODEL, selling=2, budget=0)["state"],
			"unresolved")
		self.assertEqual(self.gb.puts, [])

	def test_a_pool_that_cannot_be_read_does_not_block(self):
		with patch.object(doc_events, "uniform_free_serials", side_effect=RuntimeError("x")):
			self.assertEqual(doc_events.model_sale_verdict(MODEL, selling=2, budget=3)["state"],
				"unresolved")

	def test_a_model_with_no_baseline_cannot_be_measured(self):
		frappe.db.set_value("Item", MODEL, {"gb_qty_basis": 0, "gb_qty_basis_id": ""},
			update_modified=False)
		self.assertIsNone(doc_events.model_sale_verdict(MODEL, selling=2, budget=3))

	def test_an_unlisted_model_or_a_per_gun_listing_is_not_this_gate(self):
		frappe.db.set_value("Item", MODEL, "gb_item_id", "", update_modified=False)
		with patch.object(doc_events, "_fast_client") as fast:
			self.gate(*self.guns)
		fast.assert_not_called()

	def test_the_scan_time_advisory_says_so_before_the_ticket_is_built(self):
		with patch.object(doc_events, "probe_gb_sale", return_value=None):
			answer = crosschannel.presale_probe(self.guns[0], timeout=3)
		self.assertIsNone(answer, "one of two free guns with one unseen sale is still sellable")
		frappe.db.set_value("Serial No", self.guns[1], "status", "Delivered")
		with patch.object(doc_events, "probe_gb_sale", return_value=None):
			answer = crosschannel.presale_probe(self.guns[0], timeout=3)
		self.assertTrue(answer["blocked"])
		self.assertEqual(answer["channel"], "gunbroker")

	def test_the_chain_read_stops_at_the_deadline(self):
		listing = item_listings.read_model_listing(
			self.gb, item_listings.item_state(MODEL), deadline=0)
		self.assertEqual(listing["state"], "unreadable")
		self.assertEqual(listing["chain"], [])

	def test_a_sold_listing_followed_to_its_replacement_for_the_counter(self):
		self.gb = FakeGB(**{LISTED: {"quantity": 3}})
		self.gb.sold(LISTED, 1, relist_as=REPLACEMENT)
		verdict = doc_events.model_sale_verdict(MODEL, selling=2, budget=3)
		self.assertEqual((verdict["state"], verdict["unseen"]), ("refuse", 1))
