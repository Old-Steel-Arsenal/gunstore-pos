"""Unit tests for woocommerce/client_api.py.

Exercises the routing logic inside whitelisted endpoints without making real
WooCommerce API calls or writing to a live Frappe database.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.client import WooCommerceError
from ffl_woo_sync.woocommerce.client_api import (
    push_item_now,
    push_serial_now,
    reconcile_now,
)


class TestGuardRefusalsAreReportedNotCountedAsSuccess(FrappeTestCase):
    """A refusal must never be reported as a push (review major-1).

    Both guards RETURN a skip dict rather than raising, so `_push_serial_list`
    used to file them under `pushed`. The Item form then announced a green
    "Pushed 2/2 serial(s)" over guns that were still draft on the store — the
    same silent no-op this branch exists to remove, arriving through the
    aggregate path instead of the single-gun one.

    It matters most in exactly the incident's shape: SG 551 is a per-serial
    firearm, so the documented recovery step (price it, then push) runs through
    here. A false green would have made the FIX look done while both rifles were
    still off the shop.
    """

    def _run_aggregate(self, push_result):
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("frappe.db.get_value",
                   return_value={"is_firearm": 1, "uniform_sell_price": 0}), \
             patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=["410054", "415635"]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            MockWC.from_settings.return_value = mock_client
            mock_ps.return_value = push_result
            return push_item_now("SG551")

    def test_a_refused_serial_is_not_counted_as_pushed(self):
        result = self._run_aggregate({"skipped": "delisted", "serial_no": "410054"})
        self.assertEqual(result["pushed"], [])
        self.assertEqual(len(result["skipped"]), 2)

    def test_an_aggregate_refusal_is_not_ok(self):
        """`ok` drives the form's green alert. It has to go false, or the
        operator is told the guns went up when they did not."""
        result = self._run_aggregate({"skipped": "delisted"})
        self.assertFalse(result["ok"])

    def test_each_skip_carries_a_reason_the_operator_can_act_on(self):
        """A bare "skipped" leaves the operator stuck: the Item button will
        never work for these guns, so the message has to name
        the per-gun route that does."""
        result = self._run_aggregate({"skipped": "delisted"})
        entry = result["skipped"][0]
        self.assertEqual(entry["serial_no"], "410054")
        self.assertEqual(entry["reason"], "delisted")
        self.assertIn("Serial No", entry["message"])

    def test_an_unpriced_skip_says_so(self):
        result = self._run_aggregate({"skipped": "unpriced"})
        self.assertIn("price", result["skipped"][0]["message"].lower())

    def test_a_real_push_is_still_reported_as_success(self):
        """Regression guard: the ordinary path must stay green."""
        result = self._run_aggregate({"id": 999})
        self.assertTrue(result["ok"])
        self.assertEqual(result["pushed"], ["410054", "415635"])
        self.assertEqual(result["skipped"], [])

    def test_single_gun_whitelist_path_reports_the_refusal(self):
        """The other half of major-1: push_serial_now on a refused gun."""
        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   return_value={"skipped": "unpriced", "serial_no": "410054"}):
            result = push_serial_now("410054")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skipped"], "unpriced")
        self.assertEqual(result["woo_product_id"], "")
        self.assertIn("price", result["message"].lower())


class TestPushItemNowRouting(FrappeTestCase):
    """push_item_now routes per-serial vs item-level based on item type.

    Routing rules (server-side, not JS):
    - is_firearm=1 AND uniform_sell_price=0  →  per-serial path
    - is_firearm=0 OR  uniform_sell_price=1  →  item-level path
    """

    def _item_row(
        self,
        is_firearm: int = 0,
        uniform_sell_price: int = 0,
    ) -> dict:
        return {
            "is_firearm": is_firearm,
            "uniform_sell_price": uniform_sell_price,
        }

    # ------------------------------------------------------------------
    # Per-serial path
    # ------------------------------------------------------------------

    def test_per_serial_item_pushes_serials_not_item(self):
        """push_item_now on a per-serial firearm calls sync.push_serial for each serial."""
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=1, uniform_sell_price=0)), \
             patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=["SN-A", "SN-B"]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            MockWC.from_settings.return_value = mock_client
            mock_ps.return_value = {"id": 1}
            result = push_item_now("GLOCK-19")

        # push_serial called once per active serial; push_item never called.
        self.assertEqual(mock_ps.call_count, 2)
        mock_pi.assert_not_called()

        self.assertTrue(result["ok"])
        self.assertEqual(result["pushed"], ["SN-A", "SN-B"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["total"], 2)
        self.assertNotIn("woo_product_id", result)

    def test_legacy_group_per_serial_firearm_pushes_serials_not_item(self):
        """#9 part 3 legacy pin: a legacy per-serial firearm — item_group in a
        firearm group, is_firearm not yet stamped by the auto-fill hook — routes
        to the per-serial push (push_serial per serial), not the item-level push.
        Unified onto ffl_core._is_firearm_row (flag OR firearm item_group); the
        flag-only test silently mis-routed this gun to push_item."""
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        legacy_row = {"is_firearm": 0, "item_group": "Used Firearm",
                      "uniform_sell_price": 0}
        with patch("frappe.db.get_value", return_value=legacy_row), \
             patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=["SN-A", "SN-B"]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            MockWC.from_settings.return_value = mock_client
            mock_ps.return_value = {"id": 1}
            result = push_item_now("USED-CZ-75")
        self.assertEqual(mock_ps.call_count, 2)
        mock_pi.assert_not_called()
        self.assertTrue(result["ok"])

    def test_per_serial_item_aggregates_errors(self):
        """Serial push errors are collected and returned, not raised to the caller."""
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=1, uniform_sell_price=0)), \
             patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=["SN-OK", "SN-FAIL"]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            MockWC.from_settings.return_value = mock_client
            mock_ps.side_effect = [{"id": 7}, RuntimeError("WC 503")]
            result = push_item_now("GLOCK-19")

        self.assertFalse(result["ok"])
        self.assertEqual(result["pushed"], ["SN-OK"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["serial_no"], "SN-FAIL")
        self.assertIn("WC 503", result["errors"][0]["error"])

    def test_per_serial_empty_serial_list_returns_ok(self):
        """When there are no Active serials the call is a no-op and returns ok."""
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=1, uniform_sell_price=0)), \
             patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            MockWC.from_settings.return_value = mock_client
            result = push_item_now("GLOCK-19-EMPTY")

        mock_ps.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["pushed"], [])
        self.assertEqual(result["total"], 0)

    # ------------------------------------------------------------------
    # Item-level path
    # ------------------------------------------------------------------
    #
    # `_relist=True` was ADDED to these expectations deliberately, not to make a
    # red test green. An operator pressing "Send to WooCommerce" is the clearest
    # relist there is, so push_item_now overrides the hidden-PRODUCT guard —
    # the item-level twin of push_serial_now, which has passed `_relist=True`
    # since the sentinel fix. The PRICE guard is not overridable and still applies, so
    # this cannot put a $0 product back on the shop.

    def test_non_firearm_delegates_to_push_item(self):
        """push_item_now on a non-firearm calls sync.push_item."""
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=0, uniform_sell_price=0)), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            mock_pi.return_value = {"id": 42}
            result = push_item_now("AMMO-9MM")

        mock_pi.assert_called_once_with("AMMO-9MM", site="retail", _relist=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["woo_product_id"], "42")
        self.assertNotIn("pushed", result)

    def test_uniform_price_firearm_delegates_to_push_item(self):
        """push_item_now on a uniform-price firearm calls sync.push_item (not serials)."""
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=1, uniform_sell_price=1)), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            mock_pi.return_value = {"id": 99}
            result = push_item_now("GLOCK-UNIFORM")

        mock_pi.assert_called_once_with("GLOCK-UNIFORM", site="retail", _relist=True)
        self.assertEqual(result["woo_product_id"], "99")
        self.assertNotIn("pushed", result)

    def test_missing_item_row_treats_as_non_firearm(self):
        """If frappe.db.get_value returns None the item is treated as non-firearm."""
        with patch("frappe.db.get_value", return_value=None), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            mock_pi.return_value = {"id": 1}
            result = push_item_now("UNKNOWN-ITEM")

        mock_pi.assert_called_once_with("UNKNOWN-ITEM", site="retail", _relist=True)
        self.assertEqual(result["woo_product_id"], "1")


# ---------------------------------------------------------------------------
# site threading — retail is the only store (the dealer store left in 1.11.0)
# ---------------------------------------------------------------------------


class TestSiteThreading(FrappeTestCase):
    """The site parameter is forwarded, and only "retail" gets that far."""

    def _item_row(self, is_firearm: int = 0, uniform_sell_price: int = 0) -> dict:
        return {"is_firearm": is_firearm, "uniform_sell_price": uniform_sell_price}

    def test_push_item_now_threads_site_to_sync_non_firearm(self):
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=0, uniform_sell_price=0)), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            mock_pi.return_value = {"id": 42}
            result = push_item_now("AMMO-9MM", site="retail")
        mock_pi.assert_called_once_with("AMMO-9MM", site="retail", _relist=True)
        self.assertTrue(result["ok"])

    def test_push_item_now_per_serial_builds_the_retail_client(self):
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("frappe.db.get_value",
                   return_value=self._item_row(is_firearm=1, uniform_sell_price=0)), \
             patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial"):
            MockWC.from_settings.return_value = mock_client
            push_item_now("GLOCK-19")
        MockWC.from_settings.assert_called_once_with("retail")

    def test_an_unknown_store_is_refused_before_anything_is_read(self):
        with patch("frappe.db.get_value") as gv, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            with self.assertRaises(frappe.ValidationError):
                push_item_now("GLOCK-19", site="wholesale")
        gv.assert_not_called()
        mock_pi.assert_not_called()


class TestRetailRouting(FrappeTestCase):
    """The retail store's push / reconcile routes, end to end through the endpoint."""

    def _item_row(self, is_firearm: int = 0, uniform_sell_price: int = 0) -> dict:
        return {"is_firearm": is_firearm, "uniform_sell_price": uniform_sell_price}

    def test_push_item_now_retail(self):
        with patch("frappe.db.get_value", return_value=self._item_row()), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            mock_pi.return_value = {"id": 7}
            result = push_item_now("GLOCK-19", site="retail")
        mock_pi.assert_called_once()
        self.assertTrue(result["ok"])

    def test_push_serial_now_retail(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            mock_ps.return_value = {"id": 5}
            result = push_serial_now("SN-001", site="retail")
        mock_ps.assert_called_once_with("SN-001", site="retail", _relist=True)
        self.assertTrue(result["ok"])

    def test_reconcile_now_retail(self):
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.sync.push_item",
                   side_effect=ValueError("per-serial")):
            MockWC.from_settings.return_value = mock_client
            result = reconcile_now("AMMO-9MM", site="retail")
        MockWC.from_settings.assert_called_once_with("retail")
        self.assertIsNotNone(result)

    def test_reconcile_now_buckets_a_dangling_404_and_does_not_error(self):
        """#9 part 2: a dangling product_id (the WC product was deleted → push_item
        raises WooCommerceError 404) is reported in its own `dangling` bucket, not
        as item_error, and never fails the reconcile or re-creates the product —
        deletion on Woo may be deliberate; cleanup is woo_audit_dangling_ids."""
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.sync.push_item",
                   side_effect=WooCommerceError("PUT /products/9 -> 404", status_code=404)):
            MockWC.from_settings.return_value = mock_client
            result = reconcile_now("DEAD-ITEM", site="retail")
        self.assertIsNone(result["item_error"])
        self.assertIsNotNone(result["dangling"])
        self.assertEqual(result["dangling"]["item_code"], "DEAD-ITEM")
        self.assertTrue(result["ok"])

    def test_reconcile_now_still_reports_a_non_404_push_error(self):
        """A non-404 push failure stays a real item_error (and ok=False) — only the
        dangling 404 is special-cased."""
        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
             patch("frappe.get_all", return_value=[]), \
             patch("ffl_woo_sync.woocommerce.sync.push_item",
                   side_effect=WooCommerceError("500 boom", status_code=500)):
            MockWC.from_settings.return_value = mock_client
            result = reconcile_now("SICK-ITEM", site="retail")
        self.assertIsNone(result["dangling"])
        self.assertIn("500 boom", result["item_error"])
        self.assertFalse(result["ok"])
