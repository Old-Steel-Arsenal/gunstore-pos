"""Sale trio on the Woo side: payload shape, builder emission, drift compare.

The load-bearing facts (all verified against WC REST v3 on a live store,
2026-08-21) this file pins:

* ``""`` is the only reliable clearing value — JSON ``null`` for the date
  keys is silently IGNORED by the WC controller (PHP ``isset()``), so the
  trio must be emitted on EVERY payload, blanks included.
* ``date_on_sale_to`` runs to end-of-day (23:59:59), matching wp-admin's own
  scheduler, so a sale "through the 30th" holds all day on the 30th.
* WooCommerce silently DROPS a sale >= regular on save — POS-side validators
  refuse those before they ever reach a payload (pinned in ffl_core's
  test_sale_price, not here).
"""
from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.mapping import (
    build_serial_product_payload,
    build_simple_product_payload,
    sale_price_payload,
)
from ffl_woo_sync.woocommerce.tasks import _wc_sale_drifted

_CLEARED = {"sale_price": "", "date_on_sale_from": "", "date_on_sale_to": ""}


class SalePricePayloadShape(FrappeTestCase):
    def test_priced_with_window_expands_to_day_bounds(self):
        self.assertEqual(
            sale_price_payload(22, "2026-08-25", "2026-08-30"),
            {
                "sale_price": "22.00",
                "date_on_sale_from": "2026-08-25T00:00:00",
                "date_on_sale_to": "2026-08-30T23:59:59",
            },
        )

    def test_open_ended_dates_stay_blank(self):
        self.assertEqual(
            sale_price_payload(22),
            {"sale_price": "22.00", "date_on_sale_from": "", "date_on_sale_to": ""},
        )

    def test_no_sale_is_the_full_clearing_trio(self):
        self.assertEqual(sale_price_payload(0), _CLEARED)
        self.assertEqual(sale_price_payload(None, "2026-08-25", "2026-08-30"), _CLEARED)


class BuildersAlwaysEmitTheTrio(FrappeTestCase):
    """Omitting a key would strand the store's stored value forever (the WC
    ``isset()`` rule above) — so both builders emit the trio unconditionally."""

    def test_simple_builder_emits_values(self):
        payload = build_simple_product_payload(
            item_code="ACME-1", item_name="Acme", standard_rate=49.99,
            sale_price=39.99, sale_from="2026-08-25", sale_to="2026-08-30",
        )
        self.assertEqual(payload["sale_price"], "39.99")
        self.assertEqual(payload["date_on_sale_from"], "2026-08-25T00:00:00")
        self.assertEqual(payload["date_on_sale_to"], "2026-08-30T23:59:59")

    def test_simple_builder_emits_the_clearing_trio_by_default(self):
        payload = build_simple_product_payload(item_code="ACME-1", item_name="Acme")
        for key, blank in _CLEARED.items():
            self.assertEqual(payload[key], blank)

    def test_serial_builder_emits_values_and_clearing(self):
        priced = build_serial_product_payload(
            item_code="SG551", serial_no="410054", item_name="SG 551",
            sell_price=5400.0, sale_price=4900.0,
        )
        self.assertEqual(priced["sale_price"], "4900.00")
        cleared = build_serial_product_payload(
            item_code="SG551", serial_no="410054", item_name="SG 551",
            sell_price=5400.0,
        )
        for key, blank in _CLEARED.items():
            self.assertEqual(cleared[key], blank)


class SaleDriftComparator(FrappeTestCase):
    """tasks._wc_sale_drifted decides when the reconcile lean push fires —
    its expected side comes from sale_price_payload itself, so these cases
    pin the wc-response side of the comparison."""

    def test_no_sale_anywhere_is_no_drift(self):
        self.assertFalse(_wc_sale_drifted(
            {"sale_price": "", "date_on_sale_from": None, "date_on_sale_to": None},
            0, None, None))

    def test_pos_sale_missing_on_store_drifts(self):
        self.assertTrue(_wc_sale_drifted(
            {"sale_price": "", "date_on_sale_from": None, "date_on_sale_to": None},
            22, "2026-08-25", "2026-08-30"))

    def test_matching_trio_is_no_drift(self):
        self.assertFalse(_wc_sale_drifted(
            {
                "sale_price": "22.00",
                "date_on_sale_from": "2026-08-25T00:00:00",
                "date_on_sale_to": "2026-08-30T23:59:59",
            },
            22, "2026-08-25", "2026-08-30"))

    def test_wp_admin_format_heals_once_then_settles(self):
        # A hand-entered "22" drifts against POS's "22.00" — one lean push
        # later the store echoes our format and the comparison goes quiet.
        wc = {"sale_price": "22", "date_on_sale_from": None, "date_on_sale_to": None}
        self.assertTrue(_wc_sale_drifted(wc, 22, None, None))
        wc["sale_price"] = "22.00"
        self.assertFalse(_wc_sale_drifted(wc, 22, None, None))

    def test_stray_store_sale_heals_to_none(self):
        # A cleared retail sale: POS says no sale, the store still shows one
        # → drift → clearing push.
        self.assertTrue(_wc_sale_drifted(
            {"sale_price": "9.99", "date_on_sale_from": None, "date_on_sale_to": None},
            0, None, None))
