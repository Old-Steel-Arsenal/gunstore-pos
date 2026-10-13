"""Counter pickup ticket rules (api/counter_pickup) — pure, no site."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from ffl_core.api import counter_pickup


def _si(**over):
	base = {"name": "SI-1", "docstatus": 1, "ffl_pickup_in_store": 1,
		"outstanding_amount": 0, "items": []}
	base.update(over)
	return SimpleNamespace(**base)


class PickupBlock(unittest.TestCase):
	def setUp(self):
		for p in (
			mock.patch("ffl_core.compliance.firearm.is_awaiting_procurement_row",
				lambda si, row: bool(getattr(row, "awaiting", False))),
		):
			p.start()
			self.addCleanup(p.stop)

	def _block(self, si, pending=({"item_code": "G", "serial": "S1"},)):
		return counter_pickup.pickup_block(si, list(pending))

	def test_ready(self):
		self.assertIsNone(self._block(_si()))

	def test_not_a_pickup(self):
		self.assertIn("not a counter pickup", self._block(_si(ffl_pickup_in_store=0)))

	def test_not_submitted(self):
		self.assertIn("not a submitted", self._block(_si(docstatus=0)))

	def test_gun_not_arrived(self):
		si = _si(items=[SimpleNamespace(awaiting=True)])
		self.assertIn("not arrived", self._block(si))

	def test_nothing_to_hand_over(self):
		self.assertIn("waiting to be handed over", self._block(_si(), pending=()))

	def test_balance_owed_does_not_stop_the_ticket(self):
		# The 4473 may run while the customer still owes; the hand-over is gated
		# at ticket submit instead (require_settled_counter_order).
		self.assertIsNone(self._block(_si(outstanding_amount=12.5)))

	def test_submit_waits_for_the_balance(self):
		ticket = SimpleNamespace(ffl_counter_order="SINV-1")
		real = frappe.db.get_value

		def owing(amount):
			# Only the order's balance is faked; frappe.format_value reads settings
			# through the same get_value and must keep working.
			return lambda *a, **k: amount if a[:2] == ("Sales Invoice", "SINV-1") else real(*a, **k)

		with mock.patch("frappe.db.get_value", side_effect=owing(12.5)):
			with self.assertRaises(frappe.ValidationError) as cm:
				counter_pickup.require_settled_counter_order(ticket)
		self.assertIn("still owes", str(cm.exception))
		with mock.patch("frappe.db.get_value", side_effect=owing(0)):
			counter_pickup.require_settled_counter_order(ticket)   # settled → silent
		counter_pickup.require_settled_counter_order(SimpleNamespace(ffl_counter_order=""))


class ZeroTicket(unittest.TestCase):
	def test_pin_zero_clears_every_price_field(self):
		row = SimpleNamespace(rate=599, price_list_rate=650, discount_amount=51, discount_percentage=0)
		counter_pickup._pin_zero(SimpleNamespace(items=[row]))
		self.assertEqual((row.rate, row.price_list_rate, row.discount_amount, row.discount_percentage),
			(0, 0, 0, 0))

	def test_ticket_items_carry_the_serial_at_no_charge(self):
		with mock.patch("frappe.db.get_value", return_value="Stores - X"):
			items = counter_pickup._ticket_items([{"item_code": "G", "serial": "S1"}])
		self.assertEqual(items, [{"item_code": "G", "qty": 1, "rate": 0, "price_list_rate": 0,
			"discount_amount": 0, "serial_no": "S1", "use_serial_batch_fields": 1,
			"warehouse": "Stores - X"}])


class TicketStaysFreeScope(unittest.TestCase):
	"""keep_ticket_free / assert_ticket_free touch a draft TICKET only: a return
	of one and an ordinary sale keep their prices (the ticket path itself runs
	against a real site in test_counter_to_store)."""

	def _priced(self, **over):
		base = {"ffl_counter_order": "", "is_return": 0, "grand_total": 670,
			"paid_amount": 670, "change_amount": 0,
			"items": [frappe._dict(item_code="G", rate=670, price_list_rate=670)],
			"payments": [frappe._dict(mode_of_payment="Credit Card", amount=670, base_amount=670)]}
		base.update(over)
		return frappe._dict(base)

	def _untouched(self, doc):
		self.assertEqual((doc["items"][0].rate, doc.payments[0].amount, doc.grand_total, doc.paid_amount),
			(670, 670, 670, 670))

	def test_an_ordinary_sale_keeps_its_price(self):
		doc = self._priced()
		counter_pickup.keep_ticket_free(doc)
		counter_pickup.assert_ticket_free(doc)
		self._untouched(doc)

	def test_a_return_of_a_ticket_is_left_alone(self):
		doc = self._priced(ffl_counter_order="SI-1", is_return=1, grand_total=-670, paid_amount=-670)
		doc["items"][0].rate = 670
		counter_pickup.keep_ticket_free(doc)
		counter_pickup.assert_ticket_free(doc)
		self.assertEqual(doc["items"][0].rate, 670)
		self.assertEqual(doc.paid_amount, -670)

	def test_hooks_are_registered_before_and_after_core_validate(self):
		from ffl_core import hooks
		pos = hooks.doc_events["POS Invoice"]
		self.assertEqual(pos["before_validate"][0], "ffl_core.api.counter_pickup.keep_ticket_free",
			"first, so every later before_validate hook and ERPNext's validate see $0")
		self.assertIn("ffl_core.api.counter_pickup.assert_ticket_free", pos["validate"])


class SubmitHook(unittest.TestCase):
	def test_an_ordinary_pos_invoice_is_left_alone(self):
		with mock.patch.object(counter_pickup, "_clear_if_done") as clear:
			counter_pickup.close_counter_order_on_pos_submit(SimpleNamespace(ffl_counter_order=""))
		clear.assert_not_called()

	def test_a_clearing_failure_never_fails_the_sale(self):
		with mock.patch.object(counter_pickup, "_clear_if_done", side_effect=RuntimeError("x")), \
				mock.patch("frappe.log_error") as log:
			counter_pickup.close_counter_order_on_pos_submit(
				SimpleNamespace(ffl_counter_order="SI-1", name="POS-1"))
		log.assert_called_once()

	def test_is_pickup_order_reads_the_marker(self):
		self.assertTrue(counter_pickup.is_pickup_order(SimpleNamespace(ffl_pickup_in_store="1")))
		self.assertFalse(counter_pickup.is_pickup_order(SimpleNamespace()))
		self.assertFalse(counter_pickup.is_pickup_order(frappe._dict(ffl_pickup_in_store=0)))


if __name__ == "__main__":
	unittest.main()
