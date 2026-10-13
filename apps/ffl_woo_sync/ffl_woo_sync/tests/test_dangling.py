"""Unit tests for woo_audit_dangling_ids — the dangling Woo product-id GC (#9 part 1).

The WooCommerce client is fully mocked; no real API calls, no DB writes (the fix
path's set_value / add_comment / logger are all patched, so nothing commits —
test discipline, docs/testing.md).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.client import WooCommerceConfigError, WooCommerceError
from ffl_woo_sync.woocommerce.dangling import (
    _clamp_batch_size,
    woo_audit_dangling_ids,
)


def _client_where(live_ids):
    """A mock WC client whose get_product returns a product for a live id and
    raises a 404 WooCommerceError for anything else (a product deleted on Woo)."""
    client = MagicMock()

    def _get_product(woo_id):
        if str(woo_id) in live_ids:
            return {"id": woo_id}
        raise WooCommerceError(f"GET products/{woo_id} -> 404", status_code=404)

    client.get_product.side_effect = _get_product
    return client


class TestClampBatchSize(FrappeTestCase):
    def test_defaults_to_50_when_falsy(self):
        self.assertEqual(_clamp_batch_size(0), 50)
        self.assertEqual(_clamp_batch_size(""), 50)

    def test_caps_at_200(self):
        self.assertEqual(_clamp_batch_size(10 ** 6), 200)

    def test_floors_at_1(self):
        self.assertEqual(_clamp_batch_size(-5), 1)

    def test_passes_a_normal_value(self):
        self.assertEqual(_clamp_batch_size("50"), 50)


class TestWooAuditDanglingIds(FrappeTestCase):
    def test_requires_system_manager(self):
        """The whitelisted entry point is HTTP-reachable and fix=True is a write —
        it must gate on System Manager before anything else."""
        with patch("frappe.only_for") as mock_of, \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient"):
            woo_audit_dangling_ids(site="retail")
        mock_of.assert_called_once_with("System Manager")

    def test_reports_dangling_but_does_not_clear_by_default(self):
        def _get_all(doctype, filters=None, fields=None, **k):
            if doctype == "Item":
                return [{"name": "ITEM-LIVE", "woo_product_id": "WC-LIVE"},
                        {"name": "ITEM-DEAD", "woo_product_id": "WC-DEAD"}]
            return []

        client = _client_where({"WC-LIVE"})
        with patch("frappe.only_for"), \
             patch("frappe.get_all", side_effect=_get_all), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient") as MockWC, \
             patch("frappe.db.set_value") as mock_sv:
            MockWC.from_settings.return_value = client
            result = woo_audit_dangling_ids(site="retail")

        self.assertEqual([d["name"] for d in result["dangling"]], ["ITEM-DEAD"])
        self.assertEqual(result["dangling"][0]["field"], "woo_product_id")
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["fixed"], 0)
        mock_sv.assert_not_called()  # report-only: nothing cleared

    def test_fix_clears_the_id_with_comment_and_log(self):
        def _get_all(doctype, filters=None, fields=None, **k):
            if doctype == "Serial No":
                return [{"name": "SN-DEAD", "woo_product_id": "WC-DEAD"}]
            return []

        client = _client_where(set())  # every id dangling
        mock_doc = MagicMock()
        with patch("frappe.only_for"), \
             patch("frappe.get_all", side_effect=_get_all), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient") as MockWC, \
             patch("frappe.db.set_value") as mock_sv, \
             patch("frappe.get_doc", return_value=mock_doc), \
             patch("frappe.logger"):
            MockWC.from_settings.return_value = client
            result = woo_audit_dangling_ids(fix=1, site="retail")

        self.assertEqual(result["fixed"], 1)
        mock_sv.assert_called_once_with(
            "Serial No", "SN-DEAD", "woo_product_id", None, update_modified=False)
        mock_doc.add_comment.assert_called_once()

    def test_a_non_404_error_is_not_swallowed_as_dangling(self):
        """A dangling id is a 404 only. A 500 (or any other API failure) is a real
        problem and must propagate, never be recorded as 'deleted'."""
        def _get_all(doctype, filters=None, fields=None, **k):
            if doctype == "Item":
                return [{"name": "ITEM-X", "woo_product_id": "WC-X"}]
            return []

        client = MagicMock()
        client.get_product.side_effect = WooCommerceError("500 boom", status_code=500)
        with patch("frappe.only_for"), \
             patch("frappe.get_all", side_effect=_get_all), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient") as MockWC:
            MockWC.from_settings.return_value = client
            with self.assertRaises(WooCommerceError):
                woo_audit_dangling_ids(site="retail")

    def test_scans_item_and_serial_no_on_the_retail_store(self):
        """Default (no site) covers both paths: Item and Serial No, on the retail
        store's woo_product_id — the only store there is."""
        scanned = set()

        def _get_all(doctype, filters=None, fields=None, **k):
            for field in (filters or {}):
                scanned.add((doctype, field))
            return []

        with patch("frappe.only_for"), \
             patch("frappe.get_all", side_effect=_get_all), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient"):
            woo_audit_dangling_ids()

        self.assertEqual(scanned, {("Item", "woo_product_id"), ("Serial No", "woo_product_id")})


class TestAStoreWithNoUsableClient(FrappeTestCase):
    """A store whose client cannot be built (missing or broken settings).

    The audit is the top of its own call stack, so a config error raised out of
    here reaches the request boundary as a 500 and takes the whole transaction
    with it. So the store is reported as skipped, not raised.
    """

    @staticmethod
    def _rows(doctype, filters=None, fields=None, **k):
        """One candidate on each store's field, so both are actually reached."""
        if doctype == "Item":
            field = next(iter(filters or {"woo_product_id": None}))
            return [{"name": f"ITEM-{field}", field: f"WC-{field}"}]
        return []

    def _run(self, fix=0):
        def _from_settings(site):
            raise WooCommerceConfigError("Consumer Key is missing.")

        with patch("frappe.only_for"), \
             patch("frappe.get_all", side_effect=self._rows), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient") as MockWC, \
             patch("frappe.db.set_value") as set_value, \
             patch("frappe.get_doc", return_value=MagicMock()), \
             patch("frappe.logger"):
            MockWC.from_settings.side_effect = _from_settings
            return woo_audit_dangling_ids(fix=fix), set_value

    def test_the_unusable_store_is_reported_not_raised(self):
        result, _ = self._run()

        self.assertEqual([s["site"] for s in result["skipped"]], ["retail"])
        self.assertIn("Consumer Key", result["skipped"][0]["reason"])
        self.assertEqual(result["checked"], 0)

    def test_a_store_this_pos_does_not_run_is_refused(self):
        """site= names a store that is not connected: a ValidationError, before any
        record is read or any client built. (The message's translation lookup may
        read the Translation table on a cold cache — that is not a record read.)"""
        with patch("frappe.only_for"), \
             patch("frappe.get_all") as get_all, \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient") as MockWC:
            with self.assertRaises(frappe.ValidationError):
                woo_audit_dangling_ids(site="dealer")
        self.assertFalse([c for c in get_all.call_args_list
                          if c.args and c.args[0] in ("Item", "Serial No")])
        MockWC.from_settings.assert_not_called()

    def test_a_store_with_nothing_listed_needs_no_client_at_all(self):
        """The laziness is load-bearing, not incidental: a shop that has never
        listed anything on a store must not be told to configure it."""
        with patch("frappe.only_for"), \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.dangling.WooCommerceClient") as MockWC:
            MockWC.from_settings.side_effect = AssertionError("built a client")
            result = woo_audit_dangling_ids()

        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["checked"], 0)
