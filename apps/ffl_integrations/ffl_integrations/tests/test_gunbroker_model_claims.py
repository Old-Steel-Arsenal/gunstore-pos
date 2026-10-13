"""A model-level GunBroker sale is a CLAIM until staff name the gun.

One listing sells N interchangeable guns of a uniform-price model. The order owes
a unit per piece sold and reserves no `Serial No`, so nothing per-gun can see it:
the claim is what makes every channel's available count drop, and the rows that
carry it must survive the poll that rebuilds the table every fifteen minutes.

Each class is one way this goes wrong without looking wrong:

* ``IngestingAModelLevelOrder`` — qty N must become N rows (not one, not zero),
  must not look like an unmatched line, and a bare SKU for a gun priced one by
  one is STILL an unmatched line (the only thing that line may mean is a listing
  made by hand on the website, and waving it through would ship nobody's gun).
* ``AssignmentsSurviveTheNextPoll`` — the poll rebuilds the serial table; losing a
  person's assignment would turn finished work back into an open claim, hourly.
* ``ClaimsAreCountedAndReleased`` — the count the other channels subtract, and
  what gives the units back (a cancellation, a smaller quantity).
* ``AnOversoldOrderIsStillRecorded`` — the money is already taken; recording the
  sale is never optional, shouting about it is.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core.reservations import uniform_free_serials, uniform_sellable_units
from ffl_integrations.gunbroker import orders
from ffl_integrations.tests.gb_model_fixtures import AMMO, MODEL, SINGLE, ModelUnitCase


class IngestingAModelLevelOrder(ModelUnitCase):
	def test_a_quantity_of_two_becomes_two_claims_and_no_unmatched_alert(self):
		self.stock("GBM-A1")
		self.stock("GBM-A2")
		order = self.ingest(self.detail(qty=2))
		units = self.units(order)
		self.assertEqual([(u.unit_index, u.item_code, u.confirmed_serial or "") for u in units],
			[(0, MODEL, ""), (1, MODEL, "")])
		self.assertEqual({u.gb_item_id for u in units}, {"16087299"})
		self.assertFalse((order.lines[0].reservation_error or "").strip())
		self.assertEqual([a for a in self.alerts if a.startswith("gb_unmatched_")], [])

	def test_a_claim_reserves_no_gun(self):
		serial = self.stock("GBM-B1")
		self.ingest(self.detail(qty=1))
		self.assertEqual(self.held_by(serial), (False, ""))
		self.assertEqual(self.held_by(serial)[0], False)

	def test_a_model_level_line_is_never_issued_from_stock_by_quantity(self):
		"""`is_item_level_line` must stay false for a gun: it is what makes
		Dispose issue a quantity with no serial and no bound-book entry."""
		order = self.ingest(self.detail(qty=2))
		self.assertFalse(orders.is_item_level_line(order.lines[0]))
		self.assertTrue(orders.is_model_unit_line(order.lines[0]))

	def test_the_two_line_kinds_are_disjoint(self):
		for sku, item_level, model in ((AMMO, True, False), (MODEL, False, True),
				(SINGLE, False, False), (f"{MODEL}::SN1", False, False),
				("NO-SUCH-ITEM", False, False)):
			line = frappe._dict(sku=sku, serial_number="", gb_item_id="1")
			with self.subTest(sku=sku):
				self.assertEqual(orders.is_item_level_line(line), item_level)
				self.assertEqual(orders.is_model_unit_line(line), model)

	def test_a_bare_sku_for_a_gun_priced_one_by_one_is_still_unmatched(self):
		"""R2-9. Only a hand-made listing can sell this, and it names no gun."""
		self.stock("GBM-S1", item=SINGLE)
		order = self.ingest(self.detail(sku=SINGLE, qty=1))
		self.assertEqual([r for r in order.serials], [])
		self.assertIn("Unmatched GunBroker item", order.lines[0].reservation_error)
		self.assertTrue(any(a.startswith("gb_unmatched_") for a in self.alerts))

	def test_each_unit_of_a_per_gun_listing_is_unchanged(self):
		serial = self.stock("GBM-S2", item=SINGLE)
		order = self.ingest(self.detail(sku=f"{SINGLE}::{serial}", qty=1))
		(row,) = order.serials
		self.assertEqual((row.confirmed_serial, bool(row.is_model_unit)), (serial, False))
		self.assertEqual(self.held_by(serial), (True, order.name))


class AssignmentsSurviveTheNextPoll(ModelUnitCase):
	def _assigned(self):
		"""An ingested qty-2 order whose first unit staff have assigned."""
		first, second = self.stock("GBM-C1"), self.stock("GBM-C2")
		detail = self.detail(qty=2)
		order = self.ingest(detail)
		unit = self.units(order)[0]
		# What the assignment endpoint writes, without the endpoint (tested elsewhere).
		frappe.db.set_value("Serial No", first, {"gb_reserved": 1, "gb_order": order.name})
		frappe.db.set_value("GunBroker Order Serial", unit.name, {
			"confirmed_serial": first, "assigned_by": "Administrator",
			"assigned_at": "2026-10-10 09:00:00"})
		return detail, order, first, second

	def test_re_polling_keeps_the_assigned_gun_and_who_assigned_it(self):
		detail, order, first, _second = self._assigned()
		order = self.ingest(detail)
		units = self.units(order)
		self.assertEqual(units[0].confirmed_serial, first)
		self.assertEqual(units[0].assigned_by, "Administrator")
		self.assertEqual(str(units[0].assigned_at)[:16], "2026-10-10 09:00")
		self.assertEqual(units[1].confirmed_serial or "", "")
		self.assertEqual(self.held_by(first), (True, order.name))

	def test_re_polling_re_confirms_the_hold_when_it_has_been_lost(self):
		"""The hold is re-asserted every poll (idempotently), not trusted."""
		detail, order, first, _second = self._assigned()
		frappe.db.set_value("Serial No", first, {"gb_reserved": 0, "gb_order": ""})
		order = self.ingest(detail)
		self.assertEqual(self.held_by(first), (True, order.name))
		self.assertEqual(self.units(order)[0].confirmed_serial, first)

	def test_a_gun_taken_by_someone_else_is_reported_on_that_unit_only(self):
		detail, order, first, _second = self._assigned()
		frappe.db.set_value("Serial No", first, {
			"gb_reserved": 0, "gb_order": "", "woo_reserved": 1, "woo_order": "WOO-9"})
		order = self.ingest(detail)
		units = self.units(order)
		self.assertIn("WOO-9", units[0].reservation_error)
		self.assertFalse((units[1].reservation_error or "").strip())
		self.assertIn("WOO-9", order.lines[0].reservation_error)
		self.assertTrue(any(a.startswith("gb_conflict_") for a in self.alerts))
		# The assignment itself is kept for a person to resolve.
		self.assertEqual(units[0].confirmed_serial, first)

	def test_a_refunded_mark_survives_the_poll(self):
		detail, order, _first, _second = self._assigned()
		unit = self.units(order)[1]
		frappe.db.set_value("GunBroker Order Serial", unit.name, "refunded_on_gb", 1)
		order = self.ingest(detail)
		self.assertEqual([bool(u.refunded_on_gb) for u in self.units(order)], [False, True])

	def test_a_smaller_quantity_gives_back_the_gun_it_no_longer_needs(self):
		detail, order, first, second = self._assigned()
		detail["orderItemsCollection"][0]["quantity"] = 1
		detail["orderItemsCollection"][0]["itemSubTotal"] = 499.0
		order = self.ingest(detail)
		self.assertEqual(len(self.units(order)), 1)
		self.assertEqual(self.units(order)[0].confirmed_serial, first)  # unit 0 stays
		# And dropping the assigned unit itself releases ITS gun.
		detail["orderItemsCollection"][0]["quantity"] = 2
		order = self.ingest(detail)
		other = self.units(order)[1]
		frappe.db.set_value("Serial No", second, {"gb_reserved": 1, "gb_order": order.name})
		frappe.db.set_value("GunBroker Order Serial", other.name, "confirmed_serial", second)
		detail["orderItemsCollection"][0]["quantity"] = 1
		self.ingest(detail)
		self.assertEqual(self.held_by(second), (False, ""))
		self.assertTrue(any(a.startswith("gb_unit_dropped_") for a in self.alerts))


class ClaimsAreCountedAndReleased(ModelUnitCase):
	def _claims(self):
		return orders.channel_committed_units(MODEL)

	def test_the_count_is_the_unassigned_units_on_pending_orders(self):
		guns = [self.stock(f"GBM-D{n}") for n in range(4)]
		before = self._claims()
		a = self.ingest(self.detail(qty=2))
		self.ingest(self.detail(qty=1))
		self.assertEqual(self._claims(), before + 3)
		# Naming a gun for one unit takes it off the claims.
		frappe.db.set_value("GunBroker Order Serial", self.units(a)[0].name,
			"confirmed_serial", guns[0])
		self.assertEqual(self._claims(), before + 2)

	def test_a_refunded_unit_is_not_a_claim(self):
		self.stock("GBM-D5")
		before = self._claims()
		order = self.ingest(self.detail(qty=2))
		frappe.db.set_value("GunBroker Order Serial", self.units(order)[1].name,
			"refunded_on_gb", 1)
		self.assertEqual(self._claims(), before + 1)

	def test_a_shipped_order_no_longer_counts(self):
		self.stock("GBM-D6")
		before = self._claims()
		order = self.ingest(self.detail(qty=1))
		frappe.db.set_value("GunBroker Order", order.name, "order_status", "Transferred")
		self.assertEqual(self._claims(), before)

	def test_the_claim_takes_a_unit_off_what_every_channel_may_sell(self):
		for n in range(3):
			self.stock(f"GBM-E{n}")
		self.assertEqual(uniform_sellable_units(MODEL), 3)
		self.ingest(self.detail(qty=2))
		self.assertEqual(uniform_sellable_units(MODEL), 1)

	def test_the_other_channels_are_told_when_claims_appear(self):
		self.stock("GBM-F1")
		self.ingest(self.detail(qty=1))
		self.assertEqual(self.heard, [(MODEL, "gunbroker")])

	def test_an_unchanged_re_poll_announces_nothing(self):
		self.stock("GBM-F2")
		detail = self.detail(qty=1)
		self.ingest(detail)
		self.heard.clear()
		self.ingest(detail)
		self.assertEqual(self.heard, [])

	def test_a_cancellation_releases_the_claims_and_the_assigned_guns(self):
		first = self.stock("GBM-G1")
		self.stock("GBM-G2")
		detail = self.detail(qty=2)
		order = self.ingest(detail)
		unit = self.units(order)[0]
		frappe.db.set_value("Serial No", first, {"gb_reserved": 1, "gb_order": order.name})
		frappe.db.set_value("GunBroker Order Serial", unit.name, "confirmed_serial", first)
		self.assertEqual(self._claims(), 1)
		self.heard.clear()
		detail["status"] = 6  # cancelled on GunBroker
		order = self.ingest(detail)
		self.assertEqual(order.order_status, "Cancelled")
		self.assertEqual(self._claims(), 0)
		self.assertEqual(self.held_by(first), (False, ""))
		self.assertEqual(self.heard, [(MODEL, "gunbroker")])
		self.assertEqual(uniform_sellable_units(MODEL), 2)

	def test_an_ammunition_order_still_counts_by_quantity(self):
		before = orders.channel_committed_units(AMMO)
		self.ingest(self.detail(sku=AMMO, qty=3))
		self.assertEqual(orders.channel_committed_units(AMMO), before + 3)


class AnOversoldOrderIsStillRecorded(ModelUnitCase):
	def test_more_units_than_free_guns_alerts_and_keeps_every_claim(self):
		self.stock("GBM-H1")
		order = self.ingest(self.detail(qty=2))
		self.assertEqual(len(self.units(order)), 2)
		self.assertTrue(any(a.startswith("gb_oversold_") for a in self.alerts), self.alerts)
		self.assertFalse((order.lines[0].reservation_error or "").strip())
		self.assertFalse(any((u.reservation_error or "").strip() for u in self.units(order)))
		self.assertEqual(uniform_sellable_units(MODEL), 0)  # floored, never negative

	def test_a_filled_order_does_not_alert(self):
		self.stock("GBM-H2")
		self.stock("GBM-H3")
		self.ingest(self.detail(qty=2))
		self.assertEqual([a for a in self.alerts if a.startswith("gb_oversold_")], [])

	def test_the_other_channels_claims_count_towards_oversell(self):
		self.stock("GBM-H4")
		self.stock("GBM-H5")
		with patch.object(orders, "committed_units",
				return_value={"gunbroker": 1, "woo": 2}):
			order = self.ingest(self.detail(qty=1))
		self.assertTrue(any(a.startswith("gb_oversold_") for a in self.alerts))
		self.assertEqual(len(self.units(order)), 1)

	def test_a_channel_that_cannot_answer_never_loses_the_order(self):
		self.stock("GBM-H6")
		with patch.object(orders, "committed_units", side_effect=RuntimeError("woo down")), \
				patch.object(orders, "safe_log_error") as logged:
			order = self.ingest(self.detail(qty=1))
		self.assertEqual(len(self.units(order)), 1)
		logged.assert_called()
		self.assertEqual([a for a in self.alerts if a.startswith("gb_oversold_")], [])

	def test_the_free_pool_is_what_the_check_reads(self):
		serial = self.stock("GBM-H7")
		self.assertEqual(uniform_free_serials(MODEL), [serial])


class FreshChildRowsAfterTheLock(ModelUnitCase):
	"""Review r1: after the order lock, child rows must come from a locking read, or
	an assignment committed while the poll waited is written back over."""

	def test_the_reload_replaces_stale_in_memory_rows_with_the_stored_ones(self):
		gun = self.stock("GBM-F1")
		order = self.ingest(self.detail(qty=2))
		stale = frappe.get_doc("GunBroker Order", order.name)
		frappe.db.set_value("GunBroker Order Serial", self.units(order)[0].name,
			{"confirmed_serial": gun, "assigned_by": "Administrator"})
		self.assertEqual(stale.serials[0].confirmed_serial or "", "")
		orders.reload_children_for_update(stale)
		units = sorted((r for r in stale.serials if r.is_model_unit), key=lambda r: r.unit_index)
		self.assertEqual(units[0].confirmed_serial, gun)
		self.assertEqual(units[0].assigned_by, "Administrator")
		self.assertEqual(len(stale.lines), len(order.lines))

	def test_a_re_poll_of_an_existing_order_reloads_under_the_lock(self):
		detail = self.detail(qty=1)
		self.stock("GBM-F2")
		self.ingest(detail)
		with patch.object(orders, "reload_children_for_update",
				wraps=orders.reload_children_for_update) as reload:
			self.ingest(detail)
		reload.assert_called_once()


class AUnitThatLostItsGunIsStillOwed(ModelUnitCase):
	"""Review r1: an assigned unit whose hold failed (reservation_error) still owes
	the buyer a gun, so it counts as a claim until a person fixes it."""

	def test_an_errored_unit_counts_as_a_claim(self):
		gun = self.stock("GBM-E1")
		before = orders.channel_committed_units(MODEL)
		order = self.ingest(self.detail(qty=1))
		unit = self.units(order)[0]
		frappe.db.set_value("GunBroker Order Serial", unit.name, "confirmed_serial", gun)
		self.assertEqual(orders.channel_committed_units(MODEL), before)
		frappe.db.set_value("GunBroker Order Serial", unit.name, "reservation_error",
			"held for WOO-9")
		self.assertEqual(orders.channel_committed_units(MODEL), before + 1)
		order = frappe.get_doc("GunBroker Order", order.name)
		self.assertEqual(orders._claim_commitments(order), {MODEL: 1})
