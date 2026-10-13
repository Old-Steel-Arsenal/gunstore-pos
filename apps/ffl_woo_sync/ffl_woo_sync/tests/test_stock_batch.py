"""Batched stock refresh: ``sync.push_stock_batch`` + ``tasks.push_stock_batch_task``.

Woo client and Item reads are mocked; no Redis or database state is involved.
Pins: ceil(N/100) batch requests, per-item bookkeeping (committed) after success,
per-entry errors falling back to the single-item path without losing the rest,
and every failure — including a requests timeout, which is not a
``WooCommerceError`` — being reported per item like ``push_stock_task`` does,
without the task raising or re-queueing anything.
(The hook side — collect, enqueue once after commit — is in
test_distributor_listing_push.py.)
"""
from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import sync, tasks
from ffl_woo_sync.woocommerce.client import WooCommerceError

_ENTRY_ERROR = {"code": "x", "message": "boom", "data": {"status": 400}}


def _client(errors: dict[int, dict] | None = None):
    """A client whose batch endpoint echoes every update, or errors for some ids."""
    errors = errors or {}
    client = MagicMock()

    def _batch(updates):
        return {"update": [
            {"id": u["id"], "error": errors[u["id"]]} if u["id"] in errors
            else {"id": u["id"], "stock_quantity": u["stock_quantity"]}
            for u in updates]}

    client.batch_update_products.side_effect = _batch
    client.update_product.side_effect = lambda woo_id, payload: {"id": int(woo_id), **payload}
    return client


class _Base(FrappeTestCase):
    def setUp(self):
        super().setUp()
        for target, kw in (
            # "I-7" -> product id 1007
            ("frappe.db.get_value",
             {"side_effect": lambda dt, name, field: str(int(name.split("-")[1]) + 1000)}),
            ("frappe.db.set_value", {}),
            ("ffl_woo_sync.woocommerce.sync._now", {"return_value": "2026-09-29 00:00:00"}),
            ("ffl_woo_sync.woocommerce.sync.mapping.stock_for_item", {"side_effect": lambda code: 5}),
            ("ffl_woo_sync.woocommerce.sync.safe_commit", {}),
        ):
            p = patch(target, **kw)
            setattr(self, "m_" + target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)


class TestPushStockBatch(_Base):
    def test_68_items_cost_one_request(self):
        codes = [f"I-{n}" for n in range(68)]
        client = _client()
        results = sync.push_stock_batch(codes, client=client, _retries=0)
        self.assertEqual(client.batch_update_products.call_count, 1)
        client.update_product.assert_not_called()
        self.assertEqual(set(results), set(codes))

    def test_payload_carries_run_time_quantity_and_dedupes(self):
        client = _client()
        self.m_stock_for_item.side_effect = lambda code: 7
        sync.push_stock_batch(["I-1", "I-1"], client=client, _retries=0)
        client.batch_update_products.assert_called_once_with(
            [{"id": 1001, "stock_quantity": 7}])

    def test_stamps_are_written_and_committed_per_successful_item(self):
        sync.push_stock_batch(["I-1", "I-2"], client=_client(), _retries=0)
        stamped = [c.args[1] for c in self.m_set_value.call_args_list]
        self.assertEqual(stamped, ["I-1", "I-2"])
        for c in self.m_set_value.call_args_list:
            self.assertEqual(c.args[2].get("woo_sync_error"), "")
            self.assertTrue(c.args[2].get("woo_synced_at"))
        # one commit for the fresh read snapshot + one per stamped item
        self.assertEqual(self.m_safe_commit.call_count, 3)

    def test_one_erroring_entry_falls_back_and_others_still_land(self):
        client = _client(errors={1002: _ENTRY_ERROR})
        results = sync.push_stock_batch(["I-1", "I-2", "I-3"], client=client, _retries=0)
        client.update_product.assert_called_once_with("1002", {"stock_quantity": 5})
        self.assertTrue(all(not isinstance(v, Exception) for v in results.values()))
        # the fallback's stamp is committed like the batch path's: 1 snapshot + 3 items
        self.assertEqual(self.m_set_value.call_count, 3)
        self.assertEqual(self.m_safe_commit.call_count, 4)

    def test_erroring_entry_whose_fallback_fails_is_returned_not_raised(self):
        client = _client(errors={1002: _ENTRY_ERROR})
        client.update_product.side_effect = WooCommerceError("Forbidden", status_code=403)
        results = sync.push_stock_batch(["I-1", "I-2", "I-3"], client=client, _retries=0)
        self.assertIsInstance(results["I-2"], WooCommerceError)
        self.assertNotIsInstance(results["I-1"], Exception)
        self.assertNotIsInstance(results["I-3"], Exception)
        self.assertEqual([c.args[1] for c in self.m_set_value.call_args_list], ["I-1", "I-3"])

    def test_whole_request_failure_reports_every_item_without_per_item_storm(self):
        for exc in (
            WooCommerceError("blocked", status_code=403),
            requests.exceptions.ReadTimeout("read timed out"),
            requests.exceptions.ConnectionError("refused"),
        ):
            with self.subTest(exc=type(exc).__name__):
                client = _client()
                client.batch_update_products.side_effect = exc
                self.m_set_value.reset_mock()
                results = sync.push_stock_batch(["I-1", "I-2"], client=client, _retries=0)
                self.assertEqual(set(results), {"I-1", "I-2"})
                self.assertTrue(all(v is exc for v in results.values()))
                client.update_product.assert_not_called()
                self.m_set_value.assert_not_called()

    def test_item_without_product_id_is_skipped_and_others_proceed(self):
        self.m_get_value.side_effect = lambda dt, name, field: "" if name == "I-1" else "2000"
        client = _client()
        results = sync.push_stock_batch(["I-1", "I-2"], client=client, _retries=0)
        self.assertEqual(results["I-1"]["skipped"], "no_product_id")
        self.assertEqual(client.batch_update_products.call_args.args[0],
                         [{"id": 2000, "stock_quantity": 5}])

    def test_a_bad_product_id_poisons_only_its_own_item(self):
        self.m_get_value.side_effect = lambda dt, name, field: "not-a-number" if name == "I-1" else "2000"
        client = _client()
        results = sync.push_stock_batch(["I-1", "I-2", "I-3"], client=client, _retries=0)
        self.assertIsInstance(results["I-1"], ValueError)
        self.assertNotIsInstance(results["I-2"], Exception)
        self.assertNotIsInstance(results["I-3"], Exception)
        self.assertEqual(client.batch_update_products.call_count, 1)
        self.assertEqual(len(client.batch_update_products.call_args.args[0]), 2)

    def test_stock_read_failure_for_one_item_does_not_sink_the_chunk(self):
        def _stock(code):
            if code == "I-1":
                raise ValueError("per-serial firearm")
            return 5
        self.m_stock_for_item.side_effect = _stock
        client = _client()
        results = sync.push_stock_batch(["I-1", "I-2"], client=client, _retries=0)
        self.assertIsInstance(results["I-1"], ValueError)
        self.assertEqual(len(client.batch_update_products.call_args.args[0]), 1)

    def test_an_item_missing_from_the_response_is_reported(self):
        client = _client()
        client.batch_update_products.side_effect = lambda updates: {
            "update": [{"id": updates[0]["id"]}]}
        results = sync.push_stock_batch(["I-1", "I-2"], client=client, _retries=0)
        self.assertNotIsInstance(results["I-1"], Exception)
        self.assertIsInstance(results["I-2"], WooCommerceError)

    def test_a_malformed_response_reports_everything_instead_of_crashing(self):
        client = _client()
        client.batch_update_products.side_effect = lambda updates: ["nope"]
        results = sync.push_stock_batch(["I-1", "I-2"], client=client, _retries=0)
        self.assertTrue(all(isinstance(v, WooCommerceError) for v in results.values()))

    def test_1020_on_the_stamp_is_retried_on_a_fresh_snapshot(self):
        calls = []

        def _sv(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise frappe.QueryDeadlockError("1020")
        self.m_set_value.side_effect = _sv
        with patch("frappe.db.rollback") as rollback:
            sync.push_stock_batch(["I-1"], client=_client(), _retries=0)
        rollback.assert_called_once_with()
        self.assertEqual(len(calls), 2)


class TestPushStockBatchTask(_Base):
    def _run(self, codes, client=None):
        client = client or _client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("ffl_woo_sync.woocommerce.tasks.safe_commit"):
            tasks.push_stock_batch_task(codes, "retail")
        return client

    def test_chunks_of_100_one_request_each(self):
        client = self._run([f"I-{n}" for n in range(250)])
        self.assertEqual(client.batch_update_products.call_count, math.ceil(250 / 100))
        self.assertEqual([len(c.args[0]) for c in client.batch_update_products.call_args_list],
                         [100, 100, 50])

    def test_68_items_one_request_and_duplicates_collapse(self):
        client = self._run([f"I-{n}" for n in range(68)] + ["I-3"])
        self.assertEqual(client.batch_update_products.call_count, 1)
        self.assertEqual(len(client.batch_update_products.call_args.args[0]), 68)

    def test_timeout_reports_every_item_and_does_not_raise(self):
        client = _client()
        client.batch_update_products.side_effect = requests.exceptions.ReadTimeout("slow")
        with patch("frappe.log_error") as log_error, \
             patch("ffl_woo_sync.woocommerce.tasks._report_listing_push_failure") as report:
            self._run(["I-1", "I-2", "I-3"], client)
        self.assertEqual([c.kwargs["title"] for c in log_error.call_args_list],
                         [f"Woo push_stock_task failed: I-{n}" for n in (1, 2, 3)])
        self.assertEqual([c.args[:2] for c in report.call_args_list],
                         [("I-1", "retail"), ("I-2", "retail"), ("I-3", "retail")])
        self.assertTrue(all(isinstance(c.args[2], requests.exceptions.ReadTimeout)
                            for c in report.call_args_list))
        # a failed request is not retried or re-queued: one POST per chunk, no fallbacks
        self.assertEqual(client.batch_update_products.call_count, 1)
        client.update_product.assert_not_called()

    def test_failure_goes_through_the_listing_failed_hook(self):
        client = _client()
        client.batch_update_products.side_effect = WooCommerceError("down", status_code=500)
        hook = MagicMock()
        with patch("frappe.log_error"), \
             patch("frappe.get_hooks", return_value=["some.app.record_push_failure"]), \
             patch("frappe.get_attr", return_value=hook):
            self._run(["I-1", "I-2"], client)
        self.assertEqual([c.args[:2] for c in hook.call_args_list],
                         [("I-1", "retail"), ("I-2", "retail")])
        self.assertIn("WooCommerceError", hook.call_args.args[2])

    def test_a_bad_woo_id_reports_only_that_item(self):
        self.m_get_value.side_effect = lambda dt, name, field: "abc" if name == "I-1" else "2000"
        with patch("frappe.log_error") as log_error, \
             patch("ffl_woo_sync.woocommerce.tasks._report_listing_push_failure") as report:
            client = self._run(["I-1", "I-2"])
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0], "I-1")
        self.assertEqual(log_error.call_count, 1)
        self.assertEqual(client.batch_update_products.call_args.args[0],
                         [{"id": 2000, "stock_quantity": 5}])

    def test_per_entry_error_falls_back_to_the_single_item_push(self):
        client = _client(errors={1002: _ENTRY_ERROR})
        with patch("frappe.log_error") as log_error:
            self._run(["I-1", "I-2"], client)
        client.update_product.assert_called_once_with("1002", {"stock_quantity": 5})
        log_error.assert_not_called()

    def test_failed_fallback_is_logged_and_reported_like_a_single_push(self):
        client = _client(errors={1002: _ENTRY_ERROR})
        client.update_product.side_effect = WooCommerceError("Forbidden", status_code=403)
        with patch("frappe.log_error") as log_error, \
             patch("ffl_woo_sync.woocommerce.tasks._report_listing_push_failure") as report:
            self._run(["I-1", "I-2"], client)
        self.assertEqual(log_error.call_args.kwargs["title"], "Woo push_stock_task failed: I-2")
        report.assert_called_once()
        self.assertEqual(report.call_args.args[:2], ("I-2", "retail"))

    def test_an_unexpected_crash_still_reports_every_item_of_the_chunk(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_stock_batch",
                   side_effect=RuntimeError("x")), \
             patch("frappe.log_error"), \
             patch("ffl_woo_sync.woocommerce.tasks._report_listing_push_failure") as report, \
             patch("ffl_woo_sync.woocommerce.tasks.safe_commit"):
            tasks.push_stock_batch_task(["I-1", "I-2"], "retail")
        self.assertEqual([c.args[0] for c in report.call_args_list], ["I-1", "I-2"])

    def test_failure_records_are_committed_after_each_chunk(self):
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=_client()), \
             patch("ffl_woo_sync.woocommerce.tasks.safe_commit") as commit:
            tasks.push_stock_batch_task([f"I-{n}" for n in range(150)], "retail")
        self.assertEqual(commit.call_count, 2)
