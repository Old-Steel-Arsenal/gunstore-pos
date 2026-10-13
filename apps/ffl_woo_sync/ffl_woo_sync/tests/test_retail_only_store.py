"""The retail store is the only WooCommerce store (1.11.0 removed the dealer store).

Acceptance pins for the removal, from the outside in:

* A retail web order runs end to end (webhook create → completion books the invoice →
  a refund amends it) against the real code, only Woo's HTTP faked, and every store
  connection it opens is the retail one.
* The Item / Serial No push, delist and reconcile endpoints talk to the retail store
  only: one store, one product write per action.
* Every whitelisted endpoint that takes a ``site`` refuses ``site="dealer"`` with a
  ValidationError; a webhook posted to ``?site=dealer`` gets a 400, processes nothing and leaves
  one info log line (no Error Log, no ToDo); a blank ``?site=`` is the retail store.
* Below the entry points nothing is re-targeted: an unknown site is an ``UnknownStoreError``
  raised without a message dialog; the two gates that answer "is this store on" treat a blank
  site as retail and an unknown one as off; a Woo push / delist job queued for a store that is
  gone skips at entry with one log line.

Neither shop ever took a dealer-store order, so no code path handles one (the patch's
safety-net log is covered in ffl_core/tests/test_remove_dealer_store.py).

No commits: everything rolls back with the class (docs/testing.md).
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_woo_sync.woocommerce import client_api

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"
STORE = "https://woo.test"
_SECRET = "retail-only-secret"
ACCESSORY = "QA-RETAIL-ONLY-ACC"
GUN_ITEM = "QA-RETAIL-ONLY-GUN"
GUN_SERIAL = "QA-RETAIL-ONLY-SN1"
GROUP = "QA Retail Only Accessory"


class _Store:
	"""The HTTP boundary, and only it: every WooCommerceClient is real, built from a dummy
	config; ``from_settings`` records which store was asked for; the session answers like a
	WooCommerce store that accepts everything. ``refunds`` answers GET orders/<id>/refunds."""

	def __init__(self, refunds=None):
		self.sites: list[str] = []
		self.calls: list[tuple[str, str]] = []
		self.refunds = refunds or []

	def _config(self, site="retail"):
		from ffl_woo_sync.woocommerce.client import WooCommerceConfig

		self.sites.append(site)
		return WooCommerceConfig(store_url=STORE, consumer_key="ck", consumer_secret="cs", timeout=5)

	def _request(self, _session, method, url, **kwargs):
		self.calls.append((method, url))
		path = url.split("/wc/v3/", 1)[-1]
		if method == "GET" and path.endswith("/refunds"):
			body = self.refunds if (kwargs.get("params") or {}).get("page", 1) == 1 else []
		elif method == "GET" and path.startswith("orders/"):
			body = {"id": path.split("/")[1], "meta_data": [], "line_items": []}
		elif method == "GET":
			body = []
		else:
			body = {"id": int(path.split("/")[1]) if path.count("/") and path.split("/")[1].isdigit()
				else 1, "status": "publish"}
		return MagicMock(status_code=200, content=b"x", text=json.dumps(body),
			json=MagicMock(return_value=body), headers={})

	@contextlib.contextmanager
	def open(self):
		from ffl_woo_sync.woocommerce.client import WooCommerceConfig

		store = self

		def _request(session, method, url, **kwargs):
			return store._request(session, method, url, **kwargs)

		with patch.object(WooCommerceConfig, "from_settings", side_effect=self._config), \
				patch("requests.Session.request", _request):
			yield self

	def product_writes(self):
		return [(m, u) for m, u in self.calls if m in ("PUT", "POST") and "/products/" in u
			and "/attributes" not in u and "/categories" not in u and "/brands" not in u]


def _sig(raw: bytes) -> str:
	return base64.b64encode(hmac.new(_SECRET.encode(), raw, hashlib.sha256).digest()).decode()


def _deliver(body: dict, topic: str, *, site_param=None, handlers=None):
	"""Post one webhook the way WooCommerce does (JSON body, optional ?site=), HMAC-signed with
	the retail secret. Returns (result, http status, secret lookups)."""
	from ffl_woo_sync.woocommerce import webhook

	raw = json.dumps(body).encode()
	headers = {"X-WC-Webhook-Signature": _sig(raw), "X-WC-Webhook-Topic": topic}
	secrets = []

	def _secret(site="retail"):
		secrets.append(site)
		return _SECRET

	request = MagicMock()
	request.args = {"site": site_param} if site_param is not None else {}
	frappe.local.response = frappe._dict()
	stack = contextlib.ExitStack()
	with stack:
		stack.enter_context(patch.object(webhook, "_read_raw_body", return_value=raw))
		stack.enter_context(patch.object(webhook, "_get_webhook_secret", side_effect=_secret))
		stack.enter_context(patch.object(frappe, "get_request_header",
			side_effect=lambda h, *a: headers.get(h) or headers.get(h.title()) or ""))
		stack.enter_context(patch.object(frappe.local, "request", request, create=True))
		stack.enter_context(patch.object(frappe.db, "commit"))
		if handlers is not None:
			stack.enter_context(patch.dict(webhook._HANDLERS, handlers))
		result = webhook.woo_event()
	return result, frappe.local.response.get("http_status_code"), secrets


def _ensure_accessory():
	if not frappe.db.exists("Item Group", GROUP):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": GROUP,
			"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
	if not frappe.db.exists("Item", ACCESSORY):
		frappe.get_doc({"doctype": "Item", "item_code": ACCESSORY, "item_name": "QA Sling",
			"item_group": GROUP, "stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1,
			"is_firearm": 0, "standard_rate": 100}).insert(ignore_permissions=True)


def _ensure_gun():
	from ffl_core.firearm import PISTOL

	if not frappe.db.exists("Item", GUN_ITEM):
		frappe.get_doc({"doctype": "Item", "item_code": GUN_ITEM, "item_name": "QA Pistol",
			"item_group": PISTOL, "stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1,
			"is_firearm": 1, "firearm_type": "Pistol"}).insert(ignore_permissions=True)
	if not frappe.db.exists("Serial No", GUN_SERIAL):
		frappe.get_doc({"doctype": "Serial No", "serial_no": GUN_SERIAL,
			"item_code": GUN_ITEM}).insert(ignore_permissions=True)


def _logged(logger) -> list[str]:
	"""The info lines a patched ``frappe.logger`` received."""
	return [str(c.args[0]) for c in logger.return_value.info.call_args_list if c.args]


def _order_body(woo_id: int, status: str, refunds=None) -> dict:
	body = {
		"id": woo_id, "number": str(woo_id), "status": status, "currency": "USD",
		"total": "100.00", "total_tax": "0.00", "shipping_total": "0.00",
		"discount_total": "0.00",
		"billing": {"first_name": "QA", "last_name": "Retail", "email": "qa-retail-only@example.com",
			"phone": "555-0102"},
		"shipping": {"first_name": "QA", "last_name": "Retail", "address_1": "1 Main St",
			"city": "Austin", "state": "TX", "postcode": "78701", "country": "US"},
		"line_items": [{"id": 1, "sku": ACCESSORY, "quantity": 1, "price": "100.00",
			"subtotal": "100.00", "total": "100.00", "total_tax": "0.00"}],
		"meta_data": [],
	}
	if refunds:
		body["refunds"] = refunds
	return body


class RetailOrderEndToEnd(FrappeTestCase):
	"""create → complete (revenue booked) → partial refund (invoice amended to net),
	with no dealer store anywhere in reach."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_accessory()

	def tearDown(self):
		frappe.flags.woo_sync_site = None
		frappe.flags.woo_sync_inbound = None
		frappe.local.response = frappe._dict()

	def test_a_retail_order_creates_completes_and_refunds(self):
		from ffl_woo_sync.woocommerce import refunds

		woo_id = 880000 + (hash(frappe.generate_hash()) % 9999)
		store = _Store(refunds=[{"id": 7301, "amount": "20.00", "line_items": [],
			"shipping_lines": [], "fee_lines": []}])
		jobs = []
		with store.open(), patch("frappe.enqueue",
				side_effect=lambda path, **kw: jobs.append((path, kw))):
			result, status, secrets = _deliver(_order_body(woo_id, "processing"), "order.created")
			self.assertEqual((status, result.get("ok")), (None, True), result)
			name = frappe.db.get_value("Woo Online Order", {"woo_order_id": str(woo_id)}, "name")
			self.assertTrue(name)
			self.assertEqual(frappe.db.get_value("Woo Online Order", name, "site"), "retail")

			# WooCommerce completes the order: the accessory-only auto-clear books the revenue.
			result, status, _ = _deliver(_order_body(woo_id, "completed"), "order.updated")
			self.assertTrue(result.get("ok"), result)
			self.assertEqual(frappe.db.get_value("Woo Online Order", name, "order_status"), "Transferred")
			completion = [kw for path, kw in jobs if path.endswith("create_sales_invoice_for_web_order")]
			self.assertEqual(len(completion), 1, jobs)
			frappe.get_attr("ffl_woo_sync.woocommerce.revenue.create_sales_invoice_for_web_order")(
				frappe.get_doc("Woo Online Order", name))
			gross = frappe.get_doc("Sales Invoice", {"woo_online_order": name, "docstatus": 1})
			self.assertAlmostEqual(flt(gross.grand_total), 100.0, places=2)

			# A partial refund on Woo: recorded by the webhook, then the reconcile job amends.
			result, status, _ = _deliver(
				_order_body(woo_id, "completed", refunds=[{"id": 7301, "total": "-20.00"}]),
				"order.updated")
			self.assertTrue(result.get("ok"), result)
			self.assertTrue([kw for path, kw in jobs if path.endswith("refunds._reconcile")], jobs)
			with patch.object(frappe, "in_test", False):
				outcome = refunds._reconcile(name)
		self.assertEqual(outcome["status"], "adjusted", outcome)
		amended = frappe.get_doc("Sales Invoice", outcome["sales_invoice"])
		self.assertAlmostEqual(flt(amended.grand_total), 80.0, places=2)
		# Only the retail store was ever asked for — by the webhook and by every job.
		self.assertEqual(set(secrets), {"retail"})
		self.assertTrue(store.sites)
		self.assertEqual(set(store.sites), {"retail"})
		self.assertTrue(all(url.startswith(STORE) for _m, url in store.calls))
		self.assertFalse([kw for _p, kw in jobs if kw.get("site") not in (None, "retail")], jobs)


class RetailPushDelistReconcile(FrappeTestCase):
	"""One store, one product write per action, for an Item and for a Serial No."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_accessory()
		_ensure_gun()

	def setUp(self):
		frappe.db.set_value("Item", ACCESSORY, {"woo_product_id": "555", "woo_listed": 1,
			"woo_delisted": 0, "standard_rate": 100}, update_modified=False)
		frappe.db.set_value("Serial No", GUN_SERIAL, {"woo_product_id": "777", "status": "Active",
			"sell_price": 500, "woo_delisted": 0, "woo_reserved": 0}, update_modified=False)

	def _action(self, fn, *args):
		with _Store().open() as store, patch("frappe.enqueue"):
			result = fn(*args)
		return result, store

	def _assert_retail_only(self, store, product_id):
		self.assertTrue(store.sites)
		self.assertEqual(set(store.sites), {"retail"})
		self.assertTrue(all(url.startswith(STORE) for _m, url in store.calls))
		writes = store.product_writes()
		self.assertEqual(len(writes), 1, writes)
		self.assertTrue(writes[0][1].endswith(f"/products/{product_id}"), writes)

	def test_item_push(self):
		result, store = self._action(client_api.push_item_now, ACCESSORY)
		self.assertTrue(result["ok"], result)
		self._assert_retail_only(store, "555")

	def test_item_delist(self):
		result, store = self._action(client_api.delist_item_now, ACCESSORY)
		self.assertTrue(result["ok"], result)
		self._assert_retail_only(store, "555")

	def test_item_reconcile(self):
		result, store = self._action(client_api.reconcile_now, ACCESSORY)
		self.assertTrue(result["ok"], result)
		self._assert_retail_only(store, "555")

	def test_serial_push(self):
		result, store = self._action(client_api.push_serial_now, GUN_SERIAL)
		self.assertTrue(result["ok"], result)
		self._assert_retail_only(store, "777")

	def test_serial_delist(self):
		result, store = self._action(client_api.delist_serial_now, GUN_SERIAL)
		self.assertTrue(result["ok"], result)
		self._assert_retail_only(store, "777")


class TheDealerSiteIsRefused(FrappeTestCase):
	"""site="dealer" is a ValidationError on every endpoint that takes a site, and a
	webhook naming it is a 400 with nothing processed — only the alert for a human."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.local.response = frappe._dict()
		frappe.set_user("Administrator")

	def test_every_site_taking_endpoint_refuses_the_dealer_store(self):
		from ffl_woo_sync.woocommerce import categories, dangling

		endpoints = (
			(client_api.test_connection, ()),
			(client_api.push_item_now, ("ITEM-X",)),
			(client_api.push_all_serials, ("ITEM-X",)),
			(client_api.delist_item_now, ("ITEM-X",)),
			(client_api.push_serial_now, ("SN-X",)),
			(client_api.delist_serial_now, ("SN-X",)),
			(client_api.reconcile_now, ("ITEM-X",)),
			(client_api.pull_product_from_woo, ("SKU-X",)),
			(categories.recategorize_store, ()),
			(dangling.woo_audit_dangling_ids, ()),
		)
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceConfig.from_settings") as build:
			for fn, args in endpoints:
				with self.subTest(endpoint=fn.__name__):
					with self.assertRaises(frappe.ValidationError):
						fn(*args, site="dealer")
		build.assert_not_called()

	def test_a_webhook_for_the_dealer_store_is_a_400_and_processes_nothing(self):
		handler = MagicMock()
		before = frappe.db.count("Woo Online Order")
		with patch("frappe.enqueue") as enq:
			result, status, secrets = _deliver(_order_body(990001, "processing"), "order.created",
				site_param="dealer", handlers={"order.created": handler})
		self.assertEqual(status, 400)
		self.assertFalse(result["ok"])
		self.assertEqual(secrets, [])            # refused before any secret is read
		handler.assert_not_called()
		self.assertFalse([c for c in enq.call_args_list
			if str(c.args[0] if c.args else "").startswith("ffl_woo_sync")])
		self.assertEqual(frappe.db.count("Woo Online Order"), before)

	def test_the_refusal_leaves_one_info_line_and_nothing_else(self):
		"""Nothing to tell anyone (no dealer store exists to send anything): the 400 and an
		info line in the bench log — no Error Log, no ToDo, even as Guest."""
		errors = frappe.db.count("Error Log")
		todos = frappe.db.count("ToDo")
		frappe.set_user("Guest")
		try:
			with patch("frappe.logger") as logger:
				_result, status, _ = _deliver(_order_body(990002, "processing"), "order.created",
					site_param="dealer", handlers={"order.created": MagicMock()})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(status, 400)
		lines = [m for m in _logged(logger) if "does not run" in m]
		self.assertEqual(len(lines), 1, lines)
		logger.return_value.warning.assert_not_called()
		logger.return_value.error.assert_not_called()
		self.assertEqual(frappe.db.count("Error Log"), errors)
		self.assertEqual(frappe.db.count("ToDo"), todos)

	def test_a_blank_site_param_is_the_retail_store(self):
		"""``?site=`` with nothing (or spaces) in it names no store: processed as retail."""
		for blank in ("", "   "):
			with self.subTest(param=repr(blank)):
				handler = MagicMock(return_value=None)
				with patch("frappe.enqueue"):
					result, status, secrets = _deliver(_order_body(990004, "processing"),
						"order.created", site_param=blank, handlers={"order.created": handler})
				self.assertEqual(status, None, result)
				self.assertEqual(secrets, ["retail"])
				handler.assert_called_once()


	def test_a_site_field_in_the_body_or_form_is_order_data_not_a_store(self):
		"""Only ``?site=`` names the store. A ``site`` key in the JSON body (or in the form Frappe
		builds from it) must not 400 the retail webhook."""
		handler = MagicMock(return_value=None)
		body = _order_body(990005, "processing")
		body["site"] = "dealer"
		with patch("frappe.enqueue"), patch.object(frappe.local, "form_dict",
				frappe._dict(site="dealer"), create=True):
			_result, status, secrets = _deliver(body, "order.created",
				handlers={"order.created": handler})
		self.assertEqual(status, None)
		self.assertEqual(secrets, ["retail"])
		handler.assert_called_once()


class BelowTheEntryPoints(FrappeTestCase):
	"""No re-targeting below the entry points: an unknown site raises (no dialog) or reads as
	off; a blank one is the retail store."""

	def test_internal_lookups_raise_without_a_message_dialog(self):
		from ffl_woo_sync.woocommerce import categories, sync
		from ffl_woo_sync.woocommerce.client import UnknownStoreError, WooCommerceConfig

		frappe.clear_messages()
		for call in (lambda: sync._f("dealer", "product_id"),
				lambda: WooCommerceConfig.from_settings("dealer"),
				lambda: categories.follows_tree("dealer")):
			with self.assertRaises(UnknownStoreError):
				call()
		self.assertEqual(frappe.message_log, [])

	def test_a_whitelisted_endpoint_shows_why(self):
		from ffl_woo_sync.woocommerce.client import UnknownStoreError

		frappe.clear_messages()
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceConfig.from_settings"), \
				self.assertRaises(UnknownStoreError):
			client_api.push_item_now("ITEM-X", site="dealer")
		self.assertEqual(len(frappe.message_log), 1)
		frappe.clear_messages()

	def test_the_store_on_gates_read_blank_as_retail_and_unknown_as_off(self):
		from ffl_woo_sync.woocommerce import status_events, tasks

		settings = MagicMock(auto_sync_enabled=1, store_url="https://woo.test/")
		settings.get = lambda key, default=None: getattr(settings, key, default)
		settings.get_password = MagicMock(return_value="s3cret")
		with patch("frappe.get_single", return_value=settings) as get_single:
			for blank in ("", None, "   ", " Retail "):
				with self.subTest(site=blank):
					self.assertTrue(tasks._site_sync_enabled(blank))
					self.assertEqual(status_events.outbound_config(blank),
						("https://woo.test", "s3cret"))
			self.assertEqual({c.args[0] for c in get_single.call_args_list},
				{"WooCommerce Settings"})
			get_single.reset_mock()
			self.assertFalse(tasks._site_sync_enabled("dealer"))
			self.assertIsNone(status_events.outbound_config("dealer"))
			get_single.assert_not_called()


class TheOneSiteResolver(FrappeTestCase):
	"""``client.resolve_site`` never raises; every free-form ``site`` reads through it."""

	def test_resolve_site(self):
		from ffl_woo_sync.woocommerce.client import resolve_site

		for value in (None, "", "   ", "retail", " Retail\n"):
			self.assertEqual(resolve_site(value), "retail", repr(value))
		for value in ("dealer", "wholesale", 7, ["retail"]):
			self.assertIsNone(resolve_site(value), repr(value))

	def test_the_distributor_hook_reads_blank_as_retail_and_still_raises_for_unknown(self):
		from ffl_woo_sync.woocommerce.distributor import distributor_listing_push

		with patch("frappe.enqueue") as enqueue:
			distributor_listing_push("QA-RESOLVER-ITEM", {"site": "  ", "stock": 1, "price": 5})
		self.assertEqual(enqueue.call_args.kwargs["site"], "retail")
		with patch("frappe.enqueue") as enqueue, self.assertRaises(ValueError):
			distributor_listing_push("QA-RESOLVER-ITEM", {"site": "wholesale"})
		enqueue.assert_not_called()


class QueuedDealerJobs(FrappeTestCase):
	"""A Woo push / delist job naming a store this POS does not run (one queued across the
	1.11.0 deploy) logs one line and does nothing — no store lookup, no Error Log traceback."""

	def test_a_dealer_store_job_skips_at_entry_and_a_retail_one_runs(self):
		from ffl_woo_sync.woocommerce import sync, tasks

		with patch.object(sync, "push_serial") as push, patch("frappe.log_error") as log_error, \
				patch("frappe.logger") as logger:
			tasks.push_serial_task("QA-QUEUED-SN", site="dealer")
			push.assert_not_called()
			tasks.push_serial_task("QA-QUEUED-SN")
			tasks.push_serial_task("QA-QUEUED-SN", "retail")
		self.assertEqual([c.kwargs.get("site") for c in push.call_args_list], ["retail", "retail"])
		log_error.assert_not_called()
		lines = [m for m in _logged(logger) if "the dealer store is gone" in m]
		self.assertEqual(len(lines), 1, lines)
		self.assertIn("push_serial_task", lines[0])

	def test_a_blank_site_is_retail_and_any_other_unknown_site_still_reaches_the_error_log(self):
		from ffl_woo_sync.woocommerce import sync, tasks

		with patch.object(sync, "push_serial") as push:
			tasks.push_serial_task("QA-QUEUED-SN", site="  ")
			tasks.push_serial_task("QA-QUEUED-SN", site=None)
		self.assertEqual([c.kwargs.get("site") for c in push.call_args_list], ["retail", "retail"])
		with patch("frappe.log_error") as log_error:
			tasks.delist_serial_task("QA-QUEUED-SN", site="wholesale")  # real sync, no store
		log_error.assert_called_once()
		self.assertIn("wholesale", log_error.call_args.kwargs["title"])

	def test_every_woo_push_and_delist_task_is_guarded(self):
		"""Every enqueue target in tasks.py that takes a ``site`` goes through the guard — except
		``rebucket_and_apply_serial_task``: only a dealer-store order could queue it for that
		store, and there never was one."""
		import inspect

		from ffl_woo_sync.woocommerce import tasks

		names = [name for name, fn in vars(tasks).items()
			if name.endswith("_task") and callable(fn) and "site" in inspect.signature(fn).parameters]
		self.assertGreaterEqual(len(names), 12, names)
		for name in names:
			with self.subTest(task=name):
				wrapped = hasattr(getattr(tasks, name), "__wrapped__")
				self.assertEqual(wrapped, name != "rebucket_and_apply_serial_task", name)
