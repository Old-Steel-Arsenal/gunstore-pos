"""Create Order: partial payments at creation + resumable drafts.

Partial payment: an order may be created with a deposit (Payment Entry on
account — the is_pos inline path stays full-amount-only), but an INDIVIDUAL
order must be fully paid before it disposes/ships (dealers keep their
on-account privilege).

Drafts: the page's form state parks in the shared Create Order Draft doctype;
serials are NOT reserved and are re-validated on resume.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin


class OrderExtrasFixtures(BuildOrderMixin, FrappeTestCase):
	def _payload(self, serial: str, *, customer: str, rate: float = 700, **over) -> dict:
		base = {
			"buyer_type": "individual",
			"customer": customer,
			"destination_ffl": DEST_FFL,
			"dispose_now": 0,
			"payment_received": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": rate, "serials": [serial]}],
		}
		base.update(over)
		return base

	def _exempt_indiv(self, name: str) -> str:
		"""A dedicated tax-exempt individual so totals are exact (no tax math)."""
		customer = self._indiv(name)
		manual_order.set_customer_tax_exempt(customer, 1, None)
		return customer


class TestPartialPayment(OrderExtrasFixtures):
	def test_partial_books_payment_entry_and_outstanding(self):
		customer = self._exempt_indiv("PP Deposit Buyer")
		serial = self._stocked_serial("PP-PART-1")
		out = manual_order.create_order(self._payload(serial, customer=customer,
			payment_received=1, mode_of_payment="Cash", paid_amount=200))
		self.assertTrue(out["ok"])
		self.assertEqual(flt(out["paid_amount"]), 200)
		self.assertEqual(flt(out["outstanding"]), 500)
		pes = frappe.get_all("Payment Entry Reference",
			filters={"reference_doctype": "Sales Invoice",
				"reference_name": out["sales_invoice"], "docstatus": 1},
			fields=["allocated_amount"])
		self.assertEqual(len(pes), 1)
		self.assertEqual(flt(pes[0].allocated_amount), 200)
		# The deposit order sits in the queue unpaid → dispose still blocked.
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertIsNotNone(manual_order.ship_payment_block(si))

	def test_partial_then_record_payment_unblocks(self):
		customer = self._exempt_indiv("PP Balance Buyer")
		serial = self._stocked_serial("PP-PART-2")
		out = manual_order.create_order(self._payload(serial, customer=customer,
			payment_received=1, mode_of_payment="Cash", paid_amount=250))
		res = manual_order.record_payment(out["sales_invoice"], "Cash", amount=450)
		self.assertTrue(res["paid"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertIsNone(manual_order.ship_payment_block(si))

	def test_partial_with_dispose_now_individual_throws(self):
		customer = self._exempt_indiv("PP DisposeNow Buyer")
		serial = self._stocked_serial("PP-PART-3")
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.create_order(self._payload(serial, customer=customer,
				dispose_now=1, payment_received=1, mode_of_payment="Cash",
				paid_amount=100))
		self.assertIn("full", str(cm.exception).lower())
		# Nothing half-created sticks around.
		self.assertFalse(frappe.db.exists("Serial No",
			{"name": serial, "status": ("!=", "Active")}))

	def test_business_partial_dispose_now_still_allowed(self):
		"""Dealers keep their on-account shipping privilege — a deposit must not
		block dispose_now for a Business buyer."""
		serial = self._stocked_serial("PP-PART-4")
		# Cash, not ACH: the ACH Mode of Payment Account seeding is
		# nondeterministic on a fresh CI database, and the tender is
		# irrelevant here — this test pins the BUYER-TYPE gate.
		out = manual_order.create_order(self._payload(serial,
			customer="_Test BO Dealer", buyer_type="business",
			dispose_now=1, payment_received=1, mode_of_payment="Cash",
			paid_amount=100, tax_exempt=1))
		self.assertTrue(out["disposed"])
		self.assertEqual(flt(out["outstanding"]), 600)

	def test_negative_amount_throws(self):
		customer = self._exempt_indiv("PP Negative Buyer")
		serial = self._stocked_serial("PP-PART-5")
		with self.assertRaises(frappe.ValidationError):
			manual_order.create_order(self._payload(serial, customer=customer,
				payment_received=1, mode_of_payment="Cash", paid_amount=-50))

	def test_full_payment_is_pos_path_unchanged(self):
		customer = self._exempt_indiv("PP Full Buyer")
		serial = self._stocked_serial("PP-PART-6")
		out = manual_order.create_order(self._payload(serial, customer=customer,
			payment_received=1, mode_of_payment="Cash"))
		self.assertEqual(flt(out["outstanding"]), 0)
		self.assertEqual(flt(out["paid_amount"]), 700)
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", out["sales_invoice"], "is_pos"), 1)


class TestOrderDrafts(OrderExtrasFixtures):
	def _draft_payload(self, serial: str | None = None) -> dict:
		items = []
		if serial:
			items.append({"item_code": "BO-GUN", "qty": 1, "rate": 900,
				"serials": [serial]})
		return {"buyer_type": "individual", "customer": "_Test BO Individual",
			"destination_ffl": DEST_FFL, "items": items,
			"mode_of_payment": "Cash", "payment_received": 0}

	def test_draft_roundtrip(self):
		saved = manual_order.save_order_draft(
			json.dumps(self._draft_payload("DRAFT-SN-1")))
		name = saved["name"]
		self.assertTrue(frappe.db.exists("Create Order Draft", name))

		rows = manual_order.list_order_drafts()
		mine = next(r for r in rows if r["name"] == name)
		self.assertEqual(mine["customer"], "_Test BO Individual")
		self.assertEqual(flt(mine["total"]), 900)

		got = manual_order.get_order_draft(name)
		self.assertEqual(got["payload"]["customer"], "_Test BO Individual")
		self.assertEqual(got["payload"]["items"][0]["serials"], ["DRAFT-SN-1"])

		payload2 = self._draft_payload("DRAFT-SN-1")
		payload2["items"][0]["rate"] = 950
		saved2 = manual_order.save_order_draft(json.dumps(payload2), name=name)
		self.assertEqual(saved2["name"], name)
		self.assertEqual(
			flt(manual_order.get_order_draft(name)["payload"]["items"][0]["rate"]), 950)

		manual_order.delete_order_draft(name)
		self.assertFalse(frappe.db.exists("Create Order Draft", name))

	def test_create_order_consumes_draft(self):
		customer = self._exempt_indiv("Draft Consume Buyer")
		serial = self._stocked_serial("DRAFT-SN-2")
		draft = manual_order.save_order_draft(json.dumps(self._draft_payload(serial)))
		out = manual_order.create_order(self._payload(serial, customer=customer,
			draft=draft["name"]))
		self.assertTrue(out["ok"])
		self.assertFalse(frappe.db.exists("Create Order Draft", draft["name"]))

	def test_dispose_now_auto_pushes_to_shipstation(self):
		"""Dispose & Ship Now auto-pushes the shipment — and a push failure
		must NEVER unwind the created+disposed order (it stays on the queue
		for a manual Ship instead)."""
		serial = self._stocked_serial("AUTOSHIP-1")
		with patch.object(manual_order, "_try_push_shipment",
				return_value=(True, "")) as pushed:
			out = manual_order.create_order(self._payload(serial,
				customer="_Test BO Dealer", buyer_type="business",
				dispose_now=1, tax_exempt=1))
		pushed.assert_called_once_with(out["sales_invoice"])
		self.assertTrue(out["disposed"])
		self.assertTrue(out["ship_pushed"])

		serial2 = self._stocked_serial("AUTOSHIP-2")
		with patch.object(manual_order, "_try_push_shipment",
				return_value=(False, "boom")):
			out2 = manual_order.create_order(self._payload(serial2,
				customer="_Test BO Dealer", buyer_type="business",
				dispose_now=1, tax_exempt=1))
		self.assertTrue(out2["ok"])
		self.assertTrue(out2["disposed"])
		self.assertFalse(out2["ship_pushed"])
		self.assertIn("boom", out2["ship_error"])

	def test_deferred_order_does_not_auto_push(self):
		customer = self._exempt_indiv("AutoShip Defer Buyer")
		serial = self._stocked_serial("AUTOSHIP-3")
		with patch.object(manual_order, "_try_push_shipment") as pushed:
			out = manual_order.create_order(self._payload(serial,
				customer=customer, dispose_now=0))
		pushed.assert_not_called()
		self.assertTrue(out["ok"])

	def test_try_push_shipment_gates(self):
		"""Disabled ShipStation → quiet no-op; push exception → (False, err)."""
		with patch.object(manual_order, "_shipstation_enabled", return_value=False):
			self.assertEqual(manual_order._try_push_shipment("SI-X"), (False, ""))
		with patch.object(manual_order, "_shipstation_enabled", return_value=True), \
			patch("frappe.get_attr") as ga, \
			patch("frappe.log_error"):
			ga.return_value = MagicMock(side_effect=Exception("api down"))
			ok, err = manual_order._try_push_shipment("SI-X")
			self.assertFalse(ok)
			self.assertIn("api down", err)
		with patch.object(manual_order, "_shipstation_enabled", return_value=True), \
			patch("frappe.get_attr") as ga2:
			ga2.return_value = MagicMock(return_value={"ok": True})
			self.assertEqual(manual_order._try_push_shipment("SI-X"), (True, ""))

	def test_note_lands_on_invoice_remarks_and_queue_row(self):
		"""A staff note entered at Create Order must live where staff LOOK:
		on the Sales Invoice (remarks) and on its Pending Order queue row —
		not only as a buried Sales Order timeline comment."""
		customer = self._exempt_indiv("Note Buyer")
		serial = self._stocked_serial("NOTE-SN-1")
		note = "Hold until Friday — customer adding a suppressor."
		out = manual_order.create_order(self._payload(serial, customer=customer,
			notes=note))
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", out["sales_invoice"], "ffl_staff_note"),
			note)
		with patch.object(manual_order, "_shipstation_enabled", return_value=False):
			rows = manual_order.list_pending_dispositions()
		mine = next(r for r in rows if r["sales_invoice"] == out["sales_invoice"])
		self.assertEqual(mine.get("note"), note)

	def test_check_serials_available(self):
		good = self._stocked_serial("DRAFT-SN-3")
		sold = self._stocked_serial("DRAFT-SN-4")
		self._ship(sold, update_stock=1)
		res = manual_order.check_serials_available(
			json.dumps([good.lower(), sold, "NO-SUCH-SN"]))
		self.assertTrue(res[good.lower()]["ok"])
		self.assertEqual(res[good.lower()]["serial"], good)
		self.assertFalse(res[sold]["ok"])
		self.assertIn("not in stock", res[sold]["reason"])
		self.assertFalse(res["NO-SUCH-SN"]["ok"])
