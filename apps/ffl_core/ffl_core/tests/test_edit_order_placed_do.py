"""Editing a counter order whose goods are already on order with a house.

The sale can still be corrected — a note, a price, another item — but the part a
distributor is already shipping is fixed: it comes back on the rebuilt invoice
unchanged and its Distributor Order follows the sale instead of dying on the
cancelled invoice. Anything NEW in the same edit is a purchase of its own.
"""
from __future__ import annotations

import frappe

from ffl_core.api import manual_order
from ffl_core.tests.test_counter_dropship_integration import (ACC_ITEM,
	CounterDropshipFixtures)
from ffl_core.tests.test_counter_to_store import (ACC, DEST_FFL, DISTRIBUTOR, GUN,
	CounterToStoreFixtures)


class EditWithPlacedOrder(CounterToStoreFixtures):

	def _placed_order(self, items=None, **kw):
		"""A counter order whose Distributor Order has been placed with the house."""
		out = self._counter(items or [self._line(ACC)], ffl=DEST_FFL, **kw)
		do = self._only_do(out["sales_invoice"])
		frappe.db.set_value("Distributor Order", do.name,
			{"status": "Placed", "web_ref": "TEST-REF"}, update_modified=False)
		return frappe.get_doc("Sales Invoice", out["sales_invoice"]), do

	def _payload(self, si):
		return manual_order.get_order_for_edit(si.name)["payload"]

	def _dos(self, si_name):
		return frappe.get_all("Distributor Order",
			filters={"source_doctype": "Sales Invoice", "source_name": si_name},
			fields=["name", "status"])

	# ------------------------------------------------------------- the gate

	def test_the_prefill_marks_the_ordered_line(self):
		si, do = self._placed_order()
		item = self._payload(si)["items"][0]
		self.assertEqual(item["ordered_do"], do.name)
		self.assertEqual(item["source"], "to_store")

	# ------------------------------------------------------------- arrived goods

	def _arrived_pickup(self, serial):
		"""A counter pickup whose ship-to-store gun has arrived — serial hung on
		the order, nothing paid yet (the READY FOR PICKUP card)."""
		out = manual_order.create_order({
			"buyer_type": "individual", "customer": self._indiv("Ready Rita"),
			"pos_profile": self._pos_profile(), "dispose_now": 0,
			"payment_received": 0, "mode_of_payment": "Cash",
			"contact_email": "rita@example.com", "pickup_in_store": 1,
			"items": [self._line(GUN, rate=1000)],
		})
		do = self._only_do(out["sales_invoice"])
		frappe.db.set_value("Distributor Order", do.name,
			{"status": "Placed", "web_ref": "TEST-REF"}, update_modified=False)
		self._receive(self._ship_to_store(
			frappe.get_doc("Distributor Order", do.name), serial=serial))
		return frappe.get_doc("Sales Invoice", out["sales_invoice"]), do

	def test_an_arrived_pickup_may_be_repriced_before_it_is_paid(self):
		si, do = self._arrived_pickup("TS-EDIT-SN-1")
		self.assertEqual(si.items[0].serial_no, "TS-EDIT-SN-1")
		payload = self._payload(si)
		item = payload["items"][0]
		self.assertEqual((item["source"], item["serials"], item["ordered_do"]),
			("to_store", [], do.name), "rebuilds as a serial-less ship-to-store line")
		item["rate"] = 900
		res = manual_order.update_order(si.name, payload)
		new_si = frappe.get_doc("Sales Invoice", res["sales_invoice"])
		self.assertEqual(new_si.items[0].rate, 900)
		self.assertEqual(new_si.items[0].serial_no, "TS-EDIT-SN-1",
			"the arrived gun stays held for the sale")
		self.assertEqual([d["name"] for d in self._dos(new_si.name)], [do.name],
			"the received order followed the sale; nothing was re-ordered")
		row = self._queue_row(new_si.name)
		self.assertIsNone(row["pickup_block"], "still ready for pickup")
		self.assertEqual([p["serial"] for p in row["pending_serials"]], ["TS-EDIT-SN-1"])
		self.assertEqual(row["outstanding_amount"], new_si.grand_total)

	def test_an_open_pickup_ticket_follows_the_repriced_order(self):
		from ffl_core.api import counter_pickup
		from ffl_core.tests.pos_opening import todays_pos_opening

		si, _do = self._arrived_pickup("TS-EDIT-SN-2")
		todays_pos_opening(self.company, self._pos_profile())
		ticket = counter_pickup.open_pickup_invoice(si.name)["pos_invoice"]
		payload = self._payload(si)
		payload["items"][0]["rate"] = 950
		res = manual_order.update_order(si.name, payload)
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, counter_pickup.LINK_FIELD),
			res["sales_invoice"], "the parked 4473 ticket now hands over the new invoice")
		self.assertEqual(frappe.db.get_value("POS Invoice", ticket, "docstatus"), 0)
		self.assertEqual(self._queue_row(res["sales_invoice"])["pickup_invoice"], ticket)
		# ...and cancelling from the queue still refuses over the open ticket.
		with self.assertRaises(frappe.ValidationError):
			manual_order._refuse_with_pickup_ticket(frappe.get_doc("Sales Invoice", res["sales_invoice"]))

	def test_a_buyer_swap_is_refused_while_a_pickup_ticket_is_open(self):
		from ffl_core.api import counter_pickup
		from ffl_core.tests.pos_opening import todays_pos_opening

		si, _do = self._arrived_pickup("TS-EDIT-SN-3")
		todays_pos_opening(self.company, self._pos_profile())
		counter_pickup.open_pickup_invoice(si.name)
		payload = self._payload(si)
		payload["customer"] = self._indiv("Other Olly")
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.update_order(si.name, payload)
		self.assertIn("pickup ticket", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1, "untouched")

	# ------------------------------------------------------------- editing

	def test_a_note_edit_keeps_the_order_and_moves_it_to_the_new_invoice(self):
		si, do = self._placed_order()
		payload = self._payload(si)
		payload["notes"] = "Customer called — hold at the counter."
		res = manual_order.update_order(si.name, payload)
		new_si = res["sales_invoice"]
		self.assertNotEqual(new_si, si.name)
		# The purchase moved; nothing was ordered a second time.
		self.assertEqual(self._dos(si.name), [])
		self.assertEqual([d["name"] for d in self._dos(new_si)], [do.name])
		self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "status"), "Placed")
		self.assertEqual(frappe.db.get_value("Sales Invoice", new_si, "ffl_staff_note"),
			"Customer called — hold at the counter.")
		row = frappe.get_doc("Sales Invoice", new_si).items[0]
		self.assertEqual(row.ffl_to_store_distributor, DISTRIBUTOR)

	def test_a_new_line_gets_its_own_order(self):
		si, do = self._placed_order()
		payload = self._payload(si)
		payload["items"].append(self._line(GUN))
		res = manual_order.update_order(si.name, payload)
		orders = {d["name"]: d["status"] for d in self._dos(res["sales_invoice"])}
		self.assertIn(do.name, orders)
		self.assertEqual(orders[do.name], "Placed")
		fresh = [n for n in orders if n != do.name]
		self.assertEqual(len(fresh), 1, orders)
		self.assertEqual(orders[fresh[0]], "Draft")
		new_lines = frappe.get_all("Distributor Order Line",
			filters={"parent": fresh[0]}, pluck="item_code")
		self.assertEqual(new_lines, [GUN])   # only the added item, never the bought one

	def test_the_price_of_an_ordered_line_may_change(self):
		si, do = self._placed_order()
		payload = self._payload(si)
		payload["items"][0]["rate"] = 777
		res = manual_order.update_order(si.name, payload)
		row = frappe.get_doc("Sales Invoice", res["sales_invoice"]).items[0]
		self.assertEqual(row.rate, 777)
		self.assertEqual([d["name"] for d in self._dos(res["sales_invoice"])], [do.name])

	# ------------------------------------------------------------- refusals

	def test_removing_an_ordered_line_is_refused(self):
		si, _do = self._placed_order()
		payload = self._payload(si)
		payload["items"] = [self._line(GUN)]
		with self.assertRaises(frappe.ValidationError):
			manual_order.update_order(si.name, payload)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 1)

	def test_reducing_an_ordered_line_is_refused(self):
		si, _do = self._placed_order([self._line(ACC, qty=2)])
		payload = self._payload(si)
		payload["items"][0]["qty"] = 1
		with self.assertRaises(frappe.ValidationError):
			manual_order.update_order(si.name, payload)

	def test_raising_the_quantity_orders_only_the_difference(self):
		si, do = self._placed_order()
		payload = self._payload(si)
		payload["items"][0]["qty"] = 3
		res = manual_order.update_order(si.name, payload)
		fresh = [d["name"] for d in self._dos(res["sales_invoice"]) if d["name"] != do.name]
		self.assertEqual(len(fresh), 1)
		lines = frappe.get_all("Distributor Order Line", filters={"parent": fresh[0]},
			fields=["item_code", "wish_qty"])
		self.assertEqual([(l.item_code, l.wish_qty) for l in lines], [(ACC, 2)])

	def test_more_of_an_ordered_item_buys_only_the_extra(self):
		"""The locked line covers what the house is sending; a second row of the same
		item is new goods, so exactly the extra units are ordered."""
		si, do = self._placed_order()
		payload = self._payload(si)
		payload["items"].append(self._line(ACC, qty=2))
		res = manual_order.update_order(si.name, payload)
		fresh = [d["name"] for d in self._dos(res["sales_invoice"]) if d["name"] != do.name]
		self.assertEqual(len(fresh), 1)
		lines = frappe.get_all("Distributor Order Line", filters={"parent": fresh[0]},
			fields=["item_code", "wish_qty"])
		self.assertEqual([(l.item_code, l.wish_qty) for l in lines], [(ACC, 2)])

	def test_changing_the_buyer_of_a_store_bound_order_is_allowed(self):
		"""Goods ordered INTO the shop don't care who buys them — the shop takes
		delivery either way."""
		si, do = self._placed_order()
		payload = self._payload(si)
		payload["customer"] = self._indiv("Someone Else")
		res = manual_order.update_order(si.name, payload, refund_mode="Cash")
		self.assertEqual([d["name"] for d in self._dos(res["sales_invoice"])], [do.name])

	def test_an_order_with_nothing_left_to_send_still_follows_the_sale(self):
		"""Every line re-sourced elsewhere: the order no longer locks any line, but it
		is still live at the house — it must not be stranded on the cancelled invoice."""
		si, do = self._placed_order()
		# What fulfill_from_stock leaves behind: the units are retired on the order
		# and the invoice row is a plain local line again.
		frappe.db.set_value("Distributor Order Line", do.lines[0].name, "substituted_qty",
			do.lines[0].wish_qty, update_modified=False)
		frappe.db.set_value("Sales Invoice Item", si.items[0].name,
			"ffl_to_store_distributor", "", update_modified=False)
		si.reload()
		self.assertEqual(manual_order._committed_distributor_orders(si.name), [])
		res = manual_order.update_order(si.name, self._payload(si))
		self.assertEqual([d["name"] for d in self._dos(res["sales_invoice"])], [do.name])
		self.assertEqual(self._dos(si.name), [])


class EditWithPlacedDropshipOrder(CounterDropshipFixtures):
	"""The drop-ship lane: a Distributor Order Line stores the ROUTER's lane
	(``fds`` / ``direct``), not the counter's ``dropship`` — an edit must read
	across that gap instead of refusing every drop-ship order."""

	def _placed(self):
		out = self._counter(
			items=[{"item_code": ACC_ITEM, "qty": 1, "rate": 40, "source": "dropship"}],
			ship_to=self._ACC_SHIP_TO)
		do = self._orders_for(out["sales_invoice"])[0]
		frappe.db.set_value("Distributor Order", do["name"], "status", "Placed",
			update_modified=False)
		return frappe.get_doc("Sales Invoice", out["sales_invoice"]), do["name"]

	def test_a_note_edit_keeps_the_placed_dropship_order(self):
		si, do_name = self._placed()
		payload = manual_order.get_order_for_edit(si.name)["payload"]
		self.assertEqual(payload["items"][0]["ordered_do"], do_name)
		payload["notes"] = "Ring the buyer when it lands."
		res = manual_order.update_order(si.name, payload)
		self.assertEqual([d["name"] for d in self._orders_for(res["sales_invoice"])],
			[do_name])
		self.assertEqual(frappe.db.get_value("Distributor Order", do_name, "status"),
			"Placed")

	def test_the_buyer_of_a_placed_dropship_order_cannot_change(self):
		si, _do = self._placed()
		payload = manual_order.get_order_for_edit(si.name)["payload"]
		payload["customer"] = self._indiv("Other Buyer")
		with self.assertRaises(frappe.ValidationError):
			manual_order.update_order(si.name, payload)
