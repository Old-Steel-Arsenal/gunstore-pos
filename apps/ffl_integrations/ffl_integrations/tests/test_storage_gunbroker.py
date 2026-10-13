"""Storage locations on GunBroker orders: an item-level line (ammunition, optics,
parts) can say which in-store location staff took its units from at Dispose; the
units leave that location when the stock issue posts. A bad pick is refused before
any stock moves, and a line with no pick goes to the "to confirm" list."""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_core import storage
from ffl_core.tests.test_storage import StorageFixtures
from ffl_integrations.gunbroker import fulfillment


class GunBrokerStorage(StorageFixtures):
	def setUp(self):
		super().setUp()
		p = patch.object(fulfillment, "_order_company", return_value=self.company)
		p.start()
		self.addCleanup(p.stop)
		self.order = frappe._dict(name="GB-ORD-STG", gb_order_id="445566")

	def line(self, item, qty, gb_item_id="1001"):
		return frappe._dict(item_code=item, qty=qty, gb_item_id=gb_item_id)

	def se_count(self):
		return frappe.db.count("Stock Entry")

	def test_the_issue_takes_the_units_out_of_the_picked_location(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, r1, 4)
		storage.put_qty(ammo, r2, 6)
		issued = fulfillment._issue_stock_units(self.order, [self.line(ammo, 3)], {"1001": r2})
		self.assertEqual(issued[0]["qty"], 3)
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (4, 3))
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 0)
		log = self.moves(item_code=ammo)[-1]
		self.assertEqual((log.reason, log.from_location, log.voucher_type, log.voucher_no),
			("ship", r2, "Stock Entry", issued[0]["stock_entry"]))

	def test_a_line_with_no_pick_goes_to_confirm(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		fulfillment._issue_stock_units(self.order, [self.line(ammo, 2)])
		self.assertEqual(storage.qty_at(r1, ammo), 5)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), -2)

	def test_a_bad_pick_is_refused_before_any_stock_moves(self):
		a, = self.slots(1)
		ammo = self.item()
		self.stock(ammo, 5)
		before = self.se_count()
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._issue_stock_units(self.order, [self.line(ammo, 2)], {"1001": a})
		self.assertIn("one firearm only", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._issue_stock_units(self.order, [self.line(ammo, 2)], {"777": a})
		self.assertIn("not a stock line", str(ctx.exception))
		self.assertEqual(self.se_count(), before)

	def test_a_stale_pick_takes_what_is_there_and_the_shipment_still_goes(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 1)
		issued = fulfillment._issue_stock_units(self.order, [self.line(ammo, 3)], {"1001": r1})
		self.assertEqual(issued[0]["qty"], 3)
		self.assertEqual(storage.qty_at(r1, ammo), 0)

	def test_the_dialog_lists_each_item_level_line_with_where_it_is(self):
		r1, r2 = self.open_zone(2)
		ammo, optic = self.item(), self.item(group="Optic")
		self.stock(ammo, 6)
		self.stock(optic, 2)
		storage.put_qty(ammo, r1, 3)
		storage.put_qty(ammo, r2, 3)
		storage.put_qty(optic, r2, 2)
		lines = [self.line(ammo, 2, "A1"), self.line(optic, 1, "A2")]
		real_get_doc = frappe.get_doc

		def _get_doc(*args, **kw):
			if args and args[0] == fulfillment.ORDER_DOCTYPE:
				return self.order
			return real_get_doc(*args, **kw)

		with patch("frappe.get_doc", side_effect=_get_doc), \
				patch.object(fulfillment, "_stock_lines", return_value=lines):
			out = {l["item_code"]: l for l in fulfillment.get_dispose_stock_lines("GB-ORD-STG")}
		self.assertEqual({o["location"] for o in out[ammo]["locations"]}, {r1, r2})
		self.assertIsNone(out[ammo]["chosen"])
		self.assertEqual((out[optic]["chosen"], out[optic]["line"], out[ammo]["qty"]), (r2, "A2", 2))

	def test_cancelling_the_stock_issue_puts_the_units_back_in_their_location(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		issued = fulfillment._issue_stock_units(self.order, [self.line(ammo, 2)], {"1001": r1})
		self.assertEqual(storage.qty_at(r1, ammo), 3)
		frappe.get_doc("Stock Entry", issued[0]["stock_entry"]).cancel()
		self.assertEqual(storage.qty_at(r1, ammo), 5)

	def test_the_dialog_does_not_preselect_when_some_units_are_in_no_location(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 6)
		storage.put_qty(ammo, r1, 3)
		real_get_doc = frappe.get_doc

		def _get_doc(*args, **kw):
			if args and args[0] == fulfillment.ORDER_DOCTYPE:
				return self.order
			return real_get_doc(*args, **kw)

		with patch("frappe.get_doc", side_effect=_get_doc), \
				patch.object(fulfillment, "_stock_lines", return_value=[self.line(ammo, 1, "A1")]):
			out = fulfillment.get_dispose_stock_lines("GB-ORD-STG")
		self.assertIsNone(out[0]["chosen"])

	def test_picks_are_checked_against_the_order_and_this_company(self):
		r1, = self.open_zone(1)
		abbr = "GB" + self.tag[:3]
		other = frappe.get_doc({"doctype": "Company", "company_name": f"_Test STG GB {self.tag}", "abbr": abbr,
			"default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True).name
		theirs = self.open_zone(1, name="Oth" + self.tag, company=other)[0]
		ammo = self.item()
		lines = [self.line(ammo, 1, "A1")]
		self.assertEqual(dict(fulfillment._checked_picks(self.order, lines, {"A1": r1})), {"A1": r1})
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._checked_picks(self.order, lines, {"A1": theirs})
		self.assertIn("different company", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError) as ctx:
			fulfillment._checked_picks(self.order, lines, {"nope": r1})
		self.assertIn("not a stock line", str(ctx.exception))
		storage.set_disabled(None, r1, 1)   # disabled since the dialog: dropped, not an error
		self.assertEqual(dict(fulfillment._checked_picks(self.order, lines, {"A1": r1})), {})


from ffl_integrations.tests.test_gunbroker_fulfillment import _FulfillmentCase  # noqa: E402 (fixture base, no tests)
from ffl_integrations.tests import gunbroker_fixtures as fx  # noqa: E402


class GunBrokerDisposeChecksPicksFirst(_FulfillmentCase):
	def test_a_bad_pick_is_refused_before_a_disposition_or_antique_issue_is_booked(self):
		order = self._order(fx.order_detail(sku="AMMO-X", serial="", ffl_received=False))
		order.set("serials", [])
		order.lines[0].item_code = "AMMO-X"
		order.lines[0].is_ffl_required = 0
		self._patch_order_doc(order)
		with patch.object(fulfillment.gb_orders, "is_item_level_line", return_value=True), \
				patch.object(fulfillment, "_order_company", return_value="TSA"), \
				patch.object(fulfillment, "_book_dispositions") as book, \
				patch.object(fulfillment, "_retire_antique_units") as retire, \
				patch.object(fulfillment, "_issue_stock_units") as issue, \
				patch.object(fulfillment, "_resolve_and_validate_ffl", return_value="FFL-1"), \
				patch("ffl_core.storage.validate_open_location", side_effect=frappe.ValidationError("bad pick")):
			with self.assertRaises(frappe.ValidationError):
				fulfillment.dispose_gb_order("GB-ORD-00001", locations={str(order.lines[0].gb_item_id or ""): "L-1"})
		book.assert_not_called()
		retire.assert_not_called()
		issue.assert_not_called()
