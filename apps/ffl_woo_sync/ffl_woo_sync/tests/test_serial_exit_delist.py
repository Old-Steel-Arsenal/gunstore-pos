"""A listed per-serial firearm must leave WooCommerce when it leaves stock.

Root cause under test: ERPNext v16 flips Serial No status on delivery via a
direct QB UPDATE (serial_batch_bundle.py), so Serial No.on_update NEVER fires
— yet the SLE hook deliberately skipped per-serial firearms and deferred to
that dead event. Counter sales (Create Order dispose, POS) therefore never
delisted the gun; only web orders did, via their explicit _delist_disposed.

The fix: the SLE hook enqueues sync_serial_listing_task for every listed
serial an SLE touches; the task reads the serial's COMMITTED state and pushes
(sellable) or retires (gone) accordingly.

Since the SOLD-archive lifecycle, "retires" routes
through sync.route_serial_exit: a gun that SOLD keeps a live page at stock 0
(archive), while one that merely left stock is drafted (delist). This hook is
the counter/POS sale's only exit signal, so it is where that distinction has to
hold — SP000279 was drafted through exactly this path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.channel_query_doubles import get_all_double

ITEM = "WOO-EXIT-GUN"


def _ensure_item():
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({
			"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
			"item_group": "Firearm", "stock_uom": "Nos",
			"is_stock_item": 1, "has_serial_no": 1,
		}).insert(ignore_permissions=True)
	frappe.db.set_value("Item", ITEM,
		{"is_firearm": 1, "uniform_sell_price": 0}, update_modified=False)


def _gv_except_dropship(row):
	"""Answer every `frappe.db.get_value` with `row` EXCEPT the drop-ship probe.

	`distributor_stock_source` (ffl_integrations) is registered as the
	FIRST statement of `stock_for_item`, so an unconditional stub answers that
	probe too: a truthy row makes an ordinary local item look drop-ship-sourced,
	the published quantity collapses to 0, and the local-stock assertions below
	fail. Returning None for that one field is exactly the behaviour before that probe existed, so
	nothing here is weakened — only the new probe is excluded.
	"""

	def _fn(doctype=None, name=None, field=None, *args, **kwargs):
		if field == "dropship_distributor" or (
				isinstance(field, (list, tuple)) and "dropship_distributor" in field):
			return None
		return row

	return _fn


class SerialExitFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value(
			"Company", {"company_name": "FFL Test"}, "name")
		cls.warehouse = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "is_group": 0}, "name")
		_ensure_item()

	def _stock_in(self, serial: str):
		from ffl_core.firearm import serialized_receipt_row
		if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
				"Serial No", serial, "status") == "Active":
			return
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code=ITEM, qty=1, warehouse=self.warehouse,
				basic_rate=500, serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

	def _issue(self, serial: str):
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Issue",
			"company": self.company,
			"items": [{
				"item_code": ITEM, "qty": 1, "s_warehouse": self.warehouse,
				"serial_no": serial, "use_serial_batch_fields": 1,
			}],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return se


class TestExitEnqueuesListingSync(SerialExitFixtures):
	def test_issue_of_listed_serial_enqueues_listing_sync(self):
		serial = "WOO-EXIT-1"
		self._stock_in(serial)
		frappe.db.set_value("Serial No", serial, "woo_product_id", "91001",
			update_modified=False)
		with patch("frappe.enqueue") as enq:
			self._issue(serial)
		calls = [c for c in enq.call_args_list
			if c.args and "sync_serial_listing_task" in str(c.args[0])
			and c.kwargs.get("serial_no") == serial]
		self.assertTrue(calls,
			"an outbound listing sync must be enqueued when a listed "
			"per-serial gun leaves stock")
		self.assertEqual(calls[0].kwargs.get("site", "retail"), "retail")

	def test_issue_of_unlisted_serial_enqueues_nothing_for_it(self):
		serial = "WOO-EXIT-2"
		self._stock_in(serial)
		frappe.db.set_value("Serial No", serial, "woo_product_id", "",
			update_modified=False)
		with patch("frappe.enqueue") as enq:
			self._issue(serial)
		calls = [c for c in enq.call_args_list
			if c.args and "sync_serial_listing_task" in str(c.args[0])]
		self.assertFalse(calls)


class TestListingSyncTask(FrappeTestCase):
	def test_gone_serial_retires_and_active_pushes(self):
		"""The hook's full exit matrix. Since the SOLD-archive lifecycle
		"gone" has TWO outcomes, routed by
		sync.route_serial_exit: a SOLD gun keeps a live page (archive), a gun
		that merely left stock is drafted (delist). Delivered is the counter-sale
		case — this hook is the only exit signal a POS sale produces."""
		from ffl_woo_sync.woocommerce import tasks

		def _gv(state):
			"""The task reads the serial's state as a dict; route_serial_exit then
			reads the site's delist sentinel as a scalar."""
			def _fn(doctype, name, field, *args, **kwargs):
				if isinstance(field, (list, tuple)):
					return frappe._dict(state)
				return {"status": state["status"], "woo_delisted": 0}.get(field)
			return _fn

		with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as archive, \
			patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.db.get_value") as gv:
			# SOLD → archive, never a draft (no 404 window on a ranked page)
			gv.side_effect = _gv(dict(
				status="Delivered", warehouse=None, woo_reserved=0, item_code="X"))
			tasks.sync_serial_listing_task("SN-SOLD", site="retail")
			archive.assert_called_once_with("SN-SOLD", site="retail")
			delist.assert_not_called()
			push.assert_not_called()

			# Left stock WITHOUT selling → delist, unchanged
			archive.reset_mock()
			gv.side_effect = _gv(dict(
				status="Inactive", warehouse=None, woo_reserved=0, item_code="X"))
			tasks.sync_serial_listing_task("SN-GONE", site="retail")
			delist.assert_called_once_with("SN-GONE", site="retail")
			archive.assert_not_called()
			push.assert_not_called()

			# Back on the shelf → push
			delist.reset_mock()
			gv.side_effect = _gv(dict(
				status="Active", warehouse="Stores - X", woo_reserved=0, item_code="X"))
			tasks.sync_serial_listing_task("SN-BACK", site="retail")
			push.assert_called_once_with("SN-BACK", site="retail", _relist=True,
				pos_fields_only=True)
			delist.assert_not_called()
			archive.assert_not_called()

	def test_counter_held_serial_delists_even_while_active(self):
		"""A gun parked on an OPEN counter order (Create Order / POS awaiting
		payment + 4473) is still Active in stock — but must not be buyable
		online. That is the gap."""
		from ffl_woo_sync.woocommerce import tasks
		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds") as held, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Stores - X", woo_reserved=0,
				item_code="GUN-X"))
			held.return_value = {"SN-HELD": {"ref": "ACC-SINV-2026-99001",
				"source": "sales_invoice", "kind": "sales_invoice"}}
			tasks.sync_serial_listing_task("SN-HELD", site="retail")
			delist.assert_called_once_with("SN-HELD", site="retail")
			push.assert_not_called()

	def test_a_parked_POS_sale_keeps_its_page_at_stock_zero(self):
		"""DISP-2026-02000's other side. A Draft POS Invoice hold is neither a
		departure nor a sale — the gun is on the shelf and the order is cancelled
		often — so it rides the pending-web-order lane: stock 0, page intact.

		Delisting it would stamp `{site}_delisted`, which `route_serial_exit` rule
		2 reads as "the business took this page down": the same gun sold at the
		register minutes later would hit EXIT_SKIP and silently forfeit the SOLD
		archive page it had earned."""
		from ffl_woo_sync.woocommerce import tasks
		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as archive, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds") as held, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Stores - X", woo_reserved=0,
				item_code="GUN-X"))
			held.return_value = {"SN-PARKED": {"ref": "ACC-PSINV-2026-99002",
				"source": "pos", "kind": "pos"}}
			tasks.sync_serial_listing_task("SN-PARKED", site="retail")
		push.assert_called_once_with("SN-PARKED", site="retail",
			pos_fields_only=True)
		delist.assert_not_called()
		archive.assert_not_called()

	def test_a_parked_POS_sale_does_not_resurrect_a_pulled_page(self):
		"""No `_relist`: the flag answers "may a machine republish this?", and a
		page an operator deliberately pulled must stay pulled."""
		from ffl_woo_sync.woocommerce import tasks
		with patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds") as held, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Stores - X", woo_reserved=0,
				item_code="GUN-X"))
			held.return_value = {"SN-PARKED2": {"kind": "pos"}}
			tasks.sync_serial_listing_task("SN-PARKED2", site="retail")
		self.assertNotIn("_relist", push.call_args[1])

	def test_a_released_hold_relists_through_the_LEAN_payload(self):
		"""Releasing a counter hold re-pushes the PRICE along with status and stock,
		because price is a POS-owned field in this repo —
		that is deliberate and stays. What must not change is that
		the push is LEAN: drop `pos_fields_only` from this call and the same relist
		starts overwriting the shop's own name / description / images with POS
		content every time a cashier cancels an order.

		Asserted on the payload that actually reaches WooCommerce rather than on
		the kwargs, so the guarantee survives a refactor at either end.
		"""
		from ffl_woo_sync.woocommerce import tasks

		full = {
			"name": "Colt Python", "description": "<p>shop copy</p>",
			"short_description": "shop copy", "images": [{"src": "x.jpg"}],
			"sku": "WOO-EXIT-GUN::SN-FREE",
			"status": "publish", "regular_price": "5400", "sale_price": "",
			"date_on_sale_from": "", "date_on_sale_to": "",
			"manage_stock": True, "stock_quantity": 1,
			"meta_data": [{"key": "_osa_serial", "value": "SN-FREE"},
				{"key": "_yoast_wpseo_title", "value": "hand written"}],
		}
		values = {
			"status": "Active", "warehouse": "Stores - X", "woo_reserved": 0,
			"item_code": "WOO-EXIT-GUN", "is_transfer_gun": 0,
			"woo_product_id": "77", "woo_delisted": 0, "woo_sold_archived": 0,
			"sell_price": 5400,
		}

		def gv(doctype=None, name=None, field=None, *a, **k):
			if field == "dropship_distributor" or (
					isinstance(field, (list, tuple)) and "dropship_distributor" in field):
				return None
			if isinstance(field, (list, tuple)):
				return frappe._dict({f: values.get(f) for f in field})
			return values.get(field)

		client = MagicMock()
		with patch("ffl_woo_sync.woocommerce.sync._client", return_value=client), \
			patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
				return_value=dict(full)), \
			patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
			patch("ffl_woo_sync.woocommerce.sync.other_channel_reservation",
				return_value=None), \
			patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
				return_value=5400.0), \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.db.get_value", side_effect=gv), \
			patch("frappe.db.set_value"):
			tasks.sync_serial_listing_task("SN-FREE", site="retail")
		client.update_product.assert_called_once()
		woo_id, payload = client.update_product.call_args[0]
		self.assertEqual(woo_id, "77")
		# The POS-owned half, price included — this is the kept behaviour.
		self.assertEqual(payload["regular_price"], "5400")
		self.assertEqual(payload["stock_quantity"], 1)
		self.assertEqual(payload["status"], "publish")
		# The shop-owned half, untouched.
		for key in ("name", "description", "short_description", "images", "sku"):
			self.assertNotIn(key, payload)
		self.assertEqual({m["key"] for m in payload["meta_data"]}, {"_osa_serial"})

	def test_a_draft_consignment_hold_still_delists(self):
		"""Staged for a partner FFL is a departure, not a parked counter sale."""
		from ffl_woo_sync.woocommerce import tasks
		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds") as held, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Stores - X", woo_reserved=0,
				item_code="GUN-X"))
			held.return_value = {"SN-CONO": {"kind": "consignment"}}
			tasks.sync_serial_listing_task("SN-CONO", site="retail")
		delist.assert_called_once_with("SN-CONO", site="retail")
		push.assert_not_called()


class TestUniformStockSubtractsHolds(FrappeTestCase):
	def test_stock_for_item_subtracts_counter_holds(self):
		from ffl_woo_sync.woocommerce import mapping
		with patch("ffl_core.reservations.counter_holds") as held, \
			patch("ffl_core.reservations.custody_serials",
				return_value=set()), \
			patch("frappe.get_all") as ga, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				is_firearm=1, item_group="Rifle", uniform_sell_price=1))
			ga.side_effect = get_all_double(["SN-A", "SN-B", "SN-C"])
			held.return_value = {"SN-B": {"ref": "ACC-SINV-2026-99001"}}
			self.assertEqual(mapping.stock_for_item("UNI-GUN"), 2)


class TestInvoiceHoldSyncsListing(SerialExitFixtures):
	FFL = "9-88-777-66-5E-44444"

	def _make_si(self, serial: str):
		customer = "Woo Exit Buyer"
		if not frappe.db.exists("Customer", customer):
			frappe.get_doc({"doctype": "Customer", "customer_name": customer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		# A destination FFL exempts the SI from the 4473 submit gate — the same
		# shape Create Order's pending transfer orders have.
		if not frappe.db.exists("ATF FFL Record", self.FFL):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": self.FFL,
				"licensee_name": "Woo Exit FFL",
				"expiry_date": frappe.utils.add_days(frappe.utils.nowdate(), 365),
			}).insert(ignore_permissions=True)
		si = frappe.get_doc({
			"doctype": "Sales Invoice", "customer": customer,
			"company": self.company, "update_stock": 0,
			"ffl_destination_ffl": self.FFL,
			"items": [{"item_code": ITEM, "qty": 1, "rate": 900,
				"warehouse": self.warehouse, "serial_no": serial,
				"use_serial_batch_fields": 1}],
		})
		si.flags.ignore_permissions = True
		si.set_missing_values()
		si.insert()
		return si

	def test_si_submit_and_cancel_enqueue_listing_sync(self):
		serial = "WOO-EXIT-3"
		self._stock_in(serial)
		frappe.db.set_value("Serial No", serial, "woo_product_id", "91002",
			update_modified=False)
		si = self._make_si(serial)
		with patch("frappe.enqueue") as enq:
			si.submit()
		calls = [c for c in enq.call_args_list
			if c.args and "sync_serial_listing_task" in str(c.args[0])
			and c.kwargs.get("serial_no") == serial]
		self.assertTrue(calls,
			"parking a listed gun on a counter order must reconcile its listing")

		si.reload()
		with patch("frappe.enqueue") as enq2:
			si.cancel()
		calls2 = [c for c in enq2.call_args_list
			if c.args and "sync_serial_listing_task" in str(c.args[0])
			and c.kwargs.get("serial_no") == serial]
		self.assertTrue(calls2,
			"cancelling the order must reconcile the listing back")
