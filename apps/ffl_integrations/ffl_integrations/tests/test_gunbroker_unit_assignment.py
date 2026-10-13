"""Naming the gun for a model-level unit: `assign_gb_unit_serial`, the refund mark,
the Pending Order card's suggestions, and the Dispose gate.

The endpoint moves a reservation, so the ways it can be wrong are the ones that
look fine in a single-user test:

* the LOCK ORDER (Order, then the Item's mutex, then the Serial No). The web
  store's order path takes the Item mutex before any Serial No; a third order
  that took them the other way round would deadlock with it, and the web webhook
  answers 200 on a rollback, so the loser's paid order is silently lost;
* a swap that releases a gun some OTHER order holds;
* a refusal that leaves the unit half-changed;
* a second press that re-stamps who assigned it and when.

Every refusal test asserts the unit is exactly as it was: a throw that has
already moved the hold is worse than no check.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.reservations import uniform_free_serials
from ffl_integrations.gunbroker import fulfillment, orders
from ffl_integrations.tests.gb_model_fixtures import (
	MODEL,
	SINGLE,
	ModelUnitCase,
)

KEY0, KEY1 = "16087299:0", "16087299:1"
C_AND_R = "9-99-999-03-9A-12345"  # a Type 03 collector's licence number


class _AssignCase(ModelUnitCase):
	def setUp(self):
		super().setUp()
		self.notified: list[tuple] = []
		self._start(patch.object(fulfillment, "notify_channel_commitments_changed",
			side_effect=lambda item, *, source: self.notified.append((item, source))))

	def order_of(self, qty=2, **kw):
		return self.ingest(self.detail(qty=qty, **kw))

	def assign(self, order, key, serial):
		return fulfillment.assign_gb_unit_serial(order.name, key, serial)

	def unit(self, order, key):
		return next(u for u in self.units(order) if f"{u.gb_item_id}:{u.unit_index}" == key)

	def snapshot(self, order, *serials):
		"""Everything a refused call must leave alone."""
		return ([(u.unit_index, u.confirmed_serial or "", u.assigned_by, u.refunded_on_gb)
			for u in self.units(order)], [self.held_by(s) for s in serials])


class AssigningAGun(_AssignCase):
	def test_a_unit_takes_the_gun_and_the_gun_is_held_for_the_order(self):
		a, b = self.stock("GBM-A1"), self.stock("GBM-A2")
		order = self.order_of()
		result = self.assign(order, KEY0, a)
		unit = self.unit(order, KEY0)
		self.assertEqual((result["ok"], result["serial"]), (True, a))
		self.assertEqual(unit.confirmed_serial, a)
		self.assertEqual(unit.assigned_by, frappe.session.user)
		self.assertTrue(unit.assigned_at)
		self.assertEqual(self.held_by(a), (True, order.name))
		self.assertEqual(self.held_by(b), (False, ""))
		# The claim is spent: one open unit left.
		self.assertEqual(orders.channel_committed_units(MODEL), 1)
		self.assertEqual(self.notified, [(MODEL, "gunbroker")])
		# The other storefronts are told the gun is gone.
		orders.fire_serial_channel_exit.assert_called_with(a, reason="sold")

	def test_the_answer_is_the_refreshed_card(self):
		a, _b = self.stock("GBM-A3"), self.stock("GBM-A4")
		order = self.order_of()
		card = self.assign(order, KEY0, a)["row"]
		self.assertEqual(card["gunbroker_order"], order.name)
		units = {u["row"]: u for u in card["assign_units"]}
		self.assertEqual(units[KEY0]["serial"], a)
		self.assertEqual(units[KEY1]["serial"], "")

	def test_the_unit_is_found_by_its_identity_not_its_row_name(self):
		"""Every poll re-issues the child rows' names; the card cannot rely on them."""
		a, _b = self.stock("GBM-A5"), self.stock("GBM-A6")
		detail = self.detail()
		order = self.ingest(detail)
		old_name = self.units(order)[0].name
		order = self.ingest(detail)
		self.assertNotEqual(self.units(order)[0].name, old_name)
		self.assign(order, KEY0, a)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial, a)

	def test_pressing_it_twice_changes_nothing_the_second_time(self):
		a, _b = self.stock("GBM-A7"), self.stock("GBM-A8")
		order = self.order_of()
		self.assign(order, KEY0, a)
		first = self.unit(order, KEY0)
		stamp = (first.assigned_by, first.assigned_at)
		self.notified.clear()
		orders.fire_serial_channel_exit.reset_mock()
		self.assign(order, KEY0, a)
		again = self.unit(order, KEY0)
		self.assertEqual((again.assigned_by, again.assigned_at), stamp)
		self.assertEqual(self.notified, [])
		orders.fire_serial_channel_exit.assert_not_called()
		self.assertEqual(self.held_by(a), (True, order.name))

	def test_a_lock_is_taken_on_the_order_then_the_item_then_the_serial(self):
		a, _b = self.stock("GBM-A9"), self.stock("GBM-A10")
		order = self.order_of()
		calls: list[str] = []
		real_get_doc, real_lock, real_hold = frappe.get_doc, fulfillment.lock_item_reservations, \
			orders._hold_serial

		def get_doc(*args, **kwargs):
			if args and args[0] == "GunBroker Order" and kwargs.get("for_update"):
				calls.append("order")
			return real_get_doc(*args, **kwargs)

		def lock(item):
			calls.append("item")
			return real_lock(item)

		def hold(o, serial):
			calls.append("serial")
			return real_hold(o, serial)

		with patch("frappe.get_doc", side_effect=get_doc), \
				patch.object(fulfillment, "lock_item_reservations", side_effect=lock), \
				patch.object(orders, "_hold_serial", side_effect=hold):
			self.assign(order, KEY0, a)
		self.assertEqual(calls, ["order", "item", "serial"])


class RefusingTheWrongGun(_AssignCase):
	def _refused(self, order, key, serial, *keep, fragment=None):
		before = self.snapshot(order, serial, *keep)
		with self.assertRaises(frappe.ValidationError) as caught:
			self.assign(order, key, serial)
		if fragment:
			self.assertIn(fragment, str(caught.exception))
		self.assertEqual(self.snapshot(order, serial, *keep), before)
		return caught.exception

	def test_an_unknown_serial(self):
		self.stock("GBM-R0")
		order = self.order_of(qty=1)
		before = self.snapshot(order)
		with self.assertRaises(frappe.ValidationError) as caught:
			self.assign(order, KEY0, "NO-SUCH-SERIAL")
		self.assertIn("No such serial", str(caught.exception))
		self.assertEqual(self.snapshot(order), before)

	def test_a_gun_of_another_model(self):
		other = self.stock("GBM-R1", item=SINGLE)
		self.stock("GBM-R1b")
		order = self.order_of(qty=1)
		self._refused(order, KEY0, other, fragment=SINGLE)

	def test_a_gun_of_a_pooled_model_asks_to_be_filed_under_this_one_first(self):
		other = self.stock("GBM-R2", item=SINGLE)
		self.stock("GBM-R2b")
		frappe.db.set_value("Item", SINGLE, "sells_through_item", MODEL)
		order = self.order_of(qty=1)
		self._refused(order, KEY0, other, fragment="File it under")

	def test_a_customers_consignment_gun(self):
		"""cpa A4: the cost and proceeds differ, so it is not store stock."""
		gun = self.stock("GBM-R3", is_consignment=1)
		self.stock("GBM-R3b")
		order = self.order_of(qty=1)
		self._refused(order, KEY0, gun, fragment="consignment")

	def test_a_customers_transfer_gun(self):
		"""cpa A4, the other half."""
		gun = self.stock("GBM-R4", is_transfer_gun=1)
		self.stock("GBM-R4b")
		order = self.order_of(qty=1)
		self._refused(order, KEY0, gun, fragment="transfer")

	def test_a_gun_in_customer_custody(self):
		gun = self.stock("GBM-R5")
		self.stock("GBM-R5b")
		order = self.order_of(qty=1)
		with patch("ffl_core.custody.is_custody_serial", return_value=True):
			self._refused(order, KEY0, gun, fragment="custody")

	def test_a_gun_another_channel_holds(self):
		gun = self.stock("GBM-R6", woo_reserved=1, woo_order="WOO-7")
		self.stock("GBM-R6b")
		order = self.order_of(qty=1)
		self._refused(order, KEY0, gun, fragment="WOO-7")

	def test_a_gun_another_gunbroker_order_holds(self):
		gun = self.stock("GBM-R7")
		self.stock("GBM-R7b")
		other = self.order_of(qty=1)
		self.assign(other, KEY0, gun)
		order = self.order_of(qty=1)
		self._refused(order, KEY0, gun, fragment=other.name)

	def test_a_gun_possibly_already_sold_on_gunbroker(self):
		gun = self.stock("GBM-R8", gb_sold_unknown=1)
		self.stock("GBM-R8b")
		order = self.order_of(qty=1)
		self._refused(order, KEY0, gun)

	def test_a_gun_the_order_already_holds_for_another_unit(self):
		a, _b = self.stock("GBM-R9"), self.stock("GBM-R9b")
		order = self.order_of()
		self.assign(order, KEY0, a)
		self._refused(order, KEY1, a, fragment="already assigned")

	def test_a_young_gun_for_a_curio_and_relic_collector(self):
		young = self.stock("GBM-R10", year_made=2015)
		self.stock("GBM-R10b", year_made=1950)
		order = self.order_of(qty=1, ffl_number=C_AND_R)
		self.assertTrue(order.is_cr_transfer)
		self._refused(order, KEY0, young, fragment="curio")

	def test_an_old_gun_is_fine_for_a_curio_and_relic_collector(self):
		old = self.stock("GBM-R11", year_made=1950)
		order = self.order_of(qty=1, ffl_number=C_AND_R)
		self.assign(order, KEY0, old)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial, old)

	def test_the_models_own_year_counts_when_the_gun_records_none(self):
		frappe.db.set_value("Item", MODEL, "year_made", 1940)
		self.addCleanup(frappe.db.set_value, "Item", MODEL, "year_made", 0)
		gun = self.stock("GBM-R12")
		order = self.order_of(qty=1, ffl_number=C_AND_R)
		self.assign(order, KEY0, gun)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial, gun)

	def test_an_order_that_is_not_pending_fulfillment(self):
		gun = self.stock("GBM-R13")
		order = self.order_of(qty=1)
		frappe.db.set_value("GunBroker Order", order.name, "order_status", "Transferred")
		with self.assertRaises(frappe.ValidationError):
			self.assign(order, KEY0, gun)
		self.assertEqual(self.held_by(gun), (False, ""))

	def test_an_order_cancelled_on_gunbroker(self):
		gun = self.stock("GBM-R14")
		order = self.order_of(qty=1)
		frappe.db.set_value("GunBroker Order", order.name, "gb_status", 6)
		with self.assertRaises(frappe.ValidationError):
			self.assign(order, KEY0, gun)

	def test_a_unit_that_does_not_exist(self):
		gun = self.stock("GBM-R15")
		order = self.order_of(qty=1)
		with self.assertRaises(frappe.ValidationError):
			self.assign(order, "16087299:9", gun)

	def test_a_per_gun_line_is_not_a_unit(self):
		gun = self.stock("GBM-R16", item=SINGLE)
		order = self.ingest(self.detail(sku=f"{SINGLE}::{gun}", qty=1))
		with self.assertRaises(frappe.ValidationError):
			self.assign(order, f"16087299:{order.serials[0].unit_index}", gun)

	def test_a_refused_swap_keeps_the_gun_the_unit_already_had(self):
		old, bad = self.stock("GBM-R17"), self.stock("GBM-R17b", woo_reserved=1, woo_order="W-1")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, old)
		self._refused(order, KEY0, bad, old)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial, old)
		self.assertEqual(self.held_by(old), (True, order.name))

	def test_the_role_gate_is_asked_first(self):
		order = self.order_of(qty=1)
		with patch("frappe.only_for", side_effect=frappe.PermissionError) as gate:
			with self.assertRaises(frappe.PermissionError):
				self.assign(order, KEY0, "X")
		gate.assert_called_once()


class ChangingYourMind(_AssignCase):
	def test_swapping_releases_the_old_gun_and_holds_the_new_one(self):
		old, new = self.stock("GBM-S1"), self.stock("GBM-S2")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, old)
		self.assign(order, KEY0, new)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial, new)
		self.assertEqual(self.held_by(old), (False, ""))
		self.assertEqual(self.held_by(new), (True, order.name))

	def test_swapping_never_releases_a_gun_another_order_holds(self):
		"""The old gun may have been taken since: letting it go would free a rifle
		some other order is waiting on."""
		old, new = self.stock("GBM-S3"), self.stock("GBM-S4")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, old)
		frappe.db.set_value("Serial No", old, {"gb_order": "GB-ORD-OTHER"})
		self.assign(order, KEY0, new)
		self.assertEqual(self.held_by(old), (True, "GB-ORD-OTHER"))
		self.assertEqual(self.held_by(new), (True, order.name))

	def test_unassigning_gives_the_gun_back_and_reopens_the_claim(self):
		a, _b = self.stock("GBM-S5"), self.stock("GBM-S6")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, a)
		self.assertEqual(orders.channel_committed_units(MODEL), 0)
		result = self.assign(order, KEY0, "")
		unit = self.unit(order, KEY0)
		self.assertEqual(result["serial"], "")
		self.assertEqual((unit.confirmed_serial or "", unit.assigned_by, unit.assigned_at),
			("", None, None))
		self.assertEqual(self.held_by(a), (False, ""))
		self.assertEqual(orders.channel_committed_units(MODEL), 1)
		self.assertEqual(self.notified[-1], (MODEL, "gunbroker"))

	def test_unassigning_a_unit_with_no_gun_does_nothing(self):
		self.stock("GBM-S7")
		order = self.order_of(qty=1)
		self.notified.clear()
		self.assign(order, KEY0, "")
		self.assertEqual(self.notified, [])

	def test_a_gun_whose_bound_book_entry_is_booked_cannot_be_swapped(self):
		a, b = self.stock("GBM-S8"), self.stock("GBM-S9")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, a)
		with patch.object(frappe.db, "exists", side_effect=lambda dt, *a, **kw:
				dt == "FFL Disposition" or frappe.db.get_value(dt, *a, **kw) is not None):
			with self.assertRaises(frappe.ValidationError):
				self.assign(order, KEY0, b)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial, a)

	def test_the_assignment_never_touches_the_stock_ledger(self):
		"""cpa A3: specific identification. Cost follows the gun that finally
		leaves; a gun that was assigned and then swapped out never moved, so it has
		no ledger entry to reverse."""
		a, b = self.stock("GBM-S10", rate=210), self.stock("GBM-S11", rate=260)
		order = self.order_of(qty=1)
		before = frappe.db.count("Stock Ledger Entry")
		gl = frappe.db.count("GL Entry")
		self.assign(order, KEY0, a)
		self.assign(order, KEY0, b)
		self.assertEqual((frappe.db.count("Stock Ledger Entry"), frappe.db.count("GL Entry")),
			(before, gl))


class MarkingAUnitRefunded(_AssignCase):
	def test_it_leaves_the_claims_and_needs_no_gun(self):
		self.stock("GBM-M1")
		order = self.order_of()
		self.assertEqual(orders.channel_committed_units(MODEL), 2)
		result = fulfillment.mark_gb_unit_refunded(order.name, KEY1)
		unit = self.unit(order, KEY1)
		self.assertTrue(result["refunded"])
		self.assertEqual((bool(unit.refunded_on_gb), unit.confirmed_serial or ""), (True, ""))
		self.assertEqual(unit.assigned_by, frappe.session.user)
		self.assertEqual(orders.channel_committed_units(MODEL), 1)
		self.assertEqual(self.notified, [(MODEL, "gunbroker")])

	def test_a_unit_that_already_has_a_gun_gives_it_back(self):
		a = self.stock("GBM-M2")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, a)
		fulfillment.mark_gb_unit_refunded(order.name, KEY0)
		self.assertEqual(self.held_by(a), (False, ""))
		self.assertEqual(self.unit(order, KEY0).confirmed_serial or "", "")

	def test_it_can_be_undone(self):
		self.stock("GBM-M3")
		order = self.order_of(qty=1)
		fulfillment.mark_gb_unit_refunded(order.name, KEY0)
		fulfillment.mark_gb_unit_refunded(order.name, KEY0, refunded=0)
		unit = self.unit(order, KEY0)
		self.assertEqual((bool(unit.refunded_on_gb), unit.assigned_by), (False, None))
		self.assertEqual(orders.channel_committed_units(MODEL), 1)

	def test_marking_twice_is_a_no_op(self):
		self.stock("GBM-M4")
		order = self.order_of(qty=1)
		fulfillment.mark_gb_unit_refunded(order.name, KEY0)
		self.notified.clear()
		fulfillment.mark_gb_unit_refunded(order.name, KEY0)
		self.assertEqual(self.notified, [])

	def test_a_refunded_unit_will_not_take_a_gun(self):
		a = self.stock("GBM-M5")
		order = self.order_of(qty=1)
		fulfillment.mark_gb_unit_refunded(order.name, KEY0)
		with self.assertRaises(frappe.ValidationError):
			self.assign(order, KEY0, a)
		self.assertEqual(self.held_by(a), (False, ""))


class TheCardOffersGuns(_AssignCase):
	def cards(self):
		return {r["gunbroker_order"]: r for r in fulfillment.list_pending_gb_orders()}

	def test_each_unit_is_suggested_the_oldest_free_gun_without_repeats(self):
		guns = [self.stock(f"GBM-P{n}") for n in range(5)]
		for age, gun in zip((500, 400, 300, 200, 100), guns):
			self.age(gun, age)
		first = self.order_of(qty=2)
		second = self.order_of(qty=2)
		cards = self.cards()
		suggested_first = [u["suggested"] for u in cards[first.name]["assign_units"]]
		suggested_second = [u["suggested"] for u in cards[second.name]["assign_units"]]
		self.assertEqual(suggested_first, guns[0:2])
		self.assertEqual(suggested_second, guns[2:4])

	def test_the_oldest_order_gets_the_oldest_gun(self):
		guns = [self.stock(f"GBM-P1{n}") for n in range(3)]
		for age, gun in zip((300, 200, 100), guns):
			self.age(gun, age)
		first = self.order_of(qty=1)
		second = self.order_of(qty=1)
		cards = self.cards()
		self.assertEqual(cards[first.name]["assign_units"][0]["suggested"], guns[0])
		self.assertEqual(cards[second.name]["assign_units"][0]["suggested"], guns[1])

	def test_more_units_than_guns_leaves_the_rest_with_no_suggestion(self):
		gun = self.stock("GBM-P20")
		order = self.order_of(qty=2)
		units = self.cards()[order.name]["assign_units"]
		self.assertEqual([u["suggested"] for u in units], [gun, ""])

	def test_a_curio_and_relic_order_is_only_suggested_curios(self):
		young = self.stock("GBM-P30", year_made=2015)
		old = self.stock("GBM-P31", year_made=1950)
		self.age(young, 900)  # older on the shelf, but ineligible
		order = self.order_of(qty=1, ffl_number=C_AND_R)
		card = self.cards()[order.name]
		self.assertEqual(card["assign_units"][0]["suggested"], old)
		self.assertEqual([c["serial"] for c in card["assign_candidates"][MODEL]], [old])

	def test_an_ordinary_order_is_offered_every_free_gun_oldest_first(self):
		guns = [self.stock(f"GBM-P4{n}") for n in range(3)]
		for age, gun in zip((300, 200, 100), guns):
			self.age(gun, age)
		order = self.order_of(qty=1)
		picks = self.cards()[order.name]["assign_candidates"][MODEL]
		self.assertEqual([c["serial"] for c in picks], guns)
		self.assertIn("received", picks[0]["label"])

	def test_the_pick_list_is_capped(self):
		for n in range(30):
			self.stock(f"GBM-P5{n}")
		order = self.order_of(qty=1)
		self.assertEqual(len(self.cards()[order.name]["assign_candidates"][MODEL]), 25)

	def test_a_suggestion_holds_nothing(self):
		gun = self.stock("GBM-P60")
		order = self.order_of(qty=1)
		self.cards()
		self.assertEqual(self.held_by(gun), (False, ""))
		self.assertEqual(orders.channel_committed_units(MODEL), 1)
		self.assertEqual(self.unit(order, KEY0).confirmed_serial or "", "")

	def test_an_assigned_unit_shows_its_gun_and_no_suggestion(self):
		a, b = self.stock("GBM-P70"), self.stock("GBM-P71")
		order = self.order_of(qty=1)
		self.assign(order, KEY0, a)
		unit = self.cards()[order.name]["assign_units"][0]
		self.assertEqual((unit["serial"], unit["suggested"]), (a, ""))
		self.assertEqual(unit["assigned_by"], frappe.session.user)
		self.assertEqual(uniform_free_serials(MODEL), [b])

	def test_the_card_stays_out_of_the_waiting_section_and_asks_for_dispose(self):
		self.stock("GBM-P80")
		order = self.order_of(qty=1)
		card = self.cards()[order.name]
		self.assertFalse(card["awaiting_serial"])
		self.assertTrue(card["needs_disposition"])
		self.assertNotEqual(card["stage"], "waiting")

	def test_a_refunded_unit_needs_nothing_and_is_flagged(self):
		self.stock("GBM-P90")
		order = self.order_of(qty=2)
		fulfillment.mark_gb_unit_refunded(order.name, KEY1)
		units = self.cards()[order.name]["assign_units"]
		self.assertEqual([(u["refunded"], u["suggested"] == "") for u in units],
			[(False, False), (True, True)])

	def test_a_per_gun_order_has_no_units(self):
		gun = self.stock("GBM-P91", item=SINGLE)
		order = self.ingest(self.detail(sku=f"{SINGLE}::{gun}", qty=1))
		card = self.cards()[order.name]
		self.assertEqual((card["assign_units"], card["assign_candidates"]), ([], {}))
		self.assertTrue(card["needs_disposition"])

	def test_a_unit_whose_gun_was_lost_carries_the_conflict_on_the_card(self):
		a, _b = self.stock("GBM-P92"), self.stock("GBM-P93")
		detail = self.detail(qty=1)
		order = self.ingest(detail)
		self.assign(order, KEY0, a)
		frappe.db.set_value("Serial No", a, {"gb_reserved": 0, "gb_order": "",
			"woo_reserved": 1, "woo_order": "WOO-3"})
		self.ingest(detail)
		card = self.cards()[order.name]
		self.assertIn("WOO-3", card["assign_units"][0]["error"])
		self.assertTrue(card["serial_errors"])


class TheDisposeGate(_AssignCase):
	def ready(self, qty=2):
		order = self.order_of(qty=qty)
		frappe.db.set_value("GunBroker Order", order.name, {
			"payment_mode_used": "GunBroker Check",
			"payment_received_on": frappe.utils.nowdate(), "payment_amount": 1038})
		order.reload()
		return order

	def gate(self, order):
		fulfillment._require_ready_to_dispose(order)

	def test_an_unassigned_unit_refuses_dispose(self):
		a = self.stock("GBM-G1")
		self.stock("GBM-G2")
		order = self.ready()
		self.assign(order, KEY0, a)
		order.reload()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.gate(order)
		self.assertIn("no gun assigned", str(caught.exception))

	def test_every_unit_assigned_passes(self):
		a, b = self.stock("GBM-G3"), self.stock("GBM-G4")
		order = self.ready()
		self.assign(order, KEY0, a)
		self.assign(order, KEY1, b)
		order.reload()
		self.gate(order)  # no raise

	def test_a_unit_marked_refunded_counts_as_settled(self):
		a = self.stock("GBM-G5")
		order = self.ready()
		self.assign(order, KEY0, a)
		fulfillment.mark_gb_unit_refunded(order.name, KEY1)
		order.reload()
		self.gate(order)  # no raise

	def test_a_gun_another_order_has_since_taken_refuses_dispose(self):
		a, b = self.stock("GBM-G6"), self.stock("GBM-G7")
		order = self.ready()
		self.assign(order, KEY0, a)
		self.assign(order, KEY1, b)
		frappe.db.set_value("Serial No", b, {"gb_order": "GB-ORD-OTHER"})
		order.reload()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.gate(order)
		self.assertIn("GB-ORD-OTHER", str(caught.exception))

	def test_a_gun_nobody_holds_any_more_refuses_dispose(self):
		a, b = self.stock("GBM-G8"), self.stock("GBM-G9")
		order = self.ready()
		self.assign(order, KEY0, a)
		self.assign(order, KEY1, b)
		frappe.db.set_value("Serial No", b, {"gb_reserved": 0, "gb_order": ""})
		order.reload()
		with self.assertRaises(frappe.ValidationError):
			self.gate(order)

	def test_a_unit_carrying_a_reservation_error_refuses_dispose(self):
		a = self.stock("GBM-G10")
		order = self.ready(qty=1)
		self.assign(order, KEY0, a)
		frappe.db.set_value("GunBroker Order Serial", self.unit(order, KEY0).name,
			"reservation_error", "Held by woo WOO-1")
		order.reload()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.gate(order)
		self.assertIn("WOO-1", str(caught.exception))

	def test_an_order_with_every_unit_refunded_has_nothing_to_dispose(self):
		self.stock("GBM-G11")
		order = self.ready(qty=1)
		fulfillment.mark_gb_unit_refunded(order.name, KEY0)
		order.reload()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.gate(order)
		self.assertIn("no gun matched", str(caught.exception))
