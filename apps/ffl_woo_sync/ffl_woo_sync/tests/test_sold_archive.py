"""SOLD-archive lifecycle for per-serial firearms.

A per-serial gun's WooCommerce page currently has a nondeterministic fate: it
either goes ``draft`` (→ 404, dropping accrued rankings/backlinks) or lingers as
a "zombie" (``publish`` + out-of-stock, with a ``{site}_delisted=1`` sentinel that
makes reconcile skip it forever — the live SP000279 case).

This suite pins the deterministic replacement:

* :func:`sync.route_serial_exit` — the SINGLE SOURCE OF TRUTH deciding what
  happens to a serial leaving the shelf.  First match wins: not sold → delist; sold but already drafted → skip, keeping the
  page and its 301; sold and live → archive.
* :func:`sync.archive_serial_sold` — pins ``publish`` + stock 0 and stamps the
  ``_osa_sold_*`` product meta the storefront reads.
* ``mapping.serial_to_product_payload`` sold-awareness — a post-sale gallery edit
  re-pushes the product WITHOUT resurrecting purchasability and WITHOUT clobbering
  the sold meta (the zombie killer).
* The relist path — a gun coming back to Active clears both the sentinel and the
  storefront meta.
* Reconcile Pass 3's two arms and their sentinels.
* Web fulfillment's realized-price pass-through.

All WooCommerce HTTP and Frappe DB access is mocked — no live site or store.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import mapping, sync

_FAKE_NOW = "2026-07-18 09:30:00"
_FAKE_TODAY = "2026-07-18"


def _make_client():
    """A MagicMock matching the WooCommerceClient domain-method interface."""
    client = MagicMock()
    client.get_attribute_ids.return_value = {}
    client.create_product.return_value = {"id": 999, "status": "publish"}
    client.update_product.return_value = {"id": 999, "status": "publish"}
    client.get_product.return_value = {"id": 999}
    return client


def _meta_map(payload: dict) -> dict:
    """``payload["meta_data"]`` → ``{key: value}`` for readable assertions."""
    return {m["key"]: m["value"] for m in (payload.get("meta_data") or [])}


def setUpModule():
    # These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
    # the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
    from ffl_core.tests.channel_query_doubles import warm_channel_meta

    warm_channel_meta()


class _PatchClock(FrappeTestCase):
    """Freeze ``sync._now`` / ``sync._today`` — both hit System Settings."""

    def setUp(self):
        super().setUp()
        for target, value in (("_now", _FAKE_NOW), ("_today", _FAKE_TODAY)):
            patcher = patch(f"ffl_woo_sync.woocommerce.sync.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


# ===========================================================================
# route_serial_exit — the routing matrix (first match wins)
# ===========================================================================

class TestRouteSerialExit(FrappeTestCase):
    def test_delivered_routes_to_archive(self):
        """Rule 1: a Delivered serial sold — archive it."""
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                status="Delivered", woo_delisted=0)):
            self.assertEqual(sync.route_serial_exit("SN-1"), sync.EXIT_ARCHIVE)

    def test_delivered_and_consigned_out_routes_to_archive(self):
        """Rule 1 wins over rule 2: a consigned-out gun that SOLD at the dealer
        is still a sale — first match wins, so consigned_out never downgrades a
        Delivered serial to a delist."""
        def _gv(doctype, name, field, *a, **kw):
            return {"status": "Delivered", "consigned_out": 1,
                    "woo_delisted": 0}.get(field)

        with patch("frappe.db.get_value", side_effect=_gv):
            self.assertEqual(sync.route_serial_exit("SN-2"), sync.EXIT_ARCHIVE)

    def test_active_consigned_out_routes_to_delist(self):
        """Rule 2: consigned out but NOT sold — it was removed, not sold."""
        with patch("frappe.db.get_value", return_value="Active"):
            self.assertEqual(sync.route_serial_exit("SN-3"), sync.EXIT_DELIST)

    def test_inactive_routes_to_delist(self):
        with patch("frappe.db.get_value", return_value="Inactive"):
            self.assertEqual(sync.route_serial_exit("SN-4"), sync.EXIT_DELIST)

    def test_retail_is_the_default_site(self):
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                status="Delivered", woo_delisted=0)):
            self.assertEqual(sync.route_serial_exit("SN-1"), sync.EXIT_ARCHIVE)

    def test_already_drafted_serial_is_skipped_not_archived(self):
        """A deliberately drafted page stays draft + 301.

        Generalizes the sweep's `delisted=0` guard to every call site, so
        `{site}_delisted=1` now means one thing everywhere — "a human or a
        deliberate mechanism drafted this; do not auto-republish it" — with the
        backfill as the single, owner-gated exception."""
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                status="Delivered", woo_delisted=1)):
            self.assertEqual(sync.route_serial_exit("SN-D"), sync.EXIT_SKIP)

    def test_undrafted_sold_serial_still_archives(self):
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                status="Delivered", woo_delisted=0)):
            self.assertEqual(sync.route_serial_exit("SN-D"), sync.EXIT_ARCHIVE)

    def test_the_skip_guard_does_not_apply_to_delist(self):
        """The guard is on the ARCHIVE decision only. A non-sold serial that is
        already delisted keeps its existing behaviour — the guard is not a
        licence to change the delist path."""
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                status="Inactive", woo_delisted=1)):
            self.assertEqual(sync.route_serial_exit("SN-D"), sync.EXIT_DELIST)

    def test_prefetched_status_costs_no_query_on_the_common_path(self):
        """Callers that already SELECTed status (reconcile, the SLE hook) must
        not pay for it again. A non-sold serial — the overwhelmingly common
        case — therefore reads nothing at all."""
        with patch("frappe.db.get_value") as mock_gv:
            self.assertEqual(
                sync.route_serial_exit("SN-5", status="Active"), sync.EXIT_DELIST)
        mock_gv.assert_not_called()

    def test_sold_path_reads_only_the_delist_sentinel(self):
        """The skip guard costs exactly one extra read, and only when a gun actually
        sold — not once per reconciled row."""
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                woo_delisted=0)) as mock_gv:
            self.assertEqual(
                sync.route_serial_exit("SN-5", status="Delivered"), sync.EXIT_ARCHIVE)
        self.assertEqual(mock_gv.call_count, 1)
        self.assertEqual(mock_gv.call_args[0][2], "woo_delisted")


class TestSoldStatePredicates(FrappeTestCase):
    """The two predicates sync and mapping share, so "presents as SOLD" and
    "came back to the shelf" mean exactly one thing each.

    ``status`` is the authority; ``{site}_sold_archived`` records that the WC
    page is CURRENTLY rendered SOLD. The pair is mutually exclusive by
    construction — the state machine has no overlap to argue about.
    """

    def test_delivered_is_sold(self):
        self.assertTrue(sync.is_sold_state("Delivered", 0))

    def test_archived_flag_holds_sold_when_status_moves_off_delivered(self):
        """A hand-edited status must not silently un-sell a page that already
        carries sold meta; only a return to Active does that."""
        self.assertTrue(sync.is_sold_state("Inactive", 1))

    def test_active_unarchived_is_not_sold(self):
        self.assertFalse(sync.is_sold_state("Active", 0))

    def test_active_with_archive_flag_is_a_return_not_a_sale(self):
        """Back to Active = returns / undispose. This is the relist signal, so
        it must NOT read as sold."""
        self.assertFalse(sync.is_sold_state("Active", 1))

    def test_relisted_requires_both_active_and_the_flag(self):
        self.assertTrue(sync.is_relisted_state("Active", 1))

    def test_never_archived_active_serial_is_not_a_relist(self):
        """Nothing to clear — don't write blank meta to every live listing."""
        self.assertFalse(sync.is_relisted_state("Active", 0))

    def test_sold_serial_is_not_a_relist(self):
        self.assertFalse(sync.is_relisted_state("Delivered", 1))

    def test_the_two_states_never_overlap(self):
        for status in ("Active", "Delivered", "Inactive", "Expired", ""):
            for flag in (0, 1):
                with self.subTest(status=status, flag=flag):
                    self.assertFalse(
                        sync.is_sold_state(status, flag)
                        and sync.is_relisted_state(status, flag)
                    )


# ===========================================================================
# archive_serial_sold
# ===========================================================================

class TestArchiveSerialSold(_PatchClock):
    def test_pins_publish_and_zero_stock(self):
        """The whole point: the page STAYS live (200, indexable) at stock 0."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value"):
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        payload = client.update_product.call_args[0][1]
        self.assertEqual(payload["status"], "publish")
        self.assertEqual(payload["stock_quantity"], 0)

    def test_stamps_sold_at_iso_date(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value"):
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        meta = _meta_map(client.update_product.call_args[0][1])
        self.assertEqual(meta["_osa_sold_at"], _FAKE_TODAY)

    def test_omits_sold_price_when_not_supplied(self):
        """POS / reconcile archives have no exact figure — showing the asking price
        as the realized price would be wrong."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value"):
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        self.assertNotIn("_osa_sold_price", _meta_map(client.update_product.call_args[0][1]))

    def test_writes_sold_price_when_supplied(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value"):
            sync.archive_serial_sold("SN-X", sold_price=1589.0, client=client, _retries=0)
        meta = _meta_map(client.update_product.call_args[0][1])
        self.assertEqual(meta["_osa_sold_price"], "1589.00")

    def test_sets_sold_archived_sentinel(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_sold_archived"], 1)

    def test_clears_delisted_flag(self):
        """The two sentinels are mutually exclusive: an archived product is
        PUBLISHED, so leaving delisted=1 would be a lie (and is exactly the
        contradiction that made reconcile skip SP000279 forever)."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_delisted"], 0)

    def test_writes_synced_at(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        self.assertEqual(mock_sv.call_args[0][2]["woo_synced_at"], _FAKE_NOW)

    def test_still_archives_an_already_drafted_serial(self):
        """The skip guard lives in the ROUTER, never here — and that
        boundary is load-bearing. SP000279 is `Delivered AND delisted=1`, so a
        sink-level guard would refuse exactly the serials the backfill
        exists to rescue, silently turning the owner-confirmed path into a
        no-op. The routing layer declines to archive automatically; the operator
        can still archive explicitly."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value="279"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.archive_serial_sold("SP000279", client=client, _retries=0)
        client.update_product.assert_called_once()
        self.assertEqual(mock_sv.call_args[0][2]["woo_sold_archived"], 1)

    def test_raises_when_no_product_id(self):
        """Nothing was ever listed → there is no page to archive."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value=None):
            with self.assertRaises(ValueError):
                sync.archive_serial_sold("SN-X", client=client)

    def test_does_not_touch_price_or_title(self):
        """An archive is a lifecycle flip, not a re-push: it must not rewrite
        the listing's own fields (a stale price/title would overwrite curated
        content)."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value"):
            sync.archive_serial_sold("SN-X", client=client, _retries=0)
        payload = client.update_product.call_args[0][1]
        self.assertEqual(set(payload), {"status", "stock_quantity", "meta_data"})


# ===========================================================================
# Sold-aware payload — the zombie killer (idempotency)
# ===========================================================================

class TestSoldAwarePayload(FrappeTestCase):
    """A post-sale gallery/detail edit enqueues push_serial_task, whose payload
    carried ``status: publish`` and republished SP000279. Re-pushing a sold gun
    must be SAFE, not resurrecting."""

    def _mock_serial(self, **kwargs):
        serial = MagicMock()
        serial.name = kwargs.get("name", "SN-SOLD")
        serial.item_code = kwargs.get("item_code", "GUN-1")
        serial.status = kwargs.get("status", "Active")
        serial.image_gallery = kwargs.get("image_gallery", [])
        serial.get = lambda f, d=None: kwargs.get(f, d)
        return serial

    def _mock_item(self, **kwargs):
        item = MagicMock()
        item.item_code = kwargs.get("item_code", "GUN-1")
        item.item_name = kwargs.get("item_name", "PSA Spiker")
        item.description = kwargs.get("description", "")
        item.image_gallery = kwargs.get("image_gallery", [])
        item.get = lambda f, d=None: kwargs.get(f, d)
        return item

    def _payload(self, serial, item, site="retail"):
        with patch("frappe.get_doc", side_effect=[serial, item]):
            return mapping.serial_to_product_payload(serial.name, site=site)

    def test_sold_serial_repush_stays_publish_at_zero_stock(self):
        serial = self._mock_serial(status="Delivered", sell_price=899.0, woo_reserved=0)
        payload = self._payload(serial, self._mock_item())
        self.assertEqual(payload["status"], "publish")
        self.assertEqual(payload["stock_quantity"], 0)

    def test_archived_flag_holds_the_page_sold_when_status_moves(self):
        """A sold gun whose status is later hand-edited (Delivered → Inactive)
        keeps its SOLD page instead of drifting back into an unmanaged zombie."""
        serial = self._mock_serial(
            status="Inactive", sell_price=899.0, woo_reserved=0, woo_sold_archived=1
        )
        payload = self._payload(serial, self._mock_item())
        self.assertEqual(payload["stock_quantity"], 0)
        self.assertEqual(payload["status"], "publish")

    def test_builder_pins_zero_stock_over_an_active_flag(self):
        """Contract at the pure-builder layer: ``sold_archived`` alone forces
        stock 0. Today the loader can't produce sold+Active (the predicates are
        exclusive), so this is defence in depth — it keeps a future change to
        the ``is_active`` derivation from handing the storefront a buyable unit
        on a sold gun."""
        payload = mapping.build_serial_product_payload(
            item_code="GUN-1", serial_no="SN-SOLD", item_name="PSA Spiker",
            sell_price=899.0, is_active=True, woo_reserved=False, sold_archived=True,
        )
        self.assertEqual(payload["stock_quantity"], 0)
        self.assertEqual(payload["status"], "publish")

    def test_sold_repush_preserves_sold_meta_by_not_resending_it(self):
        """WooCommerce ``meta_data`` is UPSERT — keys absent from the payload keep
        their stored values. So the safe way to preserve ``_osa_sold_*`` on a
        re-push is to not send them at all; sending an empty value would wipe the
        storefront's 'Sold for $X'."""
        serial = self._mock_serial(status="Delivered", sell_price=899.0, woo_reserved=0)
        meta = _meta_map(self._payload(serial, self._mock_item()))
        self.assertNotIn("_osa_sold_at", meta)
        self.assertNotIn("_osa_sold_price", meta)

    def test_live_serial_is_untouched(self):
        """No regression for the normal path: an Active, unsold gun is buyable."""
        serial = self._mock_serial(status="Active", sell_price=899.0, woo_reserved=0)
        payload = self._payload(serial, self._mock_item())
        self.assertEqual(payload["stock_quantity"], 1)
        self.assertNotIn("_osa_sold_at", _meta_map(payload))


# ===========================================================================
# Relist — a gun coming back must shed every trace of the sale
# ===========================================================================

class TestRelistClearsSoldState(FrappeTestCase):
    def _mock_serial(self, **kwargs):
        serial = MagicMock()
        serial.name = kwargs.get("name", "SN-BACK")
        serial.item_code = "GUN-1"
        serial.status = kwargs.get("status", "Active")
        serial.image_gallery = []
        serial.get = lambda f, d=None: kwargs.get(f, d)
        return serial

    def _mock_item(self):
        item = MagicMock()
        item.item_code = "GUN-1"
        item.item_name = "PSA Spiker"
        item.description = ""
        item.image_gallery = []
        item.get = lambda f, d=None: None
        return item

    def test_returned_serial_payload_blanks_sold_meta(self):
        """Active again + a stale archive flag = a return/undispose. The
        storefront must stop saying SOLD, so the meta is explicitly blanked
        (WC upsert semantics mean omitting them would LEAVE them)."""
        serial = self._mock_serial(status="Active", sell_price=899.0,
                                   woo_reserved=0, woo_sold_archived=1)
        with patch("frappe.get_doc", side_effect=[serial, self._mock_item()]):
            payload = mapping.serial_to_product_payload("SN-BACK")
        meta = _meta_map(payload)
        self.assertEqual(meta["_osa_sold_at"], "")
        self.assertEqual(meta["_osa_sold_price"], "")

    def test_relisted_gun_renders_no_sold_ui(self):
        """The empty-string residue case, end to end.

        A returned gun must leave NOTHING the storefront can read as sold: the
        page is buyable again, and both `_osa_sold_*` keys are present-but-empty
        so WC's upsert actually overwrites the stored values. Consumers gate on
        `!empty()`, so empty == absent; a consumer testing key EXISTENCE would
        render an empty 'Sold for $' here, which is why the contract is written
        down rather than left implicit."""
        serial = self._mock_serial(status="Active", sell_price=1299.0,
                                   woo_reserved=0, woo_sold_archived=1)
        with patch("frappe.get_doc", side_effect=[serial, self._mock_item()]):
            payload = mapping.serial_to_product_payload("SN-BACK")
        meta = _meta_map(payload)
        self.assertEqual(payload["status"], "publish")
        self.assertEqual(payload["stock_quantity"], 1)          # buyable again
        self.assertEqual(payload["regular_price"], "1299.00")   # priced again
        for key in ("_osa_sold_at", "_osa_sold_price"):
            with self.subTest(key=key):
                self.assertIn(key, meta, "the key must be SENT, or upsert keeps the old value")
                self.assertEqual(meta[key], "", "and sent EMPTY, so !empty() reads it as absent")

    def test_push_serial_clears_sold_archived_when_back_on_the_shelf(self):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.sync._now", return_value=_FAKE_NOW), \
             patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                   return_value={"sku": "GUN-1::SN-BACK"}), \
             patch("frappe.db.get_value", side_effect=_gv_factory(
                 status="Active", woo_sold_archived=1, woo_product_id="77")), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("SN-BACK", client=client, attr_ids={}, _retries=0)
        self.assertEqual(mock_sv.call_args[0][2]["woo_sold_archived"], 0)

    def test_push_serial_keeps_sold_archived_on_a_sold_gun(self):
        """The gallery-edit re-push must NOT clear the sentinel, or reconcile
        would re-archive the same gun on every 2-hourly sweep."""
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.sync._now", return_value=_FAKE_NOW), \
             patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                   return_value={"sku": "GUN-1::SN-SOLD"}), \
             patch("frappe.db.get_value", side_effect=_gv_factory(
                 status="Delivered", woo_sold_archived=1, woo_product_id="77")), \
             patch("frappe.db.set_value") as mock_sv:
            sync.push_serial("SN-SOLD", client=client, attr_ids={}, _retries=0)
        self.assertNotIn("woo_sold_archived", mock_sv.call_args[0][2])


def _is_archive_filter(filters: dict | None) -> bool:
    """True when this Serial No query is Pass 3a (the SOLD-archive arm).

    Keyed on the OPERATOR (`in` selects sold statuses, `not in` excludes them)
    rather than on a literal status value — otherwise adding a status to
    SOLD_STATUS silently re-points every helper that uses this, which is exactly
    how the Consumed gap stayed invisible.
    """
    cond = (filters or {}).get("status")
    return isinstance(cond, (list, tuple)) and cond[0] == "in"


def _matches(row: dict, filters: dict | None, or_filters: dict | None = None) -> bool:
    """Would ``frappe.get_all(filters=..., or_filters=...)`` return this row?

    A minimal evaluator for the two forms this module's queries use — a literal
    (equality) and ``[op, value]`` with ``!=``. Frappe ANDs ``filters`` and ORs
    ``or_filters``, then ANDs the two groups together.
    """
    def _one(field, cond) -> bool:
        actual = row.get(field)
        if isinstance(cond, (list, tuple)):
            op, expected = cond
            if op == "!=":
                return actual != expected
            if op == "in":
                return actual in expected
            if op == "not in":
                return actual not in expected
            raise AssertionError(f"unsupported filter op {op!r} — extend _matches")
        return actual == cond

    if not all(_one(f, c) for f, c in (filters or {}).items()):
        return False
    if or_filters:
        return any(_one(f, c) for f, c in or_filters.items())
    return True


def _gv_factory(**values):
    """``frappe.db.get_value`` side effect: field name → canned value.

    Handles both the scalar form (``get_value(dt, name, "field")``) and the
    list form (``get_value(dt, name, [...], as_dict=True)``).

    ``sell_price`` defaults to a real price: these fixtures describe guns moving
    through the SOLD lifecycle, never unpriced ones, and without it the unpriced
    guard (2026-07-19 mispricing incident) refuses the push before the archive
    logic under test is reached.
    """
    values.setdefault("sell_price", 1200)

    def _gv(doctype, name, field, *args, **kwargs):
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _gv


# ===========================================================================
# Reconcile Pass 3 — two arms, two sentinels
# ===========================================================================

class Pass3ArmsHelpers(FrappeTestCase):
    """_run / _arm for the Pass 3 tests, WITHOUT test methods — subclass this,
    not TestReconcilePass3Arms (its tests would re-run under the subclass)."""

    @staticmethod
    def _side_effect(pass1=(), pass2=(), archive_rows=(), delist_rows=()):
        """Doctype+filter-keyed dispatcher (mirrors test_consignment_out_delist)
        extended for Pass 3's split: the archive arm is the Serial No query
        whose filters pin ``status == "Delivered"``."""
        def _fn(doctype, *args, **kwargs):
            filters = kwargs.get("filters") if "filters" in kwargs else (
                args[0] if args else {})
            filters = filters or {}
            if doctype == "Item":
                return list(pass1)
            if doctype == "Serial No" and filters.get("status") == "Active":
                return list(pass2)
            if doctype == "Serial No" and _is_archive_filter(filters):
                return list(archive_rows)
            if doctype == "Serial No":
                return list(delist_rows)
            return []
        return _fn

    def _run(self, **rows):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as mock_arch, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_del, \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = self._side_effect(**rows)
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        return client, mock_ga, mock_arch, mock_del, mock_sv

    @staticmethod
    def _serial_calls(mock_ga):
        return [c for c in mock_ga.call_args_list if c.args[:1] == ("Serial No",)]

    def _arm(self, mock_ga, *, archive: bool):
        """The get_all call for one Pass 3 arm."""
        for c in self._serial_calls(mock_ga):
            filters = c.kwargs.get("filters") or {}
            if filters.get("status") == "Active":
                continue  # Pass 2
            if _is_archive_filter(filters) == archive:
                return c
        raise AssertionError(f"Pass 3 {'archive' if archive else 'delist'} arm query not found")


class TestReconcilePass3Arms(Pass3ArmsHelpers):
    def test_archive_arm_archives_a_sold_serial(self):
        client, _ga, mock_arch, mock_del, _sv = self._run(
            archive_rows=[frappe._dict(
                name="SN-SOLD", woo_product_id="WC-SOLD", status="Delivered")])
        mock_arch.assert_called_once_with("SN-SOLD", client=client, site="retail")
        mock_del.assert_not_called()

    def test_archive_arm_passes_no_price(self):
        """Reconcile has no order line — never show an inexact figure."""
        _c, _ga, mock_arch, _del, _sv = self._run(
            archive_rows=[frappe._dict(
                name="SN-SOLD", woo_product_id="WC-SOLD", status="Delivered")])
        self.assertNotIn("sold_price", mock_arch.call_args.kwargs)

    def test_archive_arm_is_gated_by_its_own_sentinel(self):
        _c, mock_ga, _arch, _del, _sv = self._run()
        filters = self._arm(mock_ga, archive=True).kwargs.get("filters") or {}
        self.assertEqual(filters.get("woo_sold_archived"), 0)
        self.assertEqual(filters.get("status"), ["in", list(sync.SOLD_STATUS)])
        self.assertEqual(filters.get("woo_product_id"), ["!=", ""])

    def test_archive_arm_excludes_already_drafted_serials(self):
        """The archive arm must NOT pick up delisted=1.

        Tempting as it is to let the sweep rescue the historical zombies, a
        drafted-and-sold serial republishing itself means an unknown number of
        pages flip to `publish` unsupervised within 2 hours of deploy — bypassing
        the owner gate that the backfill exists to provide. Those serials surface
        only through the confirmed backfill. With call site 3 routed, no NEW sold
        gun lands in this state, so the population is finite and historical."""
        _c, mock_ga, _arch, _del, _sv = self._run()
        filters = self._arm(mock_ga, archive=True).kwargs.get("filters") or {}
        self.assertEqual(filters.get("woo_delisted"), 0)

    def test_pass3_leaves_a_drafted_sold_serial_to_the_backfill(self):
        """The SP000279 shape (Delivered AND delisted=1) must match NEITHER arm.

        Asserted by evaluating the row against the queries' real filters rather
        than by restating the filter literals — it is the exclusion that matters,
        not how it happens to be spelled."""
        _c, mock_ga, _arch, _del, _sv = self._run()
        zombie = {"name": "SP000279", "status": "Delivered", "woo_product_id": "WC-279",
                  "woo_delisted": 1, "woo_sold_archived": 0, "consigned_out": 0, "is_transfer_gun": 0}
        for archive in (True, False):
            call = self._arm(mock_ga, archive=archive)
            with self.subTest(arm="archive" if archive else "delist"):
                self.assertFalse(
                    _matches(zombie, call.kwargs.get("filters"),
                             call.kwargs.get("or_filters")),
                    "a drafted sold serial must wait for the owner-confirmed backfill",
                )

    def test_an_archived_serial_matches_neither_arm(self):
        """No re-work, no flapping: once archived, both arms are done with it."""
        _c, mock_ga, _arch, _del, _sv = self._run()
        archived = {"name": "SN-DONE", "status": "Delivered", "woo_product_id": "WC-1",
                    "woo_delisted": 0, "woo_sold_archived": 1, "consigned_out": 0, "is_transfer_gun": 0}
        for archive in (True, False):
            call = self._arm(mock_ga, archive=archive)
            with self.subTest(arm="archive" if archive else "delist"):
                self.assertFalse(_matches(archived, call.kwargs.get("filters"),
                                          call.kwargs.get("or_filters")))

    def test_delist_arm_excludes_sold_statuses(self):
        """Without this a sold gun would be drafted (404) by the delist arm and
        archived by the other one — a page flapping between 200 and 404."""
        _c, mock_ga, _arch, _del, _sv = self._run()
        filters = self._arm(mock_ga, archive=False).kwargs.get("filters") or {}
        self.assertEqual(filters.get("status"), ["not in", list(sync.SOLD_STATUS)])

    def test_delist_arm_keeps_both_sentinels(self):
        _c, mock_ga, _arch, _del, _sv = self._run()
        filters = self._arm(mock_ga, archive=False).kwargs.get("filters") or {}
        self.assertEqual(filters.get("woo_delisted"), 0)
        self.assertEqual(filters.get("woo_sold_archived"), 0)

    def test_delist_arm_still_ors_consigned_out(self):
        """The consigned-out safety net (an Active gun sitting at a dealer) must
        survive the split."""
        _c, mock_ga, _arch, _del, _sv = self._run()
        or_filters = self._arm(mock_ga, archive=False).kwargs.get("or_filters") or {}
        self.assertEqual(or_filters.get("consigned_out"), 1)
        self.assertEqual(or_filters.get("status"), ["!=", "Active"])

    def test_active_consigned_out_serial_is_delisted_not_archived(self):
        client, _ga, mock_arch, mock_del, _sv = self._run(
            delist_rows=[frappe._dict(
                name="SN-CONSIGNED", woo_product_id="WC-C", status="Active")])
        mock_del.assert_called_once_with("SN-CONSIGNED", client=client, site="retail")
        mock_arch.assert_not_called()

    def test_both_arms_count_towards_push_count(self):
        _c, _ga, _arch, _del, mock_sv = self._run(
            archive_rows=[frappe._dict(
                name="SN-SOLD", woo_product_id="WC-S", status="Delivered")],
            delist_rows=[frappe._dict(
                name="SN-GONE", woo_product_id="WC-G", status="Inactive")],
        )
        self.assertEqual(mock_sv.call_args[0][2]["last_push_count"], 2)

    def test_archive_failure_is_collected_not_fatal(self):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("frappe.get_all") as mock_ga, \
             patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold",
                   side_effect=RuntimeError("WC down")), \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as mock_del, \
             patch("frappe.db.set_value") as mock_sv:
            mock_ga.side_effect = self._side_effect(
                archive_rows=[frappe._dict(
                    name="SN-BAD", woo_product_id="WC-B", status="Delivered")],
                delist_rows=[frappe._dict(
                    name="SN-GONE", woo_product_id="WC-G", status="Inactive")],
            )
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        mock_del.assert_called_once()  # the other arm still ran
        self.assertIn("SN-BAD", mock_sv.call_args[0][2].get("last_sync_error", ""))


# ===========================================================================
# Call site 3 — the SLE hook (counter / POS sales)
# ===========================================================================

def _hidden_store():
    """A WC client whose page answer is 'draft' — what EXIT_SKIP assumes."""
    store = MagicMock()
    store.get_product.return_value = {"status": "draft", "stock_quantity": 0}
    return store


class TestCounterSaleRouting(FrappeTestCase):
    """ERPNext v16 flips Serial No status with a direct QB UPDATE that never
    fires on_update, so the stock-ledger hook is the ONLY exit signal a counter
    sale produces. It is how SP000279 got drafted in the first place."""

    def _run(self, **serial_state):
        from ffl_woo_sync.woocommerce import tasks

        state = {"status": "Delivered", "warehouse": "", "woo_reserved": 0,
                 "item_code": "GUN-1", "consigned_out": 0, "is_transfer_gun": 0}
        state.update(serial_state)
        def _gv(doctype, name, field, *args, **kwargs):
            if isinstance(field, (list, tuple)):
                return frappe._dict(state)
            return {"status": state["status"], "woo_delisted": 0}.get(field)

        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as push, \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_gv):
            tasks.sync_serial_listing_task("SN-COUNTER", site="retail")
        return arch, delist, push

    def test_counter_sale_archives_with_no_intermediate_draft(self):
        """The matrix cell: a gun sold at the counter goes
        straight to its SOLD page. delist_serial must never be called, or the
        page 404s for up to 2 hours until the sweep archives it back — the worst
        possible window for a page that has accrued rankings."""
        arch, delist, push = self._run(status="Delivered")
        arch.assert_called_once_with("SN-COUNTER", site="retail")
        delist.assert_not_called()
        push.assert_not_called()

    def test_manually_delisted_then_counter_sold_does_not_auto_archive(self):
        """The skip guard's accepted side effect, stated plainly:
        a gun an operator pulled from the web and later sold at the counter stays
        draft + 301 rather than resurfacing as a SOLD page. If the owner wants it
        surfaced, the backfill archives it explicitly."""
        from ffl_woo_sync.woocommerce import tasks

        def _gv(doctype, name, field, *args, **kwargs):
            if isinstance(field, (list, tuple)):
                return frappe._dict(status="Delivered", warehouse="", woo_reserved=0,
                                    item_code="GUN-1", consigned_out=0)
            return {"status": "Delivered", "woo_delisted": 1}.get(field)

        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
             patch("ffl_woo_sync.woocommerce.sync._client",
                   return_value=_hidden_store()), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_gv):
            tasks.sync_serial_listing_task("SN-PULLED", site="retail")
        arch.assert_not_called()
        delist.assert_not_called()  # already draft — no redundant WC PUT either

    def test_a_page_drafted_before_the_sale_stays_drafted(self):
        """A page drafted before the gun sold (another
        channel took it, or a manual pull) carries a 301; the retail-only rule cannot
        protect it (its site IS "retail", so the retail-only rule permits an
        archive) — only this guard keeps the draft and its redirect intact."""
        from ffl_woo_sync.woocommerce import tasks

        def _gv(doctype, name, field, *args, **kwargs):
            if isinstance(field, (list, tuple)):
                return frappe._dict(status="Delivered", warehouse="", woo_reserved=0,
                                    item_code="GUN-1", consigned_out=0)
            return {"status": "Delivered", "woo_delisted": 1}.get(field)

        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
             patch("ffl_woo_sync.woocommerce.sync._client",
                   return_value=_hidden_store()), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_gv):
            tasks.sync_serial_listing_task("SN-XSITE", site="retail")
        arch.assert_not_called()
        delist.assert_not_called()

    # ── EXIT_SKIP reality check + the 1020 race (serial 7008, 2026-09-03) ──

    def _run_pulled(self, store, *, product_id="WC-7008"):
        """Delivered + woo_delisted=1 (route says EXIT_SKIP); `store` is what
        WC answers for the page."""
        from ffl_woo_sync.woocommerce import tasks

        def _gv(doctype, name, field, *args, **kwargs):
            if isinstance(field, (list, tuple)):
                return frappe._dict(status="Delivered", warehouse="", woo_reserved=0,
                                    item_code="GUN-1", consigned_out=0)
            return {"status": "Delivered", "woo_delisted": 1,
                    "woo_product_id": product_id}.get(field)

        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
             patch("ffl_woo_sync.woocommerce.sync._client", return_value=store), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_gv):
            tasks.sync_serial_listing_task("SN-7008", site="retail")
        return arch, delist, store

    def test_stale_delisted_flag_on_a_live_page_still_archives(self):
        """Serial 7008: POS said delisted=1, the store said publish + stock 1
        (the relist PUT went out before its write-back lost a 1020). Trusting
        the flag left a sold gun buyable and every reconcile arm skipped it.
        The store's answer wins: archive, which also clears the stale flag."""
        store = MagicMock()
        store.get_product.return_value = {"status": "publish", "stock_quantity": 1}
        arch, delist, store = self._run_pulled(store)
        store.get_product.assert_called_once_with("WC-7008")
        arch.assert_called_once_with("SN-7008", client=store, site="retail")
        delist.assert_not_called()

    def test_genuinely_hidden_page_is_left_alone(self):
        arch, delist, store = self._run_pulled(_hidden_store())
        arch.assert_not_called()
        delist.assert_not_called()

    def test_missing_page_is_left_alone(self):
        store = MagicMock()
        store.get_product.return_value = None
        arch, delist, _ = self._run_pulled(store)
        arch.assert_not_called()

    def test_payload_without_status_is_not_read_as_live(self):
        """A PUBLISH decision must fail closed: is_hidden_on_woo is fail-open
        (missing `status` ⇒ not hidden) for delist decisions, and borrowing it
        here would republish a deliberately drafted page and delete its 301."""
        store = MagicMock()
        store.get_product.return_value = {"stock_quantity": 1}
        arch, _delist, _ = self._run_pulled(store)
        arch.assert_not_called()

    def test_dangling_product_id_is_a_warning_not_an_error_log(self):
        """get_product raises on 404 (client._request), it does not return
        None — the same shape push_stock downgrades for a page deleted on the
        store. Nothing live to archive, and no Error Log per sale."""
        from ffl_woo_sync.woocommerce.client import WooCommerceError

        store = MagicMock()
        store.get_product.side_effect = WooCommerceError("GET → 404", status_code=404)
        with patch("frappe.log_error") as log:
            arch, _delist, _ = self._run_pulled(store)
        arch.assert_not_called()
        log.assert_not_called()

    def test_other_http_errors_on_the_check_are_still_logged(self):
        from ffl_woo_sync.woocommerce.client import WooCommerceError

        store = MagicMock()
        store.get_product.side_effect = WooCommerceError("GET → 500", status_code=500)
        with patch("frappe.log_error") as log:
            arch, _delist, _ = self._run_pulled(store)
        arch.assert_not_called()
        log.assert_called_once()

    def test_no_product_id_costs_no_http(self):
        store = MagicMock()
        arch, _delist, store = self._run_pulled(store, product_id="")
        store.get_product.assert_not_called()
        arch.assert_not_called()

    def test_deadlock_is_handed_back_to_the_job_runner(self):
        """The 1020 must NOT be swallowed into an Error Log: raise
        RetryBackgroundJobError so execute_job re-runs the decision on the
        committed row. Swallowing it is how 7008's exit routing never ran."""
        from ffl_woo_sync.woocommerce import tasks

        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   side_effect=frappe.QueryDeadlockError((1020, "Record has changed"))), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.log_error") as log, \
             patch("frappe.db.get_value", return_value=frappe._dict(
                 status="Active", warehouse="Stores - FT", woo_reserved=0,
                 item_code="GUN-1", consigned_out=0, is_transfer_gun=0)):
            with self.assertRaises(frappe.RetryBackgroundJobError):
                tasks.sync_serial_listing_task("SN-RACE", site="retail")
        log.assert_not_called()

    def test_lock_wait_timeout_is_retried_the_same_way(self):
        """1205 on the same write-back reproduces the identical defect."""
        from ffl_woo_sync.woocommerce import tasks

        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   side_effect=frappe.QueryTimeoutError((1205, "Lock wait timeout"))), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", return_value=frappe._dict(
                 status="Active", warehouse="Stores - FT", woo_reserved=0,
                 item_code="GUN-1", consigned_out=0, is_transfer_gun=0)):
            with self.assertRaises(frappe.RetryBackgroundJobError):
                tasks.sync_serial_listing_task("SN-RACE", site="retail")

    def test_the_7008_race_through_a_real_push_serial(self):
        """The actual mechanism, not a mocked push: the WC PUT lands, then
        push_serial's own write-back hits 1020 on the stale snapshot, its inner
        `except` tries to stamp sync_error on the SAME snapshot and hits 1020
        again — and what reaches this task must still be the deadlock class,
        converted to a retry, not swallowed."""
        from ffl_woo_sync.woocommerce import tasks

        client = _make_client()
        boom = frappe.QueryDeadlockError((1020, "Record has changed since last read"))
        with patch("ffl_woo_sync.woocommerce.sync._client", return_value=client), \
             patch("ffl_woo_sync.woocommerce.sync.other_channel_reservation",
                   return_value=None), \
             patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                   return_value={"sku": "GUN-1::SN-7008", "status": "publish",
                                 "stock_quantity": 1}), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_gv_factory(
                 status="Active", warehouse="Stores - FT", woo_reserved=0,
                 item_code="GUN-1", consigned_out=0, is_transfer_gun=0,
                 woo_product_id="4773", woo_delisted=0, woo_sold_archived=0)), \
             patch("frappe.db.set_value", side_effect=boom) as mock_sv, \
             patch("frappe.log_error") as log:
            with self.assertRaises(frappe.RetryBackgroundJobError):
                tasks.sync_serial_listing_task("SN-7008", site="retail")
        client.update_product.assert_called_once()   # the PUT had gone out
        self.assertEqual(mock_sv.call_count, 2)      # write-back, then sync_error stamp
        log.assert_not_called()

    def test_other_failures_are_still_logged_not_raised(self):
        from ffl_woo_sync.woocommerce import tasks

        with patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   side_effect=RuntimeError("WC down")), \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.log_error") as log, \
             patch("frappe.db.get_value", return_value=frappe._dict(
                 status="Active", warehouse="Stores - FT", woo_reserved=0,
                 item_code="GUN-1", consigned_out=0, is_transfer_gun=0)):
            tasks.sync_serial_listing_task("SN-DOWN", site="retail")
        log.assert_called_once()

    def test_counter_sale_passes_no_price(self):
        """POS knows a figure, but `_osa_sold_price` is reserved for web
        fulfillment's order line — the one path with the exact realized amount."""
        arch, _delist, _push = self._run(status="Delivered")
        self.assertNotIn("sold_price", arch.call_args.kwargs)

    def test_consigned_out_gun_still_delists(self):
        """Regression guard: Active + consigned_out is a removal, not a sale."""
        arch, delist, _push = self._run(status="Active", consigned_out=1)
        delist.assert_called_once_with("SN-COUNTER", site="retail")
        arch.assert_not_called()

    def test_reserved_gun_still_delists(self):
        arch, delist, _push = self._run(status="Active", warehouse="Stores - FT",
                                        woo_reserved=1)
        delist.assert_called_once_with("SN-COUNTER", site="retail")
        arch.assert_not_called()

    def test_sellable_gun_still_pushes(self):
        """The routing change must not touch the push side of the hook."""
        arch, delist, push = self._run(status="Active", warehouse="Stores - FT")
        push.assert_called_once_with("SN-COUNTER", site="retail", _relist=True,
                                     pos_fields_only=True)
        arch.assert_not_called()
        delist.assert_not_called()

# ===========================================================================
# archive_serial_sold_task
# ===========================================================================

class TestArchiveSerialSoldTask(FrappeTestCase):
    def test_forwards_price_and_site(self):
        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as mock_a:
            from ffl_woo_sync.woocommerce.tasks import archive_serial_sold_task
            archive_serial_sold_task("SN-1", sold_price=1589.0, site="retail")
        mock_a.assert_called_once_with("SN-1", sold_price=1589.0, site="retail")

    def test_defaults_to_retail_and_no_price(self):
        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as mock_a:
            from ffl_woo_sync.woocommerce.tasks import archive_serial_sold_task
            archive_serial_sold_task("SN-1")
        mock_a.assert_called_once_with("SN-1", sold_price=None, site="retail")

    def test_absorbs_and_logs_failure(self):
        """A background job must not crash the worker."""
        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold",
                   side_effect=RuntimeError("boom")), \
             patch("frappe.log_error") as mock_log:
            from ffl_woo_sync.woocommerce.tasks import archive_serial_sold_task
            archive_serial_sold_task("SN-1")  # must not raise
        mock_log.assert_called_once()


# ===========================================================================
# Web fulfillment — realized-price pass-through
# ===========================================================================

class TestFulfillmentRouting(FrappeTestCase):
    def _run_delist_disposed(self, *, status, sold_prices=None, pid="WC-1"):
        from ffl_woo_sync.woocommerce.fulfillment import _delist_disposed

        def _gv(doctype, name, field, *args, **kwargs):
            if field == "status":
                return status
            if field == "woo_product_id":
                return pid
            return None

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.enqueue") as mock_enq:
            _delist_disposed([("GUN-1", "SN-1")], "retail", sold_prices or {})
        return mock_enq

    def test_sold_serial_enqueues_archive_with_realized_price(self):
        mock_enq = self._run_delist_disposed(
            status="Delivered", sold_prices={"SN-1": 1589.0})
        target = mock_enq.call_args[0][0]
        self.assertEqual(target, "ffl_woo_sync.woocommerce.tasks.archive_serial_sold_task")
        self.assertEqual(mock_enq.call_args.kwargs["sold_price"], 1589.0)
        self.assertEqual(mock_enq.call_args.kwargs["serial_no"], "SN-1")

    def test_archive_is_enqueued_after_commit(self):
        """Same transaction discipline as the delist path it replaces."""
        mock_enq = self._run_delist_disposed(
            status="Delivered", sold_prices={"SN-1": 1589.0})
        self.assertTrue(mock_enq.call_args.kwargs["enqueue_after_commit"])

    def test_missing_price_archives_without_a_figure(self):
        """No line match → archive anyway, just with no 'Sold for $X'."""
        mock_enq = self._run_delist_disposed(status="Delivered", sold_prices={})
        self.assertEqual(mock_enq.call_args[0][0],
                         "ffl_woo_sync.woocommerce.tasks.archive_serial_sold_task")
        self.assertIsNone(mock_enq.call_args.kwargs["sold_price"])

    def test_non_sold_serial_still_delists(self):
        mock_enq = self._run_delist_disposed(status="Active")
        self.assertEqual(mock_enq.call_args[0][0],
                         "ffl_woo_sync.woocommerce.tasks.delist_serial_task")

    def test_already_drafted_serial_enqueues_nothing(self):
        """At call site 1: fulfillment must not resurrect a page that
        was deliberately drafted before the sale."""
        from ffl_woo_sync.woocommerce.fulfillment import _delist_disposed

        def _gv(doctype, name, field, *args, **kwargs):
            return {"status": "Delivered", "woo_delisted": 1,
                    "woo_product_id": "WC-1"}.get(field)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.enqueue") as mock_enq:
            _delist_disposed([("GUN-1", "SN-1")], "retail", {"SN-1": 1589.0})
        mock_enq.assert_not_called()

    def test_unlisted_serial_enqueues_nothing_for_the_serial(self):
        from ffl_woo_sync.woocommerce.fulfillment import _delist_disposed

        with patch("frappe.db.get_value", return_value=None), \
             patch("frappe.enqueue") as mock_enq:
            _delist_disposed([("GUN-1", "SN-1")], "retail", {})
        mock_enq.assert_not_called()


class TestSoldPrices(FrappeTestCase):
    """Serial → realized per-unit price, joined on the WooCommerce line id."""

    def _order(self, lines, serials):
        return frappe._dict(lines=[frappe._dict(**l) for l in lines],
                            serials=[frappe._dict(**s) for s in serials])

    def test_maps_serial_to_its_line_total(self):
        from ffl_woo_sync.woocommerce.fulfillment import _sold_prices

        order = self._order(
            lines=[{"woo_line_id": "11", "qty": 1, "unit_price": 1589.0,
                    "line_total": 1489.0, "line_tax": 92.0}],
            serials=[{"woo_line_id": "11", "confirmed_serial": "SN-1"}],
        )
        # line_total is post-discount and EX-tax — the apples-to-apples realized
        # figure against the listing's own ex-tax regular_price.
        self.assertEqual(_sold_prices(order), {"SN-1": 1489.0})

    def test_splits_a_multi_unit_line_per_unit(self):
        from ffl_woo_sync.woocommerce.fulfillment import _sold_prices

        order = self._order(
            lines=[{"woo_line_id": "11", "qty": 2, "unit_price": 500.0,
                    "line_total": 900.0, "line_tax": 0.0}],
            serials=[{"woo_line_id": "11", "confirmed_serial": "SN-1"},
                     {"woo_line_id": "11", "confirmed_serial": "SN-2"}],
        )
        self.assertEqual(_sold_prices(order), {"SN-1": 450.0, "SN-2": 450.0})

    def test_zero_total_yields_no_price(self):
        """A $0 line (comp/warranty replacement) must not render 'Sold for $0.00'."""
        from ffl_woo_sync.woocommerce.fulfillment import _sold_prices

        order = self._order(
            lines=[{"woo_line_id": "11", "qty": 1, "unit_price": 0.0,
                    "line_total": 0.0, "line_tax": 0.0}],
            serials=[{"woo_line_id": "11", "confirmed_serial": "SN-1"}],
        )
        self.assertEqual(_sold_prices(order), {})

    def test_serial_without_a_matching_line_is_skipped(self):
        from ffl_woo_sync.woocommerce.fulfillment import _sold_prices

        order = self._order(
            lines=[{"woo_line_id": "11", "qty": 1, "unit_price": 100.0,
                    "line_total": 100.0, "line_tax": 0.0}],
            serials=[{"woo_line_id": "99", "confirmed_serial": "SN-ORPHAN"}],
        )
        self.assertEqual(_sold_prices(order), {})


# ===========================================================================
# PRODUCTION GAP — `Consumed` is a sold status too (hotfix)
# ===========================================================================

class TestConsumedIsSold(FrappeTestCase):
    """The routing shipped with `SOLD_STATUS = "Delivered"` alone, which made the
    flagship path — a web sale becoming a SOLD page — fail silently in prod.

    ERPNext derives the terminal status from the stock-out VOUCHER, not from the
    business meaning of the sale (`serial_batch_bundle.get_status_for_serial_nos`):
    a negative SLE is `Delivered` unless it came from a Stock Entry whose purpose
    is in `CONSUMED_SERIAL_NO_STOCK_ENTRY_PURPOSES` — which includes
    **Material Issue**. Web fulfillment books an FFL Disposition whose on_submit
    issues exactly that Material Issue (`disposition_stock.issue_stock_on_submit`),
    so every web-sold gun lands on `Consumed` and routed to delist → draft → 404.
    Counter/B2B sales go out on POS Invoice / Sales Invoice, stay `Delivered`,
    and worked — which is why the unit matrix never caught it.

    The tests below assert the CONTRACT (every member of SOLD_STATUS is sold)
    plus an explicit `Consumed` pin, so removing it from the tuple fails loudly.
    """

    def test_consumed_is_a_sold_status(self):
        self.assertIn("Consumed", sync.SOLD_STATUS)

    def test_delivered_is_still_a_sold_status(self):
        """The counter/B2B path must not regress while fixing the web path."""
        self.assertIn("Delivered", sync.SOLD_STATUS)

    def test_membership_is_exact_not_substring(self):
        """A tuple, not a string — `"Deliver" in "Delivered"` would be True and
        would silently widen the predicate."""
        self.assertIsInstance(sync.SOLD_STATUS, tuple)
        self.assertFalse(sync.is_sold_state("Deliver", 0))
        self.assertFalse(sync.is_sold_state("Consume", 0))

    def test_every_sold_status_routes_to_archive(self):
        for status in sync.SOLD_STATUS:
            with self.subTest(status=status):
                with patch("frappe.db.get_value", side_effect=_gv_factory(
                        status=status, woo_delisted=0)):
                    self.assertEqual(sync.route_serial_exit("SN-1"), sync.EXIT_ARCHIVE)

    def test_every_sold_status_reads_as_sold_state(self):
        for status in sync.SOLD_STATUS:
            with self.subTest(status=status):
                self.assertTrue(sync.is_sold_state(status, 0))

    def test_non_sold_statuses_still_delist(self):
        for status in ("Active", "Inactive", "Expired", ""):
            with self.subTest(status=status):
                with patch("frappe.db.get_value", return_value=status):
                    self.assertEqual(sync.route_serial_exit("SN-1"), sync.EXIT_DELIST)

    def test_consumed_and_drafted_still_skips(self):
        """The skip guard must hold for the newly-recognised status too."""
        with patch("frappe.db.get_value", side_effect=_gv_factory(
                status="Consumed", woo_delisted=1)):
            self.assertEqual(sync.route_serial_exit("SN-1"), sync.EXIT_SKIP)

    def test_consumed_payload_is_sold_aware(self):
        """mapping inherits the fix through is_sold_state — pinned, not assumed."""
        serial = MagicMock()
        serial.name = "SN-WEB"
        serial.item_code = "GUN-1"
        serial.status = "Consumed"
        serial.image_gallery = []
        serial.get = lambda f, d=None: {"sell_price": 899.0, "woo_reserved": 0}.get(f, d)
        item = MagicMock()
        item.item_code = "GUN-1"
        item.item_name = "PSA Spiker"
        item.description = ""
        item.image_gallery = []
        item.get = lambda f, d=None: None
        with patch("frappe.get_doc", side_effect=[serial, item]):
            payload = mapping.serial_to_product_payload("SN-WEB")
        self.assertEqual(payload["status"], "publish")
        self.assertEqual(payload["stock_quantity"], 0)


class TestConsumedAtEveryCallSite(FrappeTestCase):
    """The gap was invisible because each call site was only ever exercised with
    `Delivered`. Pin all three against the status prod actually produces."""

    def test_web_fulfillment_archives_a_consumed_serial(self):
        """Call site 1 — THE regression. This is the exact prod path: dispose a
        web order, serial goes Consumed, page must become a SOLD archive."""
        from ffl_woo_sync.woocommerce.fulfillment import _delist_disposed

        def _gv(doctype, name, field, *args, **kwargs):
            return {"status": "Consumed", "woo_delisted": 0,
                    "woo_product_id": "WC-1"}.get(field)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.enqueue") as mock_enq:
            _delist_disposed([("GUN-1", "SN-1")], "retail", {"SN-1": 1589.0})
        self.assertEqual(mock_enq.call_args[0][0],
                         "ffl_woo_sync.woocommerce.tasks.archive_serial_sold_task")
        self.assertEqual(mock_enq.call_args.kwargs["sold_price"], 1589.0)

    def test_sle_hook_archives_a_consumed_serial(self):
        """Call site 3 — the Material Issue also fires the SLE hook, so both
        paths must agree or the page flaps."""
        from ffl_woo_sync.woocommerce import tasks

        def _gv(doctype, name, field, *args, **kwargs):
            if isinstance(field, (list, tuple)):
                return frappe._dict(status="Consumed", warehouse="", woo_reserved=0,
                                    item_code="GUN-1", consigned_out=0)
            return {"status": "Consumed", "woo_delisted": 0}.get(field)

        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as arch, \
             patch("ffl_woo_sync.woocommerce.sync.delist_serial") as delist, \
             patch("ffl_core.reservations.counter_holds", return_value={}), \
             patch("frappe.db.get_value", side_effect=_gv):
            tasks.sync_serial_listing_task("SN-WEB", site="retail")
        arch.assert_called_once_with("SN-WEB", site="retail")
        delist.assert_not_called()


class TestReconcileArmsCoverConsumed(Pass3ArmsHelpers):
    """Call site 2 — the sweep is the safety net that should have caught the
    prod gap. It didn't, because its filters carried the same single status."""

    def test_archive_arm_selects_both_sold_statuses(self):
        _c, mock_ga, _arch, _del, _sv = self._run()
        filters = self._arm(mock_ga, archive=True).kwargs.get("filters") or {}
        self.assertEqual(filters.get("status"), ["in", list(sync.SOLD_STATUS)])

    def test_delist_arm_excludes_both_sold_statuses(self):
        _c, mock_ga, _arch, _del, _sv = self._run()
        filters = self._arm(mock_ga, archive=False).kwargs.get("filters") or {}
        self.assertEqual(filters.get("status"), ["not in", list(sync.SOLD_STATUS)])

    def test_a_consumed_serial_matches_archive_and_not_delist(self):
        """The property that was broken in prod, asserted against the queries'
        real filters rather than their literals."""
        _c, mock_ga, _arch, _del, _sv = self._run()
        web_sold = {"name": "348S", "status": "Consumed", "woo_product_id": "WC-348",
                    "woo_delisted": 0, "woo_sold_archived": 0, "consigned_out": 0, "is_transfer_gun": 0}
        archive_call = self._arm(mock_ga, archive=True)
        delist_call = self._arm(mock_ga, archive=False)
        self.assertTrue(_matches(web_sold, archive_call.kwargs.get("filters"),
                                 archive_call.kwargs.get("or_filters")),
                        "a web-sold (Consumed) serial must reach the archive arm")
        self.assertFalse(_matches(web_sold, delist_call.kwargs.get("filters"),
                                  delist_call.kwargs.get("or_filters")),
                         "and must NOT be drafted by the delist arm")
