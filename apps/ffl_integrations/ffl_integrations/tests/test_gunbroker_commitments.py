"""GunBroker's half of the shared pool: what it has committed, what it is told,
and what it offers.

An item-level (ammunition, optics, magazines) GunBroker listing carries a COUNT.
GunBroker decrements it itself the moment somebody buys; our stock ledger does
not move until the order is shipped. Until now `sellable_qty` read the ledger
alone, so the next quantity push after a sale put the sold boxes back on the
listing — the oversell this module closes. It now subtracts what every channel
has sold and not yet shipped, and tells the other channels when its own number
moves.

Three failure modes get their own tests because each looks like success:

* a quantity push computed WITHOUT a channel that could not answer (it would
  publish a larger number than is true — on an auction site, a refund);
* a notification that fires on every poll of an unchanged order (a push per
  poll, forever);
* a firearm reaching the item-level path (a GunBroker listing for a gun is
  Run 3's, and nothing here may create one).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import crosschannel, doc_events, item_listings, orders
from ffl_integrations.tests import gunbroker_fixtures as fx

AMMO = "GBC-AMMO-001"
GUN = "GBC-GUN-001"
OTHER_AMMO = "GBC-AMMO-002"


def _item(code, *, firearm):
	if frappe.db.exists("Item", code):
		return
	frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": "All Item Groups", "stock_uom": "Nos",
		"is_stock_item": 1, "is_firearm": firearm,
		"has_serial_no": firearm, "uniform_sell_price": firearm,
	}).insert(ignore_permissions=True)


def _order(status, *lines):
	return frappe.get_doc({
		"doctype": "GunBroker Order", "gb_order_id": frappe.generate_hash(length=10),
		"order_status": status,
		"lines": [{"sku": sku, "qty": qty, "item_code": sku.split("::")[0],
			"gb_item_id": "1"} for sku, qty in lines],
	}).insert(ignore_permissions=True)


class TheGunBrokerRegistrantOfCommittedUnits(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_item(AMMO, firearm=0)
		_item(OTHER_AMMO, firearm=0)
		_item(GUN, firearm=1)

	def test_it_labels_itself_as_gunbroker(self):
		self.assertEqual(orders.channel_committed_units.channel, "gunbroker")

	def test_open_item_level_lines_are_summed(self):
		before = orders.channel_committed_units(AMMO)
		_order("Pending Fulfillment", (AMMO, 3))
		_order("Pending Fulfillment", (AMMO, 2), (OTHER_AMMO, 7))
		self.assertEqual(orders.channel_committed_units(AMMO), before + 5)

	def test_a_shipped_or_cancelled_order_no_longer_counts(self):
		before = orders.channel_committed_units(AMMO)
		_order("Transferred", (AMMO, 4))
		_order("Cancelled", (AMMO, 6))
		self.assertEqual(orders.channel_committed_units(AMMO), before)

	def test_a_per_gun_sku_is_not_an_item_level_commitment(self):
		before = orders.channel_committed_units(AMMO)
		_order("Pending Fulfillment", (f"{AMMO}::SN123", 1))
		self.assertEqual(orders.channel_committed_units(AMMO), before)

	def test_a_gun_line_owes_nothing_until_it_has_claim_rows(self):
		"""A firearm is owed through its CLAIM rows (see test_gunbroker_model_claims),
		never through its line quantity: a bare-SKU gun line with no claim rows —
		an order that has not been through the poll's reservation step — commits
		nothing."""
		_order("Pending Fulfillment", (GUN, 2))
		self.assertEqual(orders.channel_committed_units(GUN), 0)

	def test_an_unknown_item_owes_nothing(self):
		self.assertEqual(orders.channel_committed_units("GBC-NO-SUCH-ITEM"), 0)


class TheOfferedQuantity(FrappeTestCase):
	"""``sellable_qty`` for an item-level listing."""

	def _qty(self, bin_qty, committed, *, warehouse="Stores - T"):
		real = frappe.db.get_value

		def _get_value(doctype, filters=None, fieldname=None, *a, **kw):
			if doctype == "Bin":
				return bin_qty
			return real(doctype, filters, fieldname, *a, **kw)

		with patch("ffl_core.warehouses.default_stock_warehouse", return_value=warehouse), \
				patch.object(item_listings, "_company", return_value="Test Co"), \
				patch("frappe.db.get_value", side_effect=_get_value), \
				patch.object(item_listings, "committed_units", return_value=committed):
			return item_listings.sellable_qty(AMMO)

	def test_the_bin_less_every_channels_open_orders(self):
		self.assertEqual(self._qty(10, {"woo": 3, "gunbroker": 2}), 5)

	def test_nothing_committed_is_the_bin_as_before(self):
		self.assertEqual(self._qty(10, {}), 10)

	def test_fractional_stock_is_floored_before_the_subtraction(self):
		self.assertEqual(self._qty(4.6, {"gunbroker": 1}), 3)

	def test_an_oversold_shelf_offers_zero(self):
		self.assertEqual(self._qty(3, {"woo": 2, "gunbroker": 4}), 0)

	def test_no_warehouse_offers_zero(self):
		self.assertEqual(self._qty(10, {"woo": 3}, warehouse=None), 0)

	def test_the_count_asks_every_channel_including_its_own(self):
		"""GunBroker's own open orders are exactly what the ledger has not seen."""
		with patch.object(item_listings, "committed_units", return_value={}) as asked, \
				patch("ffl_core.warehouses.default_stock_warehouse", return_value="W"), \
				patch.object(item_listings, "_company", return_value="C"), \
				patch("frappe.db.get_value", return_value=5):
			item_listings.sellable_qty(AMMO)
		asked.assert_called_once_with(AMMO)


class ASilentChannelNeverRaisesTheOffer(FrappeTestCase):
	"""A channel that cannot answer must stop the push, not read as 'owes nothing'."""

	def test_the_count_raises(self):
		with patch("ffl_core.warehouses.default_stock_warehouse", return_value="W"), \
				patch.object(item_listings, "_company", return_value="C"), \
				patch("frappe.db.get_value", return_value=9), \
				patch.object(item_listings, "committed_units",
					side_effect=RuntimeError("web tables not migrated")), \
				self.assertRaises(RuntimeError):
			item_listings.sellable_qty(AMMO)

	def test_a_quantity_sync_sends_nothing_and_ends_nothing(self):
		"""Raising before the 'sold out' branch is what keeps this from ending a
		healthy listing over a count it could not read."""
		from ffl_integrations.gunbroker import tasks

		with patch.object(item_listings, "item_state", return_value={
					"gb_item_id": "123", "gb_qty_pushed": 4}), \
				patch.object(item_listings, "sellable_qty",
					side_effect=RuntimeError("count unreadable")), \
				patch.object(item_listings, "end_item_listing") as ended, \
				patch.object(tasks, "safe_log_error") as logged, \
				patch("ffl_integrations.gunbroker.item_listings.GunBrokerClient") as gb:
			tasks.gb_sync_item_quantity_task(AMMO)
		self.assertFalse(ended.called)
		self.assertFalse(gb.from_settings.called)
		self.assertTrue(logged.called)


class TheGunBrokerHandlerOfCommitmentsChanged(FrappeTestCase):
	def _heard(self, item_row, *, skipped=False, source="woo"):
		with patch.object(doc_events, "_skip", return_value=skipped), \
				patch("frappe.db.get_value", return_value=item_row), \
				patch("frappe.enqueue") as enq:
			crosschannel.channel_commitments_changed(AMMO, source=source)
		return [(c.args[0].rsplit(".", 1)[-1], c.kwargs.get("job_id"))
			for c in enq.call_args_list]

	def test_a_listed_item_has_its_quantity_re_sent(self):
		heard = self._heard({"is_firearm": 0, "gb_item_id": "99",
			"gb_item_history": "", "gb_delisted": 0})
		self.assertEqual(heard, [("gb_sync_item_quantity_task", f"gb-qty:{AMMO}")])

	def test_it_does_so_for_its_own_orders_too(self):
		"""A cancelled GunBroker order returns units to the pool, and GunBroker
		does not put them back on the listing by itself."""
		heard = self._heard({"is_firearm": 0, "gb_item_id": "99",
			"gb_item_history": "", "gb_delisted": 0}, source="gunbroker")
		self.assertEqual(len(heard), 1)

	def test_a_sold_out_item_is_relisted_when_units_come_back(self):
		heard = self._heard({"is_firearm": 0, "gb_item_id": "",
			"gb_item_history": "55", "gb_delisted": 0})
		self.assertEqual(heard, [("gb_relist_item_task", f"gb-relist:{AMMO}")])

	def test_an_item_taken_down_by_hand_stays_down(self):
		self.assertEqual(self._heard({"is_firearm": 0, "gb_item_id": "",
			"gb_item_history": "55", "gb_delisted": 1}), [])

	def test_an_item_never_on_gunbroker_costs_nothing(self):
		self.assertEqual(self._heard({"is_firearm": 0, "gb_item_id": "",
			"gb_item_history": "", "gb_delisted": 0}), [])

	def test_a_firearm_is_not_touched(self):
		"""A model-level gun listing is a later run's; nothing here may queue one."""
		self.assertEqual(self._heard({"is_firearm": 1, "gb_item_id": "99",
			"gb_item_history": "", "gb_delisted": 0}), [])

	def test_a_disabled_integration_does_nothing(self):
		self.assertEqual(self._heard({"is_firearm": 0, "gb_item_id": "99",
			"gb_item_history": "", "gb_delisted": 0}, skipped=True), [])

	def test_a_stock_movement_still_takes_the_same_path(self):
		with patch.object(doc_events, "sync_item_level_listing") as sync:
			doc_events._from_sle_item(frappe._dict(item_code=AMMO))
		sync.assert_called_once_with(AMMO)


class IngestingAnOrderAnnouncesWhatItCommits(FrappeTestCase):
	def _ingest(self, order, *, lines=None, cancel=False):
		"""Run `_ingest_order` with everything but the commitment accounting stubbed."""
		def _apply_lines(doc, _src):
			doc.set("lines", [fx.FakeChildRow(r) for r in (lines or [])])

		def _unwind(doc):
			if cancel:
				doc.order_status = "Cancelled"

		client = fx.FakeDoc()
		client.get_order = lambda _id: {}
		with patch.object(orders, "_order_id", return_value="777"), \
				patch("frappe.db.get_value", return_value=order.name if order.name else None), \
				patch("frappe.get_doc", return_value=order), \
				patch("frappe.new_doc", return_value=order), \
				patch.object(orders, "apply_order_fields"), \
				patch.object(orders, "reload_children_for_update"), \
				patch.object(orders, "apply_order_lines", side_effect=_apply_lines), \
				patch.object(orders, "_warn_on_layaway"), \
				patch.object(orders, "reserve_order_serials"), \
				patch.object(orders, "_acknowledge"), \
				patch.object(orders, "_detect_unwound", side_effect=_unwind), \
				patch.object(orders, "is_item_level_line",
					side_effect=lambda line: not (line.sku or "").count("::")), \
				patch.object(orders, "notify_channel_commitments_changed") as notify:
			orders._ingest_order(client, {"orderID": "777"})
		return sorted((c.args[0], c.kwargs["source"]) for c in notify.call_args_list)

	def _existing(self, status="Pending Fulfillment", *lines):
		order = fx.FakeDoc(name="GB-ORD-1", order_status=status)
		order.set("lines", [fx.FakeChildRow(r) for r in lines])
		return order

	def test_a_new_order_with_ammunition_announces_it(self):
		order = fx.FakeDoc(name=None, order_status="Pending Fulfillment")
		heard = self._ingest(order, lines=[{"sku": AMMO, "qty": 2}])
		self.assertEqual(heard, [(AMMO, "gunbroker")])

	def test_an_unchanged_order_re_polled_announces_nothing(self):
		order = self._existing("Pending Fulfillment", {"sku": AMMO, "qty": 2})
		self.assertEqual(self._ingest(order, lines=[{"sku": AMMO, "qty": 2}]), [])

	def test_a_quantity_change_announces_the_item(self):
		order = self._existing("Pending Fulfillment", {"sku": AMMO, "qty": 2})
		self.assertEqual(self._ingest(order, lines=[{"sku": AMMO, "qty": 5}]),
			[(AMMO, "gunbroker")])

	def test_a_cancellation_announces_the_units_coming_back(self):
		order = self._existing("Pending Fulfillment", {"sku": AMMO, "qty": 2})
		heard = self._ingest(order, lines=[{"sku": AMMO, "qty": 2}], cancel=True)
		self.assertEqual(heard, [(AMMO, "gunbroker")])

	def test_a_gun_only_order_announces_nothing(self):
		order = fx.FakeDoc(name=None, order_status="Pending Fulfillment")
		self.assertEqual(
			self._ingest(order, lines=[{"sku": f"{GUN}::SN1", "qty": 1}]), [])

	def test_an_order_already_shipped_commits_nothing(self):
		order = self._existing("Transferred", {"sku": AMMO, "qty": 2})
		self.assertEqual(self._ingest(order, lines=[{"sku": AMMO, "qty": 2}]), [])


class TheHooksAreRegistered(FrappeTestCase):
	def test_both_registrations_resolve(self):
		from ffl_integrations import hooks

		self.assertIn("ffl_integrations.gunbroker.orders.channel_committed_units",
			hooks.channel_committed_units)
		self.assertIn(
			"ffl_integrations.gunbroker.crosschannel.channel_commitments_changed",
			hooks.channel_commitments_changed)
		for path in (*hooks.channel_committed_units, *hooks.channel_commitments_changed):
			self.assertTrue(callable(frappe.get_attr(path)), path)


class TheTwoChannelsSubtractEachOther(FrappeTestCase):
	"""Both registrants live, against real orders on both sides — the property the
	hook plumbing exists for: a unit sold on either channel and not yet shipped
	comes off the OTHER channel's quantity, and neither counts its own twice."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		import importlib.util

		if importlib.util.find_spec("ffl_woo_sync") is None:
			raise unittest.SkipTest("ffl_woo_sync is not installed on this bench")
		_item(AMMO, firearm=0)

	def _open_web_order(self, qty):
		frappe.get_doc({
			"doctype": "Woo Online Order",
			"woo_order_id": f"GBC-{frappe.generate_hash(length=8)}",
			"order_status": "Pending Fulfillment", "site": "retail",
			"customer_name": "QA Both",
			"lines": [{"item_code": AMMO, "qty": qty, "lane": "local"}],
		}).insert(ignore_permissions=True)

	def _web_stock(self, bin_qty):
		from ffl_woo_sync.woocommerce import mapping

		with patch.object(mapping, "get_item_stock", return_value={AMMO: bin_qty}), \
				patch.object(mapping, "custody_warehouse_qty", return_value=0):
			return mapping.local_stock_for_item(AMMO)

	def _gb_offer(self, bin_qty):
		with patch("ffl_core.warehouses.default_stock_warehouse", return_value="W"), \
				patch.object(item_listings, "_company", return_value="C"), \
				patch("frappe.db.get_value", side_effect=_bin_or_real(bin_qty)):
			return item_listings.sellable_qty(AMMO)

	def test_a_gunbroker_order_comes_off_the_storefront_count(self):
		before = self._web_stock(10)
		_order("Pending Fulfillment", (AMMO, 4))
		self.assertEqual(self._web_stock(10), before - 4)

	def test_a_web_order_comes_off_gunbrokers_offer(self):
		before = self._gb_offer(10)
		self._open_web_order(3)
		self.assertEqual(self._gb_offer(10), before - 3)

	def test_each_channel_counts_its_own_order_exactly_once(self):
		web_before, gb_before = self._web_stock(20), self._gb_offer(20)
		_order("Pending Fulfillment", (AMMO, 4))
		self._open_web_order(3)
		self.assertEqual(self._web_stock(20), web_before - 7)
		self.assertEqual(self._gb_offer(20), gb_before - 7)


def _bin_or_real(bin_qty):
	real = frappe.db.get_value

	def _get_value(doctype, filters=None, fieldname=None, *a, **kw):
		if doctype == "Bin":
			return bin_qty
		return real(doctype, filters, fieldname, *a, **kw)

	return _get_value
