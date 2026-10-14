"""GunBroker webhook receivers: who may call, what is taken from the body, where it goes.

The properties worth pinning, in order of how badly each would hurt:

* every refusal is one and the same 401 (the endpoint is guest-open);
* nothing from the body but IDs / SKU / the error text is kept or logged (the
  ItemsSold body carries the buyer's name, address and e-mail);
* a nudge that lands while an order sync is running makes it go round again
  (Frappe's `deduplicate=True` silently drops that one);
* a bid on a gun takes it off the other channels through the SAME step the
  15-minute watch uses.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import set_encrypted_password

from ffl_core.utils import single_raw
from ffl_integrations.gunbroker import listings, orders, tasks, webhook_shield, webhooks

SETTINGS = "GunBroker Settings"
USER = "gb-hook"
PASSWORD = "correct-horse-battery"


def _basic(user: str, password: str) -> str:
	return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class _Case(FrappeTestCase):
	def _patch(self, target, attr=None, **kw):
		patcher = patch.object(target, attr, **kw) if attr else patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _configure(self, *, enabled=1, user=USER, password=PASSWORD):
		"""Real settings rows (rolled back with the test), so the field names are pinned."""
		frappe.db.set_single_value(SETTINGS, {"enabled": enabled, "webhook_username": user})
		if password:
			set_encrypted_password(SETTINGS, SETTINGS, password, "webhook_password")
		else:
			frappe.db.sql("DELETE FROM `__Auth` WHERE doctype=%s AND name=%s "
				"AND fieldname='webhook_password'", (SETTINGS, SETTINGS))

	def _post(self, fn, *, header=None, body=b"{}", parked=False):
		"""Call a webhook endpoint as the web server would; returns (http status, body)."""
		frappe.local.response = frappe._dict()
		frappe.local.flags.pop(webhook_shield.STASH, None)
		raw = body if isinstance(body, bytes) else json.dumps(body).encode()
		request = SimpleNamespace(get_data=lambda: raw)
		patchers = [patch.object(frappe.local, "request", new=request, create=True)]
		if header is not None and parked:
			frappe.local.flags[webhook_shield.STASH] = header
		else:
			patchers.append(patch.object(frappe, "get_request_header",
				side_effect=lambda k, d=None: header if k.lower() == "authorization" and header is not None else d))
		for p in patchers:
			p.start()
		try:
			result = fn()
		finally:
			for p in reversed(patchers):
				p.stop()
			frappe.local.flags.pop(webhook_shield.STASH, None)
		return frappe.local.response.get("http_status_code"), result


class TestAuthentication(_Case):
	def setUp(self):
		self._configure()
		self.route = self._patch(webhooks, "_ROUTES", new={
			**webhooks._ROUTES, "items_sold": MagicMock(return_value="order 1")})

	def test_correct_credentials_are_accepted(self):
		status, result = self._post(webhooks.items_sold, header=_basic(USER, PASSWORD))
		self.assertEqual(result, {"ok": True})
		self.assertNotEqual(status, 401)
		self.route["items_sold"].assert_called_once()

	def test_credentials_parked_by_the_request_hook_are_accepted_too(self):
		status, result = self._post(
			webhooks.items_sold, header=_basic(USER, PASSWORD), parked=True)
		self.assertEqual(result, {"ok": True})

	def test_every_refusal_is_the_same_401_byte_for_byte(self):
		def refusal(**kw):
			status, result = self._post(webhooks.items_sold, **kw)
			return status, json.dumps(result, sort_keys=True)

		answers = {}
		answers["no header"] = refusal()
		answers["not basic"] = refusal(header="Bearer abc")
		answers["bad base64"] = refusal(header="Basic !!!")
		answers["no colon"] = refusal(header="Basic " + base64.b64encode(b"nocolon").decode())
		answers["not utf-8"] = refusal(header="Basic " + base64.b64encode(b"\xff\xfe:\xff").decode())
		answers["wrong user"] = refusal(header=_basic("someone", PASSWORD))
		answers["wrong password"] = refusal(header=_basic(USER, "wrong"))
		answers["empty both"] = refusal(header=_basic("", ""))
		answers["non-ascii"] = refusal(header=_basic("ü", "ß"))

		# Switched off, with the RIGHT credentials.
		self._configure(enabled=0)
		answers["switched off"] = refusal(header=_basic(USER, PASSWORD))
		# Switched on, webhook credentials never filled in.
		self._configure(enabled=1, user="", password="")
		answers["unconfigured"] = refusal(header=_basic("", ""))
		answers["unconfigured, user only"] = refusal(header=_basic(USER, PASSWORD))

		self.assertEqual(set(answers.values()), {(401, answers["no header"][1])},
			f"refusals differ: {answers}")
		self.assertEqual(json.loads(answers["no header"][1]), {"error": "Unauthorized", "ok": False})

	def test_a_refusal_never_reads_or_routes_the_body(self):
		read = self._patch(webhooks, "_read_body")
		self._post(webhooks.items_sold, header=_basic(USER, "wrong"))
		read.assert_not_called()
		self.route["items_sold"].assert_not_called()

	def test_both_halves_are_compared_even_when_the_first_is_wrong(self):
		with patch.object(webhooks.hmac, "compare_digest",
				wraps=webhooks.hmac.compare_digest) as compared:
			self._post(webhooks.items_sold, header=_basic("someone", "wrong"))
		self.assertEqual(compared.call_count, 2)

	def test_the_unconfigured_log_is_written_once_not_per_request(self):
		self._configure(enabled=1, user="", password="")
		frappe.cache.delete_value(webhooks._UNCONFIGURED_LOG_KEY)
		self.addCleanup(frappe.cache.delete_value, webhooks._UNCONFIGURED_LOG_KEY)
		logged = self._patch(webhooks, "safe_log_error")
		for _ in range(3):
			self._post(webhooks.items_sold, header=_basic("a", "b"))
		self.assertEqual(logged.call_count, 1)

	def test_all_five_endpoints_are_guest_post_only_and_share_the_gate(self):
		for slug in webhooks.EVENTS:
			fn = getattr(webhooks, slug)
			self.assertIn(fn, frappe.guest_methods, slug)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"], slug)
			status, result = self._post(fn)  # no header
			self.assertEqual((status, result["error"]), (401, "Unauthorized"), slug)

	def test_the_settings_form_lists_the_five_urls(self):
		urls = webhooks.webhook_urls()
		self.assertEqual([u["event"] for u in urls], list(webhooks.EVENTS.values()))
		for slug, row in zip(webhooks.EVENTS, urls):
			self.assertTrue(row["url"].endswith(f"/api/method/gb.{slug}"))
			self.assertTrue(row["url"].startswith(frappe.utils.get_url()))

	def test_each_url_is_short_enough_for_gunbroker_and_reaches_its_endpoint(self):
		# GunBroker's form refuses an endpoint path of 50 characters or more.
		for slug, row in zip(webhooks.EVENTS, webhooks.webhook_urls()):
			path = row["url"][len(frappe.utils.get_url()):]
			self.assertLess(len(path), 50, path)
			method = frappe.override_whitelisted_method(path.removeprefix("/api/method/"))
			self.assertIs(frappe.get_attr(method), getattr(webhooks, slug), path)


class TestRequestHook(_Case):
	"""Frappe answers its own 401 to any Basic header it cannot match to an API key,
	BEFORE the endpoint runs; the hook is what lets ours through."""

	def _run(self, path, auth="Basic abc"):
		environ = {"HTTP_AUTHORIZATION": auth} if auth is not None else {}
		request = SimpleNamespace(path=path, environ=environ)
		with patch.object(frappe.local, "request", new=request, create=True):
			frappe.local.flags.pop(webhook_shield.STASH, None)
			webhook_shield.shield_basic_auth()
		parked = frappe.local.flags.pop(webhook_shield.STASH, None)
		return environ, parked

	def test_it_is_registered_as_a_before_request_hook(self):
		self.assertIn("ffl_integrations.gunbroker.webhook_shield.shield_basic_auth",
			frappe.get_hooks("before_request"))

	def test_the_header_is_lifted_off_a_webhook_url_and_parked(self):
		for path in ("/api/method/gb.items_sold",
				"/api/method/ffl_integrations.gunbroker.webhooks.items_sold"):
			environ, parked = self._run(path)
			self.assertNotIn("HTTP_AUTHORIZATION", environ, path)
			self.assertEqual(parked, "Basic abc", path)

	def test_every_other_url_keeps_its_header(self):
		for path in ("/api/method/ping", "/api/method/ffl_integrations.fastbound.webhook.fastbound_event",
				"/api/resource/Item", "/app/gunbroker-settings", "/files/gb.items_sold.png"):
			environ, parked = self._run(path)
			self.assertEqual(environ, {"HTTP_AUTHORIZATION": "Basic abc"}, path)
			self.assertIsNone(parked, path)

	def test_a_webhook_url_without_a_header_parks_nothing_harmful(self):
		environ, parked = self._run("/api/method/ffl_integrations.gunbroker.webhooks.items_sold", auth=None)
		self.assertEqual(parked, "")

	def test_it_never_raises(self):
		with patch.object(frappe.local, "request", new=None, create=True):
			webhook_shield.shield_basic_auth()  # no request at all


class TestExtraction(_Case):
	def test_pascal_and_camel_case_read_the_same(self):
		pascal = webhooks.extract_facts("items_sold", {"OrderID": 123456, "ItemIDs": [1, 2]})
		camel = webhooks.extract_facts("items_sold", {"orderId": "123456"})
		self.assertEqual(pascal, {"order_id": "123456"})
		self.assertEqual(camel, pascal)

	def test_a_bid_event_reads_the_nested_item_in_either_casing(self):
		for body in (
			{"item": {"itemID": 777, "sku": "COLT::SN1", "bidCount": 3,
				"endingDateTimeUTC": "2026-10-20T10:00:00Z"}, "currentBid": 910.5},
			{"Item": {"ItemID": "777", "SKU": "COLT::SN1", "BidCount": 3,
				"EndingDateTimeUTC": "2026-10-20T10:00:00Z"}, "CurrentBid": "910.50"},
		):
			facts = webhooks.extract_facts("winning_bid_received", body)
			self.assertEqual(facts["item_id"], "777")
			self.assertEqual(facts["sku"], "COLT::SN1")
			self.assertEqual(facts["bid_count"], 3)
			self.assertEqual(facts["current_bid"], 910.5)
			self.assertEqual(facts["ends_at"], "2026-10-20T10:00:00Z")
			self.assertFalse(facts["fixed_price"])

	def test_a_bid_event_is_at_least_one_bid_even_if_the_summary_says_zero(self):
		facts = webhooks.extract_facts("winning_bid_received", {"item": {"itemID": 5, "bidCount": 0}})
		self.assertEqual(facts["bid_count"], 1)

	def test_a_flat_bid_body_is_read_as_the_item(self):
		facts = webhooks.extract_facts("winning_bid_received",
			{"ItemID": 9, "SKU": "A::B", "isFixedPrice": True})
		self.assertEqual((facts["item_id"], facts["sku"], facts["fixed_price"]), ("9", "A::B", True))

	def test_unsold_and_failed_events_read_the_item_and_the_error_text(self):
		self.assertEqual(
			webhooks.extract_facts("items_unsold_not_relisted", {"ItemID": 42, "SKU": "X::Y"}),
			{"item_id": "42", "sku": "X::Y"})
		failed = webhooks.extract_facts("fixed_price_listing_failed", {
			"ItemTitle": "t", "ItemID": 42, "SKU": "X::Y", "Quantity": 1,
			"Error Message": "<b>Category</b>   is\nnot valid"})
		self.assertEqual(failed["error"], "Category is not valid")
		again = webhooks.extract_facts("fixed_price_listing_failed", {"errorMessage": "plain"})
		self.assertEqual(again["error"], "plain")

	def test_garbage_yields_empty_facts_not_errors(self):
		garbage = {"OrderID": {"x": 1}, "ItemID": ["a"], "SKU": {"y": 2}, "item": "str",
			"currentBid": [1], "Error Message": None, "bidCount": "many"}
		for event in webhooks.EVENTS:
			facts = webhooks.extract_facts(event, garbage)
			self.assertFalse(facts.get("order_id"), event)
			self.assertFalse(facts.get("item_id"), event)
			self.assertFalse(facts.get("sku"), event)
			self.assertFalse(facts.get("error"), event)
		for bad in ("0", "-5", "12abc", "", 0, True, 1.5):
			self.assertEqual(webhooks._num_id(bad), "", bad)

	def test_long_text_is_truncated(self):
		facts = webhooks.extract_facts("fixed_price_listing_failed",
			{"SKU": "s" * 1000, "Error Message": "e" * 5000})
		self.assertEqual(len(facts["sku"]), webhooks._SKU_MAX)
		self.assertEqual(len(facts["error"]), webhooks._ERROR_MAX)

	def test_nothing_else_survives_extraction(self):
		body = {"OrderID": 5, "ShipTo": {"Name": "Jane Roe", "Email": "jane@example.com"},
			"BillTo": {"Phone": "555-0100"}}
		self.assertEqual(webhooks.extract_facts("items_sold", body), {"order_id": "5"})


class TestResolve(_Case):
	def _db(self, *, serials=(), items_level=(), by_id=None):
		self._patch("frappe.db.exists",
			side_effect=lambda dt, name=None, *a, **k: dt == "Serial No" and name in serials)
		self._patch(orders, "is_item_level_sku", side_effect=lambda sku: sku in items_level)
		by_id = by_id or {}

		def _sql_list(query, values=None, **_k):
			table = "Serial No" if "tabSerial No" in query else "Item"
			return by_id.get((table, values["id"]), [])

		self._patch("frappe.db.sql_list", side_effect=_sql_list)

	def test_the_sku_names_the_gun_before_any_item_number_is_tried(self):
		self._db(serials=("SN1",), by_id={("Serial No", "5"): ["OTHER"]})
		self.assertEqual(webhooks.resolve_listing("COLT::SN1", "5"), ("serial", "SN1"))

	def test_a_bare_sku_of_a_stock_item_is_item_level(self):
		self._db(items_level=("AMMO-9",))
		self.assertEqual(webhooks.resolve_listing("AMMO-9", ""), ("item", "AMMO-9"))

	def test_the_item_number_or_its_history_finds_a_hand_made_listing(self):
		self._db(by_id={("Serial No", "5"): ["SN2"]})
		self.assertEqual(webhooks.resolve_listing("", "5"), ("serial", "SN2"))
		self._db(by_id={("Item", "6"): ["AMMO-9"]})
		self.assertEqual(webhooks.resolve_listing("unknown-sku", "6"), ("item", "AMMO-9"))

	def test_an_item_number_on_two_records_is_ambiguous(self):
		self._db(by_id={("Serial No", "5"): ["SN1", "SN2"]})
		self.assertEqual(webhooks.resolve_listing("", "5"), ("", ""))

	def test_nothing_to_go_on_resolves_to_nothing(self):
		self._db()
		self.assertEqual(webhooks.resolve_listing("", ""), ("", ""))
		self.assertEqual(webhooks.resolve_listing("NOPE::NOPE", ""), ("", ""))


class TestRouting(_Case):
	def setUp(self):
		self._configure()
		self.enqueue = self._patch("frappe.enqueue")
		self.order_sync = self._patch(orders, "request_order_sync")
		self.watch = self._patch(tasks, "gb_auction_watch")
		self.sync_error = self._patch(listings, "_log_sync_error")
		self.item_error = self._patch(webhooks.item_listings, "log_sync_error")
		self.resolve = self._patch(webhooks, "resolve_listing", return_value=("serial", "SN1"))

	def _deliver(self, fn, body):
		return self._post(fn, header=_basic(USER, PASSWORD), body=body)

	def _queued(self):
		return [(c.args[0].rsplit(".", 1)[-1], c.kwargs) for c in self.enqueue.call_args_list]

	def test_sold_and_status_events_nudge_the_order_sync(self):
		for fn in (webhooks.items_sold, webhooks.order_status_update):
			self.order_sync.reset_mock()
			status, result = self._deliver(fn, {"OrderID": 9001})
			self.assertEqual(result, {"ok": True})
			self.order_sync.assert_called_once_with()
		self.enqueue.assert_not_called()

	def test_a_bid_on_a_known_gun_runs_the_per_gun_step(self):
		self._deliver(webhooks.winning_bid_received, {
			"item": {"itemID": 7, "sku": "COLT::SN1", "bidCount": 2}, "currentBid": 800})
		self.assertEqual(self._queued(), [("gb_bid_received_task", {
			"queue": "short", "serial_no": "SN1", "bid_count": 2,
			"current_bid": 800.0, "ends_at": ""})])
		self.watch.assert_not_called()

	def test_a_bid_on_an_unknown_gun_falls_back_to_the_full_watch(self):
		self.resolve.return_value = ("", "")
		status, result = self._deliver(webhooks.winning_bid_received, {"item": {"itemID": 7}})
		self.assertEqual(result, {"ok": True})
		self.watch.assert_called_once_with()
		self.enqueue.assert_not_called()

	def test_a_bid_on_a_fixed_price_listing_or_an_item_falls_back_to_the_full_watch(self):
		self._deliver(webhooks.winning_bid_received, {"item": {"itemID": 7, "isFixedPrice": True}})
		self.resolve.return_value = ("item", "AMMO-9")
		self._deliver(webhooks.winning_bid_received, {"item": {"itemID": 7}})
		self.assertEqual(self.watch.call_count, 2)
		self.enqueue.assert_not_called()

	def test_an_ended_listing_reconciles_the_gun_or_the_item_quantity(self):
		self._deliver(webhooks.items_unsold_not_relisted, {"ItemID": 7, "SKU": "COLT::SN1"})
		self.resolve.return_value = ("item", "AMMO-9")
		self._deliver(webhooks.items_unsold_not_relisted, {"ItemID": 8, "SKU": "AMMO-9"})
		self.assertEqual(self._queued(), [
			("gb_sync_serial_listing_task", {"queue": "short", "serial_no": "SN1"}),
			("gb_sync_item_quantity_task", {"queue": "short", "item_code": "AMMO-9"}),
		])

	def test_an_unmatched_ended_listing_is_acknowledged_and_does_nothing(self):
		self.resolve.return_value = ("", "")
		status, result = self._deliver(webhooks.items_unsold_not_relisted, {"ItemID": 7})
		self.assertEqual(result, {"ok": True})
		self.enqueue.assert_not_called()

	def test_a_failed_listing_records_the_plain_text_error_and_reconciles(self):
		self._deliver(webhooks.fixed_price_listing_failed, {
			"ItemID": 7, "SKU": "COLT::SN1", "Error Message": "<p>Category <b>12</b> is <i>closed</i></p>"})
		self.sync_error.assert_called_once_with(
			"SN1", "GunBroker could not list this: Category 12 is closed")
		self.assertEqual(self._queued(), [
			("gb_sync_serial_listing_task", {"queue": "short", "serial_no": "SN1"})])

	def test_a_failed_item_listing_records_on_the_item(self):
		self.resolve.return_value = ("item", "AMMO-9")
		self._deliver(webhooks.fixed_price_listing_failed, {"ItemID": 7, "SKU": "AMMO-9", "Error Message": "no"})
		self.item_error.assert_called_once_with("AMMO-9", "GunBroker could not list this: no")
		self.sync_error.assert_not_called()
		self.assertEqual(self._queued(), [
			("gb_sync_item_quantity_task", {"queue": "short", "item_code": "AMMO-9"})])

	def test_a_failed_listing_without_error_text_records_nothing(self):
		self._deliver(webhooks.fixed_price_listing_failed, {"ItemID": 7, "SKU": "COLT::SN1"})
		self.sync_error.assert_not_called()

	def test_a_delivery_stamps_the_settings_page(self):
		self._deliver(webhooks.items_sold, {"OrderID": 9001})
		self.assertEqual(single_raw(SETTINGS, "last_webhook_event"), "ItemsSold (order 9001)")
		self.assertTrue(single_raw(SETTINGS, "last_webhook_at"))
		self._deliver(webhooks.winning_bid_received, {"item": {"itemID": 7}})
		self.assertEqual(single_raw(SETTINGS, "last_webhook_event"), "WinningBidReceived (item 7)")

	def test_a_handler_that_committed_then_failed_still_answers(self):
		# request_order_sync commits, which ends the savepoint; the rollback must not
		# then turn the logged failure into an unhandled 500.
		self._patch(webhooks, "safe_log_error")
		def _no_savepoint(*a, **kw):
			if kw.get("save_point"):
				raise Exception("SAVEPOINT webhook_receive does not exist")
			return None  # the whole-transaction rollback: a no-op inside the test txn

		self._patch(frappe.db, "rollback", side_effect=_no_savepoint)
		self._patch(webhooks, "_record_delivery", side_effect=RuntimeError("boom"))
		_status, result = self._deliver(webhooks.items_unsold_not_relisted, {"ItemID": 7, "SKU": "COLT::SN1"})
		self.assertEqual(result, {"ok": False, "error": "internal"})

	def test_a_handler_failure_still_answers_200_and_undoes_its_writes(self):
		logged = self._patch(webhooks, "safe_log_error")
		frappe.db.set_single_value(SETTINGS, "last_webhook_event", "before")

		def _write_then_fail(event, ref):
			frappe.db.set_single_value(SETTINGS, "last_webhook_event", "partial")
			raise RuntimeError("boom")

		self._patch(webhooks, "_record_delivery", side_effect=_write_then_fail)
		status, result = self._deliver(webhooks.items_unsold_not_relisted, {"ItemID": 7, "SKU": "COLT::SN1"})
		self.assertNotEqual(status, 401)
		self.assertEqual(result, {"ok": False, "error": "internal"})
		self.assertEqual(single_raw(SETTINGS, "last_webhook_event"), "before")
		logged.assert_called_once()


class TestBodyHandling(_Case):
	PII = {"OrderID": 4242, "ShipTo": {"Name": "Jane Roe", "Email": "jane.roe@example.com",
		"Phone": "555-0100", "Address1": "1 Secret Lane"}}

	def setUp(self):
		self._configure()
		self.order_sync = self._patch(orders, "request_order_sync")

	def _everything_we_could_have_kept(self):
		rows = frappe.db.sql("SELECT value FROM tabSingles WHERE doctype=%s", SETTINGS)
		return json.dumps(rows) + json.dumps(webhooks.extract_facts("items_sold", self.PII))

	def test_the_buyer_details_are_neither_stored_nor_logged(self):
		logger = MagicMock()
		with patch.object(frappe, "logger", return_value=logger), \
				patch.object(webhooks, "safe_log_error") as logged:
			self._post(webhooks.items_sold, header=_basic(USER, PASSWORD), body=self.PII)
		everything = self._everything_we_could_have_kept() + str(logger.mock_calls) + str(logged.mock_calls)
		for secret in ("Jane", "Roe", "example.com", "555-0100", "Secret Lane"):
			self.assertNotIn(secret, everything)
		self.assertIn("order 4242", single_raw(SETTINGS, "last_webhook_event"))

	def test_the_buyer_details_are_not_in_a_failure_log_either(self):
		self.order_sync.side_effect = RuntimeError("boom")
		with patch.object(webhooks, "safe_log_error") as logged:
			status, result = self._post(webhooks.items_sold, header=_basic(USER, PASSWORD), body=self.PII)
		self.assertEqual(result["ok"], False)
		for secret in ("Jane", "example.com", "555-0100"):
			self.assertNotIn(secret, str(logged.mock_calls))

	def test_an_unreadable_or_oversized_body_is_acknowledged_and_never_routed(self):
		route = self._patch(webhooks, "_ROUTES", new={k: MagicMock() for k in webhooks.EVENTS})
		for body in (b"", b"not json", b"[1, 2]", b'"str"', b'{"OrderID": 1}' + b" " * webhooks.MAX_BODY_BYTES):
			status, result = self._post(webhooks.items_sold, header=_basic(USER, PASSWORD), body=body)
			self.assertEqual(result, {"ok": True, "handled": False}, body[:20])
		route["items_sold"].assert_not_called()


class TestOrderSyncRerun(_Case):
	"""Frappe's `deduplicate=True` treats a RUNNING job as queued and drops the
	enqueue, so a webhook arriving mid-sync would otherwise wait for the next cron."""

	def setUp(self):
		self.enqueue = self._patch("frappe.enqueue")
		self.passes = []

	def _sync_pass(self, *, during=None):
		def _pass():
			self.passes.append(single_raw(SETTINGS, orders.NUDGE_FIELD))
			if during:
				during(len(self.passes))
			return {"seen": 0, "ingested": 0, "failed": 0, "watermark": None}
		return self._patch(orders, "_sync_pass", side_effect=_pass)

	def test_a_nudge_writes_a_fresh_token_and_enqueues_deduplicated(self):
		orders.request_order_sync()
		first = single_raw(SETTINGS, orders.NUDGE_FIELD)
		orders.request_order_sync()
		self.assertTrue(first)
		self.assertNotEqual(single_raw(SETTINGS, orders.NUDGE_FIELD), first)
		args, kwargs = self.enqueue.call_args
		self.assertEqual(args, ("ffl_integrations.gunbroker.orders.run_order_sync",))
		self.assertEqual((kwargs["job_id"], kwargs["deduplicate"]), (orders.SYNC_JOB_ID, True))

	def test_no_nudge_means_one_pass(self):
		self._sync_pass()
		summary = orders.run_order_sync()
		self.assertEqual((len(self.passes), summary["passes"]), (1, 1))

	def test_a_nudge_during_the_pass_makes_it_go_round_again(self):
		# The enqueue inside request_order_sync is exactly what Frappe drops here.
		self._sync_pass(during=lambda n: orders.request_order_sync() if n == 1 else None)
		summary = orders.run_order_sync()
		self.assertEqual(len(self.passes), 2)
		self.assertEqual(summary["passes"], 2)
		self.assertNotEqual(self.passes[0], self.passes[1])

	def test_a_nudge_before_the_pass_is_covered_by_that_pass(self):
		orders.request_order_sync()
		self._sync_pass()
		self.assertEqual(orders.run_order_sync()["passes"], 1)

	def test_a_steady_stream_of_nudges_is_capped(self):
		self._sync_pass(during=lambda n: orders.request_order_sync())
		summary = orders.run_order_sync()
		self.assertEqual(summary["passes"], orders.MAX_NUDGE_PASSES)

	def test_an_unreadable_batch_is_not_retried_in_a_loop(self):
		def _fail():
			self.passes.append(1)
			orders.request_order_sync()
			return {"seen": 0, "ingested": 0, "failed": 0, "watermark": None, "unreadable": True}
		self._patch(orders, "_sync_pass", side_effect=_fail)
		orders.run_order_sync()
		self.assertEqual(len(self.passes), 1)


class TestBidReceivedTask(_Case):
	def setUp(self):
		self.watch = self._patch(tasks, "gb_auction_watch")
		self.lock = self._patch(tasks, "_lock_for_bids")
		self.unlock = self._patch(tasks, "_unlock_bids")
		self.row = {"name": "SN1", "gb_item_id": "7", "gb_listing_type": "Auction",
			"gb_reserved": 0, "gb_bid_locked": 0}
		self._patch("frappe.db.get_value", side_effect=lambda dt, name, *a, **k: (
			self.row if dt == "Serial No" and name == "SN1" else None))
		frappe.cache.delete_value(tasks._webhook_lock_key("SN1"))
		self.addCleanup(frappe.cache.delete_value, tasks._webhook_lock_key("SN1"))

	def test_a_bid_locks_the_gun_through_the_shared_step(self):
		tasks.gb_bid_received_task("SN1", 0, 910.5, "2026-10-20T10:00:00Z")
		self.lock.assert_called_once()
		args, kwargs = self.lock.call_args
		self.assertEqual(args[0], "SN1")
		self.assertEqual((kwargs["bids"], kwargs["current_bid"]), (1, 910.5))
		self.assertIsNotNone(kwargs["ends_at"])
		self.unlock.assert_not_called()
		self.watch.assert_not_called()

	def test_a_second_delivery_finds_the_lock_already_on(self):
		self.row["gb_bid_locked"] = 1
		self.assertEqual(
			tasks.apply_auction_row(self.row, {"isFixedPrice": False, "bidCount": 2}), tasks.BID_HELD)

	def test_a_gun_the_record_contradicts_goes_to_the_full_watch(self):
		for change in ({"gb_item_id": ""}, {"gb_listing_type": "Fixed Price"}):
			self.watch.reset_mock()
			self.row.update(change)
			tasks.gb_bid_received_task("SN1")
			self.watch.assert_called_once_with()
		self.watch.reset_mock()
		tasks.gb_bid_received_task("NO-SUCH-SERIAL")
		self.watch.assert_called_once_with()
		self.lock.assert_not_called()

	def test_a_gun_already_held_by_an_order_needs_no_bid_lock(self):
		self.row["gb_reserved"] = 1
		tasks.gb_bid_received_task("SN1")
		self.lock.assert_not_called()
		self.watch.assert_not_called()

	def test_the_watch_and_the_webhook_share_one_rule_for_fixed_price(self):
		self.assertEqual(tasks.apply_auction_row(self.row, {"bidCount": 3}), "")
		self.assertEqual(tasks.apply_auction_row(self.row, {"isFixedPrice": True, "bidCount": 3}), "")
		self.lock.assert_not_called()

	def test_bids_that_vanish_release_the_lock(self):
		self.row["gb_bid_locked"] = 1
		self.assertEqual(
			tasks.apply_auction_row(self.row, {"isFixedPrice": False, "bidCount": 0}), tasks.BID_RELEASED)
		self.unlock.assert_called_once_with("SN1")

	def test_a_webhook_lock_survives_a_lagging_zero_bid_count(self):
		# /ItemsSelling can still say 0 bids minutes after the webhook saw one.
		tasks.gb_bid_received_task("SN1")
		self.row["gb_bid_locked"] = 1
		self.assertEqual(
			tasks.apply_auction_row(self.row, {"isFixedPrice": False, "bidCount": 0}), tasks.BID_HELD)
		self.unlock.assert_not_called()


class TestSettingsValidation(_Case):
	def _doc(self, **values):
		doc = frappe.get_doc({"doctype": SETTINGS})
		doc.update(values)
		return doc

	def test_a_colon_in_the_user_name_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._doc(webhook_username="a:b", webhook_password="x" * 20)._check_webhook_credentials()

	def test_a_short_new_password_is_refused_but_a_masked_one_is_not(self):
		with self.assertRaises(frappe.ValidationError):
			self._doc(webhook_username="gb", webhook_password="short")._check_webhook_credentials()
		self._doc(webhook_username="gb", webhook_password="*" * 10)._check_webhook_credentials()
		self._doc(webhook_username="gb", webhook_password="x" * 12)._check_webhook_credentials()

	def test_the_form_gets_its_urls_on_load(self):
		doc = self._doc()
		doc.onload()
		self.assertEqual(doc.get_onload("webhook_urls"), webhooks.webhook_urls())

	def test_the_receiver_fields_exist_on_the_doctype(self):
		meta = frappe.get_meta(SETTINGS)
		for field in ("webhook_username", "webhook_password", "webhook_urls",
				"last_webhook_at", "last_webhook_event", orders.NUDGE_FIELD):
			self.assertTrue(meta.has_field(field), field)
		self.assertEqual(meta.get_field("webhook_password").fieldtype, "Password")
		self.assertTrue(meta.get_field("last_webhook_at").read_only)
