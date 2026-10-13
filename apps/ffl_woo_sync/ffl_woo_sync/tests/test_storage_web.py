"""Storage locations on web orders: the loose-stock lines of a Woo order can say which
in-store location staff took them from (at Dispose); the location rides onto the
revenue invoice and its Delivery Note, and the units leave it when that delivery
posts. A line with no pick goes to the "to confirm" list. Nothing here creates
anything in the stock ledger beyond what the order already did."""
from __future__ import annotations

import frappe
from frappe.utils import flt

from ffl_core import storage
from ffl_core.tests.test_storage import StorageFixtures
from ffl_woo_sync.woocommerce import fulfillment, revenue


class WebStorageFixtures(StorageFixtures):
	def web_order(self, lines, *, status="Transferred"):
		"""A real Woo Online Order of accessory lines: [(item_code, qty, unit_price)]."""
		total = sum(q * p for _i, q, p in lines)
		doc = frappe.get_doc({
			"doctype": "Woo Online Order", "woo_order_id": f"WSTG-{frappe.generate_hash(length=8)}",
			"order_status": status, "site": "retail", "customer_name": "Storage Web Buyer",
			"order_total": total, "woo_status": "processing",
			"lines": [{"item_code": code, "woo_line_id": str(i + 1), "qty": q, "unit_price": p,
				"subtotal": q * p, "line_total": q * p, "line_tax": 0, "lane": "local"}
				for i, (code, q, p) in enumerate(lines)]})
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc


class DisposeLocations(WebStorageFixtures):
	def test_the_dialog_lines_list_each_loose_stock_line_with_where_it_is(self):
		r1, r2 = self.open_zone(2)
		ammo, optic = self.item(), self.item(group="Optic")
		self.stock(ammo, 10)
		self.stock(optic, 4)
		storage.put_qty(ammo, r1, 4)
		storage.put_qty(ammo, r2, 6)
		storage.put_qty(optic, r1, 4)
		order = self.web_order([(ammo, 2, 5), (optic, 1, 90)], status="Pending Fulfillment")
		lines = {l["item_code"]: l for l in fulfillment.get_dispose_stock_lines(order.name)}
		self.assertEqual({o["location"]: o["qty"] for o in lines[ammo]["locations"]}, {r1: 4, r2: 6})
		self.assertIsNone(lines[ammo]["chosen"])          # two places: staff pick
		self.assertEqual(lines[optic]["chosen"], r1)      # one place: picked for them
		self.assertEqual(lines[ammo]["qty"], 2)

	def test_nothing_is_preselected_while_some_units_are_in_no_location(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 6)
		storage.put_qty(ammo, r1, 3)
		order = self.web_order([(ammo, 1, 5)], status="Pending Fulfillment")
		self.assertIsNone(fulfillment.get_dispose_stock_lines(order.name)[0]["chosen"])
		storage.put_qty(ammo, r1, 3)
		self.assertEqual(fulfillment.get_dispose_stock_lines(order.name)[0]["chosen"], r1)

	def test_a_firearm_line_is_not_a_loose_stock_line(self):
		from ffl_core.firearm import serialized_receipt_row  # noqa: F401  (just proves the import path)
		gun_item = self.item(group="Pistol", serial=True)
		ammo = self.item()
		order = self.web_order([(gun_item, 1, 500), (ammo, 1, 5)], status="Pending Fulfillment")
		self.assertEqual([l.item_code for l in revenue.local_stock_lines(order)], [ammo])

	def test_a_pick_from_another_company_is_refused_and_a_disabled_one_is_dropped(self):
		r1, = self.open_zone(1)
		abbr = "WS" + self.tag[:3]
		other = frappe.get_doc({"doctype": "Company", "company_name": f"_Test STG WS {self.tag}", "abbr": abbr,
			"default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True).name
		theirs = self.open_zone(1, name="Oth" + self.tag, company=other)[0]
		order = self.web_order([(self.item(), 1, 5)], status="Pending Fulfillment")
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._stamp_line_locations(order, {"1": theirs})
		self.assertIn("different company", str(ctx.exception))
		storage.set_disabled(None, r1, 1)
		fulfillment._stamp_line_locations(order, {"1": r1})
		order.reload()
		self.assertFalse(order.lines[0].storage_location)

	def test_a_pick_is_stamped_on_the_line_and_a_bad_one_is_refused(self):
		a, = self.slots(1)
		r1, = self.open_zone(1)
		ammo = self.item()
		order = self.web_order([(ammo, 2, 5)], status="Pending Fulfillment")
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._stamp_line_locations(order, {"1": a})
		self.assertIn("one firearm only", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._stamp_line_locations(order, {"99": r1})
		self.assertIn("not a stock line", str(ctx.exception))
		fulfillment._stamp_line_locations(order, {"1": r1})
		order.reload()
		self.assertEqual(order.lines[0].storage_location, r1)
		self.assertEqual(revenue.build_invoice_items(order)[0]["ffl_storage_location"], r1)


class RevenueDeliveryTakesFromLocation(WebStorageFixtures):
	def book(self, order):
		return revenue.create_sales_invoice_for_web_order(order)

	def test_the_delivery_takes_the_units_out_of_the_picked_location(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, r1, 4)
		storage.put_qty(ammo, r2, 6)
		order = self.web_order([(ammo, 3, 5)])
		fulfillment._stamp_line_locations(order, {"1": r2})
		order.reload()
		res = self.book(order)
		self.assertEqual(res["status"], "created")
		dn = res["delivery_notes"][0]
		self.assertEqual(frappe.db.get_value("Delivery Note Item", {"parent": dn}, "ffl_storage_location"), r2)
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (4, 3))
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 0)
		self.assertEqual(self.moves(item_code=ammo, voucher_type="Delivery Note")[0].reason, "sale")

	def test_a_line_with_no_pick_goes_to_confirm(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		order = self.web_order([(ammo, 2, 5)])
		res = self.book(order)
		self.assertTrue(res["delivery_notes"])
		self.assertEqual(storage.qty_at(r1, ammo), 5)  # nobody said where it came from
		self.assertEqual(storage.unassigned_qty(self.company, ammo), -2)
		over = {r["item_code"]: r["over"] for r in storage.unassigned_summary(self.company)["to_confirm"]}
		self.assertEqual(over[ammo], 2)
