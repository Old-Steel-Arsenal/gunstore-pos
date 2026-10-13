"""RSR ``distributor_stock_source`` hook on ``mapping.stock_for_item``.

The hook table and call convention, plus the rule that reconcile must never zero
a dropship item's stock.

Contract under test
-------------------
``fn(item_code: str) -> int | None``, registered by **ffl_integrations**.  The
resolution loop is the **first statement** of :func:`stock_for_item` — ahead of
the per-serial-firearm ``ValueError`` and ahead of every local branch.  The
first non-``None`` return wins; ``None`` (or no registered hook at all) falls
back to the existing local rules, unchanged.

Why the single point matters: all three stock writers — ``push_stock``,
reconcile Pass 1, and the SLE doc_event — read their quantity from
``stock_for_item``.  Fixing it here fixes all three at once; a dropship item's
quantity therefore cannot be overwritten by its (always 0) local Bin.

No ``ffl_integrations`` import happens in this file: the fake sources below are
registered by their **real dotted paths** through a patched ``frappe.get_hooks``,
so production's ``frappe.get_attr`` resolution runs for real.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.mapping import stock_for_item

_MOD = "ffl_woo_sync.tests.test_distributor_stock_hook"

# Call log — lets tests assert WHICH sources ran (first-non-None short circuit).
CALLS: list[tuple[str, str]] = []


# ---------------------------------------------------------------------------
# Fake distributor stock sources (stand-ins for the ffl_integrations hook)
# ---------------------------------------------------------------------------

def source_five(item_code: str):
    CALLS.append(("five", item_code))
    return 5


def source_seven(item_code: str):
    CALLS.append(("seven", item_code))
    return 7


def source_none(item_code: str):
    """Not a dropship item for this source → fall through to the next / local."""
    CALLS.append(("none", item_code))
    return None


def source_zero(item_code: str):
    """Watchdog pressed the feed to 0 — a real answer, NOT a fallback signal."""
    CALLS.append(("zero", item_code))
    return 0


def source_float(item_code: str):
    CALLS.append(("float", item_code))
    return 4.7


def source_negative(item_code: str):
    """`min(RSR qty − buffer, cap)` goes negative whenever qty < buffer."""
    CALLS.append(("negative", item_code))
    return -2


def source_boom(item_code: str):
    CALLS.append(("boom", item_code))
    raise RuntimeError("distributor feed down")


_REAL_GET_HOOKS = frappe.get_hooks


def patch_hooks(*paths: str):
    """Patch ``frappe.get_hooks`` so ``distributor_stock_source`` → ``paths``.

    Every other hook name delegates to the real implementation, so nothing else
    in the call path changes behaviour.
    """
    def fake(hook=None, *args, **kwargs):
        if hook == "distributor_stock_source":
            return list(paths)
        return _REAL_GET_HOOKS(hook, *args, **kwargs)

    return patch("frappe.get_hooks", side_effect=fake)


def _item_row(is_firearm=0, item_group="Accessories", uniform_sell_price=0):
    return {"is_firearm": is_firearm, "item_group": item_group,
            "uniform_sell_price": uniform_sell_price}


def setUpModule():
    # These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
    # the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
    from ffl_core.tests.channel_query_doubles import warm_channel_meta

    warm_channel_meta()
    # now_datetime() reads the System Settings time zone the same way.
    frappe.utils.now_datetime()


# ===========================================================================
# The hook itself
# ===========================================================================

class TestDistributorStockSourceHook(FrappeTestCase):
    def setUp(self):
        CALLS.clear()

    def test_hook_value_wins_over_local_bin(self):
        with patch_hooks(f"{_MOD}.source_five"), \
             patch("frappe.db.get_value") as mock_gv, \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock") as mock_bin:
            self.assertEqual(stock_for_item("RSR-ACC-1"), 5)
        # The loop is the FIRST statement: neither the Item row read nor the Bin
        # sum may run once a distributor answered.
        mock_gv.assert_not_called()
        mock_bin.assert_not_called()
        self.assertEqual(CALLS, [("five", "RSR-ACC-1")])

    def test_zero_is_an_answer_not_a_fallback(self):
        """Watchdog presses a stale feed to 0 — that 0 must reach WooCommerce."""
        with patch_hooks(f"{_MOD}.source_zero"), \
             patch("frappe.db.get_value", return_value=_item_row()), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"RSR-ACC-1": 12.0}) as mock_bin:
            self.assertEqual(stock_for_item("RSR-ACC-1"), 0)
        mock_bin.assert_not_called()

    def test_none_falls_back_to_local(self):
        with patch_hooks(f"{_MOD}.source_none"), \
             patch("frappe.db.get_value", return_value=_item_row()), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"ACC-001": 12.0}), \
             patch("ffl_woo_sync.woocommerce.mapping.custody_warehouse_qty",
                   return_value=0):
            self.assertEqual(stock_for_item("ACC-001"), 12)
        self.assertEqual(CALLS, [("none", "ACC-001")])

    def test_no_hook_registered_leaves_local_behaviour_unchanged(self):
        with patch_hooks(), \
             patch("frappe.db.get_value", return_value=_item_row()), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"ACC-001": 12.0}), \
             patch("ffl_woo_sync.woocommerce.mapping.custody_warehouse_qty",
                   return_value=0):
            self.assertEqual(stock_for_item("ACC-001"), 12)
        self.assertEqual(CALLS, [])

    def test_first_non_none_wins_and_short_circuits(self):
        with patch_hooks(f"{_MOD}.source_none", f"{_MOD}.source_five",
                         f"{_MOD}.source_seven"), \
             patch("frappe.db.get_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock"):
            self.assertEqual(stock_for_item("RSR-ACC-1"), 5)
        # source_seven must never run — the first non-None return ends the loop.
        self.assertEqual(CALLS, [("none", "RSR-ACC-1"), ("five", "RSR-ACC-1")])

    def test_value_is_coerced_to_int(self):
        with patch_hooks(f"{_MOD}.source_float"), \
             patch("frappe.db.get_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock"):
            result = stock_for_item("RSR-ACC-1")
        self.assertIsInstance(result, int)
        self.assertEqual(result, 4)

    def test_negative_is_clamped_at_the_funnel(self):
        """The documented formula `min(qty − buffer, cap)` is negative when the
        distributor holds fewer than the buffer.  Clamped HERE, not per caller:
        the full push clamps in the builder but the stock-only push sends the
        raw value, so a −2 would reach WooCommerce, store as 0, and then read as
        drift on every reconcile sweep for ever."""
        with patch_hooks(f"{_MOD}.source_negative"), \
             patch("frappe.db.get_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock"):
            self.assertEqual(stock_for_item("RSR-ACC-1"), 0)

    def test_hook_precedes_the_per_serial_firearm_raise(self):
        """[DIVERGENCE-4] — a dropship gun answers with a quantity, not a raise.

        The local rule ("per-serial firearms are one WC product per gun") is a
        statement about the store's OWN serialised inventory.  A dropship gun has
        no serials on hand at all, so the distributor's quantity is the answer —
        which is only possible because the loop runs before that raise.
        """
        with patch_hooks(f"{_MOD}.source_five"), \
             patch("frappe.db.get_value",
                   return_value=_item_row(is_firearm=1, item_group="Pistol")):
            self.assertEqual(stock_for_item("RSR-GLOCK-19"), 5)

    def test_uniform_firearm_never_reaches_the_serial_counting_branch(self):
        """A dropship gun is listed in the UNIFORM shape, so that branch is the
        one it would fall into if the loop ever drifted below the type check.

        The branch is pinned by its data dependencies, not by its return value:
        counting Active serials, counter holds and custody must not happen at
        all.  A quantity that merely *looks* right (0 serials on hand == the
        distributor's 0) would hide the drift until a gun was actually in stock.
        """
        with patch_hooks(f"{_MOD}.source_five"), \
             patch("frappe.db.get_value",
                   return_value=_item_row(is_firearm=1, item_group="Pistol",
                                          uniform_sell_price=1)), \
             patch("frappe.get_all") as mock_get_all, \
             patch("ffl_core.reservations.custody_serials") as mock_custody, \
             patch("ffl_core.reservations.counter_holds") as mock_held:
            self.assertEqual(stock_for_item("RSR-GLOCK-19"), 5)
        mock_get_all.assert_not_called()
        mock_custody.assert_not_called()
        mock_held.assert_not_called()

    def test_per_serial_firearm_still_raises_without_a_distributor_source(self):
        with patch_hooks(f"{_MOD}.source_none"), \
             patch("frappe.db.get_value",
                   return_value=_item_row(is_firearm=1, item_group="Pistol")):
            with self.assertRaises(ValueError):
                stock_for_item("AR15")

    def test_source_exception_propagates(self):
        """A broken feed fails loudly (Error Log) rather than silently reporting
        the local Bin — for a dropship item the local Bin is 0 and swallowing the
        error would delist the whole dropship catalogue on one bad poll."""
        with patch_hooks(f"{_MOD}.source_boom"), \
             patch("frappe.db.get_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock"):
            with self.assertRaises(RuntimeError):
                stock_for_item("RSR-ACC-1")


# ===========================================================================
# #1 regression — reconcile must not zero dropship stock
# ===========================================================================

class TestReconcileDoesNotZeroDropshipStock(FrappeTestCase):
    """Reconcile Pass 1 reads POS truth through ``mapping.stock_for_item``.

    These two tests deliberately do NOT patch ``stock_for_item`` (the existing
    Pass-1 tests do) — the whole point of #1 is that the real function, with a
    distributor source registered, is what reconcile sees.
    """

    def setUp(self):
        CALLS.clear()

    def _client(self, wc_stock: int):
        client = MagicMock()
        client.get_attribute_ids.return_value = {}
        client.get_product.return_value = {
            "stock_quantity": wc_stock, "regular_price": "100.00",
        }
        return client

    def _dropship_item(self):
        return frappe._dict(
            name="RSR-ACC-1",
            woo_product_id="WC-1",
            is_firearm=0,
            uniform_sell_price=0,
            standard_rate=100.0,
        )

    def _reconcile(self, client):
        # The price is its own hook (test_web_price_hook) — pinned to the tag here
        # so only the stock source decides whether reconcile pushes.
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("ffl_woo_sync.woocommerce.mapping.web_price_for_item",
                   side_effect=lambda code, rate=None: float(rate or 0)), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = [
                [self._dropship_item()],  # pass 1: item-level products
                [],                       # pass 2: active listed serials
                [],                       # pass 3a: sold serials
                [],                       # pass 3b: inactive listed serials
            ]
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        return mock_pi

    def test_wc_already_matches_the_distributor_qty_so_nothing_is_pushed(self):
        """The bug: WC holds the RSR quantity, the local Bin holds 0, and
        reconcile "corrects" the listing down to 0 — delisting the catalogue."""
        with patch_hooks(f"{_MOD}.source_five"), \
             patch("frappe.db.get_value") as mock_gv, \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"RSR-ACC-1": 0.0}) as mock_bin:
            mock_pi = self._reconcile(self._client(wc_stock=5))
        mock_pi.assert_not_called()          # no drift → no zeroing push
        mock_bin.assert_not_called()         # the local Bin was never consulted
        self.assertEqual(CALLS, [("five", "RSR-ACC-1")])
        # No Item row read either: the hook answered before the DB was touched.
        self.assertEqual(
            [c for c in mock_gv.call_args_list if c.args[:1] == ("Item",)], []
        )

    def test_drift_against_the_distributor_qty_still_pushes(self):
        """The fix must not go the other way either — a genuine drift (WC 0 vs
        RSR 5) still repairs the listing."""
        with patch_hooks(f"{_MOD}.source_five"), \
             patch("frappe.db.get_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock",
                   return_value={"RSR-ACC-1": 0.0}):
            mock_pi = self._reconcile(self._client(wc_stock=0))
        mock_pi.assert_called_once()
        self.assertEqual(mock_pi.call_args.args[0], "RSR-ACC-1")


# ===========================================================================
# Third writer — push_stock (the SLE doc_event's enqueue target)
# ===========================================================================

class TestPushStockUsesTheDistributorSource(FrappeTestCase):
    def setUp(self):
        CALLS.clear()

    def test_push_stock_sends_the_distributor_qty(self):
        from ffl_woo_sync.woocommerce import sync

        client = MagicMock()
        with patch_hooks(f"{_MOD}.source_five"), \
             patch("frappe.db.get_value", return_value="WC-1"), \
             patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock") as mock_bin:
            sync.push_stock("RSR-ACC-1", client=client)
        client.update_product.assert_called_once_with("WC-1", {"stock_quantity": 5})
        mock_bin.assert_not_called()

    def test_push_stock_never_sends_a_negative_quantity(self):
        from ffl_woo_sync.woocommerce import sync

        client = MagicMock()
        with patch_hooks(f"{_MOD}.source_negative"), \
             patch("frappe.db.get_value", return_value="WC-1"), \
             patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.mapping.get_item_stock"):
            sync.push_stock("RSR-ACC-1", client=client)
        client.update_product.assert_called_once_with("WC-1", {"stock_quantity": 0})
