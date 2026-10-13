"""patches/retire_special_orders: what the retired Special Order flow left on a site goes, the
Customer Deposits account stays, and a deposit item something still points at is disabled, not
deleted. Safe to run twice and on a site that never had any of it."""
from __future__ import annotations

import os

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches import retire_special_orders as patch

# The four runtime link fields the old setup created, as it created them.
_OLD_FIELDS = {
	"Sales Order": [
		{"fieldname": "ffl_special_order", "fieldtype": "Check", "label": "Special Order",
			"read_only": 1, "insert_after": "order_type"},
		{"fieldname": "ffl_deposit_invoice", "fieldtype": "Link", "options": "POS Invoice",
			"label": "Deposit Invoice", "read_only": 1, "insert_after": "ffl_special_order"},
	],
	**{dt: [{"fieldname": "ffl_special_order", "fieldtype": "Link", "options": "Sales Order",
		"label": "Special Order (Deposit)", "read_only": 1, "insert_after": "item_name"}]
		for dt in ("POS Invoice Item", "Sales Invoice Item")},
}


class RetireSpecialOrders(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import customer_deposits, dev_site
		dev_site.run()
		customer_deposits.ensure()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

	def setUp(self):
		frappe.set_user("Administrator")
		self._rows = []
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for doctype, name in self._rows:
			frappe.db.delete(doctype, {"name": name})
		for price in frappe.get_all("Item Price", filters={"item_code": patch.ITEM}, pluck="name"):
			frappe.delete_doc("Item Price", price, force=True, ignore_permissions=True)
		if frappe.db.exists("Item", patch.ITEM):
			frappe.delete_doc("Item", patch.ITEM, force=True, ignore_permissions=True)

	def _old_records(self):
		"""What a site that ran the old flow holds: report, card on the Gun Shop page, fields, item +
		tombstone price."""
		from ffl_core.workspace_shortcuts import ensure_number_card

		if not frappe.db.exists("Report", patch.REPORT):
			frappe.get_doc({"doctype": "Report", "report_name": patch.REPORT, "ref_doctype": "Sales Order",
				"report_type": "Report Builder", "is_standard": "No", "module": "FFL Core"}).insert()
		if not frappe.db.exists("Number Card", patch.NUMBER_CARD):
			frappe.get_doc({"doctype": "Number Card", "name": patch.NUMBER_CARD, "label": patch.NUMBER_CARD,
				"type": "Custom", "method": "ffl_core.api.gun_shop_cards.get_count",
				"filters_json": '{"card": "special_orders"}', "is_standard": 0, "is_public": 1,
				"module": "FFL Core"}).insert()
		ensure_number_card(patch.NUMBER_CARD, anchor="Transfer Pickups", block_id="nc_special_orders",
			workspace="Gun Shop")
		# Rows only (db_insert): creating them for real would ALTER TABLE, an implicit commit inside
		# the test transaction (docs/testing.md). The patch only deletes the rows.
		for dt, fields in _OLD_FIELDS.items():
			for f in fields:
				if not frappe.db.exists("Custom Field", f"{dt}-{f['fieldname']}"):
					cf = frappe.get_doc({"doctype": "Custom Field", "dt": dt, **f})
					cf.name = f"{dt}-{f['fieldname']}"
					cf.db_insert()
		self._item()

	def _item(self):
		if not frappe.db.exists("Item", patch.ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": patch.ITEM, "item_name": "Special Order Deposit",
				"item_group": "Service", "stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1,
				"is_purchase_item": 0}).insert(ignore_permissions=True)
		price_list = frappe.get_all("Price List", filters={"enabled": 1, "selling": 1}, pluck="name", limit=1)[0]
		frappe.get_doc({"doctype": "Item Price", "item_code": patch.ITEM, "price_list": price_list,
			"price_list_rate": 0, "valid_from": "1900-01-01", "valid_upto": "1900-01-01"}).insert(
				ignore_permissions=True)

	def _on_gun_shop(self) -> bool:
		ws = frappe.get_doc("Workspace", "Gun Shop")
		return (any(c.number_card_name == patch.NUMBER_CARD for c in ws.number_cards)
			or patch.NUMBER_CARD in (ws.content or ""))

	def test_removes_what_the_flow_left_and_a_second_run_is_a_no_op(self):
		self._old_records()
		self.assertTrue(self._on_gun_shop())
		account = frappe.db.get_value("Account",
			{"company": self.company, "account_name": "Customer Deposits", "is_group": 0}, "name")
		self.assertTrue(account)

		for _run in range(2):           # the second run finds nothing and changes nothing
			patch.execute()
			self.assertFalse(frappe.db.exists("Report", patch.REPORT))
			self.assertFalse(frappe.db.exists("Number Card", patch.NUMBER_CARD))
			self.assertFalse(self._on_gun_shop())
			for name in patch.CUSTOM_FIELDS:
				self.assertFalse(frappe.db.exists("Custom Field", name), name)
			self.assertFalse(frappe.db.exists("Item", patch.ITEM))
			self.assertFalse(frappe.db.exists("Item Price", {"item_code": patch.ITEM}))
			# never touched: the account (the GunBroker cash-basis report reads it) and the group
			self.assertTrue(frappe.db.exists("Account", account))
			self.assertTrue(frappe.db.exists("Item Group", "Service"))
		# the other cards are still on the page
		ws = frappe.get_doc("Workspace", "Gun Shop")
		self.assertIn("Transfer Pickups", [c.number_card_name for c in ws.number_cards])

	def test_an_item_a_row_still_points_at_is_disabled_not_deleted(self):
		self._item()
		row = frappe.get_doc({"doctype": "POS Invoice Item", "parenttype": "POS Invoice", "parentfield": "items",
			"parent": "SO-RETIRE-TEST", "item_code": patch.ITEM, "item_name": "Special Order Deposit",
			"qty": 1, "rate": 50, "uom": "Nos", "conversion_factor": 1})
		row.name = frappe.generate_hash(length=10)
		row.db_insert()
		self._rows.append(("POS Invoice Item", row.name))

		for _run in range(2):
			patch.execute()
			self.assertTrue(frappe.db.exists("Item", patch.ITEM))
			self.assertEqual(frappe.db.get_value("Item", patch.ITEM, "disabled"), 1)
			self.assertFalse(frappe.db.exists("Item Price", {"item_code": patch.ITEM}))
		self.assertEqual(frappe.db.get_value("POS Invoice Item", row.name, "item_code"), patch.ITEM)

	def test_an_unreferenced_item_is_not_mistaken_for_a_referenced_one(self):
		"""Every Link-to-Item field on the site is checked; none may read as a reference by error."""
		self._item()
		frappe.db.delete("Item Price", {"item_code": patch.ITEM})
		self.assertFalse(patch._referenced(patch.ITEM))

	def test_registered_after_model_sync(self):
		path = os.path.join(frappe.get_app_path("ffl_core"), "patches.txt")
		with open(path) as f:
			text = f.read()
		self.assertIn("ffl_core.patches.retire_special_orders", text.split("[post_model_sync]", 1)[1])
