"""Pending Order × Cancel Order — cancel_order() reverses a counter transfer
order end-to-end: dispositions (via the invoice's own on_cancel cascade),
stock, the linked Sales Order, and the money (refund Payment Entry), with a
best-effort ShipStation void. The void is ALWAYS patched — never live."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import BuildOrderMixin


class CancelOrderFixtures(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin (not BuildOrderFixtures) on purpose — the fixture CLASS
	carries commit-heavy test methods unittest would re-run per subclass."""

	def _cancel(self, si_name, reason="test cancel", **kw):
		with patch.object(manual_order, "_try_void_shipment",
				return_value=(True, "")) as void:
			out = manual_order.cancel_order(si_name, reason, **kw)
		return out, void


class TestCancelPlainOrder(CancelOrderFixtures):
	def test_unpaid_undisposed_cancels_invoice_only(self):
		serial = "CO-N1"
		si = self._ship(self._stocked_serial(serial), update_stock=0,
			buyer_type="Business")
		out, void = self._cancel(si.name)
		self.assertTrue(out["ok"])
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", si.name, "docstatus"), 2)
		self.assertIsNone(out["refund_entry"])
		self.assertEqual(out["dispositions_cancelled"], [])
		void.assert_not_called()  # never pushed to ShipStation
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")


class TestCancelDisposedOrder(CancelOrderFixtures):
	def test_reverses_disposition_and_stock(self):
		serial = "CO-D1"
		si = self._ship(self._stocked_serial(serial), update_stock=0,
			buyer_type="Business")  # business may dispose unpaid (on account)
		manual_order.dispose_order(si.name)
		disp = frappe.get_all("FFL Disposition",
			filters={"sales_invoice": si.name, "docstatus": 1}, pluck="name")
		self.assertEqual(len(disp), 1)
		self.assertNotEqual(
			frappe.db.get_value("Serial No", serial, "status"), "Active")

		out, _ = self._cancel(si.name)
		self.assertEqual(out["dispositions_cancelled"], disp)
		self.assertEqual(
			frappe.db.get_value("FFL Disposition", disp[0], "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")


class TestCancelPaidOrder(CancelOrderFixtures):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The card refund needs the "Credit Card" MoP's company account. A full
		# serial run used to inherit it from an earlier module; a module run on
		# its own (or in a parallel worker) must seed it itself.
		from ffl_integrations.tests.site_fixtures import ensure_credit_card_mop_account
		ensure_credit_card_mop_account()

	def test_books_refund_for_paid_total(self):
		si = self._ship(self._stocked_serial("CO-P1"), update_stock=0)
		manual_order.record_payment(si.name, "Cash")  # full outstanding
		si.reload()
		self.assertTrue(manual_order._is_paid(si.outstanding_amount))

		out, _ = self._cancel(si.name, refund_mode="Cash")
		self.assertTrue(out["refund_entry"])
		pe = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(pe.payment_type, "Pay")
		self.assertEqual(pe.party, si.customer)
		self.assertEqual(pe.docstatus, 1)
		self.assertAlmostEqual(pe.paid_amount, out["refunded_amount"], places=2)
		self.assertAlmostEqual(out["refunded_amount"], si.grand_total, places=2)

	def test_paid_requires_refund_mode(self):
		si = self._ship(self._stocked_serial("CO-P2"), update_stock=0)
		manual_order.record_payment(si.name, "Cash")
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "test")

	def test_partly_paid_refunds_paid_portion(self):
		si = self._ship(self._stocked_serial("CO-P3"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=200)
		out, _ = self._cancel(si.name, refund_mode="Cash")
		self.assertAlmostEqual(out["refunded_amount"], 200, places=2)

	def test_zelle_refund_requires_reference(self):
		si = self._ship(self._stocked_serial("CO-P4"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=100)
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "test", refund_mode="Zelle")

	def test_ach_refund_requires_reference(self):
		"""ACH is the same shape of tender as Zelle — a manual bank transfer whose
		only machine record lives in the bank — and used to be the one mode in the
		list with no reference rule at all."""
		si = self._ship(self._stocked_serial("CO-P6"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=100)
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "test", refund_mode="ACH")

	def test_card_refund_requires_approval_code_on_standalone_terminal(self):
		"""THE refund path this store actually uses — prod has never raised a POS
		return invoice — so a card-reference control that skipped it was a control
		over nothing. Gate refuses without a code; with one, the
		refund books and the Payment Entry CARRIES it (r3-M1: the forced input
		used to be discarded and the invoice name written in its place)."""
		from ffl_core.tests.settings_sandbox import single_value

		si = self._ship(self._stocked_serial("CO-P7"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=150)
		with single_value("Payroc Settings", "enabled", 0), \
				single_value("Payroc Settings", "require_card_reference", 1):
			with self.assertRaises(frappe.ValidationError) as ctx:
				manual_order.cancel_order(si.name, "test", refund_mode="Credit Card")
			self.assertIn("approval code", str(ctx.exception).lower())
			out, _ = self._cancel(si.name, refund_mode="Credit Card",
				refund_reference="APPR 889112")
		self.assertTrue(out["ok"])
		pe = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(pe.reference_no, "APPR 889112")

	def _party_balance(self, customer):
		"""Receivable net for the customer — compared as a delta so reruns on a
		warm site (the submit chain commits) never see an earlier run's rows."""
		from frappe.utils import flt
		return flt(frappe.db.sql("""select coalesce(sum(debit - credit), 0)
			from `tabGL Entry` where party_type='Customer' and party=%s
			and is_cancelled=0""", customer)[0][0])

	def test_inline_paid_cancel_books_no_second_reversal(self):
		"""A full at-counter (is_pos inline) payment is reversed by the invoice
		cancel itself — a Pay PE on top reversed the cash twice and left the
		customer owing the refund (seen on live refunds)."""
		from frappe.utils import flt
		from ffl_core.tests.test_build_order import DEST_FFL

		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		customer = self._indiv("CO Inline Buyer")
		party_before = self._party_balance(customer)
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [self._stocked_serial("CO-I1")]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		paid = flt(si.grand_total) - flt(si.outstanding_amount)
		self.assertGreater(paid, 0)

		cash_acct = manual_order._mode_of_payment_account("Cash", self.company)

		def cash_net():
			return flt(frappe.db.sql("""select coalesce(sum(debit - credit), 0)
				from `tabGL Entry` where account=%s and is_cancelled=0""", cash_acct)[0][0])

		cash_before = cash_net()
		out, _ = self._cancel(si.name, refund_mode="Cash")
		self.assertAlmostEqual(out["refunded_amount"], paid, places=2)
		# The cash leaves the drawer exactly once (was: twice).
		self.assertAlmostEqual(cash_net() - cash_before, -paid, places=2)
		# Customer nets to zero on the receivable: nothing owed, no stray credit.
		self.assertAlmostEqual(self._party_balance(customer) - party_before, 0, places=2)

	def test_inline_cash_refunded_by_zelle_moves_both_accounts(self):
		"""Cash in, Zelle out: the cancel only reverses the cash account, so the
		tender is re-booked there and the Pay PE takes the money out of the
		Zelle account — each account ends where the money physically is."""
		from frappe.utils import flt
		from ffl_core.tests.test_build_order import DEST_FFL

		real_resolve = manual_order._mode_of_payment_account
		cash_acct = real_resolve("Cash", self.company)
		# The test site maps Zelle onto the cash account; point it at a
		# separate bank account so the two legs are distinguishable.
		abbr = frappe.db.get_value("Company", self.company, "abbr")
		zelle_acct = f"CO Zelle Bank - {abbr}"
		if not frappe.db.exists("Account", zelle_acct):
			parent = frappe.db.get_value("Account", {"company": self.company,
				"is_group": 1, "account_type": "Bank"}, "name") or \
				frappe.db.get_value("Account", {"company": self.company,
					"is_group": 1, "root_type": "Asset", "account_name": "Current Assets"},
					"name")
			frappe.get_doc({"doctype": "Account", "account_name": "CO Zelle Bank",
				"company": self.company, "parent_account": parent,
				"account_type": "Bank", "is_group": 0}).insert(ignore_permissions=True)

		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		customer = self._indiv("CO Inline Zelle Refund")
		party_before = self._party_balance(customer)
		created = manual_order.create_order({
			"buyer_type": "individual", "customer": customer,
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600,
				"serials": [self._stocked_serial("CO-I2")]}],
		})
		si = frappe.get_doc("Sales Invoice", created["sales_invoice"])
		paid = flt(si.grand_total) - flt(si.outstanding_amount)

		def net(account):
			return flt(frappe.db.sql("""select coalesce(sum(debit - credit), 0)
				from `tabGL Entry` where account=%s and is_cancelled=0""", account)[0][0])

		cash_before, zelle_before = net(cash_acct), net(zelle_acct)
		with patch.object(manual_order, "_mode_of_payment_account",
				side_effect=lambda mode, company: zelle_acct if mode == "Zelle"
				else real_resolve(mode, company)):
			out, _ = self._cancel(si.name, refund_mode="Zelle",
				refund_reference="ZL-REFUND-1")
		pe = frappe.get_doc("Payment Entry", out["refund_entry"])
		self.assertEqual(pe.paid_from, zelle_acct)
		self.assertAlmostEqual(pe.paid_amount, paid, places=2)
		# Cash stays in the drawer (the cancel's reversal is undone by the
		# re-book); the refund leaves the Zelle account.
		self.assertAlmostEqual(net(cash_acct) - cash_before, 0, places=2)
		self.assertAlmostEqual(net(zelle_acct) - zelle_before, -paid, places=2)
		self.assertAlmostEqual(self._party_balance(customer) - party_before, 0, places=2)


class TestCancelGuards(CancelOrderFixtures):
	def test_refuses_4473_disposition(self):
		si = self._ship(self._stocked_serial("CO-G1"), update_stock=0,
			buyer_type="Business")
		manual_order.dispose_order(si.name)
		name = frappe.get_all("FFL Disposition",
			filters={"sales_invoice": si.name, "docstatus": 1}, pluck="name")[0]
		frappe.db.set_value("FFL Disposition", name, "form_4473_status", "Pending",
			update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "test")

	def test_refuses_double_cancel(self):
		si = self._ship(self._stocked_serial("CO-G2"), update_stock=0,
			buyer_type="Business")
		self._cancel(si.name)
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "again")

	def test_requires_reason(self):
		si = self._ship(self._stocked_serial("CO-G3"), update_stock=0,
			buyer_type="Business")
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "  ")

	def test_requires_roles(self):
		si = self._ship(self._stocked_serial("CO-G4"), update_stock=0,
			buyer_type="Business")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				manual_order.cancel_order(si.name, "test")
		finally:
			frappe.set_user("Administrator")

	def test_refuses_non_counter_orders(self):
		# No destination FFL → not a counter transfer order. _ship's own
		# submit trips the 4473 gate for a regulated firearm shipped with no
		# destination FFL (see Skip4473.test_no_destination_blocks_without_4473
		# in test_build_order.py), so build normally (with the default
		# destination FFL) then blank the field directly — cancel_order only
		# reads ffl_destination_ffl via getattr, so this exercises the same
		# guard without tripping that unrelated gate.
		si = self._ship(self._stocked_serial("CO-G5"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name,
			"ffl_destination_ffl", "", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si.name, "test")
		# Woo-mirrored revenue invoice → belongs to WooCommerce, refuse.
		si2 = self._ship(self._stocked_serial("CO-G6"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si2.name,
			"woo_online_order", "WOO-FAKE-1", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			manual_order.cancel_order(si2.name, "test")


class TestCancelShipStation(CancelOrderFixtures):
	def test_void_failure_rides_response_as_warning(self):
		si = self._ship(self._stocked_serial("CO-S1"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_order_id", "se-999", update_modified=False)
		with patch.object(manual_order, "_try_void_shipment",
				return_value=(False, "label already purchased")) as void:
			out = manual_order.cancel_order(si.name, "test")
		void.assert_called_once_with(si.name)
		self.assertFalse(out["shipstation_voided"])
		self.assertIn("se-999", out["warning"])

	def test_void_success_no_warning(self):
		si = self._ship(self._stocked_serial("CO-S2"), update_stock=0,
			buyer_type="Business")
		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_order_id", "se-1000", update_modified=False)
		out, void = self._cancel(si.name)
		void.assert_called_once_with(si.name)
		self.assertTrue(out["shipstation_voided"])
		self.assertEqual(out["warning"], "")


class TestCancelSalesOrder(CancelOrderFixtures):
	def test_linked_sales_order_cancelled(self):
		si = self._ship(self._stocked_serial("CO-SO1"), update_stock=0,
			buyer_type="Business")
		so = frappe.get_doc({
			"doctype": "Sales Order", "company": self.company,
			"customer": si.customer,
			"transaction_date": frappe.utils.nowdate(),
			"delivery_date": frappe.utils.nowdate(),
			"items": [{"item_code": "BO-ACC", "qty": 1, "rate": 10,
				"warehouse": self.warehouse}],
		})
		so.flags.ignore_permissions = True
		so.insert()
		so.submit()
		frappe.db.set_value("Sales Invoice Item", si.items[0].name,
			"sales_order", so.name, update_modified=False)
		out, _ = self._cancel(si.name)
		self.assertEqual(out["sales_orders_cancelled"], [so.name])
		self.assertEqual(
			frappe.db.get_value("Sales Order", so.name, "docstatus"), 2)


class TestQueueCancelPayload(CancelOrderFixtures):
	def test_row_carries_refund_defaults(self):
		# Cash, not Zelle: a fresh CI site has no Zelle Mode of Payment Account
		# for the test company (the persistent dev DB happens to). The point —
		# last_payment_mode mirrors the most recent payment's mode — is mode-
		# agnostic, and Cash is the one mode the fixtures guarantee an account for.
		si = self._ship(self._stocked_serial("CO-Q1"), update_stock=0)
		manual_order.record_payment(si.name, "Cash", amount=100)
		with patch.object(manual_order, "_shipstation_enabled", return_value=False):
			rows = manual_order.list_pending_dispositions()
		row = next(r for r in rows if r["sales_invoice"] == si.name)
		self.assertAlmostEqual(row["paid_total"], 100, places=2)
		self.assertEqual(row["last_payment_mode"], "Cash")
		self.assertEqual(row["sales_orders"], [])


if __name__ == "__main__":
	unittest.main()
