"""A gun with no price must never reach the storefront.

INCIDENT (2026-07-19).  SG 551 serials 410054 / 415635 were received without a
``sell_price``.  Serial No.sell_price is a NOT-NULL Currency column, so "never
set" reads as **0.00** — and the per-serial push published them at that price.
WooCommerce will happily sell a $5,400 rifle for nothing.  Staff trashed the two
products by hand; the 00:00 reconcile republished them the same night.

The rule is **skip + make the error visible.**  A serial whose
effective sell price is <= 0 is neither created nor updated on WooCommerce, the
reason lands in ``{site}_sync_error`` so it is one glance away on the Serial No
form, and a zero-priced product that ALREADY exists on the store is forced to
``draft`` so the shopfront stops selling it.

Two design points worth pinning, because both are load-bearing:

* **The guard is not overridable.**  The hidden-serial guard answers
  "may a machine republish this?" — a question a human may answer for it, hence
  ``_relist``.  This guard answers "does this gun have a price?", which no
  override can make true.  An operator pressing "Push to WooCommerce" on an
  unpriced gun gets refused too; the fix is to price it.
* **Effective price, not the raw field.**  A uniform-priced Item prices every
  one of its serials from ``Item.standard_rate`` (``firearm.serial_sell_price``),
  so a serial carrying its own 0 is fine when the parent is priced — and a
  serial carrying 900 is still unpriced when its uniform parent reads 0.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import sync

_FAKE_NOW = "2026-07-19 12:00:00"


def _make_client():
    client = MagicMock()
    client.get_attribute_ids.return_value = {}
    client.create_product.return_value = {"id": 999, "status": "publish"}
    client.update_product.return_value = {"id": 999, "status": "draft"}
    return client


def _db(serial: dict | None = None, item: dict | None = None):
    """``frappe.db.get_value`` stub dispatching on doctype.

    Both doctypes must be served: the price guard resolves a serial's price
    through its parent Item, so a Serial-No-only stub would read the parent as
    unpriced and refuse every push.
    """
    serial = serial or {}
    item = item or {}

    def _fn(doctype, name, field, *args, **kwargs):
        src = item if doctype == "Item" else serial
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: src.get(f) for f in field})
        return src.get(field)
    return _fn


def setUpModule():
    # These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
    # the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
    from ffl_core.tests.channel_query_doubles import warm_channel_meta

    warm_channel_meta()


class _Base(FrappeTestCase):
    def setUp(self):
        super().setUp()
        p = patch("ffl_woo_sync.woocommerce.sync._now", return_value=_FAKE_NOW)
        p.start()
        self.addCleanup(p.stop)
        m = patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                  return_value={"sku": "SG551::410054", "status": "publish"})
        m.start()
        self.addCleanup(m.stop)
        mi = patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                   return_value={"sku": "ITEM-1", "status": "publish"})
        mi.start()
        self.addCleanup(mi.stop)


# ===========================================================================
# Per-serial products
# ===========================================================================

class TestUnpricedSerialIsNotPublished(_Base):
    def test_unpriced_serial_is_never_created(self):
        """The gun has no WC product yet — the push must not bring one into
        existence at $0."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        client.create_product.assert_not_called()

    def test_unpriced_serial_is_never_updated_to_publish(self):
        """The SG 551 case: a product already exists, and the nightly sweep must
        not push the ordinary publish payload over it."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "1143",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        published = [c for c in client.update_product.call_args_list
                     if c.args[1].get("status") == "publish"]
        self.assertFalse(published, "an unpriced serial was pushed as published")

    def test_existing_zero_priced_product_is_forced_to_draft(self):
        """Skipping the push alone would leave the $0 product exactly as it was —
        live and purchasable. The storefront has to stop selling it."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "1143",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once_with(
            "1143", {"status": "draft", "stock_quantity": 0})

    def test_reason_is_visible_on_the_serial(self):
        """The error-visible half: an operator looking at the Serial No form must
        see WHY the gun never went up, without reading a log."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        written = [c for c in mock_sv.call_args_list if "woo_sync_error" in str(c)]
        self.assertTrue(written, "no woo_sync_error was written")
        self.assertIn("price", str(written[0]).lower())

    def test_result_reports_the_skip_reason(self):
        """Callers must be able to tell "refused" from "succeeded" — a bare
        no-op reads like a working push."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "unpriced")

    def test_relist_cannot_override_the_price_guard(self):
        """Deliberately NOT overridable. `_relist` exists so a human can answer
        "may a machine republish this?" — it cannot make an unpriced gun priced.
        Without this, the operator's Push button (which relists) would walk the
        $0 listing straight back onto the shop."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 1, "woo_product_id": "1143",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("410054", client=client, attr_ids={},
                                      _retries=0, _relist=True)
        self.assertEqual(result.get("skipped"), "unpriced")
        published = [c for c in client.update_product.call_args_list
                     if c.args[1].get("status") == "publish"]
        self.assertFalse(published)

    def test_negative_price_is_refused_too(self):
        """`<= 0`, not `== 0` — a negative price is no more sellable than zero."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "",
                        "item_code": "SG551", "sell_price": -5},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "unpriced")

    def test_a_priced_serial_still_publishes(self):
        """Regression guard: the fix must not cost the normal path."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "1143",
                        "item_code": "SG551", "sell_price": 5400, "woo_sold_archived": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once_with("1143", {"sku": "SG551::410054",
                                                               "status": "publish"})


class TestEffectivePriceFollowsTheUniformRule(_Base):
    """Pricing is a two-field rule (``firearm.serial_sell_price``); reading only
    ``Serial No.sell_price`` would both over- and under-block."""

    def test_uniform_parent_price_makes_a_zero_serial_sellable(self):
        """A uniform-priced Item prices every serial from ``standard_rate`` — the
        serial's own 0 is not a missing price, it is simply unused."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "77",
                        "item_code": "GLP61950203", "sell_price": 0, "woo_sold_archived": 0},
                item={"uniform_sell_price": 1, "standard_rate": 589.68})), \
             patch("frappe.db.set_value"):
            sync.push_serial("UNPRICED-SN-1", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once()

    def test_zero_uniform_parent_blocks_a_priced_serial(self):
        """The mirror case: under uniform pricing the serial's own tag is ignored,
        so a $900 tag on a serial whose parent reads 0 would still list at $0."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "77",
                        "item_code": "GLP61950203", "sell_price": 900},
                item={"uniform_sell_price": 1, "standard_rate": 0})), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("UNPRICED-SN-1", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "unpriced")


class TestTheReasonNeverClaimsMoreThanHappened(_Base):
    """The reason must not assert a take-down that failed.

    An earlier change moved the `sync_error` write BEFORE the HTTP so a WooCommerce outage
    could not swallow it. Correct — but the text still said "and the existing
    product drafted", which at that moment was a prediction, not a fact.

    On the whitelist paths a failure rolls the write back, so nobody sees it. On
    the reconcile / task paths the exception is caught per-record and the
    transaction still commits, so the claim PERSISTS: the Serial No form tells
    the operator a $0 product was taken off sale while it is still live and
    purchasable. That is this incident's exact failure shape with the Desk
    asserting the opposite — the same defect family as the "Pushed 2/2" green
    over draft guns, just rarer.
    """

    def _refuse(self, *, draft_raises=False):
        client = _make_client()
        if draft_raises:
            client.update_product.side_effect = RuntimeError("WooCommerce unreachable")
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "1143",
                        "item_code": "SG551", "sell_price": 0},
                item={"uniform_sell_price": 0, "standard_rate": 0})), \
             patch("frappe.db.set_value") as mock_sv:
            try:
                sync.push_serial("410054", client=client, attr_ids={}, _retries=0)
            except RuntimeError:
                pass
        return [str(c) for c in mock_sv.call_args_list]

    def test_a_failed_take_down_is_never_reported_as_done(self):
        writes = self._refuse(draft_raises=True)
        self.assertTrue(writes, "no reason was written at all")
        self.assertFalse([w for w in writes if "was taken off sale" in w],
                         "claimed the product was taken off sale, but the call failed")

    def test_the_reason_is_still_visible_when_woocommerce_is_down(self):
        """That property must survive this fix: an outage must not cost the
        operator the explanation."""
        writes = self._refuse(draft_raises=True)
        self.assertTrue([w for w in writes if "no sell price" in w])

    def test_a_successful_take_down_is_reported_in_the_past_tense(self):
        writes = self._refuse()
        self.assertTrue([w for w in writes if "was taken off sale" in w])


class TestPerSerialFirearmItemsAreExempt(_Base):
    """The exemption I flagged as load-bearing and then left untested (minor-3).

    A per-serial firearm Item legitimately carries ``standard_rate`` 0 — each gun
    holds its own tag — and has no item-level WC product at all. Two things break
    if the price guard swallows it:

    * ``reconcile_now`` DEPENDS on the ``ValueError`` from
      ``mapping.item_to_product_payload`` to route these to ``push_all_serials``;
      a skip dict instead would silently stop every per-serial gun from syncing.
    * every such SKU would be stamped with a false "no sell price is set" error,
      and any legacy item-level product force-drafted.

    Both failures are silent — nothing raises, tests stay green, guns stop
    reaching the shop. Hence a test rather than a comment.
    """

    def _push(self, *, is_firearm, uniform, standard_rate, item_group=None):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                   side_effect=ValueError("per-serial firearm")), \
             patch("frappe.db.get_value", side_effect=_db(
                 item={"is_firearm": is_firearm, "item_group": item_group,
                       "uniform_sell_price": uniform, "standard_rate": standard_rate,
                       "woo_delisted": 0, "woo_product_id": ""})), \
             patch("frappe.db.set_value"):
            try:
                return sync.push_item("PERGUN-SKU", client=client, attr_ids={},
                                      _retries=0), client, None
            except ValueError as exc:
                return None, client, exc

    def test_a_per_serial_firearm_still_raises_for_reconcile_now_to_catch(self):
        result, _client, exc = self._push(is_firearm=1, uniform=0, standard_rate=0)
        self.assertIsNone(result, "the price guard swallowed the routing signal")
        self.assertIsInstance(exc, ValueError)

    def test_a_per_serial_firearm_is_not_stamped_with_a_price_error(self):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                   side_effect=ValueError("per-serial firearm")), \
             patch("frappe.db.get_value", side_effect=_db(
                 item={"is_firearm": 1, "item_group": None, "uniform_sell_price": 0,
                       "standard_rate": 0, "woo_delisted": 0, "woo_product_id": "77"})), \
             patch("frappe.db.set_value") as mock_sv:
            with self.assertRaises(ValueError):
                sync.push_item("PERGUN-SKU", client=client, attr_ids={}, _retries=0)
        # ...and its (legacy) item-level product is NOT force-drafted.
        client.update_product.assert_not_called()
        self.assertFalse([c for c in mock_sv.call_args_list
                          if "no sell price" in str(c)])

    def test_a_uniform_priced_firearm_is_NOT_exempt(self):
        """The exemption is narrow on purpose: a uniform-priced firearm DOES have
        an item-level product priced from standard_rate, so 0 there is the very
        thing this guard must catch — this is the $35 Glock's SKU shape."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                item={"is_firearm": 1, "item_group": None, "uniform_sell_price": 1,
                      "standard_rate": 0, "woo_delisted": 0, "woo_product_id": ""})), \
             patch("frappe.db.set_value"):
            result = sync.push_item("GLP61950203", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "unpriced")

    def test_a_blank_item_code_is_refused_outright(self):
        """minor-1: `push_item_now` is whitelisted, so item_code="" arrives from
        outside. Frappe reads a falsy docname as "no filter", so all three reads
        below (delisted, the routing row, standard_rate) would resolve an
        arbitrary Item and decide this product's fate from an unrelated SKU."""
        client = _make_client()
        with patch("frappe.db.get_value") as mock_gv, \
             patch("frappe.db.set_value"):
            with self.assertRaises(ValueError):
                sync.push_item("", client=client, attr_ids={}, _retries=0)
        mock_gv.assert_not_called()


class TestPriceLookupIsRobust(_Base):
    """Found by CI, not by the local run — the local DB happened to carry a
    leftover row that masked it.

    `effective_serial_price` resolved the parent Item by passing
    `sn.item_code` straight to `get_value`. When that is blank, Frappe reads a
    falsy docname as "no filter" rather than "no such row", so the lookup
    resolves an ARBITRARY Item and prices the gun from a SKU it has nothing to
    do with. Inside the reconcile sweep it threw instead, and the per-serial
    `except Exception` swallowed it into `'SN-001: '` — an error string with no
    message, invisible in the logs, silently skipping the serial.
    """

    def test_a_serial_with_no_item_code_is_unpriced_not_an_exception(self):
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                serial={"status": "Active", "woo_delisted": 0, "woo_product_id": "",
                        "item_code": None, "sell_price": 0},
                item={"uniform_sell_price": 1, "standard_rate": 999})), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("SN-ORPHAN", client=client, attr_ids={}, _retries=0)
        # ...and specifically NOT priced from the unrelated Item above.
        self.assertEqual(result.get("skipped"), "unpriced")
        client.create_product.assert_not_called()

    def test_a_serial_that_does_not_exist_is_unpriced_not_an_exception(self):
        """The reconcile case: the sweep holds a name that no longer resolves."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value=None), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("SN-GONE", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "unpriced")

# ===========================================================================
# Item-level products
# ===========================================================================

class TestUnpricedItemIsNotPublished(_Base):
    def test_item_with_zero_standard_rate_is_not_created(self):
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                item={"standard_rate": 0, "woo_product_id": "", "item_group": None,
                      "woo_delisted": 0})), \
             patch("frappe.db.set_value"):
            result = sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0)
        client.create_product.assert_not_called()
        self.assertEqual(result.get("skipped"), "unpriced")

    def test_existing_zero_priced_item_product_is_forced_to_draft(self):
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                item={"standard_rate": 0, "woo_product_id": "WC-1", "item_group": None,
                      "woo_delisted": 0})), \
             patch("frappe.db.set_value"):
            sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once_with(
            "WC-1", {"status": "draft", "stock_quantity": 0})

    def test_reason_is_visible_on_the_item(self):
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                item={"standard_rate": 0, "woo_product_id": "", "item_group": None,
                      "woo_delisted": 0})), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0)
        written = [c for c in mock_sv.call_args_list if "woo_sync_error" in str(c)]
        self.assertTrue(written, "no woo_sync_error was written")
        self.assertIn("price", str(written[0]).lower())

    def test_a_priced_item_still_publishes(self):
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_db(
                item={"standard_rate": 100.0, "woo_product_id": "WC-1",
                      "item_group": None, "woo_delisted": 0})), \
             patch("frappe.db.set_value"):
            sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once_with(
            "WC-1", {"sku": "ITEM-1", "status": "publish"})
