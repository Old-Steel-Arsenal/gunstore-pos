"""Units a web order is waiting on are not the counter's to sell (compliance.web_hold)."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance import web_hold

ITEM = "WEB-HOLD-ACC"


def _invoice(qty, **kw):
	doc = frappe._dict(doctype="POS Invoice", is_return=0, is_consolidated=0,
		woo_online_order="", update_stock=1,
		items=[frappe._dict(item_code=ITEM, qty=qty, stock_qty=qty)], **kw)
	doc.add_comment = mock.Mock()
	return doc


class WebHold(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap

		test_bootstrap.run()
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Misc Accessory",
				"stock_uom": "Nos", "is_stock_item": 1}).insert(ignore_permissions=True)

	def setUp(self):
		for patcher in (mock.patch.object(web_hold, "_free_on_shelf", return_value=1),
				mock.patch.object(web_hold, "_held_by", return_value=["6120"]),
				mock.patch.object(frappe, "get_roles", return_value=["Sales User"])):
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_the_free_shelf_sells(self):
		web_hold.refuse_held_units(_invoice(1))

	def test_a_held_unit_is_refused_with_the_order_named(self):
		with self.assertRaisesRegex(frappe.ValidationError, "#6120"):
			web_hold.refuse_held_units(_invoice(2))

	def test_a_manager_rings_it_and_the_invoice_says_so(self):
		doc = _invoice(2)
		with mock.patch.object(frappe, "get_roles", return_value=["Sales Manager"]):
			web_hold.refuse_held_units(doc)
		self.assertIn("#6120", doc.add_comment.call_args[0][1])

	def test_not_a_counter_sale(self):
		for kw in ({"is_return": 1}, {"is_consolidated": 1}, {"woo_online_order": "WOO-1"}):
			web_hold.refuse_held_units(frappe._dict(_invoice(5), **kw))
		web_hold.refuse_held_units(frappe._dict(_invoice(5), doctype="Sales Invoice", update_stock=0))

	def test_no_webshop_holds_nothing(self):
		with mock.patch.object(web_hold, "_free_on_shelf", return_value=None):
			web_hold.refuse_held_units(_invoice(9))
