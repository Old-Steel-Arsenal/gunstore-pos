"""Storage locations on the counter: a sale line says which location the units came
from, and the units leave it when the stock ledger moves them -- on a ship-now
Sales Invoice, on a deferred order's Delivery Note, and on the invoice a POS sale
consolidates into. Cancel and return put them back. Never blocks a sale."""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core import storage
from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import BuildOrderMixin
from ffl_core.tests.test_storage import StorageFixtures


class CounterStorageFixtures(BuildOrderMixin, StorageFixtures):
	"""BuildOrderMixin (not BuildOrderFixtures: that one carries commit-heavy tests)."""

	def setUp(self) -> None:
		super().setUp()
		self.pos_profile = frappe.db.get_value(
			"POS Profile", {"company": self.company, "disabled": 0}, "name")
		self.buyer = self._indiv(f"STG Buyer {self.tag}")

	def sale(self, rows, *, update_stock=1, customer=None, submit=True):
		"""A Sales Invoice straight from rows [(item, qty, location)]."""
		si = frappe.get_doc({"doctype": "Sales Invoice", "customer": customer or self.buyer,
			"company": self.company, "update_stock": update_stock, "set_warehouse": self.warehouse,
			"items": [{"item_code": i, "qty": q, "rate": 5, "warehouse": self.warehouse,
				**({"ffl_storage_location": loc} if loc else {})} for i, q, loc in rows]})
		si.flags.ignore_permissions = True
		si.insert()
		if submit:
			si.submit()
		return si

	def bin_qty(self, item):
		return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": self.warehouse}, "actual_qty"))


class SalesInvoiceTakesFromLocation(CounterStorageFixtures):
	def test_a_ship_now_invoice_takes_units_out_of_the_named_location(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, r1, 4)
		storage.put_qty(ammo, r2, 6)
		si = self.sale([(ammo, 3, r2)])
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (4, 3))
		self.assertEqual(self.bin_qty(ammo), 7)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 0)
		log = self.moves(item_code=ammo, voucher_no=si.name)
		self.assertEqual([(m.reason, m.from_location, m.to_location, m.qty, m.voucher_type)
			for m in log], [("sale", r2, None, 3, "Sales Invoice")])

	def test_a_deferred_invoice_takes_nothing_until_the_stock_moves(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		self.sale([(ammo, 2, r1)], update_stock=0)
		self.assertEqual(storage.qty_at(r1, ammo), 5)

	def test_cancel_puts_the_units_back_exactly_once(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		si = self.sale([(ammo, 2, r1)])
		self.assertEqual(storage.qty_at(r1, ammo), 3)
		si.cancel()
		self.assertEqual(storage.qty_at(r1, ammo), 5)
		self.assertEqual(storage.reverse_voucher("Sales Invoice", si.name), 0)  # nothing left to undo
		self.assertEqual(storage.qty_at(r1, ammo), 5)

	def test_a_return_puts_the_units_back_where_they_came_from(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 6)
		storage.put_qty(ammo, r1, 3)
		storage.put_qty(ammo, r2, 3)
		si = self.sale([(ammo, 2, r2)])
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		ret = make_return_doc("Sales Invoice", si.name)
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		self.assertEqual(ret.items[0].ffl_storage_location, r2)
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (3, 3))
		self.assertEqual(self.moves(item_code=ammo, voucher_no=ret.name)[0].reason, "return")
		ret.cancel()
		self.assertEqual(storage.qty_at(r2, ammo), 1)

	def test_a_stale_pick_never_blocks_the_sale(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 1)
		si = self.sale([(ammo, 4, r1)])  # the location holds 1, the line says 4
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(storage.qty_at(r1, ammo), 0)
		self.assertEqual(self.bin_qty(ammo), 1)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 1)  # stock 1, locations hold 0

	def test_a_line_without_a_location_moves_nothing_in_the_locations(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 2)
		self.sale([(ammo, 2, None)])
		self.assertEqual(storage.qty_at(r1, ammo), 2)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 1)  # 3 in stock, 2 located

	def test_a_location_that_fails_does_not_roll_back_the_sale(self):
		ammo = self.item()
		self.stock(ammo, 5)
		from unittest.mock import patch
		r1, = self.open_zone(1)
		storage.put_qty(ammo, r1, 2)
		with patch.object(storage, "move", side_effect=RuntimeError("boom")), \
				patch("frappe.log_error") as logged:
			si = self.sale([(ammo, 2, r1)])
		self.assertEqual(si.docstatus, 1)
		self.assertTrue(logged.called)

	def test_a_sale_creates_no_location_side_effects_on_stock(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		sle_before = frappe.db.count("Stock Ledger Entry", {"item_code": ammo})
		self.sale([(ammo, 2, r1)])
		# exactly one stock ledger entry, from the invoice itself
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": ammo}), sle_before + 1)


class CounterOrderPage(CounterStorageFixtures):
	"""create_order: the payload line carries the location through the Sales Order to
	the invoice and, for a deferred order, to its Delivery Note."""

	def order(self, ammo, qty, location, *, ship_now):
		return manual_order.create_order({
			"buyer_type": "individual", "customer": self.buyer, "pos_profile": self.pos_profile,
			"dispose_now": 1 if ship_now else 0, "payment_received": 1, "mode_of_payment": "Cash",
			"items": [{"item_code": ammo, "qty": qty, "rate": 5, "storage_location": location}],
		})

	def test_a_ship_now_order_takes_from_the_chosen_location(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 8)
		storage.put_qty(ammo, r1, 3)
		storage.put_qty(ammo, r2, 5)
		out = self.order(ammo, 2, r2, ship_now=True)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.items[0].ffl_storage_location, r2)
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (3, 3))

	def test_a_deferred_order_takes_when_it_ships(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		out = self.order(ammo, 2, r1, ship_now=False)
		self.assertEqual(storage.qty_at(r1, ammo), 5)
		res = manual_order.mark_shipped_manually(out["sales_invoice"])
		self.assertTrue(res["delivery_notes"])
		dn = res["delivery_notes"][0]
		self.assertEqual(frappe.db.get_value("Delivery Note Item", {"parent": dn}, "ffl_storage_location"), r1)
		self.assertEqual(storage.qty_at(r1, ammo), 3)
		frappe.get_doc("Delivery Note", dn).cancel()
		self.assertEqual(storage.qty_at(r1, ammo), 5)

	def test_the_edit_form_gets_the_location_back(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		out = self.order(ammo, 2, r1, ship_now=False)
		edit = manual_order.get_order_for_edit(out["sales_invoice"])
		self.assertEqual(edit["payload"]["items"][0]["storage_location"], r1)

	def test_a_slot_is_refused_for_loose_stock_but_a_stale_disabled_pick_never_blocks_the_sale(self):
		a, = self.slots(1)
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.order(ammo, 1, a, ship_now=True)
		self.assertIn("one firearm only", str(ctx.exception))
		storage.set_disabled(None, r1, 1)  # disabled after the clerk's picker was drawn
		out = self.order(ammo, 1, r1, ship_now=True)
		si = frappe.get_doc("Sales Invoice", out["sales_invoice"])
		self.assertEqual(si.docstatus, 1)
		self.assertFalse(si.items[0].ffl_storage_location)  # the line comes out of the unassigned units
		self.assertEqual(self.bin_qty(ammo), 4)


class PosSaleKeepsItsLocation(CounterStorageFixtures):
	"""A POS sale reaches the stock ledger as the consolidated Sales Invoice, which
	ERPNext builds by copying each POS Invoice Item's same-named fields -- so the
	location the cashier picked must be a field on both lines."""

	def test_the_pos_line_location_survives_the_consolidation_mapping(self):
		from frappe.model.mapper import map_child_doc
		r1, = self.open_zone(1)
		ammo = self.item()
		line = frappe.new_doc("POS Invoice Item")
		line.update({"item_code": ammo, "qty": 2, "rate": 5, "warehouse": self.warehouse,
			"ffl_storage_location": r1})
		invoice = frappe.new_doc("Sales Invoice")
		merged = map_child_doc(line, invoice, {"doctype": "Sales Invoice Item"})
		self.assertEqual(merged.ffl_storage_location, r1)

	def test_the_location_is_a_real_field_on_every_line_that_carries_it(self):
		for dt in ("POS Invoice Item", "Sales Invoice Item", "Sales Order Item", "Delivery Note Item"):
			self.assertTrue(frappe.get_meta(dt).has_field("ffl_storage_location"), dt)
		self.assertTrue(frappe.get_meta("Serial No").has_field("storage_location"))
