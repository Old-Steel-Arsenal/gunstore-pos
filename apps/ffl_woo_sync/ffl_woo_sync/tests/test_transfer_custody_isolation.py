"""A customer's transfer gun must never look sellable on either WooCommerce
store — it is not ours to sell at any price, on any channel, ever.

This is the THIRD independent exclusion axis in this sync layer, and it is
deliberately not built out of the other two:

  * ``{site}_delisted`` means "a human took this off the store" —
    overridable, per-site, cleared by a relist. A custody gun is none of those:
    no human may relist someone else's property.
  * ``unpriced`` means "this record cannot be listed until its data
    improves" — the fix is to price it. A custody gun is not broken data; it is
    correctly recorded and still unsellable.
  * ``consigned_out`` is the true structural sibling — cross-site, flag-driven,
    Active-forever — and is the shape this axis copies.

Two independent keys are checked everywhere, for the same reason the
consignment code checks both: ``Serial No.is_transfer_gun`` (the flag stamped at
receive) and the gun's physical location in a custody warehouse. Either alone is
disqualifying, so flag/warehouse drift cannot re-expose a customer's gun.

Touchpoints, mirroring test_consignment_out_delist.py's four:
  1. sync.push_serial — the sink every push path funnels through.
  2. sync_serial_listing_task — the SLE-triggered push/delist decision.
  3. _run_reconcile Pass 2 (per-serial drift) and Pass 3b (delist safety net) —
     a custody gun, like a consigned-out one, stays Active indefinitely, so the
     status filter alone can never find it.
  4. mapping.stock_for_item — the shared-SKU on-hand count: a customer's
     Glock sitting in custody must not add +1 to the store's own Glock listing.
  5. sync.route_serial_exit — where the check deliberately is NOT, and why.

Two independent keys exist (flag + location), but not every touchpoint pays for
both: the ones on a per-serial hot path read the flag they already have, while
the sink (push_serial) — the only place a listing can actually be created —
checks both. So the cheap checks can miss a hand-moved, unflagged gun and the
expensive one still stops it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.channel_query_doubles import get_all_double


# ===========================================================================
# 1. The sink: push_serial
# ===========================================================================

def _gv_except_dropship(row):
	"""Answer every `frappe.db.get_value` with `row` EXCEPT the drop-ship probe.

	`distributor_stock_source` (ffl_integrations) is registered as the
	FIRST statement of `stock_for_item`, so an unconditional stub answers that
	probe too: a truthy row makes an ordinary local item look drop-ship-sourced,
	the published quantity collapses to 0, and the local-stock assertions below
	fail. Returning None for that one field is exactly the behaviour from before that probe existed, so
	nothing here is weakened — only the new probe is excluded.

	A DocType meta load goes to the real database for the same reason as the
	probe: on a cold meta cache (a run that starts here, or after a
	clear_cache()) the blanket answer built a Meta out of `row` and the test
	died on 'Meta' object has no attribute 'istable'.
	"""
	real_get_value = frappe.db.get_value

	def _fn(*args, **kwargs):
		doctype = args[0] if args else kwargs.get("doctype")
		if doctype == "DocType":
			return real_get_value(*args, **kwargs)
		field = args[2] if len(args) > 2 else kwargs.get("field")
		if field == "dropship_distributor" or (
				isinstance(field, (list, tuple)) and "dropship_distributor" in field):
			return None
		return row

	return _fn


class CustodyGunIsNeverPushed(FrappeTestCase):
	def test_a_custody_gun_is_refused_before_any_http(self):
		from ffl_woo_sync.woocommerce import sync

		with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=True), \
			patch("ffl_woo_sync.woocommerce.sync._client") as client:
			res = sync.push_serial("SN-CUSTODY", _retries=0)
		self.assertEqual(res.get("skipped"), sync.TRANSFER_CUSTODY)
		client.assert_not_called()

	def test_the_refusal_is_not_overridable_by_relist(self):
		"""``_relist=True`` is a human saying "yes, republish this" — it exists
		for guns the owner hid and later un-hid. It cannot apply here: no human
		at this store is entitled to list a customer's property, so the operator
		'Push to WooCommerce' button is refused too."""
		from ffl_woo_sync.woocommerce import sync

		with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=True), \
			patch("ffl_woo_sync.woocommerce.sync._client") as client:
			res = sync.push_serial("SN-CUSTODY", _retries=0, _relist=True)
		self.assertEqual(res.get("skipped"), sync.TRANSFER_CUSTODY)
		client.assert_not_called()

	def test_an_ordinary_gun_is_not_refused(self):
		"""Regression guard: the new axis must not touch ordinary inventory.
		Falls through to the NEXT guard (unpriced) rather than pushing, which is
		enough to prove the custody check let it past."""
		from ffl_woo_sync.woocommerce import sync

		with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
			patch("frappe.db.get_value", return_value=0), \
			patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=0), \
			patch("ffl_woo_sync.woocommerce.sync._refuse_unpriced",
				return_value={"skipped": "unpriced"}):
			res = sync.push_serial("SN-NORMAL", _retries=0)
		self.assertEqual(res.get("skipped"), "unpriced")


# ===========================================================================
# 2. The SLE-triggered decision
# ===========================================================================

class SyncSerialListingTaskDelistsCustody(FrappeTestCase):
	"""The task decides from the Serial No row it has already fetched, so the
	custody check here costs no extra query — deliberately, because this runs on
	every stock movement. It therefore reads the FLAG only; the location half of
	the rule is enforced at the sink (push_serial) and by reconcile Pass 3b, both
	covered above and below."""

	def test_a_custody_gun_delists_even_though_active(self):
		"""A custody gun is Active, has a warehouse and was never woo_reserved —
		byte for byte, it looks like ordinary shelf stock to the old rulebook."""
		from ffl_woo_sync.woocommerce import tasks

		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Customer Custody - FT",
				woo_reserved=0, item_code="GUN-X", consigned_out=0, is_transfer_gun=1))
			tasks.sync_serial_listing_task("SN-CUSTODY", site="retail")
			delist.assert_called_once_with("SN-CUSTODY", site="retail")
			push.assert_not_called()

	def test_an_ordinary_active_serial_still_pushes(self):
		from ffl_woo_sync.woocommerce import tasks

		with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				status="Active", warehouse="Stores - FT",
				woo_reserved=0, item_code="GUN-X", consigned_out=0, is_transfer_gun=0))
			tasks.sync_serial_listing_task("SN-NORMAL", site="retail")
			push.assert_called_once_with("SN-NORMAL", site="retail", _relist=True,
				pos_fields_only=True)
			delist.assert_not_called()


# ===========================================================================
# 3. Reconcile
# ===========================================================================

class ReconcilePassesExcludeCustodyGuns(FrappeTestCase):
	def _make_client(self):
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		return client

	@staticmethod
	def _reconcile_side_effect(pass1_rows, pass2_rows, pass3_rows, pass3a_rows=()):
		"""Lifted verbatim from test_consignment_out_delist.py — the doctype-keyed
		stub that tells _run_reconcile's passes apart by their status filter."""
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
				return list(pass3a_rows)
			if doctype == "Serial No":
				return pass3_rows
			return []
		return _side_effect

	def test_pass2_query_excludes_transfer_guns(self):
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
		self.assertEqual(pass2_calls[0].kwargs["filters"].get("is_transfer_gun"), 0)

	def test_pass3_delists_a_listed_custody_gun_even_though_active(self):
		"""The safety net that matters most: a gun listed BEFORE this
		feature existed, later reclassified into custody, stays Active forever
		and would otherwise sit on the storefront indefinitely."""
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_ds, \
			patch("frappe.db.set_value"):
			mock_ga.side_effect = self._reconcile_side_effect(
				[], [],
				[frappe._dict(name="SN-CUSTODY", woo_product_id="WC-CUSTODY",
					status="Active")],
			)
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()
		mock_ds.assert_called_once_with("SN-CUSTODY", client=client, site="retail")

	def test_a_sold_shaped_custody_gun_is_delisted_not_archived(self):
		"""A custody gun that went home wears a sold-shaped status, so the
		archive arm selects it — and would publish "SOLD for $X" for a gun this
		store never sold, at a price it never charged.

		It is ROUTED inside that arm rather than filtered out of it. An earlier
		revision excluded it with is_transfer_gun=0 and trusted Pass 3b to catch
		it; but 3b's base filters pin status NOT IN SOLD_STATUS, so
		such a gun matched NEITHER arm and its listing would have stayed
		published and in stock indefinitely."""
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
			patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("frappe.db.set_value"):
			mock_ga.side_effect = self._reconcile_side_effect(
				[], [], [],
				pass3a_rows=[frappe._dict(
					name="SN-CUSTODY-SOLD", woo_product_id="WC-1",
					status="Delivered", is_transfer_gun=1)],
			)
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()
		delist.assert_called_once_with("SN-CUSTODY-SOLD", client=client, site="retail")
		arch.assert_not_called()

	def test_an_ordinary_sold_gun_is_still_archived(self):
		"""The control: routing must not disarm the SOLD archive itself."""
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
			patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
			patch("frappe.db.set_value"):
			mock_ga.side_effect = self._reconcile_side_effect(
				[], [], [],
				pass3a_rows=[frappe._dict(
					name="SN-REAL-SOLD", woo_product_id="WC-2",
					status="Delivered", is_transfer_gun=0)],
			)
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()
		arch.assert_called_once_with("SN-REAL-SOLD", client=client, site="retail")
		delist.assert_not_called()

	def test_pass3_query_ors_transfer_gun_with_inactive_status(self):
		client = self._make_client()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			patch("frappe.get_all") as mock_ga, \
			patch("frappe.db.set_value"):
			mock_ga.side_effect = self._reconcile_side_effect([], [], [])
			from ffl_woo_sync.woocommerce.tasks import _run_reconcile
			_run_reconcile()

		def _is_3b(c):
			cond = (c.kwargs.get("filters") or {}).get("status")
			return isinstance(cond, (list, tuple)) and cond[0] == "not in"

		pass3_calls = [c for c in mock_ga.call_args_list
			if c.args[:1] == ("Serial No",) and _is_3b(c)]
		self.assertTrue(pass3_calls, "Pass 3b's Serial No query was not found")
		or_filters = pass3_calls[0].kwargs.get("or_filters") or {}
		self.assertEqual(or_filters.get("is_transfer_gun"), 1)
		# The sibling axis must survive untouched.
		self.assertEqual(or_filters.get("consigned_out"), 1)


# ===========================================================================
# 4. The shared-SKU stock count
# ===========================================================================

class StockForItemExcludesCustody(FrappeTestCase):
	"""The user's Item-shape decision (a transfer gun hangs off the EXISTING
	catalogue SKU) makes this the load-bearing test of the whole run: without
	it, receiving a customer's Glock 19 would add +1 to the store's own Glock 19
	listing and the site would sell a gun it does not have."""

	def test_uniform_item_stock_excludes_transfer_serials(self):
		from ffl_woo_sync.woocommerce import mapping

		with patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("ffl_core.reservations.custody_serials", return_value=set()), \
			patch("frappe.get_all") as ga, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				is_firearm=1, item_group="Rifle", uniform_sell_price=1))
			ga.side_effect = get_all_double(["SN-A", "SN-B"])
			self.assertEqual(mapping.stock_for_item("UNI-GUN"), 2)
			filters = ga.call_args[1].get("filters") or ga.call_args[0][1]
			self.assertEqual(filters.get("is_transfer_gun"), 0)
			# The sibling axis must survive untouched.
			self.assertEqual(filters.get("consigned_out"), 0)

	def test_uniform_item_stock_excludes_a_gun_sitting_in_custody(self):
		"""Second, independent key: a gun whose flag never got stamped but which
		is physically in the custody warehouse still must not be counted."""
		from ffl_woo_sync.woocommerce import mapping

		with patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("ffl_core.reservations.custody_serials",
				return_value={"SN-B"}), \
			patch("frappe.get_all") as ga, \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				is_firearm=1, item_group="Rifle", uniform_sell_price=1))
			ga.side_effect = get_all_double(["SN-A", "SN-B"])
			self.assertEqual(mapping.stock_for_item("UNI-GUN"), 1)

	def test_non_firearm_stock_excludes_custody_warehouse_quantity(self):
		"""The Bin-sum branch sums EVERY warehouse, custody included. Whatever
		is parked in custody is somebody else's."""
		from ffl_woo_sync.woocommerce import mapping

		with patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
				return_value={"ACC-001": 12.0}), \
			patch("ffl_woo_sync.woocommerce.mapping.custody_warehouse_qty",
				return_value=5), \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				is_firearm=0, item_group="Accessory", uniform_sell_price=0))
			self.assertEqual(mapping.stock_for_item("ACC-001"), 7)

	def test_non_firearm_stock_never_goes_negative(self):
		from ffl_woo_sync.woocommerce import mapping

		with patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
				return_value={"ACC-001": 2.0}), \
			patch("ffl_woo_sync.woocommerce.mapping.custody_warehouse_qty",
				return_value=9), \
			patch("frappe.db.get_value") as gv:
			gv.side_effect = _gv_except_dropship(frappe._dict(
				is_firearm=0, item_group="Accessory", uniform_sell_price=0))
			self.assertEqual(mapping.stock_for_item("ACC-001"), 0)


# ===========================================================================
# 5. The exit router — where the custody check deliberately is NOT
# ===========================================================================

class RouteSerialExitKeepsItsReadBudget(FrappeTestCase):
	"""route_serial_exit has a deliberate, separately-tested read budget: zero
	reads on the common non-sold path, exactly one on the sold path. A custody
	lookup here would spend a read on EVERY reconciled row to change the answer
	for almost none of them, so the rule lives where it is free instead — the
	task above (from the row it already holds), reconcile Pass 3b (a query
	filter), and push_serial (which never published the gun to begin with).

	This test pins that decision so a future "just add the check here" does not
	silently regress the sweep's cost."""

	def test_the_router_costs_no_read_on_the_common_non_sold_path(self):
		from ffl_woo_sync.woocommerce import sync

		with patch("frappe.db.get_value") as gv:
			self.assertEqual(
				sync.route_serial_exit("SN-5", status="Active"), sync.EXIT_DELIST)
		gv.assert_not_called()


# ===========================================================================
# 6. Web orders
# ===========================================================================

class AWebOrderNeverReservesACustodyGun(FrappeTestCase):
	"""The auto-picker chooses the oldest Active, unreserved serial of the
	ordered SKU — and a customer's transfer gun hangs off that very SKU, because
	the shared-catalogue Item shape is this feature's design.

	R4b keeps custody guns out of the pushed stock COUNT, which narrows the race
	without closing it: sell the store's own one at the counter, and before the
	stock push lands a web order arrives, skips the now-Delivered gun on status,
	and reserves the customer's. Reservation is followed by picking and shipping,
	so the failure mode is mailing away someone else's property."""

	def test_the_picker_filters_out_transfer_guns(self):
		from ffl_woo_sync.woocommerce import webhook

		with patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names", return_value=set()), \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.get_all", return_value=[]) as ga:
			webhook._pick_oldest_unreserved_serial("GUN-X")
		self.assertEqual(ga.call_args.kwargs["filters"].get("is_transfer_gun"), 0)

	def test_the_picker_skips_a_gun_sitting_in_custody(self):
		"""Second key: a gun whose flag never got stamped but which is physically
		in the custody warehouse is skipped before the row lock is even taken."""
		from ffl_woo_sync.woocommerce import webhook

		with patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names",
				return_value={"Customer Custody - FT"}), \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.get_all", return_value=[
				{"name": "SN-CUSTODY", "warehouse": "Customer Custody - FT"}]), \
			patch("frappe.db.get_value") as gv:
			picked = webhook._pick_oldest_unreserved_serial("GUN-X")
		self.assertIsNone(picked)
		gv.assert_not_called()

	def test_the_picker_still_returns_an_ordinary_gun(self):
		from ffl_woo_sync.woocommerce import webhook

		with patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names", return_value=set()), \
			patch("ffl_core.reservations.counter_holds", return_value={}), \
			patch("frappe.get_all", return_value=[
				{"name": "SN-NORMAL", "warehouse": "Stores - FT"}]), \
			patch("frappe.db.get_value", side_effect=_gv_except_dropship(frappe._dict(
				woo_reserved=0, status="Active", is_transfer_gun=0))):
			self.assertEqual(
				webhook._pick_oldest_unreserved_serial("GUN-X"), "SN-NORMAL")

	def test_an_order_line_naming_a_custody_gun_is_rejected_with_its_own_reason(self):
		"""A line that explicitly names a custody gun is not a stock discrepancy
		to reconcile — it is a gun that was never ours to list. The staff-facing
		reservation_error should say so."""
		from ffl_woo_sync.woocommerce import webhook

		with patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names", return_value=set()), \
			patch("frappe.db.get_value", side_effect=_gv_except_dropship(frappe._dict(
				item_code="GUN-X", status="Active", woo_reserved=0,
				woo_order=None, warehouse="Stores - FT", is_transfer_gun=1))):
			reason = webhook._serial_unavailable_reason("SN-CUSTODY", "GUN-X")
		self.assertTrue(reason)
		self.assertIn("transfer", reason.lower())

	def test_an_ordinary_serial_still_passes_validation(self):
		from ffl_woo_sync.woocommerce import webhook

		with patch("ffl_woo_sync.woocommerce.webhook.custody_warehouse_names", return_value=set()), \
			patch("frappe.db.get_value", side_effect=_gv_except_dropship(frappe._dict(
				item_code="GUN-X", status="Active", woo_reserved=0,
				woo_order=None, warehouse="Stores - FT", is_transfer_gun=0))):
			self.assertEqual(webhook._serial_unavailable_reason("SN-OK", "GUN-X"), "")
