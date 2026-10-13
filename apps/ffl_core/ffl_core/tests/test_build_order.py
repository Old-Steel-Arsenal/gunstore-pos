"""Build-and-ship orders: ship firearms to a destination FFL.

Guns mailed to an FFL skip our 4473 (the buyer does it there), but the bound
book still needs a transfer disposition the moment stock leaves. This covers
the build-order compliance surface:

  - the 4473 gate is skipped when ffl_destination_ffl is set (individual OR
    dealer buyer);
  - shipping NOW (update_stock=1) auto-books one transfer disposition per
    regulated serial — FFL Transfer for an individual buyer, Dealer Transfer
    for a dealer buyer — to the destination FFL;
  - antiques ship + need a serial but are NOT regulated: no FFL, no disposition;
  - cancelling the invoice cancels the disposition (FFL Transfer included — the
    leak the old Dealer-Transfer-only filter caused);
  - deferring (update_stock=0) books nothing, surfaces on the pending queue,
    and dispose_order() then books + issues stock;
  - Zelle + ACH exist as enabled Modes of Payment with a per-company account.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

INDIV = "_Test BO Individual"
DEALER = "_Test BO Dealer"
DEST_FFL = "1-99-888-01-2D-09999"
DEALER_FFL = "1-99-888-01-2D-09998"


class BuildOrderMixin:
	"""Shared fixtures + helpers WITHOUT test methods. Modules that only need
	_ship/_stocked_serial etc. subclass this alongside FrappeTestCase --
	inheriting BuildOrderFixtures instead would re-run its six commit-heavy
	tests once per subclass (unittest collects inherited test_ methods)."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		# Was: the first non-group warehouse this company has, with no order_by
		# and no exclusions — the exact pattern warehouses.default_stock_warehouse
		# exists to replace. Harmless while every warehouse was sellable; once a
		# customer-custody warehouse exists (created lazily by the first real
		# transfer receive) MySQL could hand THAT back, and these suites would
		# stock their fixtures into custody and then fail — intermittently,
		# because row order is not guaranteed. Root cause of the flaky failure:
		# the product code was right to refuse those guns.
		from ffl_core.warehouses import default_stock_warehouse
		cls.warehouse = default_stock_warehouse(cls.company)

		cls._item("BO-GUN", is_antique=0)
		cls._item("BO-ANTIQUE", is_antique=1)
		cls._item("BO-ACC", firearm=False)

		if not frappe.db.exists("Customer", INDIV):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": INDIV,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", DEALER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": DEALER,
				"customer_group": "Commercial", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", DEALER, {
			"is_ffl_dealer": 1, "ffl_number": DEALER_FFL}, update_modified=False)

		for ffl in (DEST_FFL, DEALER_FFL):
			if not frappe.db.exists("ATF FFL Record", ffl):
				frappe.get_doc({
					"doctype": "ATF FFL Record", "ffl_number": ffl,
					"licensee_name": f"Test FFL {ffl[-5:]}",
					"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
				}).insert(ignore_permissions=True)
			# A complete, shippable address so dispose_order's _require_valid_ffl
			# passes deterministically (independent of any other test's edits).
			frappe.db.set_value("ATF FFL Record", ffl, {
				"street": "100 Test Pkwy", "city": "Springfield", "state": "TX", "zip": "75024",
				"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
			}, update_modified=False)

	def _indiv(self, name: str) -> str:
		"""A dedicated individual Customer per test. create_order's submit chain
		commits (FastBound enqueue / receipt hooks), so FrappeTestCase's rollback
		can't undo a set_customer_tax_exempt on a SHARED customer — that leaks the
		is_tax_exempt flag between tests and trips over-billing on a fresh DB."""
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		return name

	def _make_warehouse(self, label: str) -> str:
		existing = frappe.db.get_value(
			"Warehouse", {"warehouse_name": label, "company": self.company}, "name")
		if existing:
			return existing
		wh = frappe.get_doc({
			"doctype": "Warehouse", "warehouse_name": label, "company": self.company})
		wh.flags.ignore_permissions = True
		wh.insert()
		return wh.name

	@classmethod
	def _item(cls, code: str, *, firearm: bool = True, is_antique: int = 0) -> None:
		if frappe.db.exists("Item", code):
			return
		doc = {
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Firearm" if firearm else "Accessory", "stock_uom": "Nos",
			"is_stock_item": 1 if firearm else 0,
			"has_serial_no": 1 if firearm else 0,
		}
		if firearm:
			doc["is_antique"] = is_antique
		frappe.get_doc(doc).insert(ignore_permissions=True)

	def _stocked_serial(self, serial: str, item_code: str = "BO-GUN",
			warehouse: str | None = None) -> str:
		from ffl_core.firearm import serialized_receipt_row
		wh = warehouse or self.warehouse
		if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
				"Serial No", serial, "status") == "Active":
			return serial
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code=item_code, qty=1, warehouse=wh,
				basic_rate=500, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return serial

	def _ship(self, serial: str, *, item_code="BO-GUN", customer=INDIV,
			destination_ffl=DEST_FFL, update_stock=1,
			buyer_type="Individual") -> "frappe.Document":
		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.customer = customer
		if destination_ffl:
			si.ffl_destination_ffl = destination_ffl
		si.ffl_buyer_type = buyer_type
		si.append("items", {
			"item_code": item_code, "qty": 1, "rate": 900,
			"warehouse": self.warehouse,
			"serial_no": serial, "use_serial_batch_fields": 1,
		})
		si.set_missing_values()
		si.flags.ignore_permissions = True
		si.insert()
		# Pin update_stock AFTER insert (the dealer-shipment before_insert hook
		# forces it on for dealer buyers); this is what create_order does.
		si.update_stock = update_stock
		si.save()
		si.submit()
		return si

	def _disp(self, si_name: str) -> list[dict]:
		return frappe.get_all(
			"FFL Disposition",
			filters={"sales_invoice": si_name, "docstatus": 1},
			fields=["name", "disposition_type", "destination_ffl", "serial", "stock_entry"])


class BuildOrderFixtures(BuildOrderMixin, FrappeTestCase):
	"""The shared fixtures' own coverage -- six build-order tests. Sibling
	modules that only need the helpers subclass BuildOrderMixin instead."""

	def test_resolve_ship_to_carries_secondary_unit_as_line2(self):
		"""The firearm ship-to Address is derived straight from the ATF record;
		a secondary unit must ride along as address_line2, not vanish now that
		the city is clean. (Per-test set_value rolls back in tearDown.)"""
		from ffl_core.api.manual_order import _resolve_ship_to
		frappe.db.set_value("ATF FFL Record", DEST_FFL, {
			"street": "100 MAIN ST", "street2": "UNIT 103",
			"city": "SPRINGFIELD", "state": "NY", "zip": "10001",
		}, update_modified=False)
		ship_to, _name = _resolve_ship_to({}, DEST_FFL)
		self.assertEqual(ship_to["address_line1"], "100 MAIN ST")
		self.assertEqual(ship_to["address_line2"], "UNIT 103")
		self.assertEqual(ship_to["city"], "SPRINGFIELD")

	def test_create_order_without_open_pos_shift(self):
		"""Building an order must not require a manually-opened POS shift: with no
		open POS Opening Entry, create_order falls back to an enabled POS Profile
		instead of throwing 'open the POS first'."""
		from unittest.mock import patch
		from ffl_core.api import manual_order
		frappe.db.delete("POS Opening Entry", {"status": "Open"})  # rolled back per-test
		# Stock into an explicit normal warehouse — the shared `self.warehouse`
		# fixture is non-deterministic (no order_by) and on a fresh DB can resolve
		# to the company's "Goods In Transit" warehouse, which isn't shippable.
		serial = self._stocked_serial(
			"BO-NOPOS-1", warehouse=self._make_warehouse("BO NoPOS WH"))
		# No open shift + no explicit profile → must fall back to an enabled
		# profile. Pin the default company so the fallback resolves THIS company's
		# profile (the shared dev DB carries others). Verifies _default_pos_profile.
		with patch.object(manual_order, "_default_company", return_value=self.company):
			out = manual_order.create_order({
				"buyer_type": "individual", "customer": self._indiv("BO NoShift Buyer"),
				"destination_ffl": DEST_FFL, "dispose_now": 0, "payment_received": 0,
				"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 700, "serials": [serial]}],
			})
		self.assertTrue(out["ok"])
		self.assertTrue(frappe.db.exists("Sales Invoice", out["sales_invoice"]))
		self.assertFalse(out["disposed"])

	def test_create_order_ships_from_serial_warehouse(self):
		"""Ship-now deducts each firearm from the warehouse its serial is actually
		in — not the POS profile's default — so a gun received into another
		warehouse doesn't trip a false 'insufficient stock'."""
		from ffl_core.api.manual_order import create_order
		# Pin THIS company's profile (the shared dev DB has other companies);
		# the gun then sits in a warehouse other than the profile's default.
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.assertTrue(pos, "dev_site should seed a POS Profile for the test company")
		annex = self._make_warehouse("BO Annex")
		serial = self._stocked_serial("BO-ANNEX-1", warehouse=annex)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "warehouse"), annex)
		out = create_order({
			"buyer_type": "business", "customer": DEALER, "pos_profile": pos,
			"destination_ffl": DEST_FFL, "dispose_now": 1, "payment_received": 0,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 800, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		self.assertTrue(out["disposed"])
		# The SI line carried the annex warehouse, and the gun shipped out of it.
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.items[0].warehouse, annex)
		self.assertNotEqual(
			frappe.db.get_value("Serial No", serial, "status"), "Active")

	def test_create_order_paid_ship_now_keeps_serial_warehouse(self):
		"""The is_pos (paid) path reruns set_pos_fields, which resets each line's
		warehouse + update_stock to the POS-profile default; the fix re-asserts the
		serial's warehouse + ship-now intent so a PAID ship-now still deducts from
		where the gun actually is (regression guard for the set_pos_fields reset)."""
		from ffl_core.api.manual_order import create_order
		pos = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		annex = self._make_warehouse("BO Annex Paid")
		serial = self._stocked_serial("BO-ANNEXPAID-1", warehouse=annex)
		out = create_order({
			"buyer_type": "individual", "customer": self._indiv("BO Paid Buyer"),
			"pos_profile": pos, "destination_ffl": DEST_FFL, "dispose_now": 1,
			"payment_received": 1, "mode_of_payment": "Cash", "tax_exempt": 1,
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		self.assertTrue(out["disposed"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.is_pos, 1)
		self.assertEqual(si.update_stock, 1)
		self.assertEqual(si.items[0].warehouse, annex)  # survived set_pos_fields
		self.assertNotEqual(
			frappe.db.get_value("Serial No", serial, "status"), "Active")

	def test_create_order_cross_company_profile_clear_error(self):
		"""A POS Profile whose company differs from where the gun is stocked fails
		with an actionable message, not a raw ERPNext warehouse-company crash."""
		from ffl_core.api.manual_order import create_order
		annex = self._make_warehouse("BO Annex XCo")
		serial = self._stocked_serial("BO-XCO-1", warehouse=annex)
		other_pos = frappe.db.get_value(
			"POS Profile", {"disabled": 0, "company": ["!=", self.company]}, "name")
		if not other_pos:
			self.skipTest("need a second company's POS Profile in the dev DB")
		with self.assertRaises(frappe.ValidationError) as cm:
			create_order({
				"buyer_type": "business", "customer": DEALER, "pos_profile": other_pos,
				"destination_ffl": DEST_FFL, "dispose_now": 1, "payment_received": 0,
				"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 700, "serials": [serial]}],
			})
		self.assertIn("inventory", str(cm.exception).lower())

	def test_create_order_bogus_pos_profile_clear_error(self):
		"""A non-existent explicit pos_profile fails cleanly, not with AttributeError."""
		from ffl_core.api.manual_order import create_order
		serial = self._stocked_serial("BO-BOGUS-1")
		with self.assertRaises(frappe.ValidationError) as cm:
			create_order({
				"buyer_type": "individual", "customer": self._indiv("BO Bogus Buyer"),
				"pos_profile": "NO-SUCH-PROFILE-XYZ",
				"destination_ffl": DEST_FFL, "dispose_now": 0, "payment_received": 0,
				"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 700, "serials": [serial]}],
			})
		self.assertIn("no longer exists", str(cm.exception).lower())


# --------------------------------------------------------------- 4473 skip


class Skip4473(BuildOrderMixin, FrappeTestCase):
	def _gate(self, doc):
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		require_4473_and_nics_for_firearm_sale(doc)

	def _ns(self, **over) -> SimpleNamespace:
		base = dict(doctype="Sales Invoice", docstatus=1, is_consolidated=0,
			customer=INDIV, ffl_destination_ffl=None,
			items=[SimpleNamespace(idx=1, item_code="BO-GUN", serial_no="X1")])
		base.update(over)
		return SimpleNamespace(**base)

	def test_destination_ffl_skips_gate(self):
		self._gate(self._ns(ffl_destination_ffl=DEST_FFL))  # no raise

	def test_no_destination_blocks_without_4473(self):
		with self.assertRaises(frappe.ValidationError):
			self._gate(self._ns(ffl_destination_ffl=None))


# ----------------------------------------------------------- booking + cancel


class ShipNowBooks(BuildOrderMixin, FrappeTestCase):
	def test_individual_books_ffl_transfer(self):
		serial = self._stocked_serial("BO-N1")
		si = self._ship(serial, customer=INDIV)
		disp = self._disp(si.name)
		self.assertEqual(len(disp), 1)
		self.assertEqual(disp[0]["disposition_type"], "FFL Transfer")
		self.assertEqual(disp[0]["destination_ffl"], DEST_FFL)
		self.assertEqual(disp[0]["serial"], serial)
		# SI moved the stock; the disposition must not double-issue.
		self.assertFalse(disp[0]["stock_entry"])
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Delivered")

	def test_dealer_books_dealer_transfer(self):
		serial = self._stocked_serial("BO-N2")
		si = self._ship(serial, customer=DEALER)
		disp = self._disp(si.name)
		self.assertEqual(len(disp), 1)
		self.assertEqual(disp[0]["disposition_type"], "Dealer Transfer")
		self.assertEqual(disp[0]["destination_ffl"], DEST_FFL)

	def test_antique_needs_no_ffl_and_books_nothing(self):
		serial = self._stocked_serial("BO-A1", item_code="BO-ANTIQUE")
		si = self._ship(serial, item_code="BO-ANTIQUE", destination_ffl=None)
		self.assertFalse(self._disp(si.name))
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Delivered")

	def test_cancel_cancels_ffl_transfer(self):
		serial = self._stocked_serial("BO-N3")
		si = self._ship(serial, customer=INDIV)
		disp = self._disp(si.name)
		self.assertEqual(len(disp), 1)
		si.reload()
		si.cancel()
		self.assertEqual(
			frappe.db.get_value("FFL Disposition", disp[0]["name"], "docstatus"), 2)


# --------------------------------------------------------------- deferral


class DeferAndDispose(BuildOrderMixin, FrappeTestCase):
	def test_deferred_books_nothing_then_dispose_order_books(self):
		from ffl_core.api.manual_order import dispose_order, list_pending_dispositions
		serial = self._stocked_serial("BO-D1")
		# A business may ship on account (defer), then dispose without paying first.
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		# Billing-only: nothing booked, gun still on the shelf.
		self.assertFalse(self._disp(si.name))
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")

		pending = list_pending_dispositions()
		self.assertIn(si.name, [r["sales_invoice"] for r in pending])

		res = dispose_order(si.name)
		self.assertTrue(res["ok"])
		self.assertEqual(res["disposed_count"], 1)
		disp = self._disp(si.name)
		self.assertEqual(len(disp), 1)
		self.assertEqual(disp[0]["disposition_type"], "FFL Transfer")
		# The manual disposition issued the stock-out itself (a Material Issue
		# Stock Entry), so the serial is Consumed — not Delivered, which is the
		# ship-now path where the Sales Invoice delivery moves the stock.
		self.assertTrue(disp[0]["stock_entry"])
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Consumed")
		# Shipped on account but still unpaid: it lingers in Pending Order awaiting
		# payment, but no longer needs disposition.
		rows = {r["sales_invoice"]: r for r in list_pending_dispositions()}
		self.assertIn(si.name, rows)
		self.assertFalse(rows[si.name]["needs_disposition"])

	def test_dispose_order_is_idempotent(self):
		from ffl_core.api.manual_order import dispose_order
		serial = self._stocked_serial("BO-D2")
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		dispose_order(si.name)
		dispose_order(si.name)  # re-run must not double-book
		self.assertEqual(len(self._disp(si.name)), 1)

	def test_individual_unpaid_order_cannot_be_disposed(self):
		from ffl_core.api.manual_order import dispose_order, list_pending_dispositions
		serial = self._stocked_serial("BO-D3")
		# An individual's guns may not ship while the order is unpaid (on account).
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Individual")
		# It stays in the Pending Order queue, awaiting payment.
		self.assertIn(si.name, [r["sales_invoice"] for r in list_pending_dispositions()])
		with self.assertRaises(frappe.ValidationError):
			dispose_order(si.name)
		# Nothing booked; gun still on the shelf.
		self.assertFalse(self._disp(si.name))
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")


# --------------------------------------------------------------- payment setup


class ManualModes(BuildOrderMixin, FrappeTestCase):
	def test_zelle_and_ach_exist_with_accounts(self):
		from ffl_core.setup.payments import ensure_manual_modes_of_payment
		ensure_manual_modes_of_payment()
		for name in ("Zelle", "ACH"):
			self.assertTrue(frappe.db.exists("Mode of Payment", name))
			self.assertTrue(frappe.db.get_value("Mode of Payment", name, "enabled"))
			acct = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": name, "company": self.company}, "default_account")
			self.assertTrue(acct, f"{name} has no account for {self.company}")


# --------------------------------------------------------- Zelle transaction no


class ZelleTransactionNumber(BuildOrderMixin, FrappeTestCase):
	"""A Zelle tender must carry its Transaction Number (stored on reference_no)
	before the sale can complete — inline at order creation and at Record Payment."""

	def test_create_order_zelle_requires_number(self):
		from ffl_core.api.manual_order import create_order
		serial = self._stocked_serial("BO-ZELLE-1")
		with self.assertRaises(frappe.ValidationError) as cm:
			create_order({
				"buyer_type": "individual", "customer": self._indiv("BO Zelle Buyer 1"),
				"destination_ffl": DEST_FFL, "dispose_now": 0,
				"payment_received": 1, "mode_of_payment": "Zelle", "tax_exempt": 1,
				"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600, "serials": [serial]}],
			})
		self.assertIn("transaction number", str(cm.exception).lower())

	def test_create_order_zelle_records_reference_no(self):
		from ffl_core.api.manual_order import create_order
		serial = self._stocked_serial("BO-ZELLE-2")
		out = create_order({
			"buyer_type": "individual", "customer": self._indiv("BO Zelle Buyer 2"),
			"destination_ffl": DEST_FFL, "dispose_now": 0,
			"payment_received": 1, "mode_of_payment": "Zelle", "tax_exempt": 1,
			"transaction_number": "ZEL-AAA-111",
			"items": [{"item_code": "BO-GUN", "qty": 1, "rate": 600, "serials": [serial]}],
		})
		self.assertTrue(out["ok"])
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.payments[0].mode_of_payment, "Zelle")
		self.assertEqual(si.payments[0].reference_no, "ZEL-AAA-111")

	def test_record_payment_zelle_requires_number(self):
		from ffl_core.api.manual_order import record_payment
		serial = self._stocked_serial("BO-ZELLE-3")
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		with self.assertRaises(frappe.ValidationError) as cm:
			record_payment(si.name, "Zelle")
		self.assertIn("transaction number", str(cm.exception).lower())

	def test_record_payment_zelle_sets_reference_no(self):
		from ffl_core.api.manual_order import record_payment
		serial = self._stocked_serial("BO-ZELLE-4")
		si = self._ship(serial, customer=INDIV, update_stock=0, buyer_type="Business")
		out = record_payment(si.name, "Zelle", transaction_number="ZEL-BBB-222")
		self.assertTrue(out["ok"])
		self.assertTrue(out["paid"])
		pe = frappe.get_doc("Payment Entry", out["payment_entry"])
		self.assertEqual(pe.reference_no, "ZEL-BBB-222")


if __name__ == "__main__":
	unittest.main()
