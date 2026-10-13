"""``distributor_web_price`` — the website price of a drop-ship listing.

A drop-ship product shows its markup price online while
the store's own price tag (``Item.standard_rate``) stays whatever the store set.
``mapping.web_price_for_item`` is the one reader: the first non-``None`` hook
answer wins, ``None`` is ``standard_rate`` as before. Every writer and checker of
an item-level product's price must go through it — the push payload, push_item's
unpriced guard and reconcile's drift check — or they would disagree about what
the store should show and reconcile would "repair" the website back to the tag.

No ffl_integrations import: the sources are registered by their real dotted
paths through a patched ``frappe.get_hooks`` (the stock-hook test's pattern);
the real registration is exercised in ffl_integrations' test_dropship_pricing.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import mapping, sync, tasks

_MOD = "ffl_woo_sync.tests.test_web_price_hook"


def source_599(item_code):
    return 599


def source_none(item_code):
    return None


_REAL_GET_HOOKS = frappe.get_hooks


def _hooks(*paths):
    def fake(hook=None, *args, **kwargs):
        if hook == "distributor_web_price":
            return [f"{_MOD}.{p}" for p in paths]
        return _REAL_GET_HOOKS(hook, *args, **kwargs)
    return patch.object(frappe, "get_hooks", side_effect=fake)


class WebPriceForItem(FrappeTestCase):
    def test_a_listing_price_wins_over_the_tag(self):
        with _hooks("source_none", "source_599"):
            self.assertEqual(mapping.web_price_for_item("X", 440), 599.0)

    def test_no_answer_is_the_tag_passed_in_or_read(self):
        with _hooks("source_none"):
            self.assertEqual(mapping.web_price_for_item("X", 440), 440.0)
            with patch.object(frappe.db, "get_value", return_value=321):
                self.assertEqual(mapping.web_price_for_item("X"), 321.0)

    def test_the_push_guard_reads_it(self):
        with _hooks("source_599"):
            self.assertEqual(sync.effective_item_price("X"), 599.0)


class EveryReaderGoesThroughIt(FrappeTestCase):
    """By source: a second read of `standard_rate` as the website price is the drift
    this hook exists to prevent, and it would not fail any mocked call."""

    @staticmethod
    def _calls(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    def test_the_payload_the_guard_and_reconcile(self):
        self.assertIn("web_price_for_item", self._calls(mapping.item_to_product_payload))
        self.assertIn("mapping.web_price_for_item", self._calls(sync.effective_item_price))
        self.assertIn("mapping.web_price_for_item", self._calls(tasks._run_reconcile))

    def test_reconcile_compares_against_it_not_the_tag(self):
        source = inspect.getsource(tasks._run_reconcile)
        self.assertNotIn('pos_price = f"{float(item.standard_rate', source)
        self.assertNotIn("if float(item.standard_rate or 0) <= 0", source)
