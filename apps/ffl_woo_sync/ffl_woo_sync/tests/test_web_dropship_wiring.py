"""The woo-side wiring a drop-ship order needs: the WooCommerce status
write-back, the buyer's original ship-to, and the revenue hole.

* ``WooCommerceClient.update_order`` is the missing verb — a drop-ship order is
  marked ``completed`` by US (RSR ships it), not by the store.
* The buyer's shipping address was never persisted on Woo Online Order, because
  until now nothing downstream needed it: the store shipped every parcel itself.
  A direct accessory ships from RSR's dock, so the address has to travel with the
  order (the ORIGINAL payload ship-to, never re-derived).
* A purely-accessory web order books no revenue at all today: the only
  path to a Sales Invoice runs through ``Transferred``, and only a firearm
  disposition sets that. Auto-clear now books it — for local accessory orders as
  much as drop-ship ones, which is where the historical hole was.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceConfig


def _client():
	cfg = WooCommerceConfig(
		store_url="https://example.test", consumer_key="ck", consumer_secret="cs",
		timeout=30)
	c = WooCommerceClient(cfg, session=MagicMock())
	c._request = MagicMock(return_value={"id": 931, "status": "completed"})
	return c


# ------------------------------------------------------------ status write-back

class TestUpdateOrder(unittest.TestCase):

	def test_puts_to_the_order_endpoint(self):
		c = _client()
		c.update_order(931, {"status": "completed"})
		method, path = c._request.call_args.args[:2]
		self.assertEqual(method, "PUT")
		self.assertEqual(path, "orders/931")
		self.assertEqual(c._request.call_args.kwargs["json"], {"status": "completed"})

	def test_complete_order_is_the_named_shorthand(self):
		c = _client()
		c.complete_order(931)
		self.assertEqual(c._request.call_args.kwargs["json"]["status"], "completed")

	def test_empty_payload_is_refused(self):
		"""A blank PUT would silently no-op and read as success."""
		c = _client()
		with self.assertRaises(ValueError):
			c.update_order(931, {})


# --------------------------------------------------------------- ship-to capture

class TestShipToCapture(unittest.TestCase):

	BODY_SHIPPING = {
		"first_name": "Ann", "last_name": "Buyer",
		"address_1": "5 Elm St", "address_2": "Apt 2",
		"city": "Springfield", "state": "TX", "postcode": "75024",
	}

	def test_shipping_block_is_captured(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_ship_to

		order = frappe._dict()
		_apply_ship_to(order, {"shipping": self.BODY_SHIPPING, "billing": {}})
		self.assertEqual(order.ship_to_name, "Ann Buyer")
		self.assertEqual(order.ship_to_line1, "5 Elm St")
		self.assertEqual(order.ship_to_line2, "Apt 2")
		self.assertEqual(order.ship_to_city, "Springfield")
		self.assertEqual(order.ship_to_state, "TX")
		self.assertEqual(order.ship_to_zip, "75024")

	def test_billing_is_the_fallback_for_a_virtual_only_cart(self):
		"""WooCommerce leaves `shipping` empty when the cart needs no shipping;
		the billing address is then the only address the buyer gave."""
		from ffl_woo_sync.woocommerce.webhook import _apply_ship_to

		order = frappe._dict()
		_apply_ship_to(order, {"shipping": {"address_1": ""},
			"billing": dict(self.BODY_SHIPPING, address_1="9 Oak Ave")})
		self.assertEqual(order.ship_to_line1, "9 Oak Ave")

	def test_ship_home_choice_beats_the_ffl_premise(self):
		"""A firearm cart's `shipping` is the dealer's premise; a buyer who chose
		"ship non-firearm items to my address" has theirs in ffl_core_ship_home."""
		from ffl_woo_sync.woocommerce.webhook import _apply_ship_to

		ffl = dict(self.BODY_SHIPPING, company="Acme Guns", address_1="1 Main St",
			address_2="", city="Austin", postcode="78701")
		order = frappe._dict()
		_apply_ship_to(order, {"shipping": ffl, "billing": {}, "meta_data": [
			{"key": "ffl_core_ship_home", "value": self.BODY_SHIPPING}]})
		self.assertEqual(order.ship_to_name, "Ann Buyer")
		self.assertEqual(order.ship_to_line1, "5 Elm St")
		self.assertEqual(order.ship_to_city, "Springfield")

		# Not chosen (no meta, or an empty one): the direct lines go with the gun.
		for meta in ([], [{"key": "ffl_core_ship_home", "value": ""}]):
			order = frappe._dict()
			_apply_ship_to(order, {"shipping": ffl, "billing": {}, "meta_data": meta})
			self.assertEqual(order.ship_to_line1, "1 Main St")

	def test_no_address_anywhere_leaves_blanks_not_none(self):
		from ffl_woo_sync.woocommerce.webhook import _apply_ship_to

		order = frappe._dict()
		_apply_ship_to(order, {})
		self.assertEqual(order.ship_to_line1, "")
		self.assertEqual(order.ship_to_state, "")

	def test_updated_address_refreshes_while_the_order_awaits_routing(self):
		"""m6: the buyer/CS can fix the shipping address on Woo after checkout, and
		the direct leg ships from it — so an order.updated must re-capture it, but
		only while the order is still Pending Route (never after it has been ordered
		from the distributor, where the address is already committed)."""
		from ffl_woo_sync.woocommerce.webhook import _refresh_ship_to_if_unrouted

		order = frappe._dict(order_status="Pending Fulfillment",
			dropship_status="Pending Route", ship_to_line1="OLD")
		changed = _refresh_ship_to_if_unrouted(
			order, {"shipping": dict(self.BODY_SHIPPING, address_1="99 New Rd")})
		self.assertTrue(changed)
		self.assertEqual(order.ship_to_line1, "99 New Rd")

	def test_updated_address_is_frozen_once_ordered(self):
		from ffl_woo_sync.woocommerce.webhook import _refresh_ship_to_if_unrouted

		order = frappe._dict(order_status="Pending Fulfillment",
			dropship_status="Ordered", ship_to_line1="COMMITTED")
		changed = _refresh_ship_to_if_unrouted(
			order, {"shipping": dict(self.BODY_SHIPPING, address_1="99 New Rd")})
		self.assertFalse(changed)
		self.assertEqual(order.ship_to_line1, "COMMITTED")

	def test_no_refresh_for_a_non_dropship_order(self):
		from ffl_woo_sync.woocommerce.webhook import _refresh_ship_to_if_unrouted

		order = frappe._dict(order_status="Pending Fulfillment",
			dropship_status="", ship_to_line1="")
		self.assertFalse(_refresh_ship_to_if_unrouted(
			order, {"shipping": self.BODY_SHIPPING}))


# ------------------------------------------------------------------ #8 revenue

class TestAccessoryOrderBooksRevenue(unittest.TestCase):
	"""The historical hole: `_auto_clear` set Transferred but never invoiced."""

	def _order(self, **kw):
		base = {"name": "WOO-ORD-9", "order_status": "Pending Fulfillment",
			"woo_status": "completed", "serials": [], "dropship_status": None,
			"lines": [frappe._dict({"item_code": "AMMO", "qty": 1})], "refunds": []}
		base.update(kw)
		return frappe._dict(base)

	def test_auto_clear_enqueues_the_invoice(self):
		from ffl_woo_sync.woocommerce.webhook import _book_revenue_after_auto_clear

		with patch("frappe.enqueue") as enq:
			_book_revenue_after_auto_clear(self._order(order_status="Transferred"))
		enq.assert_called_once()
		self.assertIn("create_sales_invoice_for_web_order", enq.call_args.args[0])
		self.assertEqual(enq.call_args.kwargs["order"], "WOO-ORD-9")

	def test_booking_is_deferred_until_after_commit(self):
		"""The cancel+amend/insert is too heavy for a webhook response, and the
		order row must be persisted before the job reads it back."""
		from ffl_woo_sync.woocommerce.webhook import _book_revenue_after_auto_clear

		with patch("frappe.enqueue") as enq:
			_book_revenue_after_auto_clear(self._order(order_status="Transferred"))
		self.assertTrue(enq.call_args.kwargs["enqueue_after_commit"])

	def test_a_still_pending_order_books_nothing(self):
		from ffl_woo_sync.woocommerce.webhook import _book_revenue_after_auto_clear

		with patch("frappe.enqueue") as enq:
			_book_revenue_after_auto_clear(self._order())
		enq.assert_not_called()


if __name__ == "__main__":
	unittest.main()
