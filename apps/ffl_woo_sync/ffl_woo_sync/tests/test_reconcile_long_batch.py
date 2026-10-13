"""The reconcile that never finished (2026-09-25 → 10-13) and the consigned gun it
never took down.

* the sweep runs on the long queue, one at a time per store;
* Passes 1 and 2 read the store's products in pages, and every id a page did not
  answer is still read on its own — so a trashed or deleted product is handled
  exactly as before;
* Pass 1 runs inside every ``distributor_stock_batch`` provider's context, and a
  provider that fails costs nothing but speed;
* rq's timeout is never turned into a per-row error;
* a consigned-out gun cannot be published or restocked by ``push_serial``.
"""
from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from rq.timeouts import JobTimeoutException

from ffl_woo_sync.woocommerce import mapping, sync, tasks
from ffl_woo_sync.woocommerce.client import WooCommerceClient, WooCommerceError
from ffl_woo_sync.tests.test_channel_exclusion import _serial_row_reader

ITEM = "ITEM-001"
SERIAL = "SN-CONS"


def _client_with(get):
	"""A real WooCommerceClient whose HTTP ``get`` is ``get``."""
	client = WooCommerceClient.__new__(WooCommerceClient)
	client.get = get
	return client


class GetProducts(FrappeTestCase):
	def test_pages_of_100_keyed_by_string_id(self):
		calls = []

		def get(path, params=None):
			calls.append((path, dict(params)))
			return [{"id": int(i), "status": "publish"} for i in params["include"].split(",")]

		got = _client_with(get).get_products([str(i) for i in range(1, 251)])
		self.assertEqual(len(calls), 3)
		self.assertEqual([len(c[1]["include"].split(",")) for c in calls], [100, 100, 50])
		self.assertTrue(all(c[0] == "products" and c[1]["status"] == "any"
			and c[1]["per_page"] == 100 for c in calls))
		self.assertEqual(set(got), {str(i) for i in range(1, 251)})

	def test_a_failed_page_is_skipped_not_fatal(self):
		def get(path, params=None):
			ids = params["include"].split(",")
			if "1" in ids:
				raise WooCommerceError("GET products → 502", status_code=502)
			return [{"id": int(i)} for i in ids]

		got = _client_with(get).get_products([str(i) for i in range(1, 151)])
		self.assertEqual(set(got), {str(i) for i in range(101, 151)})

	def test_garbage_ids_and_bodies_are_left_to_the_caller(self):
		calls = []

		def get(path, params=None):
			calls.append(params["include"])
			return {"code": "not_a_list"}

		got = _client_with(get).get_products(["12", "", None, "WC-1", 12])
		self.assertEqual(calls, ["12"], "deduplicated, numeric ids only")
		self.assertEqual(got, {})


def _client(batch):
	client = MagicMock()
	client.get_attribute_ids.return_value = {}
	client.get_products.return_value = batch
	return client


def _item(name, pid):
	return frappe._dict(name=name, woo_product_id=pid, is_firearm=0, uniform_sell_price=0,
		standard_rate=100.0)


class ReconcileReadsInBatches(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.get_meta("Serial No")  # see test_tasks.TestRunReconcile.setUp

	def _run(self, client, *, items=(), serials=(), hooks=None, stock=0, get_all_tail=None):
		get_hooks = frappe.get_hooks

		def hooks_of(name=None, *a, **kw):
			if name == tasks.STOCK_BATCH_HOOK:
				return list(hooks or [])
			return get_hooks(name, *a, **kw)

		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
					return_value=client), \
				patch("frappe.get_all") as ga, \
				patch("frappe.get_hooks", side_effect=hooks_of), \
				patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=stock), \
				patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=900.0), \
				patch("ffl_woo_sync.woocommerce.sync.push_item") as push_item, \
				patch("ffl_woo_sync.woocommerce.sync.push_serial") as push_serial, \
				patch("ffl_woo_sync.woocommerce.sync.mark_woo_delisted") as delisted, \
				patch("frappe.db.set_value") as status:
			ga.side_effect = [list(items), list(serials),
				*([[]] if serials and any(s.item_code for s in serials) else []),
				*(get_all_tail or [[], []])]
			tasks._run_reconcile()
		return frappe._dict(push_item=push_item, push_serial=push_serial, delisted=delisted,
			status=status.call_args[0][2])

	def test_the_batch_answers_and_only_the_gaps_are_read_one_by_one(self):
		client = _client({"11": {"id": 11, "status": "publish", "stock_quantity": 0,
			"regular_price": "100.00"}})
		client.get_product.side_effect = lambda pid: {
			"12": {"id": 12, "status": "trash", "stock_quantity": 0, "regular_price": "100.00"},
			"13": None,
		}[pid]
		out = self._run(client, items=[_item("A", "11"), _item("B", "12"), _item("C", "13")])
		client.get_products.assert_called_once_with(["11", "12", "13"])
		self.assertEqual(sorted(c.args[0] for c in client.get_product.call_args_list), ["12", "13"])
		# the trashed one takes the same path a single read always gave it
		out.delisted.assert_called_once_with("Item", "B", site="retail")
		out.push_item.assert_not_called()
		self.assertEqual(out.status["last_sync_error"], "")

	def test_a_batch_that_raises_falls_back_to_single_reads(self):
		client = _client(None)
		client.get_products.side_effect = RuntimeError("store down")
		client.get_product.return_value = {"id": 11, "stock_quantity": 0, "regular_price": "100.00"}
		out = self._run(client, items=[_item("A", "11")])
		client.get_product.assert_called_once_with("11")
		self.assertEqual(out.status["last_sync_error"], "")

	def test_pass_two_reads_serials_in_the_same_batch(self):
		client = _client({"21": {"id": 21, "status": "publish", "stock_quantity": 1,
			"regular_price": "900.00", "sale_price": "", "date_on_sale_from": "",
			"date_on_sale_to": ""}})
		sn = frappe._dict(name="SN-1", woo_product_id="21", woo_reserved=0, item_code="",
			sale_sell_price=0, sale_from=None, sale_to=None)
		self._run(client, serials=[sn])
		client.get_products.assert_called_with(["21"])
		client.get_product.assert_not_called()

	def test_a_drifted_row_is_read_again_and_a_draft_since_the_batch_is_honoured(self):
		client = _client({"11": {"id": 11, "status": "publish", "stock_quantity": 0,
			"regular_price": "100.00"}})
		client.get_product.return_value = {"id": 11, "status": "draft", "stock_quantity": 0,
			"regular_price": "100.00"}
		out = self._run(client, items=[_item("A", "11")], stock=5)
		client.get_product.assert_called_once_with("11")
		out.delisted.assert_called_once_with("Item", "A", site="retail")
		out.push_item.assert_not_called()

	def test_a_drifted_row_still_on_sale_is_pushed(self):
		client = _client({"11": {"id": 11, "status": "publish", "stock_quantity": 0,
			"regular_price": "100.00"}})
		client.get_product.return_value = {"id": 11, "status": "publish", "stock_quantity": 0,
			"regular_price": "100.00"}
		out = self._run(client, items=[_item("A", "11")], stock=5)
		out.push_item.assert_called_once()
		out.delisted.assert_not_called()

	def test_a_drifted_serial_drafted_since_the_batch_is_honoured(self):
		client = _client({"21": {"id": 21, "status": "publish", "stock_quantity": 0,
			"regular_price": "900.00", "sale_price": "", "date_on_sale_from": "",
			"date_on_sale_to": ""}})
		client.get_product.return_value = {"id": 21, "status": "draft", "stock_quantity": 0}
		sn = frappe._dict(name="SN-1", woo_product_id="21", woo_reserved=0, item_code="",
			sale_sell_price=0, sale_from=None, sale_to=None)
		out = self._run(client, serials=[sn])
		client.get_product.assert_called_once_with("21")
		out.delisted.assert_called_once_with("Serial No", "SN-1", site="retail")
		out.push_serial.assert_not_called()

	def test_a_draft_in_the_batch_republished_since_is_not_marked_delisted(self):
		client = _client({"11": {"id": 11, "status": "draft", "stock_quantity": 0,
			"regular_price": "100.00"}})
		client.get_product.return_value = {"id": 11, "status": "publish", "stock_quantity": 0,
			"regular_price": "100.00"}
		out = self._run(client, items=[_item("A", "11")], stock=5)
		client.get_product.assert_called_once_with("11")  # once per row, not again at the push
		out.delisted.assert_not_called()
		out.push_item.assert_called_once()

	def test_a_draft_in_the_batch_still_a_draft_is_marked_delisted(self):
		client = _client({"11": {"id": 11, "status": "draft", "stock_quantity": 0,
			"regular_price": "100.00"}})
		client.get_product.return_value = {"id": 11, "status": "draft"}
		out = self._run(client, items=[_item("A", "11")], stock=5)
		out.delisted.assert_called_once_with("Item", "A", site="retail")
		out.push_item.assert_not_called()

	def test_a_job_timeout_is_not_a_row_error(self):
		client = _client({})
		client.get_product.side_effect = JobTimeoutException("300 s")
		with self.assertRaises(JobTimeoutException):
			self._run(client, items=[_item("A", "11"), _item("B", "12")])
		self.assertEqual(client.get_product.call_count, 1, "the sweep stops at the timeout")


class StockBatchHook(FrappeTestCase):
	"""``distributor_stock_batch``: ``fn(item_codes) -> context manager | None``."""

	def setUp(self):
		super().setUp()
		frappe.get_meta("Serial No")
		self.events = []

	def _cm(self, tag, *, enter_raises=False, exit_raises=False):
		events = self.events

		@contextlib.contextmanager
		def cm():
			if enter_raises:
				raise RuntimeError(f"{tag} enter")
			events.append(f"{tag}:enter")
			try:
				yield
			finally:
				events.append(f"{tag}:exit")
				if exit_raises:
					raise RuntimeError(f"{tag} exit")
		return cm()

	def _run(self, providers, items):
		client = _client({})
		events = self.events

		def stock(item_code):
			events.append(f"stock:{item_code}")
			return 0

		client.get_product.return_value = {"id": 1, "stock_quantity": 0, "regular_price": "100.00"}
		paths = {f"fake.provider_{i}": fn for i, fn in enumerate(providers)}
		get_hooks, get_attr = frappe.get_hooks, frappe.get_attr
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
					return_value=client), \
				patch("frappe.get_all") as ga, \
				patch("frappe.get_hooks", side_effect=lambda name=None, *a, **kw: list(paths)
					if name == tasks.STOCK_BATCH_HOOK else get_hooks(name, *a, **kw)), \
				patch("frappe.get_attr", side_effect=lambda p: paths.get(p) or get_attr(p)), \
				patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", side_effect=stock), \
				patch("ffl_woo_sync.woocommerce.sync.push_item"), \
				patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
				patch("frappe.log_error") as log_error, \
				patch("frappe.db.set_value") as status:
			ga.side_effect = [items, [], [],
				[frappe._dict(name="SN-OUT", woo_product_id="9", status="Inactive")]]
			tasks._run_reconcile()
		return frappe._dict(log_error=log_error, delist=delist, status=status.call_args[0][2])

	def test_called_once_with_every_pass_one_item_and_held_open_around_the_loop(self):
		seen = []

		def provider(codes):
			seen.append(list(codes))
			return self._cm("A")

		per_serial_gun = frappe._dict(name="GUN", woo_product_id="5", is_firearm=1,
			uniform_sell_price=0, item_group="Rifle", standard_rate=1.0)
		self._run([provider], [_item("X", "1"), per_serial_gun, _item("Y", "2")])
		self.assertEqual(seen, [["X", "Y"]], "per-serial guns are not Pass-1 items")
		self.assertEqual(self.events, ["A:enter", "stock:X", "stock:Y", "A:exit"])

	def test_a_provider_that_raises_is_logged_and_the_pass_runs_unbatched(self):
		def broken(codes):
			raise RuntimeError("catalog down")

		out = self._run([broken, lambda codes: self._cm("B"), lambda codes: None],
			[_item("X", "1")])
		self.assertEqual(self.events, ["B:enter", "stock:X", "B:exit"])
		self.assertEqual(out.log_error.call_count, 1)
		self.assertIn("distributor_stock_batch", out.log_error.call_args.kwargs["title"])
		out.delist.assert_called_once()  # Pass 3 still ran

	def test_a_context_that_fails_on_entry_is_left_out(self):
		out = self._run([lambda codes: self._cm("A", enter_raises=True),
			lambda codes: self._cm("B")], [_item("X", "1")])
		self.assertEqual(self.events, ["B:enter", "stock:X", "B:exit"])
		self.assertEqual(out.log_error.call_count, 1)

	def test_a_failure_on_exit_never_costs_passes_two_and_three(self):
		out = self._run([lambda codes: self._cm("A", exit_raises=True)], [_item("X", "1")])
		self.assertEqual(self.events, ["A:enter", "stock:X", "A:exit"])
		self.assertEqual(out.log_error.call_count, 1)
		out.delist.assert_called_once()
		self.assertEqual(out.status["last_push_count"], 1)

	def test_registered_as_a_first_party_hook(self):
		from ffl_core.extensions.lint import FIRST_PARTY_HOOKS, GUNSHOP_HOOKS

		self.assertIn(tasks.STOCK_BATCH_HOOK, FIRST_PARTY_HOOKS)
		self.assertNotIn(tasks.STOCK_BATCH_HOOK, GUNSHOP_HOOKS)


class ReconcileRunsOnTheLongQueue(FrappeTestCase):
	def test_one_deduplicated_long_job_per_store(self):
		with patch("frappe.enqueue") as enqueue:
			tasks.enqueue_reconcile("retail")
		enqueue.assert_called_once_with(
			"ffl_woo_sync.woocommerce.tasks._run_queued_reconcile", queue="long",
			timeout=tasks.RECONCILE_TIMEOUT, job_id="woo-reconcile:retail",
			deduplicate=True, site="retail")
		self.assertGreater(tasks.RECONCILE_TIMEOUT, 300)

	def test_the_queued_job_obeys_a_stop_set_while_it_waited(self):
		for enabled, freq, runs in ((0, "Every 2 Hours", 0), (1, "Disabled", 0),
				(1, "Every 2 Hours", 1)):
			with patch("frappe.db.get_value", return_value=frappe._dict(
						auto_sync_enabled=enabled, reconcile_frequency=freq)), \
					patch.object(tasks, "_run_reconcile") as run:
				tasks._run_queued_reconcile("retail")
			self.assertEqual(run.call_count, runs, (enabled, freq))

	def test_the_scheduled_gate_queues_it_instead_of_running_it_inline(self):
		s = MagicMock(auto_sync_enabled=1, reconcile_frequency="Daily", last_reconcile_at=None)
		with patch("frappe.get_single", return_value=s), \
				patch("frappe.enqueue") as enqueue, \
				patch.object(tasks, "_run_reconcile") as run:
			tasks.scheduled_reconcile()
		run.assert_not_called()
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])


class AConsignedGunIsNeverPublished(FrappeTestCase):
	"""7237k / 157598: consigned out 2026-09-16, then published at stock 1 by the
	listing flow. Same rank as CHANNEL_RESERVED: only a machine update of an
	existing product — which can only take the page down — gets through."""

	def test_the_payload_is_draft_at_stock_zero(self):
		payload = mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Mauser", description="",
			sell_price=900.0, consigned_out=True)
		self.assertEqual((payload["status"], payload["stock_quantity"]), ("draft", 0))

	def test_one_that_sold_at_the_partner_keeps_its_sold_page(self):
		payload = mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Mauser", description="",
			sell_price=900.0, consigned_out=True, sold_archived=True)
		self.assertEqual((payload["status"], payload["stock_quantity"]), ("publish", 0))

	def _push(self, *, product_id="11", payload=None, relist=False, pos_fields_only=False,
			row=None, sold=False):
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		client.update_product.return_value = {"id": 11}
		client.create_product.return_value = {"id": 12}
		payload = payload or mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Mauser", description="",
			sell_price=900.0, consigned_out=True)
		with patch.object(sync, "is_custody_serial", return_value=False), \
				patch.object(sync, "other_channel_reservation", return_value=None), \
				patch.object(sync, "effective_serial_price", return_value=900.0), \
				patch.object(sync, "is_sold_state", return_value=sold), \
				patch.object(sync.mapping, "serial_to_product_payload", return_value=payload), \
				patch("frappe.db.get_value", side_effect=_serial_row_reader(dict({
					"woo_product_id": product_id, "item_code": ITEM, "consigned_out": 1,
					"warehouse": "", "woo_delisted": 0, "status": "Active",
					"woo_sold_archived": 0}, **(row or {})))), \
				patch("frappe.db.set_value"):
			return sync.push_serial(SERIAL, client=client, site="retail", _relist=relist,
				pos_fields_only=pos_fields_only), client

	def test_the_listing_button_is_refused_relist_or_not(self):
		for relist in (False, True):
			result, client = self._push(relist=relist)
			self.assertEqual(result["skipped"], sync.CONSIGNED_OUT)
			self.assertFalse(client.update_product.called or client.create_product.called)

	def test_a_machine_push_with_no_product_is_a_create_and_is_refused(self):
		result, client = self._push(product_id="", pos_fields_only=True)
		self.assertEqual(result["skipped"], sync.CONSIGNED_OUT)
		self.assertFalse(client.create_product.called)

	def test_a_machine_update_that_takes_it_down_goes_through(self):
		result, client = self._push(pos_fields_only=True)
		self.assertNotIn("skipped", result)
		sent = client.update_product.call_args[0][1]
		self.assertEqual((sent["status"], sent["stock_quantity"]), ("draft", 0))

	def test_a_payload_that_would_publish_or_restock_is_refused(self):
		down = mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Mauser", description="",
			sell_price=900.0, consigned_out=True)
		for bad in (dict(down, status="publish"), dict(down, stock_quantity=1)):
			result, client = self._push(pos_fields_only=True, payload=bad)
			self.assertEqual(result["skipped"], sync.CONSIGNED_OUT)
			self.assertFalse(client.update_product.called)

	def test_the_location_alone_is_enough(self):
		"""The two-key rule: a gun moved into a consignment warehouse by hand,
		flag never set."""
		with patch("ffl_core.warehouses.is_consignment_warehouse", return_value=True):
			result, client = self._push(row={"consigned_out": 0, "warehouse": "Consign - X"})
		self.assertEqual(result["skipped"], sync.CONSIGNED_OUT)

	def test_an_ordinary_gun_is_untouched(self):
		live = mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Mauser", description="",
			sell_price=900.0)
		result, client = self._push(row={"consigned_out": 0}, payload=live, relist=True)
		self.assertNotIn("skipped", result)
		self.assertTrue(client.update_product.called)

	def test_staff_are_told_why(self):
		from ffl_woo_sync.woocommerce.client_api import _skip_message

		self.assertIn("consignment", _skip_message(sync.CONSIGNED_OUT))
