"""Tests for woocommerce/doc_events.py.

Covers:
- _skip_push() loop guard and system-state guards
- Item.on_update: loop guard, existence guard, field-change gate, enqueue target
- Serial No.on_update: per-serial push, uniform-firearm stock push, guards
- SLE.on_submit / on_cancel: guard, per-serial skip, enqueue target
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.doc_events import (
	_gallery_changed,
	_skip_push,
	on_invoice_change,
	on_item_update,
	on_serial_no_update,
	on_sle_cancel,
	on_sle_submit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item_doc(
	woo_product_id="WC-1",
	changed_fields=("standard_rate",),
):
	"""Build a mock Item doc with has_value_changed configured."""
	doc = MagicMock()
	doc.name = "ITEM-001"
	doc.woo_product_id = woo_product_id
	doc.has_value_changed.side_effect = lambda f: f in changed_fields
	return doc


def _serial_doc(
	woo_product_id="WC-SN",
	item_code="ITEM-001",
	changed_fields=("sell_price",),
):
	"""Build a mock Serial No doc with has_value_changed configured."""
	doc = MagicMock()
	doc.name = "SN-001"
	doc.item_code = item_code
	doc.woo_product_id = woo_product_id
	doc.has_value_changed.side_effect = lambda f: f in changed_fields
	return doc


def _sle_doc(item_code="ITEM-001"):
	return frappe._dict(item_code=item_code, actual_qty=1.0)


def _patch_item_row(
	is_firearm=0,
	uniform_sell_price=0,
	woo_product_id="WC-ITEM",
	item_group="",
):
	return patch(
		"frappe.db.get_value",
		return_value={
			"is_firearm": is_firearm,
			"item_group": item_group,
			"uniform_sell_price": uniform_sell_price,
			"woo_product_id": woo_product_id,
		},
	)


# ---------------------------------------------------------------------------
# _skip_push
# ---------------------------------------------------------------------------

class TestSkipPush(FrappeTestCase):
	def test_returns_false_by_default(self):
		frappe.flags.woo_sync_inbound = False
		self.assertFalse(_skip_push())

	def test_returns_true_when_inbound_flag(self):
		frappe.flags.woo_sync_inbound = True
		try:
			self.assertTrue(_skip_push())
		finally:
			frappe.flags.woo_sync_inbound = False

	def test_returns_true_when_in_migrate(self):
		# frappe.flags is a frappe._dict (dict subclass) — direct key assignment
		# because patch.object cannot create new dict keys via __dict__.
		frappe.flags["in_migrate"] = True
		try:
			self.assertTrue(_skip_push())
		finally:
			frappe.flags.pop("in_migrate", None)

	def test_returns_true_when_in_install(self):
		frappe.flags["in_install"] = True
		try:
			self.assertTrue(_skip_push())
		finally:
			frappe.flags.pop("in_install", None)

	def test_returns_true_when_in_import(self):
		old = frappe.flags.get("in_import", False)
		frappe.flags["in_import"] = True
		try:
			self.assertTrue(_skip_push())
		finally:
			frappe.flags["in_import"] = old


# ---------------------------------------------------------------------------
# _gallery_changed
# ---------------------------------------------------------------------------

class TestGalleryChanged(FrappeTestCase):
	"""Unit tests for the _gallery_changed() helper."""

	def _row(self, image, is_primary=0):
		r = MagicMock()
		r.image = image
		r.is_primary = is_primary
		return r

	def _doc_with(self, curr_rows, prev_rows):
		doc = MagicMock()
		doc.image_gallery = curr_rows
		before = MagicMock()
		before.image_gallery = prev_rows
		doc._doc_before_save = before
		return doc

	def test_new_doc_no_before_save_returns_true(self):
		"""No _doc_before_save → treat as changed (first save always syncs)."""
		doc = MagicMock()
		doc.image_gallery = []
		doc._doc_before_save = None
		self.assertTrue(_gallery_changed(doc))

	def test_identical_empty_galleries_returns_false(self):
		self.assertFalse(_gallery_changed(self._doc_with([], [])))

	def test_row_added_returns_true(self):
		doc = self._doc_with([self._row("/files/new.jpg")], [])
		self.assertTrue(_gallery_changed(doc))

	def test_row_removed_returns_true(self):
		doc = self._doc_with([], [self._row("/files/old.jpg")])
		self.assertTrue(_gallery_changed(doc))

	def test_image_url_changed_returns_true(self):
		doc = self._doc_with(
			[self._row("/files/new.jpg")],
			[self._row("/files/old.jpg")],
		)
		self.assertTrue(_gallery_changed(doc))

	def test_is_primary_changed_returns_true(self):
		"""Changing which row is primary is detected as a change."""
		doc = self._doc_with(
			[self._row("/files/a.jpg", is_primary=1)],
			[self._row("/files/a.jpg", is_primary=0)],
		)
		self.assertTrue(_gallery_changed(doc))

	def test_identical_rows_returns_false(self):
		doc = self._doc_with(
			[self._row("/files/a.jpg", is_primary=1), self._row("/files/b.jpg", is_primary=0)],
			[self._row("/files/a.jpg", is_primary=1), self._row("/files/b.jpg", is_primary=0)],
		)
		self.assertFalse(_gallery_changed(doc))

	def test_row_reorder_returns_true(self):
		"""Swapping row order (same images, different positions) is detected as a change."""
		doc = self._doc_with(
			[self._row("/files/a.jpg"), self._row("/files/b.jpg")],
			[self._row("/files/b.jpg"), self._row("/files/a.jpg")],
		)
		self.assertTrue(_gallery_changed(doc))


# ---------------------------------------------------------------------------
# Item.on_update
# ---------------------------------------------------------------------------

class TestOnItemUpdate(FrappeTestCase):
	def test_skips_when_inbound_flag_set(self):
		doc = _item_doc()
		frappe.flags.woo_sync_inbound = True
		try:
			with patch("frappe.enqueue") as mock_enqueue:
				on_item_update(doc)
			mock_enqueue.assert_not_called()
		finally:
			frappe.flags.woo_sync_inbound = False

	def test_skips_when_no_woo_product_id(self):
		doc = _item_doc(woo_product_id="")
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_not_called()

	def test_skips_when_no_relevant_field_changed(self):
		# No field in _ITEM_WOO_FIELDS returned True
		doc = _item_doc(changed_fields=())
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_not_called()

	def test_skips_when_only_irrelevant_field_changed(self):
		# A field not in _ITEM_WOO_FIELDS changed (e.g. reorder_level)
		doc = _item_doc(changed_fields=("reorder_level",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_not_called()

	def test_enqueues_push_item_task_when_standard_rate_changed(self):
		doc = _item_doc(changed_fields=("standard_rate",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once_with(
			"ffl_woo_sync.woocommerce.tasks.push_item_task",
			queue="long",
			enqueue_after_commit=True,
			item_code="ITEM-001",
		)

	def test_enqueues_push_when_item_name_changed(self):
		doc = _item_doc(changed_fields=("item_name",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_enqueues_push_when_disabled_changed(self):
		doc = _item_doc(changed_fields=("disabled",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_enqueues_push_when_uniform_sell_price_changed(self):
		doc = _item_doc(changed_fields=("uniform_sell_price",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_enqueues_push_when_image_changed(self):
		doc = _item_doc(changed_fields=("image",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_uses_long_queue(self):
		doc = _item_doc(changed_fields=("standard_rate",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		_, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs.get("queue"), "long")

	def test_enqueue_after_commit_true(self):
		doc = _item_doc(changed_fields=("standard_rate",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		_, kwargs = mock_enqueue.call_args
		self.assertTrue(kwargs.get("enqueue_after_commit"))

	def test_condition_grade_change_enqueues_push(self):
		doc = _item_doc(changed_fields=("condition_grade",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_caliber_change_enqueues_push(self):
		doc = _item_doc(changed_fields=("caliber",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_is_used_change_enqueues_push(self):
		doc = _item_doc(changed_fields=("is_used",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_gallery_add_enqueues_push(self):
		"""Adding a gallery row triggers push even when no scalar field changed."""
		row = MagicMock(); row.image = "/files/new.jpg"; row.is_primary = 1
		before = MagicMock(); before.image_gallery = []
		doc = _item_doc(changed_fields=())
		doc.image_gallery = [row]
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_gallery_remove_enqueues_push(self):
		"""Removing a gallery row triggers push even when no scalar field changed."""
		row = MagicMock(); row.image = "/files/old.jpg"; row.is_primary = 1
		before = MagicMock(); before.image_gallery = [row]
		doc = _item_doc(changed_fields=())
		doc.image_gallery = []
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_gallery_reprimary_enqueues_push(self):
		"""Changing which gallery row is primary triggers push."""
		ra_before = MagicMock(); ra_before.image = "/files/a.jpg"; ra_before.is_primary = 0
		rb_before = MagicMock(); rb_before.image = "/files/b.jpg"; rb_before.is_primary = 1
		ra_after = MagicMock(); ra_after.image = "/files/a.jpg"; ra_after.is_primary = 1
		rb_after = MagicMock(); rb_after.image = "/files/b.jpg"; rb_after.is_primary = 0
		before = MagicMock(); before.image_gallery = [ra_before, rb_before]
		doc = _item_doc(changed_fields=())
		doc.image_gallery = [ra_after, rb_after]
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once()

	def test_gallery_unchanged_no_scalar_change_no_push(self):
		"""Identical gallery rows + no scalar field change → no push."""
		r_cur = MagicMock(); r_cur.image = "/files/a.jpg"; r_cur.is_primary = 1
		r_prv = MagicMock(); r_prv.image = "/files/a.jpg"; r_prv.is_primary = 1
		before = MagicMock(); before.image_gallery = [r_prv]
		doc = _item_doc(changed_fields=())
		doc.image_gallery = [r_cur]
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_not_called()

	def test_gallery_add_skips_unlisted_item(self):
		"""Gallery change on an unlisted item (no woo_product_id) → no push."""
		row = MagicMock(); row.image = "/files/new.jpg"; row.is_primary = 1
		before = MagicMock(); before.image_gallery = []
		doc = _item_doc(woo_product_id="", changed_fields=())
		doc.image_gallery = [row]
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# Serial No.on_update
# ---------------------------------------------------------------------------

class TestOnSerialNoUpdate(FrappeTestCase):
	def test_skips_when_inbound_flag_set(self):
		doc = _serial_doc()
		frappe.flags.woo_sync_inbound = True
		try:
			with patch("frappe.enqueue") as mock_enqueue, _patch_item_row():
				on_serial_no_update(doc)
			mock_enqueue.assert_not_called()
		finally:
			frappe.flags.woo_sync_inbound = False

	def test_per_serial_push_when_sell_price_changed(self):
		"""Serial with woo_product_id + sell_price changed → push_serial_task."""
		doc = _serial_doc(changed_fields=("sell_price",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		calls = [c for c in mock_enqueue.call_args_list
				 if "push_serial_task" in c[0][0]]
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][1]["serial_no"], "SN-001")

	def test_per_serial_no_push_when_no_serial_woo_id(self):
		"""Serial without woo_product_id → no push_serial_task."""
		doc = _serial_doc(woo_product_id="", changed_fields=("sell_price",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		serial_calls = [c for c in mock_enqueue.call_args_list
						if "push_serial_task" in c[0][0]]
		self.assertEqual(len(serial_calls), 0)

	def test_per_serial_no_push_when_irrelevant_field_changed(self):
		doc = _serial_doc(changed_fields=("some_unrelated_field",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		serial_calls = [c for c in mock_enqueue.call_args_list
						if "push_serial_task" in c[0][0]]
		self.assertEqual(len(serial_calls), 0)

	def test_uniform_firearm_stock_push_when_status_changed(self):
		"""Status change on a serial of a listed uniform-price firearm → push_stock_task."""
		doc = _serial_doc(woo_product_id="", changed_fields=("status",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=1, woo_product_id="WC-ITEM"):
			on_serial_no_update(doc)
		stock_calls = [c for c in mock_enqueue.call_args_list
					   if "push_stock_task" in c[0][0]]
		self.assertEqual(len(stock_calls), 1)
		self.assertEqual(stock_calls[0][1]["item_code"], "ITEM-001")

	def test_uniform_firearm_stock_push_for_a_legacy_group_firearm(self):
		"""#9 part 3 legacy pin: a legacy uniform-price firearm — item_group in a
		firearm group, is_firearm not yet stamped by the auto-fill hook — must push
		parent stock on a serial status change exactly like a flagged one. The
		unified predicate is _is_firearm_row (flag OR firearm item_group); a
		flag-only test silently skipped this gun."""
		doc = _serial_doc(woo_product_id="", changed_fields=("status",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=0, item_group="Used Firearm",
				uniform_sell_price=1, woo_product_id="WC-ITEM"):
			on_serial_no_update(doc)
		stock_calls = [c for c in mock_enqueue.call_args_list
					   if "push_stock_task" in c[0][0]]
		self.assertEqual(len(stock_calls), 1)
		self.assertEqual(stock_calls[0][1]["item_code"], "ITEM-001")

	def test_uniform_firearm_stock_push_when_woo_reserved_changed(self):
		"""woo_reserved change → push_stock_task for parent item."""
		doc = _serial_doc(woo_product_id="", changed_fields=("woo_reserved",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=1, woo_product_id="WC-ITEM"):
			on_serial_no_update(doc)
		stock_calls = [c for c in mock_enqueue.call_args_list
					   if "push_stock_task" in c[0][0]]
		self.assertEqual(len(stock_calls), 1)

	def test_uniform_firearm_no_stock_push_when_no_item_woo_id(self):
		"""Uniform firearm with no item woo_product_id → no enqueue."""
		doc = _serial_doc(changed_fields=("status",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=1, woo_product_id=""):
			on_serial_no_update(doc)
		stock_calls = [c for c in mock_enqueue.call_args_list
					   if "push_stock_task" in c[0][0]]
		self.assertEqual(len(stock_calls), 0)

	def test_non_firearm_serial_does_nothing(self):
		"""Non-firearm item's serials don't trigger stock push."""
		doc = _serial_doc(woo_product_id="", changed_fields=("status",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=0, woo_product_id="WC-ITEM"):
			on_serial_no_update(doc)
		stock_calls = [c for c in mock_enqueue.call_args_list
					   if "push_stock_task" in c[0][0]]
		self.assertEqual(len(stock_calls), 0)

	def test_uses_long_queue(self):
		doc = _serial_doc(changed_fields=("sell_price",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		_, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs.get("queue"), "long")

	def test_caliber_change_enqueues_push_serial_task(self):
		"""caliber change on listed serial → push_serial_task."""
		doc = _serial_doc(changed_fields=("caliber",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		serial_calls = [c for c in mock_enqueue.call_args_list
						if "push_serial_task" in c[0][0]]
		self.assertEqual(len(serial_calls), 1)

	def test_gallery_add_enqueues_push_serial_task(self):
		"""Adding a gallery row to a listed serial → push_serial_task."""
		row = MagicMock(); row.image = "/files/new.jpg"; row.is_primary = 1
		before = MagicMock(); before.image_gallery = []
		doc = _serial_doc(changed_fields=())
		doc.image_gallery = [row]
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		serial_calls = [c for c in mock_enqueue.call_args_list
						if "push_serial_task" in c[0][0]]
		self.assertEqual(len(serial_calls), 1)

	def test_gallery_add_skips_unlisted_serial(self):
		"""Gallery change on unlisted serial (no woo_product_id) → no push_serial_task."""
		row = MagicMock(); row.image = "/files/new.jpg"; row.is_primary = 1
		before = MagicMock(); before.image_gallery = []
		doc = _serial_doc(woo_product_id="", changed_fields=())
		doc.image_gallery = [row]
		doc._doc_before_save = before
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		serial_calls = [c for c in mock_enqueue.call_args_list
						if "push_serial_task" in c[0][0]]
		self.assertEqual(len(serial_calls), 0)


# ---------------------------------------------------------------------------
# SLE handlers
# ---------------------------------------------------------------------------

class TestOnSleHandlers(FrappeTestCase):
	def test_submit_skips_when_inbound_flag(self):
		frappe.flags.woo_sync_inbound = True
		try:
			with patch("frappe.enqueue") as mock_enqueue, \
				 _patch_item_row(woo_product_id="WC-1"):
				on_sle_submit(_sle_doc())
			mock_enqueue.assert_not_called()
		finally:
			frappe.flags.woo_sync_inbound = False

	def test_cancel_skips_when_inbound_flag(self):
		frappe.flags.woo_sync_inbound = True
		try:
			with patch("frappe.enqueue") as mock_enqueue, \
				 _patch_item_row(woo_product_id="WC-1"):
				on_sle_cancel(_sle_doc())
			mock_enqueue.assert_not_called()
		finally:
			frappe.flags.woo_sync_inbound = False

	def test_submit_skips_when_no_woo_product_id(self):
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(woo_product_id=""):
			on_sle_submit(_sle_doc())
		mock_enqueue.assert_not_called()

	def test_submit_skips_for_per_serial_firearm(self):
		"""Per-serial firearms: SLE stock is irrelevant — Serial No.on_update handles it."""
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0, woo_product_id="WC-1"):
			on_sle_submit(_sle_doc())
		mock_enqueue.assert_not_called()

	def test_submit_skips_for_a_legacy_group_per_serial_firearm(self):
		"""A legacy firearm — item_group in a firearm group but is_firearm not yet
		stamped (the auto-fill hook hasn't touched the row) — must route exactly
		like a flagged one: skip the item-level stock push, let the per-serial path
		handle it. The canonical firearm test is flag OR firearm item_group
		(ffl_core.firearm.is_firearm_item); a flag-only test here silently
		mis-routed such a gun to an item-level push_stock_task, which then hit
		mapping.stock_for_item's ValueError (per-serial) — the divergence the 4473
		hardening round named."""
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=0, item_group="Used Firearm",
				uniform_sell_price=0, woo_product_id="WC-1"):
			on_sle_submit(_sle_doc())
		mock_enqueue.assert_not_called()

	def test_submit_enqueues_stock_push_for_non_firearm(self):
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=0, woo_product_id="WC-1"):
			on_sle_submit(_sle_doc())
		mock_enqueue.assert_called_once_with(
			"ffl_woo_sync.woocommerce.tasks.push_stock_task",
			queue="long",
			enqueue_after_commit=True,
			item_code="ITEM-001",
		)

	def test_submit_enqueues_stock_push_for_uniform_firearm(self):
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=1, uniform_sell_price=1, woo_product_id="WC-1"):
			on_sle_submit(_sle_doc())
		mock_enqueue.assert_called_once()

	def test_cancel_enqueues_stock_push(self):
		"""Cancelled SLE also triggers a stock update — stock reversed."""
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=0, woo_product_id="WC-1"):
			on_sle_cancel(_sle_doc())
		mock_enqueue.assert_called_once()

	def test_skips_when_no_item_code_on_sle(self):
		with patch("frappe.enqueue") as mock_enqueue:
			on_sle_submit(frappe._dict(item_code=""))
		mock_enqueue.assert_not_called()

	def test_uses_long_queue(self):
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_item_row(is_firearm=0, woo_product_id="WC-1"):
			on_sle_submit(_sle_doc())
		_, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs.get("queue"), "long")

# ---------------------------------------------------------------------------
# Item push carries no site (the retail store is the only one)
# ---------------------------------------------------------------------------

class TestOnItemUpdateRetailOnly(FrappeTestCase):

	def test_retail_enqueue_has_no_site_kwarg(self):
		"""Retail push carries no site kwarg (the task defaults to the retail store)."""
		doc = _item_doc(changed_fields=("standard_rate",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_called_once_with(
			"ffl_woo_sync.woocommerce.tasks.push_item_task",
			queue="long",
			enqueue_after_commit=True,
			item_code="ITEM-001",
		)

	def test_a_cost_only_change_pushes_nothing(self):
		"""The Cost field (Item.dealer_price) is not on the storefront: changing it alone pushes nothing."""
		doc = _item_doc(changed_fields=("dealer_price",))
		with patch("frappe.enqueue") as mock_enqueue:
			on_item_update(doc)
		mock_enqueue.assert_not_called()


class TestOnInvoiceChangeLegacyFirearm(FrappeTestCase):
	"""#9 part 3 legacy pin for on_invoice_change (counter-order reconcile)."""

	def test_reconciles_a_legacy_group_firearm_row(self):
		"""Parking firearms on a counter order changes their online sellability
		before any stock moves, so on_invoice_change reconciles every FIREARM row.
		A legacy per-serial firearm — item_group in a firearm group, is_firearm not
		yet stamped — must be reconciled via _sync_row_serial_listings, not skipped.
		Flag-only did `if not is_firearm: continue` and silently dropped it."""
		doc = frappe._dict(items=[frappe._dict(item_code="ITEM-001")], is_consolidated=0)
		with patch(
			"ffl_woo_sync.woocommerce.doc_events._sync_row_serial_listings"
		) as mock_sync, _patch_item_row(
			is_firearm=0, item_group="Used Firearm", uniform_sell_price=0, woo_product_id="WC-1"
		):
			on_invoice_change(doc)
		mock_sync.assert_called_once()


# ---------------------------------------------------------------------------
# gunshop_woo_push_fields — extension-declared trigger fields
# ---------------------------------------------------------------------------

def _patch_push_fields(declared):
	"""Answer ``gunshop_woo_push_fields`` with ``declared``; other hooks as usual."""
	real = frappe.get_hooks

	def fake(hook=None, *a, **kw):
		if hook == "gunshop_woo_push_fields":
			return declared
		return real(hook, *a, **kw)

	return patch("ffl_woo_sync.woocommerce.doc_events.frappe.get_hooks", side_effect=fake)


class TestExtensionPushFields(FrappeTestCase):
	"""A custom field an extension declares triggers the push like a core field.

	The CA Legal gap (2026-09-19): the meta provider ran only when a core field
	changed in the same save, so a flag edited on its own never left the POS.
	"""

	DECLARED = {"Item": ["osa_ca_legal"], "Serial No": ["osa_ca_legal", "osa_compliant_service"]}

	def _serial_pushes(self, mock_enqueue):
		return [c for c in mock_enqueue.call_args_list if "push_serial_task" in c[0][0]]

	def _item_pushes(self, mock_enqueue):
		return [c for c in mock_enqueue.call_args_list if "push_item_task" in c[0][0]]

	def test_declared_serial_field_change_pushes_the_serial(self):
		doc = _serial_doc(changed_fields=("osa_ca_legal",))
		with patch("frappe.enqueue") as mock_enqueue, _patch_push_fields(self.DECLARED), \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		calls = self._serial_pushes(mock_enqueue)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][1]["serial_no"], "SN-001")

	def test_declared_item_field_change_pushes_the_item(self):
		doc = _item_doc(changed_fields=("osa_ca_legal",))
		with patch("frappe.enqueue") as mock_enqueue, _patch_push_fields(self.DECLARED):
			on_item_update(doc)
		calls = self._item_pushes(mock_enqueue)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][1]["item_code"], "ITEM-001")

	def test_field_declared_for_the_other_doctype_does_not_count(self):
		"""Only Item lists osa_compliant_service? Then a Serial change of it is unrelated."""
		doc = _serial_doc(changed_fields=("osa_compliant_service",))
		with patch("frappe.enqueue") as mock_enqueue, \
			 _patch_push_fields({"Item": ["osa_compliant_service"]}), \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		self.assertEqual(self._serial_pushes(mock_enqueue), [])

	def test_nothing_declared_keeps_the_core_gate(self):
		doc = _serial_doc(changed_fields=("osa_ca_legal",))
		with patch("frappe.enqueue") as mock_enqueue, _patch_push_fields([]), \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		self.assertEqual(self._serial_pushes(mock_enqueue), [])

	def test_malformed_declaration_is_ignored(self):
		"""A list where a dict is expected must not break every Serial No save."""
		doc = _serial_doc(changed_fields=("osa_ca_legal",))
		with patch("frappe.enqueue") as mock_enqueue, _patch_push_fields(["osa_ca_legal"]), \
			 _patch_item_row(is_firearm=1, uniform_sell_price=0):
			on_serial_no_update(doc)
		self.assertEqual(self._serial_pushes(mock_enqueue), [])
