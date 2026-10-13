"""Unit tests for woocommerce/pull.py — the Woo → POS content pull.

All WooCommerce HTTP, file writes and Frappe DB access are mocked, same idiom
as ``test_sync.py``.  The contract under test:
title + description + price + images are pulled; a Serial No description is
stored as PLAIN TEXT via frappe's canonical converter; the Item price lands
through ``_set_item_selling_price`` (a bare standard_rate write is not what
the register charges); a uniform-priced parent skips the serial price with a
note; the save runs under the canonical ``webhook.woo_site`` guard; a 404
from the store is a refusal, not a silent no-op; stock is never touched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import pull
from ffl_woo_sync.woocommerce.client import WooCommerceError


class _FakeDoc:
    """Just enough Document surface for pull_product_from_woo."""

    def __init__(self, doctype: str, name: str):
        self.doctype = doctype
        self.name = name
        self.image_gallery = [frappe._dict(image="/files/old.jpg", is_primary=1)]
        self.image = "/files/old.jpg"
        self.saved = False
        self.flag_during_save = None

    def set(self, field, value):
        setattr(self, field, list(value) if isinstance(value, list) else value)

    def append(self, field, row):
        getattr(self, field).append(frappe._dict(row))

    def save(self):
        self.saved = True
        self.flag_during_save = frappe.flags.get("woo_sync_inbound")


def _wc_payload(**overrides):
    payload = {
        "id": 42,
        "name": "Edited In Woo",
        "description": "<p>Curated description</p>",
        "regular_price": "129.99",
        "images": [{"src": "https://shop.example/a.jpg"}],
    }
    payload.update(overrides)
    return payload


def _client_returning(product):
    client = MagicMock()
    if isinstance(product, Exception):
        client.get_product.side_effect = product
    else:
        client.get_product.return_value = product
    return client


class TestHtmlToText(FrappeTestCase):
    """Thin wrapper over frappe.core.utils.html_to_plain_text — these pin the
    behaviours the pull depends on, not the converter's full surface."""

    def test_breaks_and_entities(self):
        html = "<p>Line one</p><br>Line two &amp; Co<div>Line three</div>"
        self.assertEqual(
            pull.html_to_text(html), "Line one\n\nLine two & Co\nLine three")

    def test_blank_line_runs_collapse(self):
        self.assertEqual(pull.html_to_text("<p>A</p><p></p><p></p><p>B</p>"), "A\n\nB")

    def test_block_tags_with_attributes_still_break(self):
        self.assertEqual(
            pull.html_to_text('<div class="woo">A</div><p style="x">B</p>'), "A\n\nB")

    def test_text_glued_to_a_block_open_tag_still_breaks(self):
        """frappe's converter only breaks at block CLOSE; the preface breaks
        before block OPENs too, or page-builder div soup runs together."""
        self.assertEqual(pull.html_to_text("A</p>B<div>C</div>"), "A B\nC")

    def test_script_is_stripped_and_inline_cells_stay_inline(self):
        html = "<table><tr><td>A</td><td>B</td></tr></table><script>evil()</script>"
        self.assertEqual(pull.html_to_text(html), "A B")

    def test_round_trip_with_the_push_renderer_is_stable(self):
        """The push side renders each plain-text line as ``<br>\\n``; the
        literal newline is HTML whitespace and must NOT double every line
        into a blank line on the way back."""
        self.assertEqual(pull.html_to_text("a<br>\nb"), "a\nb")

    def test_none_and_plain_text_pass_through(self):
        self.assertEqual(pull.html_to_text(None), "")
        self.assertEqual(pull.html_to_text("already plain"), "already plain")


class TestSerialDescriptionFixtureContract(FrappeTestCase):
    """The pull writes Serial No.description; ERPNext v16 ships that field
    with ``fetch_from: item_code.description`` (no fetch_if_empty), so every
    save would clobber it with the parent's text — the feature works ONLY
    because ffl_core's property setters clear that default.  Pin the fixture
    rows so a fixture regression cannot silently revert the serial-description
    decision while every mocked test stays green."""

    def test_property_setters_keep_serial_description_writable(self):
        import json

        path = frappe.get_app_path("ffl_core", "fixtures", "property_setter.json")
        with open(path) as fh:
            rows = json.load(fh)
        props = {
            r.get("property"): (r.get("value") or "")
            for r in rows
            if r.get("doc_type") == "Serial No" and r.get("field_name") == "description"
        }
        self.assertEqual(props.get("fetch_from"), "", "fetch_from must be cleared")
        self.assertEqual(props.get("read_only"), "0", "read_only must be lifted")

    def test_property_setter_keeps_serial_item_name_editable(self):
        """Serial No.item_name ships read_only=1 (fetch_if_empty keeps the
        blank→Item fallback, so only read_only is lifted). Without this row the
        per-gun title is uneditable on the form (09-02 bug)."""
        import json

        path = frappe.get_app_path("ffl_core", "fixtures", "property_setter.json")
        with open(path) as fh:
            rows = json.load(fh)
        props = {
            r.get("property"): (r.get("value") or "")
            for r in rows
            if r.get("doc_type") == "Serial No" and r.get("field_name") == "item_name"
        }
        self.assertEqual(props.get("read_only"), "0", "read_only must be lifted")
        self.assertNotIn("fetch_from", props, "keep fetch_if_empty fallback to Item.item_name")


class _PullBase(FrappeTestCase):
    """Shared harness: patches everything pull_product_from_woo touches."""

    def _run(self, sku, *, product=None, values=None,
             images=(["/files/new.jpg"], [], False), price_probe=None):
        values = {
            ("Serial No", "item_code"): "SG551",
            ("Item", "woo_product_id"): "42",
            ("Serial No", "woo_product_id"): "77",
            ("Item", "uniform_sell_price"): 0,
        } | (values or {})

        def _gv(doctype, name, field, *a, **k):
            return values.get((doctype, field))

        docs: dict = {}

        def _get_doc(doctype, name):
            docs["doc"] = _FakeDoc(doctype, name)
            return docs["doc"]

        client = _client_returning(product if product is not None else _wc_payload())
        with patch("frappe.db.exists", return_value=True), \
             patch("frappe.has_permission", return_value=True), \
             patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.get_doc", side_effect=_get_doc), \
             patch("ffl_core.api.receive_goods._set_item_selling_price") as mock_price, \
             patch("ffl_woo_sync.woocommerce.pull._import_images", return_value=images), \
             patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client):
            if price_probe is not None:
                mock_price.side_effect = lambda *a, **k: price_probe.update(
                    saved_before_price=docs["doc"].saved)
            result = pull.pull_product_from_woo(sku)
        return result, docs.get("doc"), mock_price


class TestPullItem(_PullBase):
    def test_writes_title_description_and_lands_the_effective_price(self):
        result, doc, mock_price = self._run("AMMO-9MM")
        self.assertEqual(doc.item_name, "Edited In Woo")
        # Item.description is HTML on both sides — stored verbatim, NOT flattened.
        self.assertEqual(doc.description, "<p>Curated description</p>")
        self.assertTrue(doc.saved)
        # The price goes through the canonical helper (standard_rate + selling
        # Item Price) — a bare doc.standard_rate write would not change what
        # the register charges.
        mock_price.assert_called_once_with("AMMO-9MM", 129.99)
        self.assertEqual(result["updated"]["selling_price"], 129.99)
        self.assertEqual(result["doctype"], "Item")

    def test_gallery_rebuilt_first_image_primary(self):
        _result, doc, _price = self._run(
            "AMMO-9MM", images=(["/files/n1.jpg", "/files/n2.jpg"], [], False))
        self.assertEqual([r.image for r in doc.image_gallery], ["/files/n1.jpg", "/files/n2.jpg"])
        self.assertEqual([r.is_primary for r in doc.image_gallery], [1, 0])
        self.assertEqual(doc.image, "/files/n1.jpg")

    def test_no_images_leaves_gallery_alone(self):
        result, doc, _price = self._run("AMMO-9MM", images=([], ["a.jpg: boom"], False))
        self.assertEqual([r.image for r in doc.image_gallery], ["/files/old.jpg"])
        self.assertEqual(doc.image, "/files/old.jpg")
        self.assertTrue(doc.saved)  # text fields still land when every image fails
        self.assertEqual(result["image_errors"], ["a.jpg: boom"])

    def test_a_drop_ship_listings_website_price_is_not_adopted_as_the_tag(self):
        """Its regular price online is the listing's website price (markup rules,
        MAP floor); written back it would become the store's price tag."""
        with patch("ffl_woo_sync.woocommerce.pull.listing_web_price", return_value=129.99):
            result, doc, mock_price = self._run("AMMO-9MM")
        mock_price.assert_not_called()
        self.assertNotIn("selling_price", result["updated"])
        self.assertIn("drop-ship", result["price_note"])
        self.assertTrue(doc.saved)   # the rest of the pull still lands

    def test_zero_price_is_not_written(self):
        _result, doc, mock_price = self._run(
            "AMMO-9MM", product=_wc_payload(regular_price=""))
        mock_price.assert_not_called()
        self.assertFalse(hasattr(doc, "standard_rate"))

    def test_price_helper_runs_after_the_save(self):
        """Order pin: the full-doc save would clobber the
        helper's targeted writes if it ran second — a comment alone cannot
        stop a refactor from swapping them."""
        probe: dict = {}
        self._run("AMMO-9MM", price_probe=probe)
        self.assertIs(probe.get("saved_before_price"), True)

    def test_budget_truncated_import_never_replaces_the_gallery(self):
        """A time-truncated set is non-deterministic — swapping it in could
        strand the gallery on an arbitrary partial forever.
        Deterministic outcomes (cap, per-image failures) still rebuild."""
        result, doc, _price = self._run(
            "AMMO-9MM", images=(["/files/only-3-of-12.jpg"], ["budget hit"], True))
        self.assertEqual([r.image for r in doc.image_gallery], ["/files/old.jpg"])
        self.assertNotIn("images", result["updated"])
        self.assertTrue(any("gallery left unchanged" in e for e in result["image_errors"]))

    def test_save_runs_under_inbound_guard_and_restores_it(self):
        frappe.flags.woo_sync_inbound = None
        _result, doc, _price = self._run("AMMO-9MM")
        self.assertTrue(doc.flag_during_save)
        self.assertIsNone(frappe.flags.get("woo_sync_inbound"))


class TestPullSerial(_PullBase):
    def test_description_stored_as_plain_text(self):
        product = _wc_payload(description="<p>Line one</p><br>Line two &amp; Co")
        _result, doc, mock_price = self._run("SG551::410054", product=product)
        self.assertEqual(doc.doctype, "Serial No")
        self.assertEqual(doc.description, "Line one\n\nLine two & Co")
        self.assertEqual(doc.sell_price, 129.99)
        self.assertEqual(doc.item_name, "Edited In Woo")
        self.assertFalse(hasattr(doc, "standard_rate"))
        mock_price.assert_not_called()  # the Item helper is the ITEM branch's

    def test_uniform_parent_skips_the_price_with_a_note(self):
        """Under uniform pricing the serial tag is ignored by
        firearm.serial_sell_price — writing it would be a silent no-op
        reported as success."""
        result, doc, _price = self._run(
            "SG551::410054", values={("Item", "uniform_sell_price"): 1})
        self.assertFalse(hasattr(doc, "sell_price"))
        self.assertNotIn("sell_price", result["updated"])
        self.assertIn("uniform", result["price_note"])

    def test_sku_parent_mismatch_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._run("OTHER-ITEM::410054")


class TestPullRefusals(_PullBase):
    def test_blank_sku_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._run("")

    def test_never_pushed_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._run("AMMO-9MM", values={("Item", "woo_product_id"): ""})

    def test_deleted_on_store_is_refused_via_the_real_404_shape(self):
        """The client RAISES on 404 (client.py) — the refusal must catch that,
        not test a falsy return the real client never produces."""
        with self.assertRaises(frappe.ValidationError):
            self._run("AMMO-9MM",
                      product=WooCommerceError("not found", status_code=404))

    def test_other_woo_errors_propagate(self):
        with self.assertRaises(WooCommerceError):
            self._run("AMMO-9MM",
                      product=WooCommerceError("boom", status_code=500))

    def test_missing_write_permission_is_refused(self):
        with patch("frappe.db.exists", return_value=True), \
             patch("frappe.has_permission", return_value=False):
            with self.assertRaises(frappe.PermissionError):
                pull.pull_product_from_woo("AMMO-9MM")


class TestImportImages(FrappeTestCase):
    def _resp(self, *, ctype="image/jpeg", content=b"bytes", status_ok=True):
        resp = MagicMock()
        resp.headers = {"content-type": ctype}
        resp.content = content
        if not status_ok:
            resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        return resp

    def _file_doc(self, created):
        file_doc = MagicMock()

        def _insert():
            file_doc.file_url = f"/files/{file_doc.file_name}"
            created.append(file_doc)
        file_doc.insert.side_effect = _insert
        return file_doc

    def test_downloads_attach_and_preserve_order(self):
        created: list = []
        with patch("ffl_woo_sync.woocommerce.pull.requests.get",
                   return_value=self._resp()), \
             patch("frappe.new_doc", side_effect=lambda dt: self._file_doc(created)):
            saved, errors, truncated = pull._import_images(
                [{"src": "https://s/a.jpg"}, {"src": "https://s/b.jpg"}], "Item", "AMMO-9MM")
        self.assertEqual(saved, ["/files/a.jpg", "/files/b.jpg"])
        self.assertEqual(errors, [])
        self.assertFalse(truncated)
        self.assertEqual(created[0].attached_to_doctype, "Item")
        self.assertEqual(created[0].attached_to_name, "AMMO-9MM")
        self.assertEqual(created[0].is_private, 0)

    def test_bad_images_are_skipped_not_fatal(self):
        created: list = []
        responses = {
            "https://s/good.jpg": self._resp(),
            "https://s/page.html": self._resp(ctype="text/html"),
            "https://s/huge.jpg": self._resp(content=b"x" * (pull.MAX_IMAGE_BYTES + 1)),
            "https://s/boom.jpg": self._resp(status_ok=False),
            "https://s/vector.svg": self._resp(ctype="image/svg+xml"),
        }
        with patch("ffl_woo_sync.woocommerce.pull.requests.get",
                   side_effect=lambda url, timeout: responses[url]), \
             patch("frappe.new_doc", side_effect=lambda dt: self._file_doc(created)):
            saved, errors, truncated = pull._import_images(
                [{"src": "https://s/good.jpg"}, {"src": "https://s/page.html"},
                 {"src": "https://s/huge.jpg"}, {"src": "https://s/boom.jpg"},
                 # SVG is scriptable — the raster allowlist must reject it.
                 {"src": "https://s/vector.svg"}, {"src": "ftp://s/nope.jpg"}],
                "Item", "AMMO-9MM")
        self.assertEqual(saved, ["/files/good.jpg"])
        self.assertEqual(len(errors), 5)
        self.assertFalse(truncated)

    def test_over_cap_truncation_is_recorded_not_silent(self):
        created: list = []
        images = [{"src": f"https://s/{i}.jpg"} for i in range(pull.MAX_IMAGES + 3)]
        with patch("ffl_woo_sync.woocommerce.pull.requests.get",
                   return_value=self._resp()), \
             patch("frappe.new_doc", side_effect=lambda dt: self._file_doc(created)):
            saved, errors, truncated = pull._import_images(images, "Item", "AMMO-9MM")
        self.assertEqual(len(saved), pull.MAX_IMAGES)
        self.assertTrue(any("3 more images skipped" in e for e in errors))
        self.assertFalse(truncated)  # the cap is deterministic, not a budget cut

    def test_time_budget_cutoff_is_recorded_not_silent(self):
        """The pull runs inside the caller's HTTP request — the download loop
        must stop at the budget and SAY so (the WP proxy timing out
        while the POS kept working produced counterfactual failures)."""
        with patch("time.monotonic", side_effect=[0, 1000]), \
             patch("ffl_woo_sync.woocommerce.pull.requests.get") as mock_get:
            saved, errors, truncated = pull._import_images(
                [{"src": "https://s/a.jpg"}, {"src": "https://s/b.jpg"}],
                "Item", "AMMO-9MM")
        mock_get.assert_not_called()
        self.assertEqual(saved, [])
        self.assertTrue(any("time budget" in e for e in errors))
        self.assertTrue(truncated)


class TestPullSale(_PullBase):
    """Sale trio adoption — unlike the regular price, 0/empty is meaningful
    ("no sale running"), so the pull adopts it instead of skipping."""

    def test_item_adopts_the_sale_trio(self):
        product = _wc_payload(
            sale_price="99.00",
            date_on_sale_from="2026-08-25T00:00:00",
            date_on_sale_to="2026-08-30T23:59:59")
        result, doc, _price = self._run("AMMO-9MM", product=product)
        self.assertEqual(doc.sale_price, 99.0)
        # POS schedules in whole days — only the calendar day survives.
        self.assertEqual(doc.sale_from, "2026-08-25")
        self.assertEqual(doc.sale_to, "2026-08-30")
        self.assertIn("sale", result["updated"])

    def test_item_adopts_a_cleared_sale(self):
        # _wc_payload carries no sale keys — the wp-admin-cleared shape must
        # CLEAR POS too, or the next lean push resurrects yesterday's promo.
        _result, doc, _price = self._run("AMMO-9MM")
        self.assertEqual(doc.sale_price, 0)
        self.assertIsNone(doc.sale_from)
        self.assertIsNone(doc.sale_to)

    def test_serial_adopts_the_sale_trio(self):
        product = _wc_payload(sale_price="99.00")
        _result, doc, _price = self._run("SG551::410054", product=product)
        self.assertEqual(doc.sale_sell_price, 99.0)

    def test_uniform_parent_skips_the_sale_with_a_note(self):
        """Same silent-no-op guard as the regular price: under uniform pricing
        firearm.serial_sale_fields ignores the serial's own sale tag."""
        product = _wc_payload(sale_price="99.00")
        result, doc, _price = self._run(
            "SG551::410054", product=product,
            values={("Item", "uniform_sell_price"): 1})
        self.assertFalse(hasattr(doc, "sale_sell_price"))
        self.assertIn("uniform", result["sale_note"])
