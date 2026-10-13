"""Real documents for the model-level (claim and assignment) GunBroker tests.

The claim, assignment and dispose-gate logic is about reservations: which
`Serial No` a `GunBroker Order Serial` row holds, who else holds it, and what the
shared pool then offers. A mock database cannot say any of that, so these tests
run against real Items, real stock-ledger receipts (which is what gives a
`Serial No` its status, warehouse and purchase rate) and a real `GunBroker Order`
saved through the same `_ingest_order` the 15-minute poll uses.

Nothing here commits: the class rollback at the end of each test class removes it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import orders
from ffl_integrations.tests import gunbroker_fixtures as fx

MODEL = "GBM-MODEL-CZ75"      # uniform-price firearm: one product, many guns
SINGLE = "GBM-SINGLE-PRICED"  # firearm priced gun by gun
AMMO = "GBM-AMMO-9MM"

# GunBroker numbers the listing; a model-level SKU is the bare item code.
GB_ITEM = 16087299

# GunBroker's combined tax figure and the buyer's service fee inside it.
TAX, FEE = 90.0, 10.0

_ORDER_IDS = iter(range(990001, 999999))
_SERIALS = iter(range(1, 999999))


class ModelUnitCase(FrappeTestCase):
	"""A bench with a company, three kinds of Item and helpers to stock guns and
	ingest orders. Alerts raised through `orders.notify_admins` are collected in
	``self.alerts`` (dedup keys) and announcements to the other channels in
	``self.heard``."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap

		test_bootstrap.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		from ffl_core.warehouses import default_stock_warehouse

		cls.warehouse = default_stock_warehouse(cls.company)
		for code, firearm, uniform in ((MODEL, 1, 1), (SINGLE, 1, 0), (AMMO, 0, 0)):
			frappe.delete_doc_if_exists("Item", code, force=1)
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "All Item Groups", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": firearm,
				"is_firearm": firearm, "uniform_sell_price": uniform,
				"standard_rate": 499,
			}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		self.alerts: list[str] = []
		self.heard: list[tuple] = []
		self._start(patch.object(orders, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"])))
		self._start(patch.object(orders, "notify_channel_commitments_changed",
			side_effect=lambda item, *, source: self.heard.append((item, source))))
		# The exits are announced to the storefronts, which would enqueue real work.
		self._start(patch.object(orders, "fire_serial_channel_exit"))
		self._start(patch.object(orders, "safe_commit"))
		self._start(patch.object(orders, "_acknowledge"))
		# The class is rolled back once, at the end: every test starts from an empty
		# shelf and no orders, or one test's guns and claims would be another's pool.
		for table in ("GunBroker Order Serial", "GunBroker Order Line", "GunBroker Order"):
			frappe.db.sql(f"DELETE FROM `tab{table}`")
		frappe.db.sql("DELETE FROM `tabSerial No` WHERE item_code IN %s", ((MODEL, SINGLE),))

	def _start(self, patcher):
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	# --------------------------------------------------------------- the shelf

	def stock(self, serial: str, *, item: str = MODEL, rate: float = 200.0, **flags) -> str:
		"""A real, Active, in-warehouse gun, received through a Material Receipt so
		its status, warehouse and purchase rate are the ledger's, not a stub's.

		The name is made unique: a test's guns are deleted at the start of the next,
		but their stock-ledger history is not, and receiving a serial the ledger
		already knows is refused."""
		from ffl_core.firearm import serialized_receipt_row

		serial = f"{serial}-{next(_SERIALS)}"

		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(item_code=item, qty=1,
				warehouse=self.warehouse, basic_rate=rate, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		# A Material Receipt values the gun in the ledger but leaves the serial's own
		# cost blank; a purchase (the real path) stamps it, and the Sales Report
		# prices a firearm line from it.
		frappe.db.set_value("Serial No", serial, {"purchase_rate": rate, **flags},
			update_modified=False)
		return serial

	def age(self, serial: str, seconds: int) -> None:
		"""Make `serial` older (received earlier) than the others, for FIFO."""
		frappe.db.set_value("Serial No", serial, "creation",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-seconds),
			update_modified=False)

	# ------------------------------------------------------------------ orders

	def detail(self, *, sku=MODEL, qty=2, price=499.0, order_id=None, ffl_required=True,
			ffl_number="1-99-888-01-2A-34567", status=4, **kw) -> dict:
		order_id = order_id or str(next(_ORDER_IDS))
		line = {
			"itemID": GB_ITEM, "title": "CZ 75 Grade A", "quantity": qty,
			"itemPrice": price, "itemSubTotal": price * qty, "serialNumber": "",
			"sku": sku, "isFFLRequired": ffl_required, "salesTax": 0,
			"itemCondition": "Used",
		}
		body = fx.order_detail(
			order_id, status=status, sku=sku, serial="", item_price=price,
			ship_cost=40.0, sales_tax_total=TAX, compliance_fee=FEE,
			total=price * qty + 40.0 + TAX, ffl_number=ffl_number, **kw)
		body["orderItemsCollection"] = [line]
		return body

	def ingest(self, detail: dict) -> "frappe.Document":
		"""The poll's own entry point for one order, with only the network faked."""
		client = MagicMock()
		client.get_order.return_value = detail
		name = orders._ingest_order(client, {"orderID": detail["orderID"]})
		return frappe.get_doc("GunBroker Order", name)

	def units(self, order) -> list:
		order.reload()
		return [r for r in order.serials if r.is_model_unit]

	def held_by(self, serial: str) -> tuple:
		row = frappe.db.get_value("Serial No", serial, ["gb_reserved", "gb_order"],
			as_dict=True)
		return bool(row.gb_reserved), row.gb_order or ""
