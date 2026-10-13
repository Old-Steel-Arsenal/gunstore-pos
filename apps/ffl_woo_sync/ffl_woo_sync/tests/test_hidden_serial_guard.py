"""A gun the owner took off the web must stay off until a human puts it back.

INCIDENT.  The first live reconcile republished all six
serials the owner had deliberately hidden — three of them ``sell_price=0``, so a
$7,899 SG 551 hung on the storefront **purchasable for $0** for hours.  Nothing
sold; the run was pulled back and ``auto_sync_enabled`` switched off.

Root cause: ``{site}_delisted`` carries a "machines must not republish this"
meaning that was only ever honoured in the EXIT router.
Every PUSH path ignored it:

* reconcile **Pass 2** selects Active listed serials without excluding the flag,
  sees WC(draft, stock 0) vs POS(Active, stock 1), calls that drift, and pushes;
* ``doc_events.on_serial_no_update`` pushes on any WC-relevant field edit;
* and ``push_serial``'s write-back cleared ``delisted`` unconditionally, so the
  sentinel destroyed itself on the first push — after which nothing remembered
  the gun had ever been hidden.

The fix inverts the default: :func:`sync.push_serial` REFUSES to publish a
serial whose site flag is set, and only a caller that explicitly means "relist"
may override it.  The four callers that legitimately mean that are enumerated in
:class:`TestLegitimateRelistPathsStillWork` — they matter as much as the guard,
because a fix that stranded returned or reserve-freed guns offline would trade
one outage for another.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import sync

_FAKE_NOW = "2026-07-18 22:00:00"


def _make_client():
    client = MagicMock()
    client.get_attribute_ids.return_value = {}
    client.create_product.return_value = {"id": 999, "status": "publish"}
    client.update_product.return_value = {"id": 999, "status": "publish"}
    return client


def _gv(**values):
    """``frappe.db.get_value`` stub: scalar reads by field, list reads → _dict.

    ``sell_price`` defaults to a real price because every gun in this module is
    a PRICED gun that is hidden (or not) — the question here is the delist
    sentinel, never the price. Without a default, the unpriced guard added for
    the 2026-07-19 mispricing incident would refuse these pushes first, and each
    test would silently pass or fail on the wrong guard. Tests that care about
    pricing live in test_unpriced_listing_guard.py and set it explicitly.
    """
    values.setdefault("sell_price", 1200)

    def _fn(doctype, name, field, *args, **kwargs):
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _fn


def setUpModule():
    # These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
    # the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
    from ffl_core.tests.channel_query_doubles import warm_channel_meta

    warm_channel_meta()


class _PatchNow(FrappeTestCase):
    def setUp(self):
        super().setUp()
        p = patch("ffl_woo_sync.woocommerce.sync._now", return_value=_FAKE_NOW)
        p.start()
        self.addCleanup(p.stop)
        m = patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                  return_value={"sku": "GUN-1::SN-HIDDEN", "status": "publish"})
        m.start()
        self.addCleanup(m.stop)


# ===========================================================================
# The guard
# ===========================================================================

class TestHiddenSerialIsNotRepublished(_PatchNow):
    def test_push_does_not_touch_woocommerce(self):
        """The whole incident in one assertion: no WC write at all."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_gv(
                status="Active", woo_delisted=1, woo_sold_archived=0,
                woo_product_id="77", item_code="")), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("SN-HIDDEN", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_not_called()
        client.create_product.assert_not_called()
        mock_sv.assert_not_called()

    def test_push_reports_why_it_skipped(self):
        """Callers (and the Error Log) must be able to tell "refused" from
        "succeeded" — a silent no-op would look like a working push."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_gv(
                status="Active", woo_delisted=1, woo_product_id="77")), \
             patch("frappe.db.set_value"):
            result = sync.push_serial("SN-HIDDEN", client=client, attr_ids={}, _retries=0)
        self.assertEqual(result.get("skipped"), "delisted")

    def test_zero_priced_hidden_gun_is_never_published(self):
        """The money case. Three of the six carried sell_price=0, so republishing
        them put a $7,899 rifle on sale for $0. Pinned explicitly because this is
        the consequence that makes the guard non-negotiable."""
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                   return_value={"sku": "X::SG551", "regular_price": "0.00"}), \
             patch("frappe.db.get_value", side_effect=_gv(
                 status="Active", woo_delisted=1, woo_product_id="1143")), \
             patch("frappe.db.set_value"):
            sync.push_serial("SG551", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_not_called()

    def test_the_sentinel_survives_a_push(self):
        """`sync.py:380` cleared `delisted` unconditionally, so the first push
        erased the evidence that the gun had been hidden. A refused push must
        write nothing at all."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_gv(
                status="Active", woo_delisted=1, woo_product_id="77")), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("SN-HIDDEN", client=client, attr_ids={}, _retries=0)
        mock_sv.assert_not_called()

    def test_an_ordinary_serial_still_publishes(self):
        """Regression guard: the guard must not cost the normal path."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_gv(
                status="Active", woo_delisted=0, woo_product_id="77", item_code="")), \
             patch("frappe.db.set_value"):
            sync.push_serial("SN-LIVE", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once()

class TestBackgroundPushPathsRespectTheGuard(FrappeTestCase):
    """The two vectors that actually fired in the incident."""

    def test_doc_events_push_task_cannot_republish(self):
        """Vector 2 — an ordinary field edit on a hidden gun."""
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push:
            from ffl_woo_sync.woocommerce.tasks import push_serial_task
            push_serial_task("SN-HIDDEN")
        # the task must pass the override explicitly OFF, not merely omit it —
        # an omitted flag would silently inherit whatever the default became.
        self.assertIs(mock_push.call_args.kwargs.get("_relist"), False)

    def test_reconcile_pass2_excludes_hidden_serials(self):
        """Vector 1 — defence in depth. The guard in push_serial already stops
        the write, but leaving hidden guns in Pass 2's set means a pointless WC
        GET per gun per sweep and a misleading 'drift' reading."""
        client = _make_client()
        client.get_product.return_value = {"stock_quantity": 0, "regular_price": ""}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("frappe.db.set_value"):
            mock_ga.side_effect = lambda doctype, **kw: []
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        pass2 = [c for c in mock_ga.call_args_list
                 if c.args[:1] == ("Serial No",)
                 and (c.kwargs.get("filters") or {}).get("status") == "Active"]
        self.assertTrue(pass2, "Pass 2's query was not found")
        self.assertEqual((pass2[0].kwargs.get("filters") or {}).get("woo_delisted"), 0)


# ===========================================================================
# The other half: relisting must still work
# ===========================================================================

class TestLegitimateRelistPathsStillWork(_PatchNow):
    """A guard that also stranded returned guns offline would trade one outage
    for another. These four callers mean "put it back on sale" and must say so
    explicitly — fail-closed by default, opt in to publish."""

    def _push_hidden(self, **kwargs):
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_gv(
                status="Active", woo_delisted=1, woo_sold_archived=0,
                woo_product_id="77", item_code="")), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("SN-BACK", client=client, attr_ids={},
                             _retries=0, **kwargs)
        return client, mock_sv

    def test_explicit_relist_publishes(self):
        client, _sv = self._push_hidden(_relist=True)
        client.update_product.assert_called_once()

    def test_explicit_relist_clears_the_sentinel(self):
        """Only here — this is the one place `delisted=0` may be written, so the
        flag can never again be erased as a side effect."""
        _client, mock_sv = self._push_hidden(_relist=True)
        self.assertEqual(mock_sv.call_args[0][2]["woo_delisted"], 0)

    def test_ordinary_push_never_writes_the_sentinel(self):
        """The `sync.py:380` bug, pinned: a normal push must not touch it even
        for a serial that is not hidden."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_gv(
                status="Active", woo_delisted=0, woo_product_id="77", item_code="")), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("SN-LIVE", client=client, attr_ids={}, _retries=0)
        self.assertNotIn("woo_delisted", mock_sv.call_args[0][2])

    def test_relist_serial_api_relists(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push:
            sync.relist_serial("SN-BACK", client=MagicMock(), attr_ids={})
        self.assertTrue(mock_push.call_args.kwargs.get("_relist"))

    def test_returned_gun_comes_back_online(self):
        """`sync_serial_listing_task`'s sellable branch — a gun back from a
        return / undispose / released counter hold. Without this the guard would
        leave every returned gun invisible forever."""
        from ffl_woo_sync.woocommerce import tasks

        def _state(doctype, name, field, *a, **k):
            if isinstance(field, (list, tuple)):
                return frappe._dict(status="Active", warehouse="Stores - FT",
                                    woo_reserved=0, item_code="GUN-1", consigned_out=0)
            return {"status": "Active", "woo_delisted": 1}.get(field)

        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push, \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_state):
            tasks.sync_serial_listing_task("SN-BACK", site="retail")
        self.assertTrue(mock_push.call_args.kwargs.get("_relist"))

    def test_reserve_release_puts_the_gun_back(self):
        """A cancelled web order frees its soft-reserve; the gun must return to
        the shop. This path was machine-delisted at reserve time, so it needs the
        override just as much as a return does."""
        from ffl_woo_sync.woocommerce import tasks

        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push:
            tasks.push_serial_task("SN-FREED", relist=True)
        self.assertTrue(mock_push.call_args.kwargs.get("_relist"))

    def test_human_push_button_relists(self):
        """The Serial No form's "Push to WooCommerce" — an operator explicitly
        putting a gun back is the clearest relist there is."""
        from ffl_woo_sync.woocommerce import client_api

        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   return_value={"id": 5}) as mock_push:
            client_api.push_serial_now("SN-BACK")
        self.assertTrue(mock_push.call_args.kwargs.get("_relist"))


class TestReservationDoorDoesNotRelist(FrappeTestCase):
    """`_enqueue_serial_or_item_push` is SHARED by three webhook paths, and only
    one of them means "put it back on sale":

        webhook.py:560  _release_serial        reserve RELEASED  → relist
        webhook.py:792  _handle_order_created  reserve TAKEN     → must NOT relist
        webhook.py:934  _apply_dealer_serial   reserve TAKEN     → must NOT relist

    The first version of this fix hardcoded ``relist=True`` inside the shared
    helper, which handed the override to the two reserve-TAKING paths as well.
    A plain-SKU order whose picker happened to land on a hidden gun would then
    have cleared its sentinel — the self-destruct this whole change exists to
    remove, walking back in through the reservation door.

    It passed CI because no webhook test asserted the kwarg. These do.
    """

    def test_the_shared_helper_does_not_relist_by_default(self):
        """Anything that does not explicitly opt in must be safe. This is the
        property that makes the two reserve-taking call sites correct *by
        construction* rather than by remembering."""
        from ffl_woo_sync.woocommerce import webhook

        with patch("frappe.db.get_value", return_value=frappe._dict(
                woo_product_id="77", item_code="GUN-1")), \
             patch("frappe.enqueue") as mock_enq:
            webhook._enqueue_serial_or_item_push("SN-RESERVED", site="retail")
        self.assertIs(mock_enq.call_args.kwargs.get("relist"), False)

    def test_releasing_a_reservation_is_the_one_exception(self):
        from ffl_woo_sync.woocommerce import webhook

        with patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.webhook._enqueue_serial_or_item_push") as mock_enq:
            webhook._release_serial("SN-FREED", site="retail")
        self.assertTrue(mock_enq.call_args.kwargs.get("relist"))

    def test_taking_a_reservation_cannot_clear_the_sentinel(self):
        """End to end through the task: a reserve-taking enqueue reaches
        push_serial with the guard ARMED, so a hidden gun caught by an order's
        serial picker is refused rather than republished."""
        from ffl_woo_sync.woocommerce import webhook

        with patch("frappe.db.get_value", return_value=frappe._dict(
                woo_product_id="77", item_code="GUN-1")), \
             patch("frappe.enqueue") as mock_enq:
            webhook._enqueue_serial_or_item_push("SN-HIDDEN", site="retail")
        kwargs = mock_enq.call_args.kwargs

        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push:
            from ffl_woo_sync.woocommerce.tasks import push_serial_task
            push_serial_task(kwargs["serial_no"], site=kwargs["site"],
                             relist=kwargs["relist"])
        self.assertIs(mock_push.call_args.kwargs.get("_relist"), False)
        client.update_product.assert_not_called()
