"""Machine pushes must not overwrite Woo-owned content (``pos_fields_only``).

Incident 2026-08-12/13: wp-admin content edits (title, description, images)
were overwritten by the 2-hourly reconcile and the doc_events full pushes —
any stock/price drift re-sent the ENTIRE product payload built from POS.

The contract under test:

* ``pos_fields_only=True`` + existing product  → the update payload is reduced
  to the POS-owned fields (``sync._POS_OWNED_KEYS`` — status / stock / price)
  plus lifecycle/pricing meta (``sync._POS_OWNED_META_KEYS``).  Content keys
  (name, description, images, attributes, sku, brands, shipping class, GTIN,
  content meta) are never sent; WooCommerce meta_data is an upsert, so a key
  not sent keeps its stored value.
* ``pos_fields_only=True`` + NO product id     → the CREATE stays full-payload
  (there is no content on the shop to protect yet).
* default (``False``)                          → unchanged full push — the
  manual entrypoints (push buttons, relist, reconcile_now, listing pipeline)
  keep their behaviour; their existing tests pin the exact kwargs.

All WooCommerce HTTP and Frappe DB access is mocked, same idiom as
``test_sync.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import sync

_FAKE_NOW = "2026-08-13 00:00:00"

# A full item payload as the builder would emit it, plus every optional content
# key push_item can attach — the filter must strip all of them.
_FULL_ITEM_PAYLOAD = {
    "name": "POS Item Name",
    "sku": "ITEM-1",
    "type": "simple",
    "status": "publish",
    "regular_price": "100.00",
    "sale_price": "89.00",
    "date_on_sale_from": "2026-08-10T00:00:00",
    "date_on_sale_to": "2026-08-31T23:59:59",
    "description": "POS description that must NOT overwrite wp-admin",
    "manage_stock": True,
    "stock_quantity": 5,
    "global_unique_id": "012345678905",
    "images": [{"src": "https://pos.example/img.jpg"}],
    "attributes": [{"name": "Caliber", "options": ["9mm"]}],
    "brands": [{"id": 7}],
    "shipping_class": "dropship-firearm",
    "meta_data": [
        {"key": "_firearm_product", "value": "yes"},
        {"key": "_ammunition_product", "value": "no"},
        {"key": "_osa_restricted_states", "value": "CA,NY"},
        # Keys the retired dealer store read. No builder emits them any more, so
        # they are no longer machine-owned: a lean push must not write them.
        {"key": "_osa_publish_to_dealer", "value": "yes"},
        {"key": "_osa_dealer_price_override", "value": "80.00"},
    ],
}

_FULL_SERIAL_PAYLOAD = {
    "name": "POS Serial Listing Title",
    "sku": "SG551::410054",
    "type": "simple",
    "status": "publish",
    "regular_price": "5400.00",
    # The clearing shape — the trio is always emitted, "" takes a sale down.
    "sale_price": "",
    "date_on_sale_from": "",
    "date_on_sale_to": "",
    "description": "POS gun description",
    "manage_stock": True,
    "stock_quantity": 1,
    "images": [{"src": "https://pos.example/gun.jpg"}],
    "attributes": [{"name": "Condition", "options": ["Grade A"]}],
    "meta_data": [
        {"key": "_firearm_product", "value": "yes"},
        {"key": "_osa_serial", "value": "410054"},
        {"key": "_osa_sold_at", "value": ""},
        {"key": "_osa_sold_price", "value": ""},
    ],
}


def _make_client(create_id=999, update_id=42):
    client = MagicMock()
    client.get_attribute_ids.return_value = {}
    client.create_product.return_value = {"id": create_id, "status": "publish"}
    client.update_product.return_value = {"id": update_id, "status": "publish"}
    client.get_product.return_value = {"id": update_id}
    return client


# Both stubs hand anything that is not their own doctype to the REAL
# ``frappe.db.get_value``: ``frappe.get_meta`` (reservations' channel-field
# probe) loads a DocType from the database on a cold cache, and a stub that
# only knew its one doctype turned that into a TypeError.
_REAL_GET_VALUE = frappe.db.get_value


def _item_gv(**overrides):
    """Field-aware ``frappe.db.get_value`` stub: an ordinary, priced, visible item."""
    values = {
        "standard_rate": 100.0, "woo_delisted": 0,
        "item_group": None, "woo_product_id": "42",
    }
    values.update(overrides)

    def _fn(doctype, *a, **k):
        if doctype != "Item":
            return _REAL_GET_VALUE(doctype, *a, **k)
        field = a[1] if len(a) > 1 else k.get("fieldname")
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _fn


def _serial_gv(**overrides):
    """Field-aware stub for a priced, visible, ordinary (non-custody) serial."""
    values = {
        "sell_price": 5400, "woo_delisted": 0,
        "item_code": "SG551", "woo_product_id": "77",
        "status": "Active", "woo_sold_archived": 0, "is_transfer_gun": 0,
        "is_firearm": 1, "uniform_sell_price": 0,
    }
    values.update(overrides)

    def _fn(doctype, *a, **k):
        if doctype != "Serial No":
            return _REAL_GET_VALUE(doctype, *a, **k)
        field = a[1] if len(a) > 1 else k.get("fieldname")
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _fn


class _PatchNow(FrappeTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("ffl_woo_sync.woocommerce.sync._now", return_value=_FAKE_NOW)
        self.addCleanup(patcher.stop)
        patcher.start()


# ---------------------------------------------------------------------------
# The filter itself
# ---------------------------------------------------------------------------

class TestPosOwnedPayloadFilter(FrappeTestCase):
    def test_strips_every_content_key(self):
        lean = sync._pos_owned_payload(_FULL_ITEM_PAYLOAD)
        # shipping_class survives on purpose: ops-owned, and the lean
        # `*/15` distributor drift push is how a class change reaches the store.
        self.assertEqual(
            set(lean),
            {"status", "regular_price", "manage_stock", "stock_quantity",
             "sale_price", "date_on_sale_from", "date_on_sale_to",
             "shipping_class", "meta_data"},
        )

    def test_meta_keeps_only_machine_owned_keys(self):
        # Every builder-emitted meta key is machine-owned and passes; the
        # protection is for keys the builders DON'T emit (SEO plugins etc.),
        # which the upsert then leaves untouched on the store.
        lean = sync._pos_owned_payload(_FULL_ITEM_PAYLOAD)
        self.assertEqual(
            {m["key"] for m in lean["meta_data"]},
            {"_firearm_product", "_ammunition_product", "_osa_restricted_states"},
        )

    def test_meta_key_omitted_entirely_when_nothing_pos_owned(self):
        payload = dict(
            _FULL_ITEM_PAYLOAD,
            # A hand-curated key no builder emits — the kind the filter exists
            # to keep machine pushes from ever writing.
            meta_data=[{"key": "_yoast_wpseo_title", "value": "SEO copy"}],
        )
        lean = sync._pos_owned_payload(payload)
        self.assertNotIn("meta_data", lean)

    def test_sold_lifecycle_meta_survives(self):
        """clear_sold blanks must flow through a machine relist, or a returned
        gun's page reads SOLD forever (meta upsert keeps unsent keys)."""
        lean = sync._pos_owned_payload(_FULL_SERIAL_PAYLOAD)
        keys = {m["key"] for m in lean["meta_data"]}
        self.assertIn("_osa_sold_at", keys)
        self.assertIn("_osa_sold_price", keys)

    def test_values_pass_through_unchanged(self):
        lean = sync._pos_owned_payload(_FULL_ITEM_PAYLOAD)
        self.assertEqual(lean["stock_quantity"], 5)
        self.assertEqual(lean["regular_price"], "100.00")
        self.assertEqual(lean["sale_price"], "89.00")
        self.assertEqual(lean["date_on_sale_to"], "2026-08-31T23:59:59")
        self.assertEqual(lean["status"], "publish")


# ---------------------------------------------------------------------------
# push_item
# ---------------------------------------------------------------------------

class TestPushItemPosFieldsOnly(_PatchNow):
    def _run(self, *, woo_product_id="42", pos_fields_only=True):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                   return_value=dict(_FULL_ITEM_PAYLOAD)), \
             patch("ffl_woo_sync.woocommerce.sync.effective_item_price",
                   return_value=100.0), \
             patch("ffl_woo_sync.woocommerce.sync._apply_category") as mock_cat, \
             patch("frappe.db.get_value",
                   side_effect=_item_gv(woo_product_id=woo_product_id)), \
             patch("frappe.db.set_value"):
            sync.push_item("ITEM-1", client=client, attr_ids={}, _retries=0,
                           pos_fields_only=pos_fields_only)
        return client, mock_cat

    def test_machine_update_sends_only_pos_owned_fields(self):
        client, _cat = self._run()
        client.update_product.assert_called_once()
        woo_id, payload = client.update_product.call_args[0]
        self.assertEqual(woo_id, "42")
        self.assertEqual(
            set(payload),
            {"status", "regular_price", "manage_stock", "stock_quantity",
             "sale_price", "date_on_sale_from", "date_on_sale_to",
             "shipping_class", "meta_data"},
        )
        for key in ("name", "description", "images", "attributes", "sku",
                    "brands", "global_unique_id"):
            self.assertNotIn(key, payload)

    def test_machine_update_skips_the_category_lookup(self):
        """Categories are Woo-owned content: a lean update must not resolve or
        send them (also saves the taxonomy round-trip per push)."""
        _client, mock_cat = self._run()
        mock_cat.assert_not_called()

    def test_machine_create_stays_full_payload(self):
        client, _cat = self._run(woo_product_id="")
        client.create_product.assert_called_once()
        payload = client.create_product.call_args[0][0]
        self.assertIn("description", payload)
        self.assertIn("images", payload)
        self.assertIn("name", payload)

    def test_default_push_still_sends_the_full_payload(self):
        """Manual entrypoints leave pos_fields_only False — unchanged."""
        client, _cat = self._run(pos_fields_only=False)
        payload = client.update_product.call_args[0][1]
        self.assertIn("description", payload)
        self.assertIn("images", payload)


# ---------------------------------------------------------------------------
# push_serial
# ---------------------------------------------------------------------------

class TestPushSerialPosFieldsOnly(_PatchNow):
    def _run(self, *, woo_product_id="77", pos_fields_only=True):
        client = _make_client()
        with patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload",
                   return_value=dict(_FULL_SERIAL_PAYLOAD)), \
             patch("ffl_woo_sync.woocommerce.sync.is_custody_serial",
                   return_value=False), \
             patch("ffl_woo_sync.woocommerce.sync.effective_serial_price",
                   return_value=5400.0), \
             patch("ffl_woo_sync.woocommerce.sync._apply_category") as mock_cat, \
             patch("frappe.db.get_value",
                   side_effect=_serial_gv(woo_product_id=woo_product_id)), \
             patch("frappe.db.set_value"):
            sync.push_serial("410054", client=client, attr_ids={}, _retries=0,
                             pos_fields_only=pos_fields_only)
        return client, mock_cat

    def test_machine_update_sends_only_pos_owned_fields(self):
        client, mock_cat = self._run()
        client.update_product.assert_called_once()
        woo_id, payload = client.update_product.call_args[0]
        self.assertEqual(woo_id, "77")
        self.assertEqual(
            set(payload),
            {"status", "regular_price", "manage_stock", "stock_quantity",
             "sale_price", "date_on_sale_from", "date_on_sale_to", "meta_data"},
        )
        # status survives on purpose: machine relists (webhook release,
        # stock-movement return) republish through this exact payload.
        self.assertEqual(payload["status"], "publish")
        self.assertEqual(
            {m["key"] for m in payload["meta_data"]},
            {"_firearm_product", "_osa_serial", "_osa_sold_at", "_osa_sold_price"},
        )
        mock_cat.assert_not_called()

    def test_machine_create_stays_full_payload(self):
        client, _cat = self._run(woo_product_id="")
        client.create_product.assert_called_once()
        payload = client.create_product.call_args[0][0]
        self.assertIn("description", payload)
        self.assertIn("name", payload)
        self.assertEqual(payload["sku"], "SG551::410054")

    def test_default_push_still_sends_the_full_payload(self):
        client, _cat = self._run(pos_fields_only=False)
        payload = client.update_product.call_args[0][1]
        self.assertIn("description", payload)
        self.assertIn("images", payload)


# ---------------------------------------------------------------------------
# The allowlist vs the REAL payload builders (the earlier
# tests only reconciled the filter against hand-written fixtures, so a builder
# growing a new POS-owned key would silently stop syncing with every test
# green). The builders are pure functions — call them for real: any NEW
# top-level or meta key they emit lands in these set assertions and forces a
# conscious classification.
# ---------------------------------------------------------------------------

from ffl_woo_sync.woocommerce import mapping  # noqa: E402


class TestAllowlistCoversTheRealBuilders(FrappeTestCase):
    _ITEM_CONTENT_KEYS = {
        "name", "sku", "type", "description", "global_unique_id",
        "images", "attributes", "brands",
    }
    _SERIAL_CONTENT_KEYS = {
        "name", "sku", "type", "description", "global_unique_id",
        "images", "attributes",
    }

    def test_item_builder_splits_exactly_into_owned_and_content(self):
        payload = mapping.build_simple_product_payload(
            item_code="RSR-ACC-1", item_name="Optic", description="<p>d</p>",
            standard_rate=349.99, stock_qty=7, is_firearm=True, is_used=True,
            condition_grade="Grade A", caliber="9mm", item_group="Pistol",
            upc="012345678905", manufacturer="Holosun", model_name="HS510C",
            image_srcs=["https://x/i.jpg"],
            restricted_states="CA,NY", fulfillment_mode="Direct",
            brand_id=7, shipping_class="dropship-accessory",
        )
        self.assertEqual(
            set(payload) - sync._POS_OWNED_KEYS - {"meta_data"},
            self._ITEM_CONTENT_KEYS,
        )
        meta_keys = {m["key"] for m in payload["meta_data"]}
        self.assertLessEqual(meta_keys, sync._POS_OWNED_META_KEYS)
        # The compliance/ops keys the lean `*/15` drift push must deliver.
        self.assertIn("_osa_restricted_states", meta_keys)
        self.assertIn("_osa_fulfillment_mode", meta_keys)
        self.assertIn("shipping_class", sync._POS_OWNED_KEYS)

    def test_serial_builder_splits_exactly_into_owned_and_content(self):
        payload = mapping.build_serial_product_payload(
            item_code="SG551", serial_no="410054", item_name="SG 551",
            description="line", sell_price=5400.0, is_antique=False,
            is_used=True, condition_grade="Grade A", caliber="5.56",
            upc="012345678905", manufacturer="SIG", model_name="551",
            woo_reserved=False, is_active=True,
            sold_archived=False, clear_sold=True,
            image_srcs=["https://x/g.jpg"], attr_ids=None,
        )
        self.assertEqual(
            set(payload) - sync._POS_OWNED_KEYS - {"meta_data"},
            self._SERIAL_CONTENT_KEYS,
        )
        meta_keys = {m["key"] for m in payload["meta_data"]}
        self.assertLessEqual(meta_keys, sync._POS_OWNED_META_KEYS)
        # clear_sold blanks must ride the lean machine relist.
        self.assertIn("_osa_sold_at", meta_keys)
        self.assertIn("_osa_sold_price", meta_keys)

    def test_serial_builder_sold_archived_retail_variant_also_reconciles(self):
        """Second input shape: a SOLD-archived retail
        serial (no clear_sold) — the probe must hold for
        the degenerate emission set too."""
        payload = mapping.build_serial_product_payload(
            item_code="SG551", serial_no="410054", item_name="SG 551",
            description="", sell_price=5400.0, is_antique=True,
            is_used=True, condition_grade="", caliber="",
            upc="", manufacturer="", model_name="",
            woo_reserved=False, is_active=False,
            sold_archived=True, clear_sold=False,
            image_srcs=None, attr_ids=None,
        )
        self.assertLessEqual(
            set(payload) - sync._POS_OWNED_KEYS - {"meta_data"},
            self._SERIAL_CONTENT_KEYS,
        )
        self.assertLessEqual(
            {m["key"] for m in payload["meta_data"]}, sync._POS_OWNED_META_KEYS)
        self.assertEqual(payload["stock_quantity"], 0)


# ---------------------------------------------------------------------------
# Task wrappers: machine default vs the deliberate full override (r1 blocker
# pin — serial_correction depends on pos_fields_only=False reaching sync)
# ---------------------------------------------------------------------------

class TestTaskWrappersHonorTheOverride(FrappeTestCase):
    def test_push_serial_task_full_override_reaches_sync(self):
        from ffl_woo_sync.woocommerce.tasks import push_serial_task
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            push_serial_task("SN-1", pos_fields_only=False)
        mock_ps.assert_called_once_with("SN-1", site="retail", _relist=False,
                                        pos_fields_only=False)

    def test_push_item_task_full_override_reaches_sync(self):
        from ffl_woo_sync.woocommerce.tasks import push_item_task
        with patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_pi:
            push_item_task("ITEM-1", pos_fields_only=False)
        mock_pi.assert_called_once_with("ITEM-1", site="retail",
                                        pos_fields_only=False)


# ---------------------------------------------------------------------------
# Manual entrypoints stay full (pin the kwargs, not just the count)
# ---------------------------------------------------------------------------

class TestManualEntrypointsStayFull(FrappeTestCase):
    def test_push_serial_now_pins_the_full_push(self):
        from ffl_woo_sync.woocommerce import client_api
        with patch("frappe.only_for"), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   return_value={"id": 7}) as mock_ps:
            client_api.push_serial_now("SN-1")
        mock_ps.assert_called_once_with("SN-1", site="retail", _relist=True)

    def test_reconcile_now_pins_the_full_push(self):
        """FULL by design: a deliberate, confirmed bulk
        'bring everything live' — the absence of pos_fields_only is the
        contract, pinned exactly for BOTH the item push and the per-serial
        route (an empty get_all skipped the serial branch)."""
        from ffl_woo_sync.woocommerce import client_api
        client = MagicMock()
        client.get_attribute_ids.return_value = {}
        with patch("frappe.only_for"), \
             patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
             patch("ffl_woo_sync.woocommerce.sync.push_item",
                   return_value={"id": 3}) as mock_pi, \
             patch("ffl_woo_sync.woocommerce.sync.push_serial",
                   return_value={"id": 4}) as mock_ps, \
             patch("frappe.get_all", return_value=["SN-1"]):
            client_api.reconcile_now("ITEM-1")
        mock_pi.assert_called_once_with("ITEM-1", client=client, attr_ids={},
                                        site="retail")
        mock_ps.assert_called_once_with("SN-1", client=client, attr_ids={},
                                        site="retail")
