"""A Uniform-Priced firearm must never mint a NEW per-serial WooCommerce product.

Incident 2026-07-21: a caliber-cleanup
batch touched ~40 Items and, through a bulk per-serial push entrypoint
(``client_api.reconcile_now`` / ``push_all_serials`` → ``_push_serial_list`` →
``sync.push_serial``), created 122 ghost ``item_code::serial`` products for
Items priced with Uniform Pricing.  Each ghost double-counted stock against the
Item-level model post (e.g. German P226 Grade B: model post stock 6 + 7 ghosts =
13 buyable for 7 real guns).

The routing rule (already the authority in ``client_api.push_item_now``):

    firearm & uniform_sell_price=0  → one per-serial product per Active serial
    uniform_sell_price=1 / non-gun  → ONE item-level product, quantity stock

The enabling defect was that ``sync.push_serial`` — the sink every push path
funnels through — had guards for custody / hand-delist / unpriced but NOT for
"this Item is uniform-priced, so it has no per-serial product at all".  This
adds that guard, scoped to the CREATE path only:

  * a NEW serial (no ``{site}_product_id``) of a uniform firearm is REFUSED;
  * a LEGACY serial that already carries a product id (e.g. a gun held for
    an in-flight order) still updates / delists / goes oos — the system must
    be able to manage an existing listing down, only new ones are refused.

Like custody, this is a STRUCTURAL fact about the parent Item ("it was never
supposed to have a per-serial product"), not a "should we list it right now?"
question — so ``_relist`` cannot lift it.

Touchpoints, mirroring test_transfer_custody_isolation.py:
  1. sync.push_serial            — the sink every push path funnels through.
  2. client_api bulk entrypoints — push_all_serials / reconcile_now funnel here.
  3. sync.push_all_serials       — the fail-fast list variant funnels here too.
  4. reconcile Pass 2            — can only ever touch EXISTING products (its
                                   query pins product_id != ""), so it cannot
                                   mint a ghost by construction; pinned here.
  5. doc_events on_item_update   — routes a uniform firearm to the item-level
                                   push, never to a per-serial fan-out.
  6. client_api._skip_message    — the refusal reads back to the operator.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


# ---------------------------------------------------------------------------
# frappe.db.get_value side effect covering every read push_serial makes
# ---------------------------------------------------------------------------

def _make_gv(*, product_id="", item_code="ITEM-UNI", is_firearm=1, uniform=1,
             item_group="", delisted=0, status="Active", sold_archived=0):
    """Return a side_effect for ``frappe.db.get_value`` that answers each read
    ``sync.push_serial`` performs, keyed by (doctype, fieldname)."""
    def _se(doctype, name=None, fieldname=None, *args, **kwargs):
        if doctype == "Serial No" and isinstance(fieldname, (list, tuple)):
            out = {}
            for f in fieldname:
                if f == "item_code":
                    out[f] = item_code
                elif f == "status":
                    out[f] = status
                elif "delisted" in f:
                    out[f] = delisted
                elif "sold_archived" in f:
                    out[f] = sold_archived
                elif "product_id" in f:
                    out[f] = product_id
                else:
                    out[f] = None
            return out
        if doctype == "Serial No":
            fn = str(fieldname)
            if fieldname == "item_code":
                return item_code
            if "delisted" in fn:
                return delisted
            if "product_id" in fn:
                return product_id
            return None
        if doctype == "Item":
            if isinstance(fieldname, (list, tuple)):
                return {"is_firearm": is_firearm, "item_group": item_group,
                        "uniform_sell_price": uniform}
            return None
        return None
    return _se


def setUpModule():
    # These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
    # the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
    from ffl_core.tests.channel_query_doubles import warm_channel_meta

    warm_channel_meta()


# ===========================================================================
# 1. The sink: push_serial
# ===========================================================================

class UniformFirearmSerialIsNeverCreated(FrappeTestCase):
    def test_a_new_serial_of_a_uniform_firearm_is_refused_before_any_http(self):
        from ffl_woo_sync.woocommerce import sync

        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=1)), \
            patch("ffl_woo_sync.woocommerce.sync._client") as client:
            res = sync.push_serial("SN-NEW", _retries=0)
        self.assertEqual(res.get("skipped"), sync.UNIFORM_ITEM)
        client.assert_not_called()

    def test_a_legacy_firearm_group_uniform_item_is_refused_too(self):
        """The firearm test is ``is_firearm OR firearm item_group`` — a legacy row
        whose is_firearm flag never got stamped must still be caught."""
        from ffl_woo_sync.woocommerce import sync

        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=0,
                                       item_group="Pistol", uniform=1)), \
            patch("ffl_woo_sync.woocommerce.sync._client") as client:
            res = sync.push_serial("SN-LEGACY-GROUP", _retries=0)
        self.assertEqual(res.get("skipped"), sync.UNIFORM_ITEM)
        client.assert_not_called()

    def test_the_refusal_is_not_overridable_by_relist(self):
        """``_relist=True`` answers "yes, a human wants this republished" — it
        exists for guns the owner HID and later un-hid.  It cannot apply here: a
        uniform Item never had a per-serial product to relist.  So even the
        operator "Push to WooCommerce" button is refused — which is why the
        button is also hidden in the UI (serial_no_woo.js), belt and suspenders."""
        from ffl_woo_sync.woocommerce import sync

        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=1)), \
            patch("ffl_woo_sync.woocommerce.sync._client") as client:
            res = sync.push_serial("SN-NEW", _retries=0, _relist=True)
        self.assertEqual(res.get("skipped"), sync.UNIFORM_ITEM)
        client.assert_not_called()

    def test_a_legacy_serial_with_an_existing_product_id_is_allowed_through(self):
        """The legacy case: a per-serial product ALREADY exists on a uniform Item.
        The system must still be able to update / delist / oos it, so the guard
        (create-only) must NOT fire.  Proven by letting it fall to the NEXT guard
        (unpriced) instead of being refused as uniform_item."""
        from ffl_woo_sync.woocommerce import sync

        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="WC-2865", is_firearm=1,
                                       uniform=1, delisted=0)), \
            patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=0), \
            patch("ffl_woo_sync.woocommerce.sync._refuse_unpriced",
                  return_value={"skipped": "unpriced"}):
            res = sync.push_serial("SN-2865", _retries=0)
        self.assertEqual(res.get("skipped"), "unpriced")

    def test_a_new_per_serial_firearm_uniform_0_is_not_refused(self):
        """Regression: the per-serial route (uniform_sell_price=0) must keep
        creating one product per gun.  A new (no product id) uniform=0 serial
        must fall PAST the uniform guard."""
        from ffl_woo_sync.woocommerce import sync

        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=0, delisted=0)), \
            patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=0), \
            patch("ffl_woo_sync.woocommerce.sync._refuse_unpriced",
                  return_value={"skipped": "unpriced"}):
            res = sync.push_serial("SN-PERSERIAL", _retries=0)
        self.assertEqual(res.get("skipped"), "unpriced")

    def test_a_non_firearm_serial_is_not_refused(self):
        from ffl_woo_sync.woocommerce import sync

        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=0, item_group="Accessory",
                                       uniform=1, delisted=0)), \
            patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=0), \
            patch("ffl_woo_sync.woocommerce.sync._refuse_unpriced",
                  return_value={"skipped": "unpriced"}):
            res = sync.push_serial("SN-ACCESSORY", _retries=0)
        self.assertEqual(res.get("skipped"), "unpriced")

    def test_a_priced_new_per_serial_firearm_still_creates_its_product(self):
        """The positive control: a genuine per-serial firearm (uniform=0) with a
        price still POSTs a new product.  Proves the guard did not disarm the
        legitimate per-serial create path."""
        from ffl_woo_sync.woocommerce import sync

        client = MagicMock()
        client.create_product.return_value = {"id": "999"}
        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=0,
                                       delisted=0, status="Active")), \
            patch("ffl_woo_sync.woocommerce.sync.effective_serial_price", return_value=900.0), \
            patch("ffl_woo_sync.woocommerce.sync._client", return_value=client), \
            patch("ffl_woo_sync.woocommerce.mapping.serial_to_product_payload", return_value={}), \
            patch("ffl_woo_sync.woocommerce.sync._apply_category"), \
            patch("frappe.db.set_value"):
            res = sync.push_serial("SN-REAL", _retries=0)
        client.create_product.assert_called_once()
        self.assertEqual(str(res.get("id")), "999")


# ===========================================================================
# 2. Bulk entrypoints funnel through the guard
# ===========================================================================

class BulkEntrypointsApplyTheSameGuard(FrappeTestCase):
    def test_sync_push_all_serials_skips_every_uniform_serial(self):
        """sync.push_all_serials is fail-fast, but a guard refusal is a RETURN,
        not a raise — so it collects skip dicts and never mints a product."""
        from ffl_woo_sync.woocommerce import sync

        client = MagicMock()
        client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.get_all", return_value=["SN-A", "SN-B"]), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=1)):
            results = sync.push_all_serials("UNI-GUN", client=client, _retries=0)
        self.assertTrue(all(r.get("skipped") == sync.UNIFORM_ITEM for r in results))
        client.create_product.assert_not_called()

    def test_client_api_push_all_serials_buckets_uniform_serials_into_skipped(self):
        from ffl_woo_sync.woocommerce import sync
        from ffl_woo_sync.woocommerce.client_api import push_all_serials

        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
            patch("frappe.get_all", return_value=["SN-A", "SN-B"]), \
            patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=1)):
            MockWC.from_settings.return_value = mock_client
            result = push_all_serials("UNI-GUN")
        self.assertFalse(result["ok"])
        self.assertEqual(result["pushed"], [])
        self.assertEqual(len(result["skipped"]), 2)
        self.assertTrue(all(s["reason"] == sync.UNIFORM_ITEM for s in result["skipped"]))
        mock_client.create_product.assert_not_called()

    def test_reconcile_now_pushes_item_level_and_skips_the_serials(self):
        """A uniform Item: reconcile_now pushes the ONE item-level product and
        buckets every serial into skipped_serials — no ghost per-serial create."""
        from ffl_woo_sync.woocommerce import sync
        from ffl_woo_sync.woocommerce.client_api import reconcile_now

        mock_client = MagicMock()
        mock_client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.client_api.WooCommerceClient") as MockWC, \
            patch("ffl_woo_sync.woocommerce.sync.push_item",
                  return_value={"id": "MODEL-POST"}) as mock_pi, \
            patch("frappe.get_all", return_value=["SN-A", "SN-B"]), \
            patch("ffl_woo_sync.woocommerce.sync.is_custody_serial", return_value=False), \
            patch("frappe.db.get_value",
                  side_effect=_make_gv(product_id="", is_firearm=1, uniform=1)):
            MockWC.from_settings.return_value = mock_client
            result = reconcile_now("UNI-GUN")
        mock_pi.assert_called_once()
        self.assertEqual(result["pushed_serials"], [])
        self.assertEqual(len(result["skipped_serials"]), 2)
        self.assertTrue(all(s["reason"] == sync.UNIFORM_ITEM
                            for s in result["skipped_serials"]))
        mock_client.create_product.assert_not_called()

    def test_reconcile_pass2_query_only_touches_existing_products(self):
        """Reconcile can never MINT a per-serial ghost: Pass 2's Serial No query
        pins ``product_id != ''``, so it only ever re-pushes products that already
        exist.  Pin the filter so a refactor cannot silently widen it."""
        client = MagicMock()
        client.get_attribute_ids.return_value = {}
        with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
                   return_value=client), \
            patch("frappe.get_all", return_value=[]) as mock_ga, \
            patch("frappe.db.set_value"):
            from ffl_woo_sync.woocommerce.sync import _f
            from ffl_woo_sync.woocommerce.tasks import _run_reconcile
            _run_reconcile()
        pass2 = [c for c in mock_ga.call_args_list if c.args[:1] == ("Serial No",)
                 and (c.kwargs.get("filters") or {}).get("status") == "Active"]
        self.assertTrue(pass2, "Pass 2's Serial No query was not found")
        self.assertEqual(pass2[0].kwargs["filters"].get(_f("retail", "product_id")),
                         ["!=", ""])


# ===========================================================================
# 3. doc_events routing
# ===========================================================================

class ItemUpdateRoutesUniformToItemLevel(FrappeTestCase):
    """on_item_update mirrors push_item_now's routing: a uniform firearm gets the
    ONE item-level push, never a per-serial fan-out.  It cannot create ghosts —
    it enqueues push_item_task only, and only when the Item already has a
    woo_product_id."""

    def _item_doc(self, changed=("caliber",), woo_product_id="WC-ITEM"):
        doc = MagicMock()
        doc.name = "UNI-GUN"
        doc.woo_product_id = woo_product_id
        doc.has_value_changed.side_effect = lambda f: f in changed
        return doc

    def test_caliber_edit_on_uniform_firearm_enqueues_item_push_not_per_serial(self):
        """The exact incident trigger — a caliber batch-edit — must route to the
        item-level push, never to per-serial pushes."""
        from ffl_woo_sync.woocommerce.doc_events import on_item_update

        doc = self._item_doc(changed=("caliber",))
        with patch("ffl_woo_sync.woocommerce.doc_events._gallery_changed", return_value=False), \
            patch("frappe.enqueue") as mock_enqueue:
            on_item_update(doc)
        targets = [c.args[0] for c in mock_enqueue.call_args_list]
        self.assertIn("ffl_woo_sync.woocommerce.tasks.push_item_task", targets)
        self.assertNotIn("ffl_woo_sync.woocommerce.tasks.push_serial_task", targets)

    def test_no_enqueue_when_item_has_no_product_id(self):
        """Existence guard: a uniform Item never pushed before has no
        woo_product_id, so a field edit enqueues nothing — doc events never
        auto-create."""
        from ffl_woo_sync.woocommerce.doc_events import on_item_update

        doc = self._item_doc(changed=("caliber",), woo_product_id="")
        with patch("ffl_woo_sync.woocommerce.doc_events._gallery_changed", return_value=False), \
            patch("frappe.enqueue") as mock_enqueue:
            on_item_update(doc)
        mock_enqueue.assert_not_called()


# ===========================================================================
# 4. The refusal reads back to the operator (the skip reason is legible)
# ===========================================================================

class UniformSkipMessageIsReadable(FrappeTestCase):
    def test_uniform_item_skip_message_is_not_the_generic_fallback(self):
        from ffl_woo_sync.woocommerce import sync
        from ffl_woo_sync.woocommerce.client_api import _skip_message

        generic = _skip_message("some_unknown_reason")
        specific = _skip_message(sync.UNIFORM_ITEM)
        self.assertNotEqual(specific, generic)

    def test_uniform_item_skip_message_points_at_the_item_level_route(self):
        from ffl_woo_sync.woocommerce import sync
        from ffl_woo_sync.woocommerce.client_api import _skip_message

        msg = _skip_message(sync.UNIFORM_ITEM).lower()
        self.assertIn("uniform", msg)
        self.assertIn("item", msg)
