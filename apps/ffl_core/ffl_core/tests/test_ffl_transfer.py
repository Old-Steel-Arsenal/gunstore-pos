"""Customer Transfer-In (4473 on behalf of the customer) — fee item, picker, and the invariant that a
$0 firearm line + a Transfer Fee line clears the same compliance gates as a sale.

The transfer model: N $0 firearm lines (to drive the existing 4473 → disposition
→ stock-out machinery) + ONE Transfer Fee line (qty = guns). These tests pin the
pieces that are new or that the $0/fee shape could break — the kickoff, gates,
and disposition coverage are reused from the firearm-sale path unchanged.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.firearm import (
	get_transfer_config,
	in_stock_firearm_serials,
	is_firearm_item,
)
from ffl_core.setup.transfer_fee import TRANSFER_FEE_ITEM


def _bootstrap():
	"""Site masters + the Accessory group (for the fee item) + the fee item."""
	from ffl_core.setup import item_groups, test_bootstrap, transfer_fee
	test_bootstrap.run()
	item_groups.ensure()      # full tree incl. Pistol / Optic / Accessory (idempotent)
	transfer_fee.ensure()     # the TRANSFER-FEE service item (idempotent)


def _company() -> str:
	return frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")


def _ensure_firearm_item(code: str) -> str:
	if not frappe.db.exists("Item", code):
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Pistol", "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
		}).insert(ignore_permissions=True)
	return code


def _ensure_optic_item(code: str) -> str:
	if not frappe.db.exists("Item", code):
		frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Optic", "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 0,
		}).insert(ignore_permissions=True)
	return code


def _ensure_serial(serial: str, item_code: str, status: str) -> None:
	if frappe.db.exists("Serial No", serial):
		frappe.delete_doc("Serial No", serial, force=1, ignore_permissions=True)
	doc = frappe.get_doc({
		"doctype": "Serial No", "serial_no": serial,
		"item_code": item_code, "company": _company(),
	}).insert(ignore_permissions=True)
	# Force the status column directly — no stock-ledger lifecycle needed for a query test.
	frappe.db.set_value("Serial No", doc.name, {"status": status}, update_modified=False)


class TransferFeeItem(FrappeTestCase):
	"""TRANSFER-FEE must be a plain non-firearm service line so it never trips the
	4473 gate, serial gate, or stock moves."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()

	def test_fee_item_is_not_a_firearm(self):
		# The whole design hinges on this: the fee never gates as a gun.
		self.assertFalse(is_firearm_item(TRANSFER_FEE_ITEM))

	def test_fee_item_is_a_non_stock_service(self):
		item = frappe.get_doc("Item", TRANSFER_FEE_ITEM)
		self.assertEqual(int(item.is_stock_item), 0)
		self.assertEqual(int(item.has_serial_no), 0)
		self.assertEqual(int(item.is_firearm or 0), 0)
		self.assertEqual(int(item.is_sales_item), 1)

	def test_ensure_is_idempotent(self):
		from ffl_core.setup import transfer_fee
		self.assertEqual(transfer_fee.ensure(), {"created": 0})   # already exists
		self.assertEqual(
			frappe.db.count("Item", {"item_code": TRANSFER_FEE_ITEM}), 1)


class TransferConfig(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()

	def test_returns_configured_fee(self):
		frappe.db.set_single_value("FFL Settings", {
			"transfer_fee_item": TRANSFER_FEE_ITEM, "transfer_fee_amount": 30,
		})
		cfg = get_transfer_config()
		self.assertEqual(cfg["fee_item"], TRANSFER_FEE_ITEM)
		self.assertEqual(cfg["fee_amount"], 30.0)

	def test_defaults_fee_item_when_setting_blank(self):
		frappe.db.set_single_value("FFL Settings", "transfer_fee_item", "")
		self.assertEqual(get_transfer_config()["fee_item"], TRANSFER_FEE_ITEM)


class InStockFirearmSerials(FrappeTestCase):
	"""The transfer picker source: every in-stock firearm serial, firearms only."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()
		_ensure_firearm_item("XFER-GUN-1")
		_ensure_optic_item("XFER-OPTIC-1")
		_ensure_serial("XFER-SN-1", "XFER-GUN-1", "Active")
		_ensure_serial("XFER-SN-2", "XFER-GUN-1", "Active")
		_ensure_serial("XFER-SN-SOLD", "XFER-GUN-1", "Delivered")     # not in stock
		_ensure_serial("XFER-SN-OPTIC", "XFER-OPTIC-1", "Active")     # not a firearm

	def test_lists_active_firearm_serials(self):
		serials = {r["serial"] for r in in_stock_firearm_serials()}
		self.assertIn("XFER-SN-1", serials)
		self.assertIn("XFER-SN-2", serials)

	def test_excludes_sold_and_non_firearm(self):
		serials = {r["serial"] for r in in_stock_firearm_serials()}
		self.assertNotIn("XFER-SN-SOLD", serials)    # Delivered → not Active
		self.assertNotIn("XFER-SN-OPTIC", serials)   # optic → not a firearm

	def test_row_shape(self):
		row = next(r for r in in_stock_firearm_serials() if r["serial"] == "XFER-SN-1")
		self.assertEqual(set(row), {"serial", "item_code", "item_name", "sell_price"})
		self.assertEqual(row["item_code"], "XFER-GUN-1")


class TransferGates(FrappeTestCase):
	"""A $0 firearm line + a Transfer Fee line must clear the SAME serial and
	4473 gates as a normal firearm sale (price-independent), while the fee line
	is ignored by every firearm check."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()
		_ensure_firearm_item("XFER-GUN-1")

	def _transfer_doc(self, *, gun_serials):
		"""A submit-time POS Invoice shaped like a transfer: $0 gun rows + a fee row."""
		items = []
		for i, ser in enumerate(gun_serials, start=1):
			items.append(frappe._dict(item_code="XFER-GUN-1", serial_no=ser, rate=0, idx=i))
		items.append(frappe._dict(
			item_code=TRANSFER_FEE_ITEM, serial_no=None, rate=25, idx=len(items) + 1))
		# SimpleNamespace (not _dict) so .items stays the list, not the dict method.
		return SimpleNamespace(
			doctype="POS Invoice", name="POSINV-XFER", docstatus=1,
			form_4473_status="Completed", nics_status="Proceed",
			is_consolidated=0, customer=None, items=items,
		)

	def test_firearm_rows_ignore_the_fee_row(self):
		# The 4473 kickoff maps N guns, not N+1: the fee line is non-firearm.
		from ffl_integrations.fastbound.pos_4473 import _firearm_rows
		doc = self._transfer_doc(gun_serials=["S1", "S2"])
		rows = _firearm_rows(doc)
		self.assertEqual(len(rows), 2)
		self.assertTrue(all(r.item_code == "XFER-GUN-1" for r in rows))

	def test_serial_gate_passes_zero_dollar_gun_and_skips_fee(self):
		from ffl_core.compliance.firearm import require_serial_for_firearm
		# $0 gun carries a serial, fee row carries none → must NOT raise.
		require_serial_for_firearm(self._transfer_doc(gun_serials=["S1"]))

	def test_serial_gate_still_blocks_gun_without_serial(self):
		from ffl_core.compliance.firearm import require_serial_for_firearm
		with self.assertRaises(frappe.ValidationError):
			require_serial_for_firearm(self._transfer_doc(gun_serials=[""]))

	def _run_4473_gate(self, doc, covered_serials):
		from ffl_core.compliance import firearm as compliance
		orig = frappe.get_all
		def fake(doctype, *a, **k):
			return list(covered_serials) if doctype == "FFL Disposition" else orig(doctype, *a, **k)
		with patch.object(frappe, "get_all", side_effect=fake):
			compliance.require_4473_and_nics_for_firearm_sale(doc)

	def test_4473_gate_passes_when_every_gun_covered(self):
		# Two $0 guns, both with committed dispositions → passes; the fee row
		# needs no disposition.
		doc = self._transfer_doc(gun_serials=["S1", "S2"])
		self._run_4473_gate(doc, covered_serials=["S1", "S2"])   # must not raise

	def test_4473_gate_blocks_uncovered_gun(self):
		doc = self._transfer_doc(gun_serials=["S1", "S2"])
		with self.assertRaises(frappe.ValidationError):
			self._run_4473_gate(doc, covered_serials=["S1"])     # S2 uncovered


class TransferReceiptTitle(FrappeTestCase):
	"""Receipt title flips to 'Transfer Receipt' only when is_ffl_transfer is set."""

	def test_title_conditional(self):
		tmpl = '{{ "Transfer Receipt" if doc.get("is_ffl_transfer") else "Sales Receipt" }}'
		self.assertEqual(
			frappe.render_template(tmpl, {"doc": frappe._dict(is_ffl_transfer=1)}),
			"Transfer Receipt")
		self.assertEqual(
			frappe.render_template(tmpl, {"doc": frappe._dict(is_ffl_transfer=0)}),
			"Sales Receipt")
		# Sales Invoice has no such field → .get() falls back to the sale title.
		self.assertEqual(
			frappe.render_template(tmpl, {"doc": frappe._dict()}),
			"Sales Receipt")


class TransferKickoffGuards(FrappeTestCase):
	"""Server-side guards on the transfer kickoff — defense beyond the UI, since
	the whitelisted method is reachable directly. The 4473 gate is skipped for
	FFL dealers, so a dealer transferee must be refused before any invoice is built."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()
		if not frappe.db.exists("Customer", "XFER Dealer"):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": "XFER Dealer",
				"customer_type": "Company", "is_ffl_dealer": 1,
			}).insert(ignore_permissions=True)

	def test_blocks_dealer_transferee(self):
		from ffl_integrations.fastbound.pos_4473 import start_transfer_for_pos_invoice
		with self.assertRaises(frappe.ValidationError):
			start_transfer_for_pos_invoice(
				serials=["ANY-SERIAL"], fee_item=TRANSFER_FEE_ITEM, fee_amount=25,
				customer="XFER Dealer", pos_profile="")

	def test_requires_at_least_one_serial(self):
		from ffl_integrations.fastbound.pos_4473 import start_transfer_for_pos_invoice
		with self.assertRaises(frappe.ValidationError):
			start_transfer_for_pos_invoice(
				serials=[], fee_item=TRANSFER_FEE_ITEM, fee_amount=25,
				customer="XFER Dealer", pos_profile="")

	def test_blocks_serial_not_in_stock(self):
		"""A serial that's already left inventory (status != Active) can't be
		transferred again, even though its Item is still a firearm — guards
		against re-transferring a gun that's already gone."""
		from ffl_integrations.fastbound.pos_4473 import start_transfer_for_pos_invoice
		_ensure_firearm_item("XFER-GUN-1")
		_ensure_serial("XFER-SN-GONE", "XFER-GUN-1", "Delivered")  # already out
		cust = "XFER Individual"
		if not frappe.db.exists("Customer", cust):
			frappe.get_doc({"doctype": "Customer", "customer_name": cust,
				"customer_type": "Individual"}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			start_transfer_for_pos_invoice(
				serials=["XFER-SN-GONE"], fee_item=TRANSFER_FEE_ITEM, fee_amount=25,
				customer=cust, pos_profile="")
		self.assertIn("in stock", str(ctx.exception).lower())


class FirearmSerialMap(FrappeTestCase):
	"""_firearm_serial_map is the kickoff's serial map for a transfer. Regression:
	on a restored/parked transfer the dialog re-sends ONLY the newly added serial,
	so the map must still cover the firearm rows ALREADY on the invoice — else the
	4473 kickoff's _validate_serials throws 'Missing serial for firearm row' on the
	pre-existing gun (you can't add a 2nd gun to a parked transfer)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_bootstrap()
		_ensure_firearm_item("FSM-GUN")

	def test_maps_existing_and_new_firearm_rows_skips_fee(self):
		from ffl_integrations.fastbound.pos_4473 import _firearm_serial_map
		rows = [
			frappe._dict(name="r1", item_code="FSM-GUN", serial_no="OLD-1"),       # already on invoice
			frappe._dict(name="r2", item_code="FSM-GUN", serial_no="NEW-2"),       # newly added
			frappe._dict(name="r3", item_code=TRANSFER_FEE_ITEM, serial_no=None),  # fee — not a firearm
		]
		# BOTH guns mapped (the bug mapped only the newly-sent NEW-2); fee excluded.
		self.assertEqual(_firearm_serial_map(rows), {"r1": "OLD-1", "r2": "NEW-2"})

	def test_skips_firearm_without_serial_and_serialed_non_firearm(self):
		from ffl_integrations.fastbound.pos_4473 import _firearm_serial_map
		rows = [
			frappe._dict(name="r1", item_code="FSM-GUN", serial_no=""),            # gun, no serial yet
			frappe._dict(name="r2", item_code=TRANSFER_FEE_ITEM, serial_no="X"),   # non-firearm w/ serial
		]
		self.assertEqual(_firearm_serial_map(rows), {})


if __name__ == "__main__":
	unittest.main()
