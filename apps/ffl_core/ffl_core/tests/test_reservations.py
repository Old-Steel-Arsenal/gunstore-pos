"""Reserved-serial helpers behind the Item-form Pending badge."""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.test_build_order import INDIV, BuildOrderMixin


class TestSiPendingFirearmSerials(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin (not BuildOrderFixtures) on purpose — the fixture CLASS
	carries commit-heavy test methods unittest would re-run per subclass."""
	def test_shipped_deferred_serial_is_pending_until_disposed(self):
		from ffl_core.api.manual_order import dispose_order
		from ffl_core.reservations import si_pending_firearm_serials
		serial = self._stocked_serial("RSV-S1")
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		rows = si_pending_firearm_serials(frappe.get_doc("Sales Invoice", si.name))
		self.assertIn({"item_code": "BO-GUN", "serial": serial}, rows)
		# After disposition the serial drops out (it now has a submitted disposition).
		dispose_order(si.name)
		rows = si_pending_firearm_serials(frappe.get_doc("Sales Invoice", si.name))
		self.assertNotIn(serial, [r["serial"] for r in rows])


class TestReservedCounterSerials(BuildOrderMixin, FrappeTestCase):
	def test_sales_invoice_serial_is_flagged(self):
		from ffl_core.reservations import reserved_counter_serials
		serial = self._stocked_serial("RSV-C1")
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		with patch("ffl_core.reservations._draft_pos_serials", return_value=[]):
			out = reserved_counter_serials("BO-GUN")
		self.assertIn(serial, out)
		self.assertEqual(out[serial]["source"], "sales_invoice")
		self.assertEqual(out[serial]["ref"], si.name)
		self.assertEqual(out[serial]["url"], f"/app/sales-invoice/{si.name}")

	def test_web_order_revenue_invoice_is_not_a_counter_hold(self):
		"""A web order's revenue SI carries the buyer's FFL like a counter order,
		but its disposition is booked before the SI exists and links the Woo
		Online Order, not the SI — so the SI can never "see" the gun disposed and
		would flag it Pending forever (real case: every web-sold CZ 75 on the Item
		form). Web holds belong to reserved_web_serials; this side must skip them."""
		from ffl_core.reservations import reserved_counter_serials
		serial = self._stocked_serial("RSV-C4")
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name, "woo_online_order", "WOO-ORD-TEST-1",
			update_modified=False)
		with patch("ffl_core.reservations._draft_pos_serials", return_value=[]):
			out = reserved_counter_serials("BO-GUN")
		self.assertNotIn(serial, out)

	def test_consolidated_and_consignment_invoices_are_not_counter_holds(self):
		"""Same shape as the web case — the fulfilment record lives elsewhere, so
		the SI can never see its gun disposed (mirrors list_pending_dispositions)."""
		from ffl_core.reservations import reserved_counter_serials
		s1 = self._stocked_serial("RSV-C5")
		si1 = self._ship(s1, customer=INDIV, update_stock=0, buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si1.name, "is_consolidated", 1, update_modified=False)
		s2 = self._stocked_serial("RSV-C6")
		si2 = self._ship(s2, customer=INDIV, update_stock=0, buyer_type="Business")
		# An extension claims si2 through a declared exclusion field (remarks stands
		# in for the Link field an extension would own).
		frappe.db.set_value("Sales Invoice", si2.name, "remarks", "settled elsewhere",
			update_modified=False)
		from ffl_core.tests.test_extension_hooks import hooks
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks"]), \
				patch("ffl_core.reservations._draft_pos_serials", return_value=[]):
			out = reserved_counter_serials("BO-GUN")
		self.assertNotIn(s1, out)
		self.assertNotIn(s2, out)

	def test_other_items_serial_not_flagged(self):
		from ffl_core.reservations import reserved_counter_serials
		serial = self._stocked_serial("RSV-C2")
		self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		with patch("ffl_core.reservations._draft_pos_serials", return_value=[]):
			out = reserved_counter_serials("SOME-OTHER-ITEM")
		self.assertNotIn(serial, out)

	def test_draft_pos_serial_is_flagged(self):
		from ffl_core.reservations import reserved_counter_serials
		# No pending Sales Invoices for this throwaway item; POS half mocked.
		with patch("ffl_core.reservations._draft_pos_serials",
				   return_value=[("POS-SN-9", "POSINV-DRAFT-1")]):
			out = reserved_counter_serials("PARKED-GUN")
		self.assertEqual(out["POS-SN-9"]["source"], "pos")
		self.assertEqual(out["POS-SN-9"]["ref"], "POSINV-DRAFT-1")
		self.assertEqual(out["POS-SN-9"]["url"], "/app/pos-invoice/POSINV-DRAFT-1")

	def test_non_sales_user_gets_empty_no_leak(self):
		"""A user without a shipping role must not enumerate reserved serials."""
		from ffl_core.reservations import reserved_counter_serials
		serial = self._stocked_serial("RSV-C3")
		self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		with patch("frappe.get_roles", return_value=["Stock User"]):
			out = reserved_counter_serials("BO-GUN")
		self.assertEqual(out, {})



class AnAuctionWithBidsIsAHold(FrappeTestCase):
	"""`gb_bid_locked` sits beside `gb_reserved`: same channel, its own words."""

	def test_the_lock_is_a_gunbroker_hold_that_names_the_listing(self):
		from ffl_core import reservations

		row = {"woo_reserved": 0, "woo_order": "", "gb_reserved": 0, "gb_order": "",
			"gb_bid_locked": 1, "gb_item_id": "16087230"}
		with patch.object(reservations, "_channel_fields_present",
				return_value=reservations.CHANNEL_RESERVE_FIELDS):
			held = reservations.channel_reservation("SN-1", row=row)
		self.assertEqual(held["channel"], reservations.CHANNEL_GUNBROKER)
		self.assertEqual(held["field"], reservations.BID_LOCK_FIELD)
		self.assertEqual(held["ref"], "16087230")
		reason = reservations.channel_hold_reason("SN-1", held)
		self.assertIn("bids in a GunBroker auction", reason)
		self.assertIn("16087230", reason)
		self.assertNotIn("already made", reason, "nothing has been sold yet")

	def test_gunbroker_does_not_see_its_own_promise_as_somebody_elses_hold(self):
		from ffl_core import reservations

		row = {"woo_reserved": 0, "woo_order": "", "gb_reserved": 0, "gb_order": "",
			"gb_bid_locked": 1, "gb_item_id": "16087230"}
		with patch.object(reservations, "_channel_fields_present",
				return_value=reservations.CHANNEL_RESERVE_FIELDS):
			self.assertIsNone(reservations.other_channel_reservation(
				"SN-1", own=reservations.CHANNEL_GUNBROKER, row=row))
			self.assertIsNotNone(reservations.other_channel_reservation(
				"SN-1", own=reservations.CHANNEL_WOO, row=row))
