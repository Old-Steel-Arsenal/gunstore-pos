"""A web order whose receiving FFL is OUR OWN licence is a counter pickup.

WooCommerce's FFL picker lists the store like any other dealer, so a local buyer
picks us and adds "I can pick up in person" in the order notes (a real web order,
2026-08-15). Nothing recognised that shape, and the Dispose button would have
booked `FFL Transfer -> TEST STORE A INC` — a transfer from us to us. In the
A&D book that reads as a gun that left the premises while it sits on the shelf,
and the disposition that DID happen — a retail sale to a non-licensee, with a
4473 — is never written at all. An ATF inspection sees a gun disposed with no
4473 behind it.

So Dispose refuses, and says what to do instead. It does NOT try to book the
retail sale itself: `FFL Disposition._reject_manual_retail_sale` reserves Retail
Sale records for FastBound's 4473 flow, and that rule is the reason a rogue
4473-bypass record cannot exist. The counter owns this sale.

The queue keeps showing the order — an order you cannot dispose and cannot see is
worse than one you cannot dispose — and labels it, so nobody re-discovers this by
pressing the button.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

FULFILLMENT = "ffl_woo_sync.woocommerce.fulfillment"
OWN = "5-99-888-01-9B-42763"
OTHER = "3-99-888-01-9G-09099"


def _order(*, ffl_license=OWN, order_status="Pending Fulfillment",
		woo_status="processing"):
	doc = MagicMock()
	doc.name = "WOO-ORD-00008"
	doc.woo_order_id = "9001"
	doc.order_status = order_status
	doc.woo_status = woo_status
	doc.ffl_license = ffl_license
	doc.ffl_business_name = "TEST STORE A INC"
	doc.serials = []
	return doc


# ---------------------------------------------------------------------------
# dispose_web_order refuses a pickup
# ---------------------------------------------------------------------------

class TestDisposeRefusesStorePickup(FrappeTestCase):
	def _dispose(self, order, *, is_own=True):
		from ffl_woo_sync.woocommerce.fulfillment import dispose_web_order
		with patch("frappe.only_for"), \
			 patch("frappe.get_doc", return_value=order), \
			 patch(f"{FULFILLMENT}.is_own_license", return_value=is_own):
			return dispose_web_order("WOO-ORD-00008")

	def test_our_own_licence_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._dispose(_order())
		message = str(caught.exception)
		# The refusal has to carry the alternative, or staff just press it again.
		self.assertIn("4473", message)

	def test_the_refusal_names_no_transfer(self):
		"""Whatever the wording becomes, it must never suggest booking a
		transfer — that is the exact mistake being prevented."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self._dispose(_order())
		self.assertNotIn("FFL Transfer", str(caught.exception))

	def test_the_check_runs_before_the_paid_gate(self):
		"""An unpaid pickup order must still be told it is a pickup, not sent
		round the loop of fixing payment first only to hit this next."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self._dispose(_order(woo_status="pending"))
		self.assertIn("4473", str(caught.exception))

	def test_an_ordinary_dealer_order_is_untouched(self):
		"""Everything else must reach the existing gates unchanged — proven by
		the NEXT guard down firing, not by a silent pass."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self._dispose(_order(ffl_license=OTHER, order_status="Transferred"),
				is_own=False)
		self.assertIn("not pending fulfillment", str(caught.exception).lower())


# ---------------------------------------------------------------------------
# the queue labels it instead of hiding it
# ---------------------------------------------------------------------------

class TestPendingQueueShowsPickup(FrappeTestCase):
	def _rows(self, *, is_own):
		from ffl_woo_sync.woocommerce.fulfillment import list_pending_web_orders
		orders = [frappe._dict({
			"name": "WOO-ORD-00008", "woo_status": "processing",
			"woo_order_id": "9001", "customer_name": "Sam Sample",
			"atf_ffl_record": "", "ffl_license": OWN,
			"ffl_business_name": "TEST STORE A INC",
			"ffl_expiration": "9B", "site": "retail",
			"creation": "2026-08-13 21:07:11"})]
		doc = MagicMock()
		doc.serials = [frappe._dict({
			"item_code": "Sample Pistol Grade A", "confirmed_serial": "X100001",
			"stock_hold_serial": "", "reservation_error": "",
			"fulfillment": "local"})]
		doc.lines = []
		with patch("frappe.only_for"), \
			 patch("frappe.get_all", return_value=orders), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch(f"{FULFILLMENT}.is_own_license", return_value=is_own), \
			 patch(f"{FULFILLMENT}.nowdate", return_value="2026-08-15"):
			return list_pending_web_orders()

	def test_a_pickup_stays_in_the_queue_and_is_flagged(self):
		rows = self._rows(is_own=True)
		self.assertEqual(len(rows), 1)
		self.assertTrue(rows[0]["store_pickup"])

	def test_a_pickup_is_not_offered_for_disposition(self):
		"""needs_disposition drives the Dispose button. Offering it here is how
		someone books the transfer-to-ourselves the guard exists to stop."""
		rows = self._rows(is_own=True)
		self.assertFalse(rows[0]["needs_disposition"])

	def test_an_ordinary_order_is_unflagged_and_still_disposable(self):
		rows = self._rows(is_own=False)
		self.assertFalse(rows[0]["store_pickup"])
		self.assertTrue(rows[0]["needs_disposition"])
