"""``gunshop_woo_product_meta`` — extension-contributed WooCommerce product meta.

Contract under test
-------------------
Provider: ``fn(doctype, name) -> {meta_key: value}``, doctype being ``"Item"``
(uniform-price / non-firearm products) or ``"Serial No"`` (per-serial guns).
Keys must look like ``^_[a-z0-9_]{2,60}$``; a core key wins a collision, and a
provider that raises is logged and skipped rather than failing the push.

``gunshop_woo_product_meta_keys`` declares the same keys statically so the
lean-update allowlist (``sync._pos_owned_payload``) can let them through
without a document in hand — an extension flag that survives a full push but
reverts on the next lean one is the drift this pairing exists to prevent.

Providers below are registered by their real dotted paths through a patched
``frappe.get_hooks``, so production's ``frappe.get_attr`` resolution runs for real.
"""
from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import mapping, sync

_MOD = "ffl_woo_sync.tests.test_extension_product_meta"

CALLS: list[tuple[str, str]] = []


def flags(doctype: str, name: str) -> dict:
    CALLS.append((doctype, name))
    return {"_osa_ca_legal": "Yes", "_osa_compliant_service": "No"}


def second_provider(doctype: str, name: str) -> dict:
    return {"_vendor_other": "1"}


def bad_keys(doctype: str, name: str) -> dict:
    return {"osa_no_underscore": "x", "_UPPER": "x", "_ok_key": "kept"}


def collides_with_core(doctype: str, name: str) -> dict:
    return {"_osa_sold_price": "0.01", "_mine": "kept"}


def explodes(doctype: str, name: str) -> dict:
    raise RuntimeError("provider blew up")


def _hooks(meta=(), keys=()):
    """Patch frappe.get_hooks so only our two hook names resolve."""
    def fake(name, *args, **kwargs):
        if name == "gunshop_woo_product_meta":
            return list(meta)
        if name == "gunshop_woo_product_meta_keys":
            return list(keys)
        return []
    return patch("frappe.get_hooks", side_effect=fake)


class TestExtensionProductMeta(FrappeTestCase):

    def setUp(self):
        CALLS.clear()

    # --- collection -------------------------------------------------------

    def test_no_providers_means_no_meta(self):
        with _hooks():
            self.assertEqual(mapping.extension_product_meta("Item", "ITEM-1"), {})

    def test_provider_values_are_collected_and_stringified(self):
        with _hooks(meta=[f"{_MOD}.flags"]):
            got = mapping.extension_product_meta("Serial No", "SER-1")
        self.assertEqual(got, {"_osa_ca_legal": "Yes", "_osa_compliant_service": "No"})
        self.assertEqual(CALLS, [("Serial No", "SER-1")])

    def test_every_provider_runs(self):
        with _hooks(meta=[f"{_MOD}.flags", f"{_MOD}.second_provider"]):
            got = mapping.extension_product_meta("Item", "ITEM-1")
        self.assertIn("_osa_ca_legal", got)
        self.assertIn("_vendor_other", got)

    def test_malformed_keys_are_dropped(self):
        with _hooks(meta=[f"{_MOD}.bad_keys"]), patch("frappe.log_error"):
            got = mapping.extension_product_meta("Item", "ITEM-1")
        self.assertEqual(got, {"_ok_key": "kept"})

    def test_a_failing_provider_is_skipped_not_fatal(self):
        with _hooks(meta=[f"{_MOD}.explodes", f"{_MOD}.flags"]), patch("frappe.log_error") as log:
            got = mapping.extension_product_meta("Item", "ITEM-1")
        self.assertEqual(got["_osa_ca_legal"], "Yes")
        self.assertTrue(log.called)

    # --- merge into a payload --------------------------------------------

    def test_merge_appends_rows(self):
        payload = {"meta_data": [{"key": "_osa_serial", "value": "X"}]}
        with _hooks(meta=[f"{_MOD}.flags"]):
            out = mapping._with_extension_meta(payload, "Serial No", "SER-1")
        keys = [row["key"] for row in out["meta_data"]]
        self.assertEqual(keys, ["_osa_serial", "_osa_ca_legal", "_osa_compliant_service"])

    def test_core_key_wins_a_collision(self):
        payload = {"meta_data": [{"key": "_osa_sold_price", "value": "1299.00"}]}
        with _hooks(meta=[f"{_MOD}.collides_with_core"]):
            out = mapping._with_extension_meta(payload, "Serial No", "SER-1")
        rows = {row["key"]: row["value"] for row in out["meta_data"]}
        self.assertEqual(rows["_osa_sold_price"], "1299.00")
        self.assertEqual(rows["_mine"], "kept")

    def test_no_contributions_leaves_the_payload_untouched(self):
        payload = {"meta_data": [{"key": "_osa_serial", "value": "X"}]}
        with _hooks():
            self.assertEqual(
                mapping._with_extension_meta(dict(payload), "Item", "ITEM-1"), payload
            )

    # --- lean-update allowlist -------------------------------------------

    def test_declared_keys_survive_a_lean_push(self):
        payload = {
            "sku": "ITEM-1",
            "name": "hand-curated title",
            "stock_quantity": 1,
            "meta_data": [
                {"key": "_osa_serial", "value": "X"},
                {"key": "_osa_ca_legal", "value": "Yes"},
                {"key": "_yoast_wpseo_title", "value": "curated"},
            ],
        }
        with _hooks(keys=["_osa_ca_legal"]):
            lean = sync._pos_owned_payload(payload)
        keys = {row["key"] for row in lean["meta_data"]}
        self.assertIn("_osa_ca_legal", keys)
        self.assertIn("_osa_serial", keys)
        self.assertNotIn("_yoast_wpseo_title", keys)

    def test_undeclared_extension_key_is_still_blocked(self):
        payload = {"meta_data": [{"key": "_osa_ca_legal", "value": "Yes"}]}
        with _hooks():
            lean = sync._pos_owned_payload(payload)
        self.assertNotIn("meta_data", lean)

    def test_malformed_declared_key_is_ignored(self):
        with _hooks(keys=["not-an-underscore-key", "_good_key"]):
            self.assertEqual(mapping.extension_meta_keys(), frozenset({"_good_key"}))
