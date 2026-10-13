"""Pending Order queue × direct (no destination FFL) counter orders.

The queue used to hard-filter on ffl_destination_ffl, so a direct order —
accessories shipping to the buyer, or a counter pickup — vanished the moment
it was created: nothing tracked its payment, and its deferred stock never
issued (no Delivery Note leg = no stock decrement, no COGS). This pins the
new retention rule (to-do-driven: owes money / undisposed serials /
undelivered deferred stock), the Delivery Note leg shared by push_shipment
and mark_shipped_manually, and the cancel cascade that returns that stock.

ShipStation enablement is ALWAYS patched (both ways) — never read from the
persistent dev DB, where manual smoke testing may have flipped the toggle.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEALER, DEALER_FFL, DEST_FFL, BuildOrderMixin

ACC_ITEM = "PD-OPTIC"
SHIP_TO = {"address_line1": "9 Test Ln", "city": "Springfield", "state": "TX",
	"pincode": "75024"}


def _row(rows: list[dict], si_name: str) -> dict | None:
	return next((r for r in rows if r.get("sales_invoice") == si_name), None)


class DirectOrderFixtures(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin (not BuildOrderFixtures) on purpose: the fixture CLASS
	carries commit-heavy test methods that unittest would re-run for every
	subclass; the mixin brings only the helpers."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# A STOCK accessory (BO-ACC is deliberately non-stock): the Delivery
		# Note leg only exists for stock items, so this is the interesting one.
		if not frappe.db.exists("Item", ACC_ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ACC_ITEM, "item_name": ACC_ITEM,
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 0,
			}).insert(ignore_permissions=True)
		cls.pos_profile = frappe.db.get_value(
			"POS Profile", {"company": cls.company, "disabled": 0}, "name")

	def _rows(self, enabled: bool) -> list[dict]:
		with patch.object(manual_order, "_shipstation_enabled", return_value=enabled):
			return manual_order.list_pending_dispositions()

	def _biz(self, name: str) -> str:
		"""A dedicated Commercial customer per test — the shared DEALER
		fixture accumulates Addresses from other modules on a full run (the
		r1 CI failure), so address-sensitive tests must own their customer."""
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": "Commercial", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		return name

	def _paid(self, si_name: str) -> None:
		frappe.db.set_value(
			"Sales Invoice", si_name, "outstanding_amount", 0, update_modified=False)

	def _stock_acc(self, qty: float, warehouse: str) -> None:
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [{"item_code": ACC_ITEM, "qty": qty,
				"t_warehouse": warehouse, "basic_rate": 100}],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

	def _direct_order(self, *, customer=None, ship_to=SHIP_TO, dispose_now=0,
			payment_received=0, buyer_type="individual",
			destination_ffl=None, item_code=ACC_ITEM, **over) -> dict:
		payload = {
			"buyer_type": buyer_type, "customer": customer or DEALER,
			"pos_profile": self.pos_profile, "dispose_now": dispose_now,
			"payment_received": payment_received,
			"items": [{"item_code": item_code, "qty": 1, "rate": 250}],
		}
		if destination_ffl:
			payload["destination_ffl"] = destination_ffl
		if ship_to:
			payload["ship_to"] = dict(ship_to)
			payload["recipient_name"] = "PD Recipient"
		if payment_received:
			payload["mode_of_payment"] = "Cash"
			payload["tax_exempt"] = 1
		payload.update(over)
		return manual_order.create_order(payload)

	def _acc_qty(self, warehouse: str) -> float:
		return flt(frappe.db.get_value(
			"Bin", {"item_code": ACC_ITEM, "warehouse": warehouse}, "actual_qty"))


class TestDirectOrderQueue(DirectOrderFixtures):
	def test_direct_deferred_order_appears_with_direct_row_shape(self):
		"""A deferred no-FFL order is IN the queue (the old filter dropped it),
		with the direct-row contract the page renders from: no FFL, no bogus
		'expired' lock, the buyer's address as the ship-to preview."""
		out = self._direct_order(customer=self._indiv("PD Direct Buyer"))
		row = _row(self._rows(enabled=True), out["sales_invoice"])
		self.assertIsNotNone(row, "direct order must surface in the queue")
		self.assertIsNone(row["destination_ffl"])
		self.assertFalse(row["ffl_expired"],
			"no FFL = nothing to expire; True would disable every button")
		self.assertTrue(row["has_ship_address"])
		self.assertEqual(row["ship_to_preview"]["city"], SHIP_TO["city"])
		self.assertTrue(row["can_mark_shipped"])
		self.assertFalse(row["needs_disposition"])

	def test_direct_paid_row_stays_until_shipped_then_delivers_stock(self):
		"""Paid but unshipped keeps the row (the third prefilter arm), and
		mark_shipped_manually books the Delivery Note leg: stock decrements,
		the SO line completes, the row leaves — ShipStation on OR off."""
		out = self._direct_order(customer=self._indiv("PD Ship Buyer"))
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.update_stock, 0)
		so_name = si.items[0].sales_order
		warehouse = si.items[0].warehouse
		self._stock_acc(1, warehouse)
		self._paid(si.name)
		before = self._acc_qty(warehouse)

		row = _row(self._rows(enabled=False), si.name)
		self.assertIsNotNone(row, "paid + undelivered must stay in the queue")
		self.assertTrue(row["can_mark_shipped"])

		res = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(res["ok"])
		self.assertEqual(len(res["delivery_notes"]), 1)
		dn_rows = frappe.get_all("Delivery Note Item",
			filters={"against_sales_order": so_name, "docstatus": 1},
			fields=["parent", "item_code", "qty"])
		self.assertEqual([(r.item_code, r.qty) for r in dn_rows], [(ACC_ITEM, 1)])
		self.assertEqual(self._acc_qty(warehouse), before - 1)
		self.assertEqual(flt(frappe.db.get_value(
			"Sales Order Item", si.items[0].so_detail, "delivered_qty")), 1)
		# Row gone with ShipStation off AND on — the generic ship arm never
		# depended on the integration toggle.
		self.assertIsNone(_row(self._rows(enabled=False), si.name))
		self.assertIsNone(_row(self._rows(enabled=True), si.name))
		# Idempotent: the second call is a no-op, not a second Delivery Note.
		again = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(again.get("already"))
		self.assertEqual(len(frappe.get_all("Delivery Note Item",
			filters={"against_sales_order": so_name, "docstatus": 1})), 1)

	def test_paid_walkin_never_appears(self):
		"""dispose_now + paid = goods and money both settled at the counter —
		exactly the row the queue must NOT accumulate."""
		warehouse = frappe.db.get_value("POS Profile", self.pos_profile, "warehouse")
		self._stock_acc(1, warehouse)
		out = self._direct_order(customer=self._indiv("PD Walkin Buyer"),
			ship_to=None, dispose_now=1, payment_received=1)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.update_stock, 1)
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_pickup_without_address_marks_picked_up(self):
		"""No ship_to at all (a counter pickup): the row carries
		has_ship_address=False for the UI, and Mark Shipped both issues the
		stock and clears it."""
		out = self._direct_order(customer=self._indiv("PD Pickup Buyer"),
			ship_to=None)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row)
		self.assertFalse(row["has_ship_address"])
		self._stock_acc(1, si.items[0].warehouse)
		self._paid(si.name)
		res = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(res["ok"])
		self.assertEqual(len(res["delivery_notes"]), 1)
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_ammo_only_ffl_order_waits_for_its_stock_leg(self):
		"""BEHAVIOUR CHANGE, deliberate: a no-serial FFL order (ammo to a
		dealer) used to vanish once paid — its stock never issued. Now it
		stays until Mark Shipped books the Delivery Note."""
		out = self._direct_order(buyer_type="business", customer=DEALER,
			destination_ffl=DEALER_FFL, ship_to=None)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self._paid(si.name)
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row,
			"paid ammo-only FFL order must wait for its stock leg now")
		self.assertTrue(row["can_mark_shipped"])
		self._stock_acc(1, si.items[0].warehouse)
		res = manual_order.mark_shipped_manually(si.name)
		self.assertEqual(len(res["delivery_notes"]), 1)
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_mark_shipped_offers_itself_on_an_undisposed_gun_row(self):
		"""User decision 2026-09-11: the firearm gate is gone. An undisposed
		gun row OFFERS Mark Shipped (can_mark_shipped pins the shared
		mark_shipped_block predicate), because the mark clears the shipping
		half only — the row stays queued for its Dispose and the bound book
		is untouched. Pinned here as well as in test_pending_ship because this
		module covers the no-Sales-Order shape of the same invoice."""
		si = self._ship(self._stocked_serial("PD-GUN-1"), update_stock=0)
		self._paid(si.name)
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row)
		self.assertTrue(row["can_mark_shipped"],
			"an undisposed gun row must still offer Mark Shipped")
		out = manual_order.mark_shipped_manually(si.name)
		self.assertEqual(out["undisposed_serials"], ["PD-GUN-1"])
		still = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(still, "the row must stay queued for its Dispose")
		self.assertTrue(still["needs_disposition"])

	def test_mark_shipped_refuses_unpaid_individual(self):
		"""The r1-added payment gate's refusal side: an unpaid individual's
		direct order must not mark shipped (goods really leave now)."""
		out = self._direct_order(customer=self._indiv("PD Unpaid Buyer"))
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self._stock_acc(1, si.items[0].warehouse)
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.mark_shipped_manually(si.name)
		self.assertIn("outstanding", str(cm.exception).lower())

	def test_desk_invoice_neither_queues_nor_marks(self):
		"""A desk-made invoice (no SO, no POS Profile, no FFL) shares arm3's
		SQL shape — the counter-order fingerprint must keep it out of the
		queue AND off the stock-issuing Mark Shipped endpoint."""
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = self._indiv("PD Desk Buyer")
		si.append("items", {"item_code": ACC_ITEM, "qty": 1, "rate": 99})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		si.submit()
		self.assertIsNone(_row(self._rows(enabled=True), si.name),
			"a desk invoice must not join the counter queue")
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.mark_shipped_manually(si.name)
		self.assertIn("not a counter order", str(cm.exception).lower())

	def test_antique_direct_order_warns_and_clears(self):
		"""r1 M4's closure pin: an antique firearm (not regulated — no
		disposition will ever issue its stock, and its serial keeps it out
		of the DN leg) must not dead-lock the row: can_mark_shipped=True,
		Mark Shipped books NO Delivery Note, warns the operator, and the
		row leaves."""
		serial = self._stocked_serial("PD-ANT-1", item_code="BO-ANTIQUE")
		out = manual_order.create_order({
			"buyer_type": "individual",
			"customer": self._indiv("PD Antique Buyer"),
			"pos_profile": self.pos_profile, "dispose_now": 0,
			"payment_received": 0,
			"items": [{"item_code": "BO-ANTIQUE", "qty": 1, "rate": 400,
				"serials": [serial]}],
		})
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self._paid(si.name)
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row)
		self.assertTrue(row["can_mark_shipped"],
			"an antique row must keep a clear-the-queue action")
		frappe.clear_messages()
		res = manual_order.mark_shipped_manually(si.name)
		self.assertEqual(res["delivery_notes"], [],
			"a serialized antique must never enter the Delivery Note leg")
		self.assertTrue(any("antique" in str(m).lower()
			for m in frappe.get_message_log()),
			"the operator must be told the antique's stock was not issued")
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_unpushed_statuses_stay_paired_with_helper(self):
		"""arm3's SQL prefilter and shipstation_pushed read the SAME
		word-list (_UNPUSHED_STATUSES) — pin the pairing so adding a new
		unpushed-looking status can't make arm3 silently drop rows the
		helper still reports as not-pushed."""
		for status in manual_order._UNPUSHED_STATUSES:
			self.assertFalse(manual_order.shipstation_pushed(None, status))
		self.assertFalse(manual_order.shipstation_pushed(None, None))
		self.assertTrue(manual_order.shipstation_pushed("se-1", None))
		self.assertTrue(manual_order.shipstation_pushed(None, "Pending"))
		self.assertTrue(manual_order.shipstation_pushed(None, "Shipped"))

	def test_cancel_direct_order_returns_delivered_stock(self):
		"""Cancel after the Delivery Note leg: the DN cancels (stock back),
		then the SO, then nothing is left in the queue. The old guard refused
		any no-FFL invoice outright."""
		# Business buyer: ships on account (ship_payment_block passes unpaid),
		# and staying UNPAID keeps cancel_order from computing a phantom
		# paid_total that would demand a refund method. Dedicated customer —
		# the shared DEALER fixture picks up Addresses from other modules.
		out = self._direct_order(buyer_type="business",
			customer=self._biz("PD Cancel Biz"))
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		so_name = si.items[0].sales_order
		warehouse = si.items[0].warehouse
		self._stock_acc(1, warehouse)
		manual_order.mark_shipped_manually(si.name)
		before = self._acc_qty(warehouse)

		res = manual_order.cancel_order(si.name, "PD cancel test")
		self.assertTrue(res["ok"])
		self.assertEqual(len(res["delivery_notes_cancelled"]), 1)
		self.assertEqual(self._acc_qty(warehouse), before + 1)
		self.assertEqual(
			frappe.db.get_value("Sales Order", so_name, "docstatus"), 2)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 2)
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_push_shipment_refuses_no_ffl_no_address(self):
		"""A direct order with no shipping address can't push a label — fail
		with the actionable message BEFORE any external call. Dedicated
		customer: with the shared DEALER, a default Address left by earlier
		modules auto-fills shipping_address_name and the precheck never
		fires (the r1 CI failure)."""
		from ffl_integrations.shipstation import api as ss_api
		out = self._direct_order(buyer_type="business",
			customer=self._biz("PD NoAddr Biz"), ship_to=None)
		self.assertFalse(frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "shipping_address_name"))
		with patch.object(ss_api, "_enabled", return_value=True):
			with self.assertRaises(frappe.ValidationError) as cm:
				ss_api.push_shipment(out["sales_invoice"])
		self.assertIn("no shipping address", str(cm.exception).lower())

	def test_pickup_clears_customer_default_address(self):
		"""A returning customer's default Shipping address must NOT make a
		counter pickup look shippable: create_order clears the auto-filled
		shipping_address_name when the operator chose no ship-to, so the
		queue reports has_ship_address=False and the Ship precheck refuses."""
		customer = self._biz("PD DefaultAddr Biz")
		addr = frappe.get_doc({
			"doctype": "Address", "address_title": customer,
			"address_type": "Shipping", "address_line1": "77 Old Default Rd",
			"city": "Springfield", "state": "TX", "pincode": "75024",
			"country": "United States",
			"links": [{"link_doctype": "Customer", "link_name": customer}],
		})
		addr.flags.ignore_permissions = True
		addr.insert()
		out = self._direct_order(buyer_type="business", customer=customer,
			ship_to=None)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertFalse(si.shipping_address_name,
			"pickup must not inherit the customer's default Shipping address")
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row)
		self.assertFalse(row["has_ship_address"])

	def test_mixed_ffl_order_dn_excludes_firearm(self):
		"""A gun+ammo FFL order: Dispose issues ONLY the
		gun (Material Issue), the ship leg's Delivery Note carries ONLY the
		ammo line, and neither line is booked twice."""
		serial = self._stocked_serial("PD-MIX-1")
		out = manual_order.create_order({
			"buyer_type": "individual",
			"customer": self._indiv("PD Mix Buyer"),
			"pos_profile": self.pos_profile, "destination_ffl": DEST_FFL,
			"dispose_now": 0, "payment_received": 0,
			"items": [
				{"item_code": "BO-GUN", "qty": 1, "rate": 900,
					"serials": [serial]},
				{"item_code": ACC_ITEM, "qty": 1, "rate": 50},
			],
		})
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		acc_line = next(r for r in si.items if r.item_code == ACC_ITEM)
		self._stock_acc(1, acc_line.warehouse)
		self._paid(si.name)
		acc_before = self._acc_qty(acc_line.warehouse)

		manual_order.dispose_order(si.name)
		self.assertNotEqual(
			frappe.db.get_value("Serial No", serial, "status"), "Active")
		self.assertEqual(self._acc_qty(acc_line.warehouse), acc_before,
			"dispose must not touch the ammo line")

		res = manual_order.mark_shipped_manually(si.name)
		self.assertEqual(len(res["delivery_notes"]), 1)
		dn_items = frappe.get_all("Delivery Note Item",
			filters={"against_sales_order": acc_line.sales_order,
				"docstatus": 1},
			fields=["item_code", "qty"])
		self.assertEqual([(r.item_code, r.qty) for r in dn_items],
			[(ACC_ITEM, 1)],
			"the Delivery Note must carry the ammo line and never the gun")
		self.assertEqual(self._acc_qty(acc_line.warehouse), acc_before - 1)
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_mark_shipped_fails_closed_without_stock(self):
		"""No stock on hand: the Delivery Note leg must throw (no
		phantom delivery) and the row must stay in the queue. ERPNext's gate
		is is_negative_stock_allowed (Stock Settings + per-Item override,
		both cached) — test sites routinely leave it open, so pin OUR
		behaviour against the gate itself; whether prod actually keeps the
		gate closed is a deploy-checklist assertion, not a test's. The item
		is dedicated and NEVER stocked: sibling tests net-deposit into the
		shared warehouse (the cancel test restores what it shipped), so the
		shared item genuinely has stock by the time this test runs — the r1
		CI red herring."""
		from erpnext.stock import stock_ledger
		if not frappe.db.exists("Item", "PD-OPTIC-NOSTOCK"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "PD-OPTIC-NOSTOCK",
				"item_name": "PD-OPTIC-NOSTOCK", "item_group": "Accessory",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 0,
			}).insert(ignore_permissions=True)
		out = self._direct_order(customer=self._indiv("PD NoStock Buyer"),
			item_code="PD-OPTIC-NOSTOCK")
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self._paid(si.name)
		with patch.object(stock_ledger, "is_negative_stock_allowed",
				return_value=False):
			with self.assertRaises(frappe.ValidationError):
				manual_order.mark_shipped_manually(si.name)
		self.assertEqual(len(frappe.get_all("Delivery Note Item",
			filters={"against_sales_order": si.items[0].sales_order,
				"docstatus": 1})), 0)
		self.assertIsNotNone(_row(self._rows(enabled=True), si.name),
			"a failed ship leg must keep the row in the queue")

	def test_cancel_refuses_dn_mixing_other_orders(self):
		"""cancel_order fails closed on a Delivery Note that also carries
		another order's lines — cancelling it would silently un-ship someone
		else's goods, so the whole cancel rolls back."""
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		# Same customer for both orders — a DN may only merge one customer's
		# Sales Orders, and that legitimate merged shape is exactly what the
		# cancel guard must refuse to cancel wholesale.
		customer = self._biz("PD MixDN Biz")
		out_a = self._direct_order(buyer_type="business", customer=customer)
		out_b = self._direct_order(buyer_type="business", customer=customer)
		si_a = frappe.get_doc("Sales Invoice", out_a["sales_invoice"])
		si_b = frappe.get_doc("Sales Invoice", out_b["sales_invoice"])
		self._stock_acc(2, si_a.items[0].warehouse)
		dn = make_delivery_note(si_a.items[0].sales_order)
		for d in make_delivery_note(si_b.items[0].sales_order).items:
			dn.append("items", d)
		dn.flags.ignore_permissions = True
		dn.insert()
		dn.submit()
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.cancel_order(si_a.name, "PD mixed DN test")
		self.assertIn("another order", str(cm.exception).lower())
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", si_a.name, "docstatus"), 1,
			"the refused cancel must leave the invoice submitted")

	def test_undelivered_stock_lines_skips_dropship_and_shipnow(self):
		"""The line filter's db-free exclusions: a ship-now invoice has no
		Delivery Note leg at all, and a drop-ship line is never our stock."""
		shipnow = SimpleNamespace(update_stock=1, items=[])
		self.assertEqual(manual_order._undelivered_stock_lines(shipnow), [])
		dropship = SimpleNamespace(update_stock=0, items=[SimpleNamespace(
			so_detail="X", sales_order="SO-X", delivered_by_supplier=1,
			item_code=ACC_ITEM)])
		self.assertEqual(manual_order._undelivered_stock_lines(dropship), [])
