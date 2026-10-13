"""Tests for woocommerce/tasks.py.

Covers:
- _due() throttle function
- scheduled_reconcile() gate logic (auto_sync_enabled, frequency, _due)
- _run_reconcile() drift detection and push behaviour (passes 1-3)
- push_*_task() error absorption and frappe.log_error logging
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from ffl_woo_sync.woocommerce.tasks import (
    _RECONCILE_MIN_INTERVAL,
    _due,
    push_item_task,
    push_serial_task,
    push_stock_task,
    scheduled_reconcile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(
    auto_sync_enabled=1,
    reconcile_frequency="Daily",
    last_reconcile_at=None,
):
    s = MagicMock()
    s.auto_sync_enabled = auto_sync_enabled
    s.reconcile_frequency = reconcile_frequency
    s.last_reconcile_at = last_reconcile_at
    return s


# ---------------------------------------------------------------------------
# _due()
# ---------------------------------------------------------------------------

class TestDue(FrappeTestCase):
    def test_returns_true_when_no_last_dt(self):
        self.assertTrue(_due(None, 3600))

    def test_returns_true_when_empty_string(self):
        self.assertTrue(_due("", 3600))

    def test_returns_true_when_elapsed(self):
        # 3 hours ago; interval is 1 hour → due
        last = add_to_date(now_datetime(), hours=-3)
        self.assertTrue(_due(last, 3600))

    def test_returns_false_when_not_elapsed(self):
        # 1 minute ago; interval is 1 hour → not due
        last = add_to_date(now_datetime(), minutes=-1)
        self.assertFalse(_due(last, 3600))

    def test_returns_true_exactly_at_boundary(self):
        # Exactly interval seconds ago → boundary is inclusive (>=)
        last = add_to_date(now_datetime(), seconds=-3600)
        self.assertTrue(_due(last, 3600))


# ---------------------------------------------------------------------------
# scheduled_reconcile() gate
# ---------------------------------------------------------------------------

class TestScheduledReconcileGate(FrappeTestCase):
    def _call_with_settings(self, s, run_mock=None):
        """Run scheduled_reconcile with *s* as the retail settings; *run_mock*
        stands in for queueing the sweep (test_reconcile_long_batch pins the queue)."""
        if run_mock is None:
            run_mock = MagicMock()

        with patch("frappe.get_single", return_value=s), \
             patch("ffl_woo_sync.woocommerce.tasks.enqueue_reconcile", run_mock):
            scheduled_reconcile()
        return run_mock

    def test_skips_when_auto_sync_disabled(self):
        s = _settings(auto_sync_enabled=0)
        mock = self._call_with_settings(s)
        mock.assert_not_called()

    def test_skips_when_auto_sync_is_zero_string(self):
        s = _settings(auto_sync_enabled="0")
        mock = self._call_with_settings(s)
        mock.assert_not_called()

    def test_skips_when_frequency_is_disabled(self):
        s = _settings(reconcile_frequency="Disabled")
        mock = self._call_with_settings(s)
        mock.assert_not_called()

    def test_skips_when_not_due(self):
        """Last reconcile was 1 minute ago; Daily interval → not due."""
        last = add_to_date(now_datetime(), minutes=-1)
        s = _settings(reconcile_frequency="Daily", last_reconcile_at=last)
        mock = self._call_with_settings(s)
        mock.assert_not_called()

    def test_runs_when_due_daily(self):
        """Last reconcile was 25 hours ago; Daily interval → due."""
        last = add_to_date(now_datetime(), hours=-25)
        s = _settings(reconcile_frequency="Daily", last_reconcile_at=last)
        mock = self._call_with_settings(s)
        mock.assert_called_once()

    def test_runs_when_no_last_reconcile_at(self):
        """No previous reconcile → always due."""
        s = _settings(last_reconcile_at=None)
        mock = self._call_with_settings(s)
        mock.assert_called_once()

    def test_runs_when_due_every_2_hours(self):
        last = add_to_date(now_datetime(), hours=-3)
        s = _settings(reconcile_frequency="Every 2 Hours", last_reconcile_at=last)
        mock = self._call_with_settings(s)
        mock.assert_called_once()

    def test_skips_when_not_due_every_2_hours(self):
        """Only 30 minutes since last reconcile; 2-hour interval → not due."""
        last = add_to_date(now_datetime(), minutes=-30)
        s = _settings(reconcile_frequency="Every 2 Hours", last_reconcile_at=last)
        mock = self._call_with_settings(s)
        mock.assert_not_called()

    def test_runs_when_due_weekly(self):
        last = add_to_date(now_datetime(), days=-8)
        s = _settings(reconcile_frequency="Weekly", last_reconcile_at=last)
        mock = self._call_with_settings(s)
        mock.assert_called_once()

    def test_skips_when_not_due_weekly(self):
        last = add_to_date(now_datetime(), days=-2)
        s = _settings(reconcile_frequency="Weekly", last_reconcile_at=last)
        mock = self._call_with_settings(s)
        mock.assert_not_called()

    def test_min_interval_map_covers_all_select_options(self):
        """_RECONCILE_MIN_INTERVAL has an entry for each non-Disabled frequency."""
        for freq in ("Every 2 Hours", "Daily", "Weekly"):
            self.assertIn(freq, _RECONCILE_MIN_INTERVAL, f"missing key: {freq!r}")


# ---------------------------------------------------------------------------
# _run_reconcile() drift detection
# ---------------------------------------------------------------------------

class TestRunReconcile(FrappeTestCase):
    """Pass-2 tests here patch ``sync.effective_serial_price`` to a real price.

    These cases are about STOCK / override drift, but Pass 2 now asks the price
    first: an unpriced serial is drafted regardless of drift, because
    being unpriced is itself the defect — that is the SG 551 case, where POS and
    WC agreed perfectly and the $0 listing simply stayed up.

    Unpatched, the helper reads a real DB where these fixture serials do not
    exist, resolves 0, and every one of them takes the unpriced branch instead of
    the branch under test. CI caught this before the local run did: the local DB
    happened to hold leftover rows that masked it.
    """

    def setUp(self):
        super().setUp()
        # These tests patch frappe.get_all with a fixed side_effect list. Building the
        # Serial No meta (reservations.channel_reserve_columns) also calls get_all, so
        # in a fresh CI worker that runs this module first it would eat a mocked answer.
        frappe.get_meta("Serial No")

    def _make_client(self):
        client = MagicMock()
        client.get_attribute_ids.return_value = {}
        return client

    def test_writes_status_after_config_error(self):
        from ffl_woo_sync.woocommerce.client import WooCommerceConfigError
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   side_effect=WooCommerceConfigError("not configured")), \
             patch("frappe.db.set_value") as mock_sv:
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_sv.assert_called_once()
        written = mock_sv.call_args[0][2]
        self.assertIn("last_reconcile_at", written)
        self.assertIn("not configured", written.get("last_sync_error", ""))

    def test_pushes_item_when_stock_drifted(self):
        client = self._make_client()
        # WC says stock=0; POS says stock=5 → drift → push
        client.get_product.return_value = {"stock_quantity": 0, "regular_price": "100.00"}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=5), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                # pass 1: items query
                [frappe._dict(
                    name="ITEM-001",
                    woo_product_id="WC-1",
                    is_firearm=0,
                    uniform_sell_price=0,
                    standard_rate=100.0,
                )],
                [],  # pass 2: active listed serials
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b: inactive listed serials
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_pi.assert_called_once_with("ITEM-001", client=client, attr_ids={}, site="retail",
                                        pos_fields_only=True)

    def test_unpriced_item_guard_push_is_lean_too(self):
        """Pass 1's $0 guard routes through push_item (which refuses and
        drafts) — that call must carry pos_fields_only like every other
        machine push from the sweep."""
        client = self._make_client()
        client.get_product.return_value = {"stock_quantity": 5, "regular_price": "0.00"}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [frappe._dict(
                    name="ITEM-001",
                    woo_product_id="WC-1",
                    is_firearm=0,
                    uniform_sell_price=0,
                    standard_rate=0.0,  # unpriced → the guard branch
                )],
                [],  # pass 2
                [],  # pass 3a
                [],  # pass 3b
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_pi.assert_called_once_with("ITEM-001", client=client, attr_ids={},
                                        site="retail", pos_fields_only=True)

    def test_skips_item_when_no_drift(self):
        client = self._make_client()
        # WC matches POS exactly → no push
        client.get_product.return_value = {"stock_quantity": 5, "regular_price": "100.00"}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=5), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [frappe._dict(
                    name="ITEM-001",
                    woo_product_id="WC-1",
                    is_firearm=0,
                    uniform_sell_price=0,
                    standard_rate=100.0,
                )],
                [],  # pass 2
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_pi.assert_not_called()

    def test_skips_per_serial_item_in_pass_1(self):
        """Per-serial firearms (no item-level WC product) are skipped in pass 1."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [frappe._dict(
                    name="ITEM-001",
                    woo_product_id="WC-1",
                    is_firearm=1,
                    uniform_sell_price=0,  # per-serial
                    standard_rate=0.0,
                )],
                [],  # pass 2
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_pi.assert_not_called()
        client.get_product.assert_not_called()

    def test_skips_a_legacy_group_per_serial_firearm_in_pass_1(self):
        """#9 part 3 legacy pin: a legacy per-serial firearm — item_group in a
        firearm group, is_firearm not yet stamped by the auto-fill hook — has no
        item-level WC product and must be skipped in pass 1, exactly like a
        flagged one. Unified onto ffl_core._is_firearm_row; the flag-only skip
        reconciled it as an item-level product and hit stock_for_item's per-serial
        ValueError."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [frappe._dict(
                    name="USED-CZ-75",
                    woo_product_id="WC-1",
                    is_firearm=0,
                    item_group="Used Firearm",
                    uniform_sell_price=0,  # per-serial (legacy: group-only firearm)
                    standard_rate=0.0,
                )],
                [],  # pass 2
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_pi.assert_not_called()
        client.get_product.assert_not_called()

    def test_pushes_serial_when_stock_drifted(self):
        """Pass 2: serial WC stock=0 but not reserved → pos_stock=1 → push."""
        client = self._make_client()
        client.get_product.return_value = {"stock_quantity": 0, "regular_price": ""}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps, \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=1200.0), \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [],  # pass 1: items query
                [frappe._dict(name="SN-001", woo_product_id="WC-SN", woo_reserved=0)],  # pass 2
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b: no inactive listed serials
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_ps.assert_called_once_with("SN-001", client=client, attr_ids={}, site="retail",
                                        pos_fields_only=True)

    def test_writes_push_count_and_reconcile_at(self):
        """last_push_count and last_reconcile_at are always written."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all", side_effect=[[], [], [], []]), \
             patch("frappe.db.set_value") as mock_sv:
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_sv.assert_called_once()
        written = mock_sv.call_args[0][2]
        self.assertIn("last_reconcile_at", written)
        self.assertIn("last_push_count", written)
        self.assertEqual(written["last_push_count"], 0)

    def test_writes_last_push_at_only_when_pushed(self):
        client = self._make_client()
        # Stock drifted → one push → last_push_at should be set
        client.get_product.return_value = {"stock_quantity": 99, "regular_price": "0.00"}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=5), \
             patch("ffl_woo_sync.woocommerce.sync.push_item"), \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = [
                [frappe._dict(
                    name="ITEM-001", woo_product_id="WC-1",
                    # priced: standard_rate 0 now MEANS "unpriced" and routes to
                    # the unpriced-listing guard instead of the drift check this test is about
                    is_firearm=0, uniform_sell_price=0, standard_rate=100.0,
                )],
                [],  # pass 2
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        written = mock_sv.call_args[0][2]
        self.assertIn("last_push_at", written)

    def test_collects_errors_without_stopping(self):
        """An error on one item should not prevent other items from being reconciled."""
        client = self._make_client()
        # First item raises; second succeeds (no drift)
        client.get_product.side_effect = [
            RuntimeError("WC down"),
            {"stock_quantity": 0, "regular_price": "100.00"},
        ]
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=0), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = [
                [
                    frappe._dict(
                        name="ITEM-BAD", woo_product_id="WC-BAD",
                        is_firearm=0, uniform_sell_price=0, standard_rate=100.0,
                    ),
                    frappe._dict(
                        # priced, and matching WC below, so this stays the
                        # "no drift → no push" case the assertion is about
                        name="ITEM-OK", woo_product_id="WC-OK",
                        is_firearm=0, uniform_sell_price=0, standard_rate=100.0,
                    ),
                ],
                [],  # pass 2
                [],  # pass 3a: sold serials (archive arm)
                [],  # pass 3b
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        # ITEM-OK had no drift so push_item not called; ITEM-BAD errored
        mock_pi.assert_not_called()
        # Error text written to settings
        written = mock_sv.call_args[0][2]
        self.assertIn("ITEM-BAD", written.get("last_sync_error", ""))

    def test_pass3b_delists_inactive_listed_serials(self):
        """Pass 3b: serials with woo_product_id that went non-Active WITHOUT
        selling are delisted.

        The example is Inactive, not Delivered: since the SOLD-archive split
 a Delivered serial belongs to Pass 3a, which keeps its page
        live instead of drafting it — see tests/test_sold_archive.py."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_ds, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [],   # pass 1: no listed items
                [],   # pass 2: no active listed serials
                [],  # pass 3a: sold serials (archive arm)
                # pass 3b: one withdrawn serial still has a woo_product_id
                [frappe._dict(name="SN-GONE", woo_product_id="WC-GONE", status="Inactive")],
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_ds.assert_called_once_with("SN-GONE", client=client, site="retail")

    def test_pass3_counts_delist_in_push_count(self):
        """Pass 3 delists increment last_push_count and set last_push_at."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial"), \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = [
                [],
                [],
                [],  # pass 3a: sold serials (archive arm)
                [frappe._dict(name="SN-GONE", woo_product_id="WC-GONE", status="Inactive")],
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        written = mock_sv.call_args[0][2]
        self.assertEqual(written["last_push_count"], 1)
        self.assertIn("last_push_at", written)

    def test_pass3_skips_already_delisted_serials(self):
        """Pass 3b's query includes woo_delisted=0; already-delisted serials absent."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_ds, \
             patch("frappe.db.set_value"):
            # Pass 3b returns empty because woo_delisted=0 filter excludes the serial
            mock_ga.side_effect = [[], [], [], []]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        # Verify the Pass 3b get_all call included woo_delisted=0 in its filters
        pass3b_call = mock_ga.call_args_list[3]
        filters = pass3b_call[1].get("filters") or pass3b_call[0][1]
        self.assertEqual(filters.get("woo_delisted"), 0)
        mock_ds.assert_not_called()

    def test_pass3_already_delisted_not_counted_in_push_count(self):
        """When Pass 3 returns no rows (all already delisted), push_count stays 0."""
        client = self._make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial"), \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = [[], [], [], []]  # no work in any pass
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        written = mock_sv.call_args[0][2]
        self.assertEqual(written["last_push_count"], 0)
        self.assertNotIn("last_push_at", written)

# ---------------------------------------------------------------------------
# push_*_task() error absorption
# ---------------------------------------------------------------------------

class TestPushTaskErrorAbsorption(FrappeTestCase):
    def test_push_item_task_absorbs_exception(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_item",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error"), \
             patch("frappe.get_traceback", return_value="tb"):
            # Should not raise
            push_item_task("ITEM-001")

    def test_push_item_task_logs_error(self):
        """push_item_task calls frappe.log_error when sync raises."""
        with patch("ffl_woo_sync.woocommerce.sync.push_item",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_traceback", return_value="traceback text"):
            push_item_task("ITEM-001")
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        self.assertIn("ITEM-001", title)

    def test_push_serial_task_absorbs_exception(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error"), \
             patch("frappe.get_traceback", return_value="tb"):
            push_serial_task("SN-001")

    def test_push_serial_task_logs_error(self):
        """push_serial_task calls frappe.log_error when sync raises."""
        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_traceback", return_value="traceback text"):
            push_serial_task("SN-001")
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        self.assertIn("SN-001", title)

    def test_push_stock_task_absorbs_exception(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_stock",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error"), \
             patch("frappe.get_traceback", return_value="tb"):
            push_stock_task("ITEM-001")

    def test_push_stock_task_logs_error(self):
        """push_stock_task calls frappe.log_error when sync raises."""
        with patch("ffl_woo_sync.woocommerce.sync.push_stock",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_traceback", return_value="traceback text"):
            push_stock_task("ITEM-001")
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        self.assertIn("ITEM-001", title)

    def test_push_item_task_calls_sync(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            push_item_task("ITEM-001")
        mock_pi.assert_called_once_with("ITEM-001", site="retail", pos_fields_only=True)

    def test_push_serial_task_calls_sync(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            push_serial_task("SN-001")
        mock_ps.assert_called_once_with("SN-001", site="retail", _relist=False,
                                        pos_fields_only=True)

    def test_push_stock_task_calls_sync(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_stock") as mock_pst:
            push_stock_task("ITEM-001")
        mock_pst.assert_called_once_with("ITEM-001", site="retail")


# ---------------------------------------------------------------------------
# delist_serial_task / delist_item_task — new tasks for cross-site delist
# ---------------------------------------------------------------------------

class TestDelistTasks(FrappeTestCase):
    def test_delist_serial_task_calls_sync_with_site(self):
        """delist_serial_task passes site to sync.delist_serial."""
        with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_ds:
            from ffl_woo_sync.woocommerce.tasks import delist_serial_task
            delist_serial_task("S1", site="retail")
        mock_ds.assert_called_once_with("S1", site="retail")

    def test_delist_serial_task_default_site_is_retail(self):
        """delist_serial_task defaults to site='retail'."""
        with patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_ds:
            from ffl_woo_sync.woocommerce.tasks import delist_serial_task
            delist_serial_task("S1")
        mock_ds.assert_called_once_with("S1", site="retail")

    def test_delist_serial_task_absorbs_exception(self):
        with patch("ffl_woo_sync.woocommerce.sync.delist_serial",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error"), \
             patch("frappe.get_traceback", return_value="tb"):
            from ffl_woo_sync.woocommerce.tasks import delist_serial_task
            delist_serial_task("S1")  # must not raise

    def test_delist_serial_task_logs_error(self):
        with patch("ffl_woo_sync.woocommerce.sync.delist_serial",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_traceback", return_value="traceback text"):
            from ffl_woo_sync.woocommerce.tasks import delist_serial_task
            delist_serial_task("S1", site="retail")
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        self.assertIn("S1", title)

    def test_delist_item_task_calls_sync_with_site(self):
        """delist_item_task passes site to sync.delist_item."""
        with patch("ffl_woo_sync.woocommerce.sync.delist_item") as mock_di:
            from ffl_woo_sync.woocommerce.tasks import delist_item_task
            delist_item_task("X", site="retail")
        mock_di.assert_called_once_with("X", site="retail")

    def test_delist_item_task_default_site_is_retail(self):
        """delist_item_task defaults to site='retail'."""
        with patch("ffl_woo_sync.woocommerce.sync.delist_item") as mock_di:
            from ffl_woo_sync.woocommerce.tasks import delist_item_task
            delist_item_task("X")
        mock_di.assert_called_once_with("X", site="retail")

    def test_delist_item_task_absorbs_exception(self):
        with patch("ffl_woo_sync.woocommerce.sync.delist_item",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error"), \
             patch("frappe.get_traceback", return_value="tb"):
            from ffl_woo_sync.woocommerce.tasks import delist_item_task
            delist_item_task("X")  # must not raise

    def test_delist_item_task_logs_error(self):
        with patch("ffl_woo_sync.woocommerce.sync.delist_item",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log, \
             patch("frappe.get_traceback", return_value="traceback text"):
            from ffl_woo_sync.woocommerce.tasks import delist_item_task
            delist_item_task("X", site="retail")
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        title = call_kwargs[1].get("title") or call_kwargs[0][0]
        self.assertIn("X", title)


# ---------------------------------------------------------------------------
# scheduled_reconcile() — the retail store is the only one swept
# ---------------------------------------------------------------------------

class TestScheduledReconcileRetailOnly(FrappeTestCase):
    def test_scheduled_reconcile_runs_the_retail_store_only(self):
        enabled = _settings(auto_sync_enabled=1, reconcile_frequency="Daily", last_reconcile_at=None)
        run_mock = MagicMock()
        with patch("frappe.get_single", return_value=enabled) as get_single, \
             patch("ffl_woo_sync.woocommerce.tasks.enqueue_reconcile", run_mock):
            scheduled_reconcile()
        run_mock.assert_called_once_with("retail")
        self.assertEqual({c.args[0] for c in get_single.call_args_list}, {"WooCommerce Settings"})
