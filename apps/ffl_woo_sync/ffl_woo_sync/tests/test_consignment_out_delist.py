"""A consigned-out firearm must never look sellable on
either WooCommerce store while it is physically sitting at a partner dealer.

The Woo listing sync has FOUR touchpoints that all judge
"sellable" from Serial No.status/warehouse/woo_reserved alone. None of them
knew about outbound consignment, so a gun that Material-Transferred into a
dealer's consignment warehouse (still status=Active, still has a warehouse,
never woo_reserved) would look exactly like ordinary in-stock inventory:

  1. sync_serial_listing_task — the SLE-triggered push/delist decision.
  2. _run_reconcile Pass 2 — per-serial product drift reconciliation.
  3. _run_reconcile Pass 3 — safety net that delists no-longer-Active listed
     serials; must ALSO catch consigned_out=1 serials, which (unlike a sold
     gun) stay Active forever while at the dealer.
  4. mapping.stock_for_item — the uniform-price item's on-hand count.

Each test below mirrors the mocking style already established for that
touchpoint (test_serial_exit_delist.py / test_tasks.py / test_mapping.py) so
this module reads as "one more case", not a new pattern.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.channel_query_doubles import get_all_double


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


class SyncSerialListingTaskDelistsConsignedOut(FrappeTestCase):
	def test_consigned_out_serial_delists_even_though_active(self):
		from ffl_woo_sync.woocommerce import tasks

		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Consignment Out - Dealer X - FT",
				woo_reserved=0, item_code="GUN-X", consigned_out=1))
			tasks.sync_serial_listing_task("SN-CONSIGNED", site="retail")
			delist.assert_called_once_with("SN-CONSIGNED", site="retail")
			push.assert_not_called()

	def test_non_consigned_active_serial_still_pushes(self):
		"""Regression guard: the new field must not touch the existing
		sellable computation for ordinary in-stock guns."""
		from ffl_woo_sync.woocommerce import tasks

		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Stores - FT",
				woo_reserved=0, item_code="GUN-X", consigned_out=0))
			tasks.sync_serial_listing_task("SN-NORMAL", site="retail")
			push.assert_called_once_with("SN-NORMAL", site="retail", _relist=True,
				pos_fields_only=True)
			delist.assert_not_called()


class ReconcilePassesExcludeConsignedOut(FrappeTestCase):
	def _make_client(self):
		from unittest.mock import MagicMock
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		return client

	@staticmethod
	def _reconcile_side_effect(pass1_rows, pass2_rows, pass3_rows, pass3a_rows=()):
		"""A doctype-keyed side_effect, robust to incidental frappe.get_all
		calls that have nothing to do with _run_reconcile's own queries
		(e.g. permission/meta-cache warm-up on first touch in a fresh test
		process) — unlike a flat list, an unrelated call here can't desync the
		pass assignment or blow up with StopIteration.

		The three Serial No queries are told apart by their exact-match status
		filter: Pass 2 pins status="Active"; Pass 3a (the SOLD-archive arm)
		pins status="Delivered"; Pass 3b (delist) has neither — its
		status moved into or_filters. `pass3_rows` stays positional and keeps
		feeding the DELIST arm, so the consigned-out cases below read
		unchanged."""
		def _side_effect(doctype, *args, **kwargs):
			filters = kwargs.get("filters") if "filters" in kwargs else (
				args[0] if args else {})
			filters = filters or {}
			if doctype == "Item":
				return pass1_rows
			if doctype == "Serial No" and filters.get("status") == "Active":
				return pass2_rows
			status_cond = filters.get("status")
			if doctype == "Serial No" and isinstance(
					status_cond, (list, tuple)) and status_cond[0] == "in":
				return list(pass3a_rows)   # Pass 3a: `in` selects sold statuses
			if doctype == "Serial No":
				return pass3_rows
			return []
		return _side_effect

	def test_pass2_query_excludes_consigned_out(self):
		"""Pass 2's per-serial reconcile query must filter consigned_out=0 —
		asserted directly on the get_all call, mirroring
		test_pass3_skips_already_delisted_serials's style in test_tasks.py."""
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("frappe.db.set_value"):
			mock_ga.side_effect = self._reconcile_side_effect([], [], [])
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()
		pass2_calls = [c for c in mock_ga.call_args_list if c.args[:1] == ("Serial No",)
			and (c.kwargs.get("filters") or {}).get("status") == "Active"]
		self.assertTrue(pass2_calls, "Pass 2's Serial No query was not found")
		self.assertEqual(pass2_calls[0].kwargs["filters"].get("consigned_out"), 0)

	def test_pass3_delists_consigned_out_serial_even_though_active(self):
		"""Pass 3 must catch a listed, Active, consigned_out=1 serial — a
		sold gun eventually goes non-Active and gets caught by the existing
		status filter, but a consigned-out gun stays Active indefinitely
		while at the dealer, so status alone can't find it."""
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_ds, \
			patch("frappe.db.set_value"):
			# pass 3 row: still Active, but consigned out — Pass 2 stays
			# empty (excluded — consigned) so this row can ONLY have reached
			# Woo via Pass 3's own query.
			mock_ga.side_effect = self._reconcile_side_effect(
				[], [],
				[frappe._dict(name="SN-CONSIGNED", woo_product_id="WC-CONSIGNED",
					status="Active")],
			)
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()
		mock_ds.assert_called_once_with("SN-CONSIGNED", client=client, site="retail")

	def test_pass3_query_ors_consigned_out_with_inactive_status(self):
		"""Pass 3's query must OR consigned_out=1 in alongside status!=Active
		(both under the AND'd product_id/delisted filters) — asserted on the
		actual get_all call shape."""
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("frappe.db.set_value"):
			mock_ga.side_effect = self._reconcile_side_effect([], [], [])
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()
		# Pass 3b is the Serial No query that pins neither status — Pass 2 pins
		# "Active", Pass 3a (SOLD archive) pins "Delivered".
		def _is_3b(c):
			cond = (c.kwargs.get("filters") or {}).get("status")
			# Pass 2 pins "Active"; Pass 3a uses `in`; Pass 3b uses `not in`.
			return isinstance(cond, (list, tuple)) and cond[0] == "not in"

		pass3_calls = [c for c in mock_ga.call_args_list
			if c.args[:1] == ("Serial No",) and _is_3b(c)]
		self.assertTrue(pass3_calls, "Pass 3b's Serial No query was not found")
		filters = pass3_calls[0].kwargs.get("filters") or {}
		or_filters = pass3_calls[0].kwargs.get("or_filters") or {}
		self.assertEqual(filters.get("woo_delisted"), 0)
		self.assertIn("consigned_out", or_filters)
		self.assertEqual(or_filters.get("consigned_out"), 1)
		self.assertIn("status", or_filters)


class StockForItemExcludesConsignedOut(FrappeTestCase):
	def test_uniform_item_stock_excludes_consigned_out_serial(self):
		from ffl_woo_sync.woocommerce import mapping

		with patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("ffl_core.reservations.custody_serials",
				return_value=set()), \
			patch("frappe.get_all") as ga, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				is_firearm=1, item_group="Rifle", uniform_sell_price=1))
			# Ground truth: the DB-level filter (consigned_out=0) is what
			# actually excludes SN-CONSIGNED — the mock simply reflects that
			# query's result (2 serials, not 3) directly, since frappe.get_all
			# itself is mocked. The filter's presence is pinned separately.
			ga.side_effect = get_all_double(["SN-A", "SN-B"])
			self.assertEqual(mapping.stock_for_item("UNI-GUN"), 2)
			filters = ga.call_args[1].get("filters") or ga.call_args[0][1]
			self.assertEqual(filters.get("consigned_out"), 0)
