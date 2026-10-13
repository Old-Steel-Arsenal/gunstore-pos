"""When a human takes a product off the shop, the machine must not put it back.

INCIDENT (2026-07-19).  Staff found two SG 551 rifles listed at $0 and did the
obvious, correct thing: they moved the products to the WordPress trash.  At
00:00 the reconcile sweep read WC(stock 0) against POS(Active, stock 1), called
that drift, and pushed — which un-trashed both products and put a $5,400 rifle
back on sale for nothing.

``{site}_delisted`` already means "machines must not republish this", and every
PUSH path was taught to honour it.  But nothing ever SET it from what the
store actually showed: the flag could only be raised by our own delist paths, so
a hand-trashed product was invisible to the guard.  This closes that half —
reconcile now READS the WooCommerce product status and, when the store says the
product is not on sale, treats it as a deliberate human act:

    Do not republish; write ``{site}_delisted=1`` back to POS.
    Re-listing then requires an explicit woo_push (which relists).

Ordering matters and is pinned below: the unpriced check runs FIRST, so a
product *we* drafted for having no price is never misread as a human hiding it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _make_client():
    client = MagicMock()
    client.get_attribute_ids.return_value = {}
    return client


def _gv(**values):
    """``frappe.db.get_value`` stub: scalar reads by field, list reads → _dict.

    Field-aware on purpose. A blanket ``return_value=`` makes EVERY field truthy,
    including ``woo_delisted`` — which silently short-circuits the hidden-serial
    guard before the price guard is ever reached, so the test passes through a
    code path it did not intend to exercise.
    """
    def _fn(doctype, name, field, *args, **kwargs):
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _fn


def _serial_rows(rows):
    """frappe.get_all side effect for the four reconcile passes."""
    def _fn(doctype, **kw):
        filters = kw.get("filters") or {}
        if doctype == "Serial No" and filters.get("status") == "Active":
            return rows
        return []
    return _fn


def setUpModule():
    # These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
    # the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
    from ffl_core.tests.channel_query_doubles import warm_channel_meta

    warm_channel_meta()


class TestReconcileRespectsAHandTrashedProduct(FrappeTestCase):
    """Pass 2 — the vector that actually fired."""

    def _run(self, wc_product, price=5400.0):
        client = _make_client()
        client.get_product.return_value = wc_product
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all", side_effect=_serial_rows([
                 frappe._dict(name="410054", woo_product_id="1143",
                              woo_reserved=0, item_code="SG551")])), \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=price), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push, \
             patch("frappe.db.set_value") as mock_sv:
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        return mock_push, mock_sv

    def test_a_trashed_product_is_not_republished(self):
        """The whole incident in one assertion."""
        mock_push, _sv = self._run({"status": "trash", "stock_quantity": 0})
        mock_push.assert_not_called()

    def test_a_trashed_product_marks_the_serial_delisted(self):
        """Not republishing once is not enough — without the write-back, the very
        next sweep would read the same drift and push again. The flag is what
        makes the decision durable, and it is the flag every push path already
        honours."""
        _push, mock_sv = self._run({"status": "trash", "stock_quantity": 0})
        wrote = [c for c in mock_sv.call_args_list if "woo_delisted" in str(c)]
        self.assertTrue(wrote, "woo_delisted was never written back")
        self.assertEqual(mock_sv.call_args_list[0][0][2].get("woo_delisted"), 1)

    def test_a_drafted_product_is_treated_the_same(self):
        """Moving a product to draft is the other way staff take something off
        sale, and it reads as stock 0 drift exactly like trash does."""
        mock_push, mock_sv = self._run({"status": "draft", "stock_quantity": 0})
        mock_push.assert_not_called()
        self.assertTrue([c for c in mock_sv.call_args_list if "woo_delisted" in str(c)])

    def test_a_pending_or_private_product_is_treated_the_same(self):
        """Any status that is not `publish` means "not for sale to the public"."""
        for status in ("pending", "private"):
            with self.subTest(status=status):
                mock_push, _sv = self._run({"status": status, "stock_quantity": 0})
                mock_push.assert_not_called()

    def test_a_published_drifted_serial_still_syncs(self):
        """Regression guard: reconcile's real job must survive. A live product
        whose stock disagrees with POS is ordinary drift and must still push."""
        mock_push, _sv = self._run({"status": "publish", "stock_quantity": 0})
        mock_push.assert_called_once()

    def test_a_product_with_no_status_field_still_syncs(self):
        """Defensive: a WC response that omits `status` must not be read as
        hidden, or a malformed payload would silently delist the whole shop."""
        mock_push, _sv = self._run({"stock_quantity": 0})
        mock_push.assert_called_once()


class TestUnpricedIsCheckedBeforeHidden(FrappeTestCase):
    """Ordering pin. The unpriced guard drafts an unpriced product; if the hand-delist check then read that draft
    as "a human hid this", our own machine action would be laundered into a
    human decision — and the gun would need an explicit relist even though
    nobody ever touched it. Unpriced is therefore resolved first."""

    def test_an_unpriced_drafted_product_is_not_marked_human_delisted(self):
        client = _make_client()
        client.get_product.return_value = {"status": "draft", "stock_quantity": 0}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all", side_effect=_serial_rows([
                 frappe._dict(name="410054", woo_product_id="1143",
                              woo_reserved=0, item_code="SG551")])), \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=0.0), \
             patch("frappe.db.set_value") as mock_sv:
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        delisted_writes = [c for c in mock_sv.call_args_list
                           if isinstance(c[0][2], dict) and "woo_delisted" in c[0][2]]
        self.assertFalse(delisted_writes,
                         "our own zero-price draft was misread as a human delist")

    def test_an_unpriced_published_product_is_drafted_even_without_drift(self):
        """The SG 551 nightly path. POS says Active/stock 1 and WC says
        published/stock 1 — no drift at all, so the old sweep did nothing and the
        $0 listing simply stayed up. Being unpriced is itself the defect."""
        client = _make_client()
        client.get_product.return_value = {"status": "publish", "stock_quantity": 1}
        # The real push_serial runs here (not mocked) so this exercises the whole
        # reconcile → guard → force-draft chain; it re-reads the product id, so
        # the stub has to serve it.
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all", side_effect=_serial_rows([
                 frappe._dict(name="410054", woo_product_id="1143",
                              woo_reserved=0, item_code="SG551")])), \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=0.0), \
             patch("frappe.db.get_value", side_effect=_gv(
                 woo_product_id="1143", woo_delisted=0)), \
             patch("frappe.db.set_value"):
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        client.update_product.assert_called_once_with(
            "1143", {"status": "draft", "stock_quantity": 0})


class TestHandoffToTheMergedGuard(FrappeTestCase):
    """The seam between this change and the push-path guard.

    That guard taught every push path to obey ``{site}_delisted``; this change is the
    only thing that ever RAISES it from what the store shows. Each half is inert
    without the other, and they were written days apart against different
    incidents — exactly the kind of join that rots silently. The chain is
    therefore pinned end to end, using the real incident's data: SG 551 serials
    410054 / 415635, Woo products 955 / 956, hand-trashed on 2026-07-19 and
    revived by the 00:00 CT sweep *after* the guard was already live in prod — because
    the guard reads the POS flag, and nobody had set it.
    """

    def test_the_field_we_write_is_the_field_pass_2_filters_on(self):
        """If these two ever drift apart — a renamed field, a new site prefix —
        the write-back would keep succeeding while the sweep kept republishing,
        and every test above would still pass. Pin them to each other."""
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()
        client.get_product.return_value = {"status": "trash", "stock_quantity": 0}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=5400.0), \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = _serial_rows([
                frappe._dict(name="415635", woo_product_id="956",
                             woo_reserved=0, item_code="SG551")])
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()

        field = sync._f("retail", "delisted")
        # what the write-back actually set...
        writeback = mock_sv.call_args_list[0][0][2]
        self.assertEqual(writeback.get(field), 1)
        # ...is the same field, with the same meaning, that the sweep filters on.
        pass2 = [c for c in mock_ga.call_args_list
                 if c.args[:1] == ("Serial No",)
                 and (c.kwargs.get("filters") or {}).get("status") == "Active"]
        self.assertTrue(pass2, "Pass 2's query was not found")
        self.assertEqual(pass2[0].kwargs["filters"].get(field), 0)

    def test_a_serial_we_flagged_is_then_refused_by_the_push_guard(self):
        """Second half of the handoff: even if something re-selects the serial
        (a field edit, a manual enqueue), the push-path guard now has a flag to read."""
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()

        def _gv(doctype, name, field, *a, **k):
            values = {"woo_delisted": 1, "woo_product_id": "956",
                      "status": "Active", "item_code": "SG551"}
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: values.get(f) for f in field})
            return values.get(field)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value") as mock_sv:
            result = sync.push_serial("415635", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "delisted")
        client.update_product.assert_not_called()
        mock_sv.assert_not_called()

    def test_an_explicit_relist_still_gets_through(self):
        """The escape hatch has to survive, or pricing the SG 551s and putting
        them back would be impossible without editing the database by hand."""
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()
        client.update_product.return_value = {"id": 956, "status": "publish"}

        def _gv(doctype, name, field, *a, **k):
            values = {"woo_delisted": 1, "woo_product_id": "956", "status": "Active",
                      "item_code": "", "sell_price": 5400, "woo_sold_archived": 0,
                      "uniform_sell_price": 0, "standard_rate": 0}
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: values.get(f) for f in field})
            return values.get(field)

        with patch("ffl_woo_sync.woocommerce.sync._now", return_value="2026-07-19 12:00:00"), \
             patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                   return_value={"sku": "SG551::415635", "status": "publish"}), \
             patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value") as mock_sv:
            sync.relist_serial("415635", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once()
        self.assertEqual(mock_sv.call_args[0][2]["woo_delisted"], 0)

    def test_relisting_an_unpriced_gun_is_still_refused(self):
        """Where the two guards meet: the SG 551s were BOTH hand-trashed AND
        unpriced. Clearing the delist sentinel must not hand a $0 gun back to
        the storefront — the unpriced guard is not overridable, so the operator must price it
        first. This is the combination that actually occurred."""
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()

        def _gv(doctype, name, field, *a, **k):
            values = {"woo_delisted": 1, "woo_product_id": "956", "status": "Active",
                      "item_code": "SG551", "sell_price": 0,
                      "uniform_sell_price": 0, "standard_rate": 0}
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: values.get(f) for f in field})
            return values.get(field)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value"):
            result = sync.relist_serial("415635", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "unpriced")
        published = [c for c in client.update_product.call_args_list
                     if c.args[1].get("status") == "publish"]
        self.assertFalse(published)


class TestTheUnpricedDraftConverges(FrappeTestCase):
    """minor-2: once drafted, an unpriced serial must stop costing writes.

    The Item branch self-limits — `_refuse_unpriced` clears `listed`, and Pass 1
    selects on `listed=1`. A serial has no such flag: Pass 2 selects on
    `status=Active, delisted=0, product_id!=""`, all still true after we draft
    it. So an unpriced gun was re-drafted on EVERY sweep, forever, and
    `last_push_count` never fell back to 0 — re-creating in a new place the
    "a sweep that did nothing reports activity" noise the earlier fix set out to remove.
    """

    def _sweep(self, wc_status):
        client = _make_client()
        client.get_product.return_value = {"status": wc_status, "stock_quantity": 0}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all", side_effect=_serial_rows([
                 frappe._dict(name="410054", woo_product_id="1143",
                              woo_reserved=0, item_code="SG551")])), \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=0.0), \
             patch("frappe.db.get_value", side_effect=_gv(
                 woo_product_id="1143", woo_delisted=0)), \
             patch("frappe.db.set_value") as mock_sv:
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        return client, mock_sv

    def test_an_already_drafted_unpriced_serial_is_left_alone(self):
        client, _sv = self._sweep("draft")
        client.update_product.assert_not_called()

    def test_it_is_not_counted_as_a_push(self):
        """`last_push_count` must be able to reach 0 again, or the number stops
        meaning anything."""
        _client, mock_sv = self._sweep("draft")
        settings_write = mock_sv.call_args[0][2]
        self.assertEqual(settings_write.get("last_push_count"), 0)

    def test_it_is_still_not_recorded_as_a_human_delist(self):
        """The draft is OURS. Recording it as a hand-delist is the laundering
        the unpriced-before-hidden ordering exists to prevent — skipping the
        redundant PUT must not quietly become a delisted write."""
        _client, mock_sv = self._sweep("draft")
        self.assertFalse([c for c in mock_sv.call_args_list
                          if isinstance(c[0][2], dict) and "woo_delisted" in c[0][2]])

    def test_a_still_live_unpriced_serial_is_drafted(self):
        """The convergence check must not disarm the guard on the first sweep."""
        client, _sv = self._sweep("publish")
        client.update_product.assert_called_once_with(
            "1143", {"status": "draft", "stock_quantity": 0})


class TestItemLevelProductsGetTheSameProtection(FrappeTestCase):
    """Pass 1. Accessories and uniform-priced firearms share one item-level
    product, so a hand-trashed item product had the same republish hole."""

    def _run(self, wc_product, price=100.0):
        client = _make_client()
        client.get_product.return_value = wc_product

        def _rows(doctype, **kw):
            if doctype == "Item":
                return [frappe._dict(name="ITEM-1", woo_product_id="WC-1", is_firearm=0,
                                     uniform_sell_price=0, standard_rate=price)]
            return []

        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all", side_effect=_rows), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=5), \
             patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi, \
             patch("frappe.db.set_value") as mock_sv:
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        return mock_pi, mock_sv

    def test_a_trashed_item_product_is_not_republished(self):
        mock_pi, _sv = self._run({"status": "trash", "stock_quantity": 0,
                                  "regular_price": "100.00"})
        mock_pi.assert_not_called()

    def test_a_trashed_item_product_is_marked_delisted(self):
        _pi, mock_sv = self._run({"status": "trash", "stock_quantity": 0,
                                  "regular_price": "100.00"})
        self.assertTrue([c for c in mock_sv.call_args_list if "woo_delisted" in str(c)])

    def test_a_published_drifted_item_still_syncs(self):
        mock_pi, _sv = self._run({"status": "publish", "stock_quantity": 0,
                                  "regular_price": "100.00"})
        mock_pi.assert_called_once()


class TestDelistItemRaisesTheSentinelOnlyForAHuman(FrappeTestCase):
    """minor-5, with the automated callers deliberately left alone.

    This PR gave Item a `woo_delisted` field but only reconcile wrote it, so the
    operator's own "Delist from WooCommerce" left the item channel with exactly
    the hole already closed for serials: hand-delist an Item, then edit any
    `_ITEM_WOO_FIELDS` value, and `push_item` reads `delisted=0` and republishes.

    Raising it UNCONDITIONALLY (as `delist_serial` does) would have broken
    something else: an automated caller delists an Item — `delist_item_task`,
    a machine take-down through the drop-ship listing pipeline — and that
    pipeline listing it again enqueues `push_item_task`, which passes no
    `_relist`. The sentinel would refuse it and the take-down would silently
    become one-way. Serials have no equivalent trap: their automated return path
    already passes `_relist=True`.

    So the sentinel follows INTENT, not mechanism — the same rule `_relist`
    encodes on the way back in.
    """

    def setUp(self):
        super().setUp()
        p = patch("ffl_woo_sync.woocommerce.sync._now", return_value="2026-07-19 12:00:00")
        p.start()
        self.addCleanup(p.stop)

    def _delist(self, **kwargs):
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()
        client.update_product.return_value = {"id": 5, "status": "draft"}
        with patch("frappe.db.get_value", return_value="WC-1"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.delist_item("ITEM-1", client=client, _retries=0, **kwargs)
        return mock_sv.call_args[0][2], client

    def test_the_operator_button_raises_the_sentinel(self):
        writeback, _client = self._delist(_deliberate=True)
        self.assertEqual(writeback.get("woo_delisted"), 1)
        self.assertEqual(writeback.get("woo_listed"), 0)

    def test_an_automated_delist_does_not(self):
        """A machine take-down must stay reversible by the machine."""
        writeback, _client = self._delist()
        self.assertNotIn("woo_delisted", writeback)
        self.assertEqual(writeback.get("woo_listed"), 0)

    def test_the_human_delist_is_then_honoured_by_push_item(self):
        """End to end: the sentinel is only worth writing if the push path reads
        it — the exact pairing already established for serials."""
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()

        def _gv(doctype, name, field, *a, **k):
            values = {"woo_delisted": 1, "woo_product_id": "WC-1",
                      "standard_rate": 100.0, "item_group": None}
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: values.get(f) for f in field})
            return values.get(field)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value"):
            result = sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "delisted")
        client.update_product.assert_not_called()


class TestPushItemHonoursTheItemFlag(FrappeTestCase):
    """The flag is only worth writing if the push paths read it — the exact gap
    already closed for serials, now closed for items."""

    def setUp(self):
        super().setUp()
        p = patch("ffl_woo_sync.woocommerce.sync._now", return_value="2026-07-19 12:00:00")
        p.start()
        self.addCleanup(p.stop)

    def test_a_delisted_item_is_not_republished(self):
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()

        def _gv(doctype, name, field, *a, **k):
            values = {"standard_rate": 100.0, "woo_delisted": 1,
                      "woo_product_id": "WC-1", "item_group": None}
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: values.get(f) for f in field})
            return values.get(field)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value") as mock_sv:
            result = sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "delisted")
        client.update_product.assert_not_called()
        client.create_product.assert_not_called()
        mock_sv.assert_not_called()

    def test_an_explicit_relist_may_republish_an_item(self):
        from ffl_woo_sync.woocommerce import sync

        client = _make_client()
        client.update_product.return_value = {"id": 1, "status": "publish"}

        def _gv(doctype, name, field, *a, **k):
            values = {"standard_rate": 100.0, "woo_delisted": 1,
                      "woo_product_id": "WC-1", "item_group": None}
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: values.get(f) for f in field})
            return values.get(field)

        with patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                   return_value={"sku": "ITEM-1", "status": "publish"}), \
             patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0, _relist=True)
        client.update_product.assert_called_once()
        self.assertEqual(mock_sv.call_args[0][2].get("woo_delisted"), 0)
