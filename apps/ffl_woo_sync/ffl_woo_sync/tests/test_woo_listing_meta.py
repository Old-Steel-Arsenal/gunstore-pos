"""RSR — listing-payload increments: restricted states, brand term, shipping class.

The ``distributor_listing_push`` payload contents, including the drop-ship
shipping classes.

Three additions, each with a different WooCommerce shape — that is the point of
the split:

* ``_osa_restricted_states`` → product **meta** (the storefront plugin's cart validator reads
  it; the value is a normalised CSV of state codes).
* ``product_brand``          → a **taxonomy term** (``brands`` on the product,
  ``/products/brands`` in REST) — NOT a ``pa_*`` attribute.  The MAP-pricing
  plugin matches on the brand term, so an attribute would not be seen by it.
* ``shipping_class``         → the product's **shipping class slug**
  (``dropship-handgun`` / ``dropship-firearm`` / ``dropship-accessory``).
* ``_osa_fulfillment_mode``  → product **meta**: "Direct" / "To Store",
  the wp-admin drop-ship badge. Same None-vs-string contract as the states
  meta — a mode FLIP must reach the shop, so listing pushes always emit it.

The builders stay pure: term **ids** are resolved (and created if absent) by the
sync layer through the live client, exactly like ``_apply_category`` already
does for the item-group category.  No HTTP is mocked in the builder tests
because none happens.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce.client import WooCommerceError
from ffl_woo_sync.woocommerce.mapping import (
    _normalize_states,
    build_simple_product_payload,
    item_to_product_payload,
)

_STATES_META = "_osa_restricted_states"


def _meta(payload: dict) -> dict:
    return {m["key"]: m["value"] for m in payload.get("meta_data", [])}


def _base(**kwargs) -> dict:
    args = {"item_code": "RSR-ACC-1", "item_name": "Holosun 507C", "standard_rate": 349.99}
    args.update(kwargs)
    return build_simple_product_payload(**args)


# ===========================================================================
# _osa_restricted_states meta
# ===========================================================================

class TestRestrictedStatesMeta(FrappeTestCase):
    """On a listing push the key is ALWAYS present (empty string = no
    restriction); on any other push it is not touched at all.

    ``None`` is the "don't touch" signal, ``""`` is the "no restrictions" value.
    They are not interchangeable: "omit when empty" made the meta write-only —
    an operator clearing ``Item.restricted_states`` would leave the old list on
    the shop forever, silently blocking a state nobody had blocked any more.
    """

    def test_written_when_provided(self):
        payload = _base(restricted_states="CA,NY")
        self.assertEqual(_meta(payload)[_STATES_META], "CA,NY")

    def test_empty_string_is_emitted_as_an_empty_value(self):
        """The clearing path: "" means "no restrictions", and it must travel."""
        self.assertEqual(_meta(_base(restricted_states=""))[_STATES_META], "")

    def test_only_separators_is_emitted_as_empty_not_dropped(self):
        self.assertEqual(_meta(_base(restricted_states=" , , "))[_STATES_META], "")

    def test_none_means_do_not_touch(self):
        self.assertNotIn(_STATES_META, _meta(_base(restricted_states=None)))

    def test_normalised_case_and_spacing(self):
        payload = _base(restricted_states="  ca , ny ,il ")
        self.assertEqual(_meta(payload)[_STATES_META], "CA,NY,IL")

    def test_whitespace_separated_input_accepted(self):
        """Operators type "CA NY MA" as often as they type commas."""
        payload = _base(restricted_states="CA NY\nMA")
        self.assertEqual(_meta(payload)[_STATES_META], "CA,NY,MA")

    def test_duplicates_collapsed_in_order(self):
        payload = _base(restricted_states="CA, ca ,NY,CA")
        self.assertEqual(_meta(payload)[_STATES_META], "CA,NY")

    def test_absent_by_default(self):
        self.assertNotIn(_STATES_META, _meta(_base()))

    def test_existing_meta_preserved(self):
        payload = _base(restricted_states="CA", is_firearm=True)
        meta = _meta(payload)
        self.assertEqual(meta["_firearm_product"], "yes")
        self.assertEqual(meta["_ammunition_product"], "no")
        self.assertEqual(meta[_STATES_META], "CA")


class TestFulfillmentModeMeta(FrappeTestCase):
    """On a listing push the key is ALWAYS present so a To-Store↔Direct
    flip reaches the shop; on any other push it is not touched at all."""

    def test_written_when_provided(self):
        payload = _base(fulfillment_mode="To Store")
        self.assertEqual(_meta(payload)["_osa_fulfillment_mode"], "To Store")

    def test_direct_travels_too(self):
        """Emitting only To Store would make the meta write-only — a brand
        authorization flipping items back to Direct must reach wp-admin."""
        payload = _base(fulfillment_mode="Direct")
        self.assertEqual(_meta(payload)["_osa_fulfillment_mode"], "Direct")

    def test_none_means_do_not_touch(self):
        self.assertNotIn("_osa_fulfillment_mode", _meta(_base(fulfillment_mode=None)))

    def test_absent_by_default(self):
        self.assertNotIn("_osa_fulfillment_mode", _meta(_base()))

    def test_value_is_trimmed(self):
        payload = _base(fulfillment_mode="  Direct  ")
        self.assertEqual(_meta(payload)["_osa_fulfillment_mode"], "Direct")


class TestLeadDaysMeta(FrappeTestCase):
    """``_osa_lead_days``: the drop-ship lead time the product page promises.
    None-vs-string contract like the other two listing keys."""

    def test_written_when_provided(self):
        self.assertEqual(_meta(_base(lead_days="3-7"))["_osa_lead_days"], "3-7")

    def test_empty_string_clears(self):
        self.assertEqual(_meta(_base(lead_days=""))["_osa_lead_days"], "")

    def test_none_means_do_not_touch(self):
        self.assertNotIn("_osa_lead_days", _meta(_base(lead_days=None)))

    def test_absent_by_default(self):
        self.assertNotIn("_osa_lead_days", _meta(_base()))


class TestListingLeadDays(FrappeTestCase):
    """``_listing_lead_days`` publishes what ffl_integrations formats
    (``dropship_source_for_item`` → ``lead_days``; the format is tested there)."""

    def _read(self, **patch_kw):
        from ffl_woo_sync.woocommerce.mapping import _listing_lead_days

        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source", **patch_kw):
            return _listing_lead_days("X")

    def test_the_hooks_lead_days_ride_through(self):
        self.assertEqual(self._read(return_value={"lead_days": "3-7"}), "3-7")

    def test_the_store_or_a_failing_hook_publishes_nothing(self):
        self.assertEqual(self._read(return_value=None), "")
        self.assertEqual(self._read(side_effect=RuntimeError("boom")), "")

    def test_a_registrant_without_lead_still_answers_the_push(self):
        from ffl_woo_sync.woocommerce.mapping import dropship_source

        with patch("frappe.get_hooks", return_value=["x.source_for_item"]), \
                patch("frappe.get_attr", return_value=lambda item_code: {"sku": item_code}):
            self.assertEqual(dropship_source("X"), {"sku": "X"})

    def test_a_failing_hook_is_logged_once_an_hour(self):
        from ffl_woo_sync.woocommerce.mapping import _listing_source

        frappe.cache().delete_value("woo_dropship_source_failed:X")
        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source",
                   side_effect=RuntimeError("boom")), \
                patch.object(frappe, "log_error") as log:
            self.assertEqual((_listing_source("X"), _listing_source("X")), ({}, {}))
        log.assert_called_once()
        self.assertIn("(X)", log.call_args.kwargs["title"])
        frappe.cache().delete_value("woo_dropship_source_failed:X")


class TestNormalizeStates(FrappeTestCase):
    def test_empty_inputs(self):
        for raw in ("", None, "   ", ",,"):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_states(raw), "")

    def test_mixed_separators(self):
        self.assertEqual(_normalize_states("ca; ny, ma"), "CA,NY,MA")


# ===========================================================================
# product_brand taxonomy term
# ===========================================================================

class TestProductBrandTerm(FrappeTestCase):
    def test_brand_id_emits_brands_array(self):
        payload = _base(brand_id=12)
        self.assertEqual(payload["brands"], [{"id": 12}])

    def test_absent_without_a_brand_id(self):
        self.assertNotIn("brands", _base())

    def test_zero_id_is_treated_as_absent(self):
        """WooCommerce term ids are positive; 0 means "unresolved", and sending
        it would blank the product's brand."""
        self.assertNotIn("brands", _base(brand_id=0))

    def test_brand_is_a_term_not_a_pa_attribute(self):
        payload = _base(brand_id=12, manufacturer="Holosun")
        slugs = [a["slug"] for a in payload.get("attributes", [])]
        self.assertNotIn("pa_brand", slugs)
        self.assertNotIn("pa_product_brand", slugs)
        # …and the existing manufacturer attribute is untouched.
        self.assertIn("pa_manufacturer", slugs)


# ===========================================================================
# shipping_class
# ===========================================================================

class TestShippingClass(FrappeTestCase):
    def test_slug_written(self):
        self.assertEqual(_base(shipping_class="dropship-handgun")["shipping_class"],
                         "dropship-handgun")

    def test_absent_by_default(self):
        """Retail pushes must not send the key at all — WooCommerce would clear
        whatever class the product already has."""
        self.assertNotIn("shipping_class", _base())

    def test_absent_when_blank(self):
        self.assertNotIn("shipping_class", _base(shipping_class="   "))

    def test_slug_normalised(self):
        self.assertEqual(_base(shipping_class=" Dropship-Handgun ")["shipping_class"],
                         "dropship-handgun")


# ===========================================================================
# Thin loader: item_to_product_payload
# ===========================================================================

class TestItemToProductPayloadListingFields(FrappeTestCase):
    def _mock_item(self, **kwargs):
        item = MagicMock()
        item.name = "RSR-ACC-1"
        item.item_code = "RSR-ACC-1"
        item.item_name = "Holosun 507C"
        item.description = ""
        item.standard_rate = 349.99
        item.item_group = kwargs.get("item_group", "Accessories")
        item.image_gallery = []
        item.get = lambda f, d=None: kwargs.get(f, d)
        return item

    def _payload(self, item, **kwargs):
        # The price is its own hook (test_web_price_hook); these tests patch
        # frappe.db.get_value for the listing-mode read and must not feed it too.
        with patch("frappe.get_doc", return_value=item), \
             patch("ffl_woo_sync.woocommerce.mapping.stock_for_item", return_value=4), \
             patch("ffl_woo_sync.woocommerce.mapping.web_price_for_item",
                   side_effect=lambda code, rate=None: float(rate or 0)):
            return item_to_product_payload("RSR-ACC-1", **kwargs)

    def test_restricted_states_read_from_the_item_on_a_listing_push(self):
        item = self._mock_item(is_firearm=0, uniform_sell_price=0,
                               restricted_states="ca,ny")
        self.assertEqual(
            _meta(self._payload(item, listing=True))[_STATES_META], "CA,NY")

    def test_listing_push_always_emits_the_key(self):
        """Present even when the Item carries nothing — "" = no restriction.
        Omitting it is what made the meta write-only (see the builder suite)."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        self.assertEqual(_meta(self._payload(item, listing=True))[_STATES_META], "")

    def test_retail_push_never_touches_the_key(self):
        """Even when the Item HAS a list: a plain retail push is not the listing
        pipeline and must not start writing dropship meta store-wide."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0,
                               restricted_states="CA")
        self.assertNotIn(_STATES_META, _meta(self._payload(item)))

    def test_missing_custom_field_is_not_an_error(self):
        """``restricted_states`` is an ffl_integrations fixture — on a site
        without that app installed the field simply is not there."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        self.assertEqual(_meta(self._payload(item, listing=True))[_STATES_META], "")
        self.assertNotIn(_STATES_META, _meta(self._payload(item)))

    def test_listing_push_emits_the_listing_fulfillment_mode(self):
        """The mode rides the same thin loader — ffl_integrations' answer to
        who ships the item (``dropship_source_for_item``)."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source",
                   return_value={"fulfillment_mode": "To Store"}):
            payload = self._payload(item, listing=True)
        self.assertEqual(_meta(payload)["_osa_fulfillment_mode"], "To Store")

    def test_listing_push_defaults_the_mode_to_direct(self):
        """A blank mode → Direct, the permissive default a listing is created
        under."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source",
                   return_value={"fulfillment_mode": ""}):
            payload = self._payload(item, listing=True)
        self.assertEqual(_meta(payload)["_osa_fulfillment_mode"], "Direct")

    def test_the_store_shipping_it_clears_the_mode(self):
        """Automatic sourcing moved it to the store's own stock (the hook's None):
        "" — the storefront reads it as store stock and promises no drop-ship —
        and the drop-ship shipping class comes off (the store's own shipping)."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source", return_value=None):
            payload = self._payload(item, listing=True, shipping_class="dropship-accessory")
        self.assertEqual((_meta(payload)["_osa_fulfillment_mode"],
            _meta(payload)["_osa_lead_days"], payload["shipping_class"]), ("", "", ""))
        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source",
                   return_value={"fulfillment_mode": "Direct"}):
            payload = self._payload(item, listing=True, shipping_class="dropship-accessory")
        self.assertEqual(payload["shipping_class"], "dropship-accessory")
        self.assertNotIn("shipping_class", self._payload(item), "a retail push leaves it")

    def test_the_store_stock_hook_names_a_uniform_guns_free_serials(self):
        """``store_stock_for_item``: the local quantity, and for a uniform-price gun
        the free serials the store's cost is read from."""
        from ffl_woo_sync.woocommerce import mapping

        for row, serials in (({"is_firearm": 1, "uniform_sell_price": 1}, ["S1", "S2"]),
                ({"is_firearm": 0, "uniform_sell_price": 0}, None)):
            with patch("frappe.db.get_value", return_value=frappe._dict(row)), \
                    patch.object(mapping, "local_stock_for_item", return_value=2), \
                    patch.object(mapping, "free_serials", return_value=["S1", "S2"]):
                self.assertEqual(mapping.store_stock_for_item("X"),
                    {"qty": 2, "serials": serials})

    def test_the_store_stock_hook_counts_free_serials_once_and_never_raises(self):
        from ffl_woo_sync.woocommerce import mapping

        gun = frappe._dict(is_firearm=1, uniform_sell_price=1)
        with patch("frappe.db.get_value", return_value=gun), \
                patch.object(mapping, "free_serials", return_value=["S1", "S2"]) as free, \
                patch.object(mapping, "unassigned_web_claims", return_value=1):
            self.assertEqual(mapping.store_stock_for_item("X"),
                {"qty": 1, "serials": ["S1", "S2"]})
        self.assertEqual(free.call_count, 1)
        for row in (frappe._dict(is_firearm=1, uniform_sell_price=0), None):
            with patch("frappe.db.get_value", return_value=row):
                self.assertIsNone(mapping.store_stock_for_item("X"),
                    "a gun priced serial by serial, or no Item: no part")
        with patch("frappe.db.get_value", side_effect=RuntimeError("db")), \
                patch.object(frappe, "log_error"):
            frappe.cache().delete_value("woo_store_stock_failed:X")
            self.assertIsNone(mapping.store_stock_for_item("X"))

    def test_a_site_without_the_listing_doctype_still_pushes(self):
        """The mode comes from ffl_integrations' hook; a failing answer degrades
        to the default instead of erroring the push."""
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        with patch("ffl_woo_sync.woocommerce.mapping.dropship_source",
                   side_effect=Exception("no such table")):
            payload = self._payload(item, listing=True)
        self.assertEqual(_meta(payload)["_osa_fulfillment_mode"], "Direct")

    def test_retail_push_never_touches_the_mode_key(self):
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        self.assertNotIn("_osa_fulfillment_mode", _meta(self._payload(item)))

    def test_brand_id_threaded_through(self):
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        self.assertEqual(self._payload(item, brand_id=12)["brands"], [{"id": 12}])

    def test_shipping_class_threaded_through(self):
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        payload = self._payload(item, shipping_class="dropship-accessory")
        self.assertEqual(payload["shipping_class"], "dropship-accessory")

    def test_no_listing_fields_by_default(self):
        item = self._mock_item(is_firearm=0, uniform_sell_price=0)
        payload = self._payload(item)
        self.assertNotIn("brands", payload)
        self.assertNotIn("shipping_class", payload)


# ===========================================================================
# sync layer — live term resolution (create-if-absent), mirroring _apply_category
# ===========================================================================

class TestBrandScope(FrappeTestCase):
    """MAJOR-1: the brand term is written on the **dropship
    listing path only**.

    `brands` is replace-semantics on a taxonomy of the live store, so an
    unattended reconcile would otherwise overwrite hand-curated brand
    assignments with POS spellings store-wide. A dropship item stays covered on
    every path (reconcile included) via `Item.dropship_distributor`; opening it
    up to the whole catalogue is a deliberate, separate decision — the
    (default-off) settings switch is its entry point, not this PR.
    """

    def _is_listing(self, *, forced=False, dropship=None, has_column=True):
        from ffl_woo_sync.woocommerce.sync import _is_distributor_listing

        with patch("frappe.db.has_column", return_value=has_column), \
             patch("frappe.db.get_value", return_value=dropship):
            return _is_distributor_listing("RSR-ACC-1", forced=forced)

    def test_hook_path_is_a_listing_push(self):
        self.assertTrue(self._is_listing(forced=True))

    def test_dropship_item_is_a_listing_push_on_any_path(self):
        """Reconcile / doc_event pushes of a dropship SKU keep brand + states
        in sync — otherwise a cleared field would never reach the store."""
        self.assertTrue(self._is_listing(dropship="RSR"))

    def test_plain_retail_item_is_not(self):
        self.assertFalse(self._is_listing(dropship=None))

    def test_site_without_ffl_integrations_is_not(self):
        """`dropship_distributor` is an ffl_integrations custom field; reading a
        column that does not exist is a SQL error, so it is probed first."""
        self.assertFalse(self._is_listing(has_column=False, dropship="RSR"))

    def test_the_probe_does_not_build_a_doctype_meta(self):
        """`frappe.get_meta` reads through `frappe.db.get_value`, so a meta-based
        probe pulls Frappe's document loader into every test that stubs that
        function — which is exactly how the suite broke under a real bench run.
        `frappe.db.has_column` answers the same question without it."""
        from ffl_woo_sync.woocommerce.sync import _dropship_distributor

        with patch("frappe.get_meta") as mock_meta, \
             patch("frappe.db.has_column", return_value=True), \
             patch("frappe.db.get_value", return_value="RSR"):
            self.assertEqual(_dropship_distributor("RSR-ACC-1"), "RSR")
        mock_meta.assert_not_called()

    def test_a_non_string_read_is_not_a_dropship_item(self):
        """A Link column holds a string.  A stubbed/mocked read that answers with
        anything else (a MagicMock is truthy!) means "no answer", and must not be
        read as "yes" — a wrong yes writes brand terms onto retail products."""
        self.assertFalse(self._is_listing(dropship=MagicMock()))

    def test_the_gate_reads_no_settings_singleton(self):
        """This runs on EVERY item push, so it may not touch document/Meta
        machinery.  `frappe.db.get_single_value` builds a Meta for the Single
        doctype, and building a Meta loads a DocType document through
        `frappe.db.get_value` — which is exactly how 17 pre-existing tests blew
        up under a real bench run.  Two cheap reads, nothing else.
        """
        from ffl_woo_sync.woocommerce.sync import _is_distributor_listing

        with patch("frappe.db.has_column", return_value=True), \
             patch("frappe.db.get_value", return_value=None), \
             patch("frappe.db.get_single_value") as mock_single, \
             patch("frappe.get_meta") as mock_meta, \
             patch("frappe.get_doc") as mock_doc:
            self.assertFalse(_is_distributor_listing("RSR-ACC-1"))
        mock_single.assert_not_called()
        mock_meta.assert_not_called()
        mock_doc.assert_not_called()


class TestBrandIdForItem(FrappeTestCase):
    def setUp(self):
        # The unsupported-store flag lives in the shared cache: without this a
        # 404 case would poison every later case in the same run (and the run
        # after it, for an hour).
        from ffl_woo_sync.woocommerce import sync
        sync._forget_taxonomy_cache()

    def test_resolves_and_creates_the_term(self):
        from ffl_woo_sync.woocommerce.sync import _brand_id_for_item

        wc = MagicMock()
        wc.get_or_create_brand.return_value = 12
        with patch("frappe.db.get_value", return_value="Holosun"):
            self.assertEqual(_brand_id_for_item(wc, "RSR-ACC-1"), 12)
        wc.get_or_create_brand.assert_called_once_with("Holosun")

    def test_no_brand_on_the_item_is_a_no_op(self):
        from ffl_woo_sync.woocommerce.sync import _brand_id_for_item

        wc = MagicMock()
        with patch("frappe.db.get_value", return_value=None):
            self.assertIsNone(_brand_id_for_item(wc, "RSR-ACC-1"))
        wc.get_or_create_brand.assert_not_called()

    def test_store_without_brand_support_degrades_instead_of_failing(self):
        """`/products/brands` 404s on a store older than WooCommerce 9.4.  A
        missing brand term must not take the whole product push down with it."""
        from ffl_woo_sync.woocommerce.sync import _brand_id_for_item

        wc = MagicMock()
        wc.get_or_create_brand.side_effect = WooCommerceError("404", status_code=404)
        with patch("frappe.db.get_value", return_value="Holosun"), \
             patch("frappe.log_error") as mock_log:
            self.assertIsNone(_brand_id_for_item(wc, "RSR-ACC-1"))
        mock_log.assert_called_once()

    def test_a_failing_error_log_cannot_break_the_push(self):
        """The reporter must never outrank what it reports: writing an Error Log
        is itself a document insert and can fail on its own."""
        from ffl_woo_sync.woocommerce.sync import _brand_id_for_item

        wc = MagicMock()
        wc.get_or_create_brand.side_effect = WooCommerceError("404", status_code=404)
        with patch("frappe.db.get_value", return_value="Holosun"), \
             patch("frappe.log_error", side_effect=RuntimeError("Error Log is down")):
            self.assertIsNone(_brand_id_for_item(wc, "RSR-ACC-1"))

    def test_transport_failure_degrades_too(self):
        """The client only converts HTTP≥400 into WooCommerceError; a `requests`
        Timeout/ConnectionError comes through raw and used to punch through this
        guard and fail the whole product push over metadata."""
        import requests
        from ffl_woo_sync.woocommerce.sync import _brand_id_for_item

        wc = MagicMock()
        wc.get_or_create_brand.side_effect = requests.RequestException("timeout")
        with patch("frappe.db.get_value", return_value="Holosun"):
            self.assertIsNone(_brand_id_for_item(wc, "RSR-ACC-1"))

    def test_unsupported_store_is_remembered_so_every_push_stops_re_probing(self):
        """Each listing task builds its own client, so the per-client memo buys
        nothing on that path: a WC<9.4 store would eat one 404 per SKU forever."""
        from ffl_woo_sync.woocommerce import sync

        sync._forget_taxonomy_cache()
        first, second = MagicMock(), MagicMock()
        first.get_or_create_brand.side_effect = WooCommerceError("404", status_code=404)
        with patch("frappe.db.get_value", return_value="Holosun"), \
             patch("frappe.log_error"):
            sync._brand_id_for_item(first, "RSR-ACC-1")
            self.assertIsNone(sync._brand_id_for_item(second, "RSR-ACC-2"))
        second.get_or_create_brand.assert_not_called()

    def test_an_ordinary_missing_brand_is_not_remembered_as_unsupported(self):
        """A single failed term (bad name, transient 500) must not switch brands
        off store-wide — only "this store has no brands endpoint" does that."""
        from ffl_woo_sync.woocommerce import sync

        sync._forget_taxonomy_cache()
        first, second = MagicMock(), MagicMock()
        first.get_or_create_brand.side_effect = WooCommerceError("500", status_code=500)
        second.get_or_create_brand.return_value = 12
        with patch("frappe.db.get_value", return_value="Holosun"):
            sync._brand_id_for_item(first, "RSR-ACC-1")
            self.assertEqual(sync._brand_id_for_item(second, "RSR-ACC-2"), 12)


class TestEnsureShippingClass(FrappeTestCase):
    """Getting this wrong costs money silently: WooCommerce accepts a product
    carrying an unknown shipping-class slug and just drops it, and the product
    then ships at the default rate with a green "push succeeded" behind it.
    """

    def setUp(self):
        # The per-batch memo lives in the Frappe cache; each test starts cold.
        from ffl_woo_sync.woocommerce import sync
        sync._forget_taxonomy_cache()

    def test_ensures_the_whole_1four_two_table_in_one_pass(self):
        """One listing batch = one GET + the missing POSTs, not one round-trip
        per SKU (the table is 3 slugs; a batch is hundreds of products)."""
        from ffl_woo_sync.woocommerce.sync import _ensure_shipping_class

        wc = MagicMock()
        wc.list_shipping_classes.return_value = {"dropship-handgun": 3}
        wc.get_or_create_shipping_class.side_effect = [4, 5]

        self.assertTrue(_ensure_shipping_class(wc, "dropship-handgun"))

        wc.list_shipping_classes.assert_called_once()
        created = sorted(c.args[0] for c in wc.get_or_create_shipping_class.call_args_list)
        self.assertEqual(created, ["dropship-accessory", "dropship-firearm"])

    def test_second_call_costs_no_http(self):
        from ffl_woo_sync.woocommerce.sync import _ensure_shipping_class

        wc = MagicMock()
        wc.list_shipping_classes.return_value = {
            "dropship-handgun": 3, "dropship-firearm": 4, "dropship-accessory": 5}
        _ensure_shipping_class(wc, "dropship-handgun")
        _ensure_shipping_class(wc, "dropship-firearm")
        wc.list_shipping_classes.assert_called_once()
        wc.get_or_create_shipping_class.assert_not_called()

    def test_off_table_slug_is_created_on_demand(self):
        from ffl_woo_sync.woocommerce.sync import _ensure_shipping_class

        wc = MagicMock()
        wc.list_shipping_classes.return_value = {
            "dropship-handgun": 3, "dropship-firearm": 4, "dropship-accessory": 5}
        wc.get_or_create_shipping_class.return_value = 9
        self.assertTrue(_ensure_shipping_class(wc, "free-shipping"))
        wc.get_or_create_shipping_class.assert_called_once_with(
            "free-shipping", name="Free Shipping")

    def test_blank_slug_is_a_no_op(self):
        from ffl_woo_sync.woocommerce.sync import _ensure_shipping_class

        wc = MagicMock()
        self.assertFalse(_ensure_shipping_class(wc, ""))
        wc.list_shipping_classes.assert_not_called()
        wc.get_or_create_shipping_class.assert_not_called()

    def test_failure_degrades_to_false_AND_leaves_a_persistent_signal(self):
        """A bench-log warning is invisible in Desk; 500 SKUs could ship at the
        wrong rate with nothing to find. Failures go to the Error Log."""
        from ffl_woo_sync.woocommerce.sync import _ensure_shipping_class

        wc = MagicMock()
        wc.list_shipping_classes.side_effect = WooCommerceError("boom")
        with patch("frappe.log_error") as mock_log:
            self.assertFalse(_ensure_shipping_class(wc, "dropship-handgun"))
        mock_log.assert_called_once()

    def test_transport_errors_degrade_too(self):
        """`requests` raises Timeout/ConnectionError straight through the client
        — those are not WooCommerceError and used to punch through the guard."""
        import requests
        from ffl_woo_sync.woocommerce.sync import _ensure_shipping_class

        wc = MagicMock()
        wc.list_shipping_classes.side_effect = requests.RequestException("timeout")
        with patch("frappe.log_error"):
            self.assertFalse(_ensure_shipping_class(wc, "dropship-handgun"))


class TestPushItemListingFields(FrappeTestCase):
    """push_item resolves the live terms, then threads plain values into the
    pure builder — so the builder stays IO-free and the ids stay correct."""

    per_serial = False

    def _db_get_value(self, doctype, name, fieldname=None, **kwargs):
        if isinstance(fieldname, (list, tuple)):
            return frappe._dict(
                is_firearm=1 if self.per_serial else 0,
                item_group="Pistol" if self.per_serial else "Accessories",
                uniform_sell_price=0,
            )
        return {
            "woo_delisted": 0,
            "woo_product_id": "WC-1",
            "item_group": "Accessories",
            "brand": "Holosun",
        }.get(fieldname)

    dropship = None          # Item.dropship_distributor

    def setUp(self):
        from ffl_woo_sync.woocommerce import sync
        sync._forget_taxonomy_cache()

    def _push(self, wc, **kwargs):
        from ffl_woo_sync.woocommerce import sync

        gv = self._db_get_value
        real = frappe.db.get_value

        def _get_value(doctype, *a, **kw):
            # Only Item reads are stubbed; frappe.get_meta's cold-cache DocType
            # load (reservations' channel-field probe) must reach the real db.
            if doctype != "Item":
                return real(doctype, *a, **kw)
            name = a[0] if a else kw.get("filters")
            fieldname = a[1] if len(a) > 1 else kw.get("fieldname")
            if fieldname == "dropship_distributor":
                return self.dropship
            return gv(doctype, name, fieldname)

        with patch("frappe.db.get_value", side_effect=_get_value), \
             patch("frappe.db.set_value"), \
             patch("frappe.db.has_column", return_value=True), \
             patch("ffl_woo_sync.woocommerce.sync.effective_item_price",
                   return_value=349.99), \
             patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                   return_value={"sku": "RSR-ACC-1"}) as mock_build:
            sync.push_item("RSR-ACC-1", client=wc, attr_ids={}, **kwargs)
        return mock_build

    def _wc(self):
        wc = MagicMock()
        wc.get_or_create_brand.return_value = 12
        wc.list_shipping_classes.return_value = {
            "dropship-handgun": 3, "dropship-firearm": 4, "dropship-accessory": 5}
        wc.update_product.return_value = {"id": 1, "shipping_class": "dropship-accessory"}
        return wc

    def test_listing_push_threads_brand_and_shipping_class(self):
        wc = self._wc()
        mock_build = self._push(wc, shipping_class="dropship-accessory", listing=True)
        kwargs = mock_build.call_args.kwargs
        self.assertEqual(kwargs["brand_id"], 12)
        self.assertEqual(kwargs["shipping_class"], "dropship-accessory")
        self.assertIs(kwargs["listing"], True)

    def test_retail_push_resolves_no_brand_at_all(self):
        """MAJOR-1: no brand lookup, no term creation, no `brands` key — an
        unattended reconcile must not rewrite the store's brand taxonomy."""
        wc = self._wc()
        mock_build = self._push(wc)
        wc.get_or_create_brand.assert_not_called()
        kwargs = mock_build.call_args.kwargs
        self.assertIsNone(kwargs["brand_id"])
        self.assertIs(kwargs["listing"], False)

    def test_dropship_item_keeps_its_brand_on_a_plain_push(self):
        """Reconcile/doc_event pushes of a dropship SKU are still listing pushes
        — otherwise the two fields would drift the moment anything else saved."""
        wc = self._wc()
        self.dropship = "RSR"
        try:
            mock_build = self._push(wc)
        finally:
            self.dropship = None
        self.assertEqual(mock_build.call_args.kwargs["brand_id"], 12)
        self.assertIs(mock_build.call_args.kwargs["listing"], True)

    def test_unresolvable_shipping_class_is_dropped_not_sent(self):
        """WooCommerce silently ignores an unknown shipping-class slug, so a
        slug we could not create must not travel in the payload — otherwise the
        push "succeeds" and the product quietly ships at the default rate."""
        wc = self._wc()
        wc.list_shipping_classes.side_effect = WooCommerceError("nope")
        with patch("frappe.log_error"):
            mock_build = self._push(wc, shipping_class="dropship-accessory",
                                    listing=True)
        self.assertEqual(mock_build.call_args.kwargs["shipping_class"], "")

    def test_shipping_class_missing_from_the_response_is_reported(self):
        """The only place the silent drop is observable: WooCommerce echoes the
        product back, and the class it actually stored is in that echo."""
        wc = self._wc()
        wc.update_product.return_value = {"id": 1, "shipping_class": ""}
        with patch("frappe.log_error") as mock_log:
            self._push(wc, shipping_class="dropship-accessory", listing=True)
        mock_log.assert_called_once()
        self.assertIn("dropship-accessory", str(mock_log.call_args))

    def test_matching_response_is_silent(self):
        wc = self._wc()
        with patch("frappe.log_error") as mock_log:
            self._push(wc, shipping_class="dropship-accessory", listing=True)
        mock_log.assert_not_called()

    def test_ordinary_retail_push_sends_no_shipping_class(self):
        wc = self._wc()
        mock_build = self._push(wc)
        self.assertEqual(mock_build.call_args.kwargs["shipping_class"], "")
        wc.list_shipping_classes.assert_not_called()
        wc.get_or_create_shipping_class.assert_not_called()

    def test_per_serial_firearm_resolves_no_brand(self):
        """Such an Item has no item-level product — the build raises and the
        caller routes to push_all_serials — so no brand term is created for a
        product that will never exist."""
        from ffl_woo_sync.woocommerce import sync

        wc = MagicMock()
        self.per_serial = True
        try:
            with patch("frappe.db.get_value", side_effect=self._db_get_value), \
                 patch("frappe.db.set_value"), \
                 patch("frappe.db.has_column", return_value=True), \
                 patch("ffl_woo_sync.woocommerce.mapping.item_to_product_payload",
                       side_effect=ValueError("per-serial firearm")):
                with self.assertRaises(ValueError):
                    sync.push_item("RSR-GLOCK-19", client=wc, attr_ids={},
                                   _retries=0, listing=True)
        finally:
            self.per_serial = False
        wc.get_or_create_brand.assert_not_called()
