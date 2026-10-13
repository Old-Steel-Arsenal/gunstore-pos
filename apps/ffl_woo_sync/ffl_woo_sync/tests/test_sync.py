"""Unit tests for woocommerce/sync.py.

All WooCommerce HTTP calls and Frappe DB operations are mocked — no live site
or WooCommerce store is required.

Design notes
------------
* ``_make_client()`` builds a MagicMock configured to match the WooCommerceClient
  domain-method interface: ``get_attribute_ids``, ``create_product``,
  ``update_product``, ``get_product``.
* ``attr_ids`` are pre-built and passed directly to avoid testing
  ``get_attribute_ids`` in every case (it has its own test class).
* ``_retries=0`` is passed to sync functions in tests that don't need to exercise
  the retry path — this prevents ``time.sleep`` calls in the test suite.
* ``sync._now`` is patched in ``setUp`` for every class whose tests call sync
  functions — ``_now()`` calls ``frappe.utils.now_datetime()`` which reads
  System Settings and requires a full site context.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import sync
from ffl_woo_sync.woocommerce.client import WooCommerceConfigError, WooCommerceError

_FAKE_NOW = "2026-06-16 00:00:00"

_DEFAULT_ATTR_IDS = {"pa_caliber": 5, "pa_condition": 7}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(
    *,
    attr_ids=None,
    create_id=999,
    update_id=None,
):
    """Return a MagicMock WooCommerceClient using the domain-method interface.

    Pass ``attr_ids={}`` to simulate an empty attribute taxonomy response.
    ``None`` (the default) uses a realistic two-attribute map.
    """
    client = MagicMock()
    client.get_attribute_ids.return_value = (
        attr_ids if attr_ids is not None else _DEFAULT_ATTR_IDS
    )
    client.create_product.return_value = {"id": create_id, "status": "publish"}
    client.update_product.return_value = {
        "id": update_id if update_id is not None else create_id,
        "status": "publish",
    }
    client.get_product.return_value = {"id": create_id}
    return client


def _item_gv(**overrides):
    """``frappe.db.get_value`` stub for an item that is PRICED and NOT hidden.

    The item-level twin of :func:`_serial_gv`, for the same reason: ``push_item``
    now refuses an unpriced or hand-delisted product before it builds a client,
    so a stub that answers ``None`` to everything describes a product that must
    not be published — not the ordinary item these tests are about.
    """
    values = {"standard_rate": 100.0, "woo_delisted": 0, "item_group": None}
    values.update(overrides)

    def _fn(doctype, name, field, *a, **k):
        if isinstance(field, (list, tuple)):
            # is_firearm / item_group / uniform_sell_price: an ordinary item, so
            # the per-serial-firearm exemption stays off
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _fn


def _serial_gv(**overrides):
    """``frappe.db.get_value`` stub for a serial that is PRICED and NOT hidden.

    ``push_serial`` gained two preconditions, so the bare
    ``return_value=None`` these tests used to mean "nothing interesting to read"
    now says something quite specific and quite different: *no price, no parent
    item, therefore refuse before starting*. Every push below would be correctly
    refused, and the tests would be asserting against a code path they are not
    about.

    So the fixture now STATES the price. ``item_code`` stays blank on purpose —
    that skips the WC category lookup (as before) and makes the price resolve
    from the serial's own tag rather than a uniform parent.
    """
    values = {"sell_price": 1200, "woo_delisted": 0, "item_code": ""}
    values.update(overrides)
    real = frappe.db.get_value  # captured before the patch replaces it

    def _fn(*args, **kwargs):
        # docs/testing.md rule 4: frappe's own reads go through. Building a cold meta
        # (reservations' get_meta("Serial No") after any cache clear) calls get_value with
        # keywords, which this positional fixture used to die on.
        if len(args) < 3 or args[0] in ("DocType", "DocField", "Custom Field", "Property Setter"):
            return real(*args, **kwargs)
        field = args[2]
        if isinstance(field, (list, tuple)):
            return frappe._dict({f: values.get(f) for f in field})
        return values.get(field)
    return _fn


class _PatchNow(FrappeTestCase):
    """Mixin: patches ``sync._now`` before each test and restores it after.

    Needed because ``_now()`` calls ``frappe.utils.now_datetime()`` which reads
    System Settings from the DB — incompatible with a patched ``frappe.db.*``
    environment.
    """
    def setUp(self):
        super().setUp()
        patcher = patch("ffl_woo_sync.woocommerce.sync._now", return_value=_FAKE_NOW)
        self.addCleanup(patcher.stop)
        patcher.start()


# ---------------------------------------------------------------------------
# push_item
# ---------------------------------------------------------------------------

class TestPushItem(_PatchNow):
    def _run_push(self, woo_id=None, create_id=42, payload=None, client=None, sv_effect=None):
        """Run push_item with a pre-built mock client; return (result, mock_sv, client)."""
        client = client or _make_client(create_id=create_id,
                              update_id=int(woo_id) if woo_id else create_id)

        # push_item now also looks up Item.item_group (for the WC category).
        # Return None for that field so these tests exercise only the core push;
        # category behaviour has its own test class (TestPushItemCategory).
        #
        # The stub must be FIELD-AWARE. It used to answer `woo_id` to every
        # scalar read, which was harmless while push_item only read the product
        # id — but push_item now also reads the two guards, and a blanket answer
        # makes both misfire: `woo_delisted` reads "42" (truthy → hidden), and
        # the `as_dict=True` row read returns a string, which `_is_firearm_row`
        # cannot `.get()`. Neither is possible against a real DB.
        def _gv(doctype, name, field, *a, **k):
            if isinstance(field, (list, tuple)):
                # the is_firearm/item_group/uniform_sell_price row: an ordinary
                # non-firearm item, so the per-serial-firearm exemption is off
                return frappe._dict({f: None for f in field})
            if field == "item_group":
                return None
            if field == "woo_delisted":
                return 0        # not hidden — the delisted-product guard must let this through
            if field == "standard_rate":
                return 100.0    # priced — the unpriced-listing guard must let this through
            if field == "dropship_distributor":
                # Third field, same reason as the two above: an ordinary
                # store-owned item, NOT a dropship SKU.  Answering `woo_id` here
                # would route these tests down the listing path (brand term
                # resolution and all) — a path they do not mean to exercise, and
                # which has its own suite in test_woo_listing_meta.py.
                return None
            return woo_id

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value", side_effect=sv_effect) as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value=payload or {"sku": "TEST-001"},
             ):
            result = sync.push_item("TEST-001", client=client,
                                    attr_ids={"pa_caliber": 5}, _retries=0)
        return result, mock_sv, client

    def test_writeback_starts_a_fresh_snapshot_after_the_store_call(self):
        """The Item row was read before a store call of several seconds; a sibling
        push stamping it meanwhile makes a write from that snapshot MariaDB 1020.
        The commit has to fall between the call and the write."""
        order = MagicMock()
        client = _make_client(update_id=42)
        client.update_product.side_effect = lambda *a, **k: (order.store(), {"id": 42})[1]
        with patch.object(sync, "safe_commit", side_effect=lambda: order.commit()):
            self._run_push(woo_id="42", client=client,
                           sv_effect=lambda *a, **k: order.write(a[0]))
        names = [c[0] for c in order.mock_calls]
        self.assertEqual(names, ["store", "commit", "write"])

    def test_creates_product_when_no_woo_id(self):
        _r, _sv, client = self._run_push(woo_id=None, create_id=42)
        client.create_product.assert_called_once()
        client.update_product.assert_not_called()

    def test_updates_product_when_woo_id_exists(self):
        _r, _sv, client = self._run_push(woo_id="42")
        client.update_product.assert_called_once_with("42", {"sku": "TEST-001"})
        client.create_product.assert_not_called()

    def test_writes_back_woo_product_id_on_create(self):
        _r, mock_sv, _c = self._run_push(woo_id=None, create_id=99)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_product_id"], "99")

    def test_writes_back_woo_listed_1(self):
        _r, mock_sv, _c = self._run_push(woo_id=None, create_id=1)
        self.assertEqual(mock_sv.call_args[0][2]["woo_listed"], 1)

    def test_writes_back_woo_synced_at(self):
        _r, mock_sv, _c = self._run_push(woo_id=None, create_id=1)
        self.assertEqual(mock_sv.call_args[0][2]["woo_synced_at"], _FAKE_NOW)

    def test_writes_back_woo_sync_error_blank(self):
        _r, mock_sv, _c = self._run_push(woo_id=None, create_id=1)
        self.assertEqual(mock_sv.call_args[0][2]["woo_sync_error"], "")

    def test_error_writes_sync_error_and_reraises(self):
        client = _make_client()
        boom = WooCommerceError("4xx bad payload", status_code=400)
        with patch("frappe.db.get_value", side_effect=_item_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 side_effect=boom,
             ):
            with self.assertRaises(WooCommerceError):
                sync.push_item("BAD-001", client=client, attr_ids={}, _retries=0)
        mock_sv.assert_called_once_with(
            "Item", "BAD-001", "woo_sync_error", str(boom)
        )

    def test_404_reraises_with_an_actionable_dangling_message(self):
        """#9 part 2: a 404 from update_product means the WC product behind the
        stored product_id was deleted (a dangling id). push_item still raises
        (behaviour unchanged), but with a message pointing at woo_audit_dangling_ids
        — the durable fix is the audit GC, not a retry."""
        client = _make_client()
        client.update_product.side_effect = WooCommerceError(
            "PUT /products/9 -> 404", status_code=404
        )

        # Field-aware: a blanket "9" would also answer woo_delisted, and the
        # delisted-product guard would refuse the push before the 404 could ever happen.
        _gv = _item_gv(woo_product_id="9")  # set → update path, where a 404 arises

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value={"sku": "DEAD-001"},
             ):
            with self.assertRaises(WooCommerceError) as ctx:
                sync.push_item("DEAD-001", client=client, attr_ids={}, _retries=0)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("woo_audit_dangling_ids", str(ctx.exception))
        self.assertIn("woo_audit_dangling_ids", mock_sv.call_args[0][3])

    def test_passes_attr_ids_to_mapping(self):
        client = _make_client()
        attr_ids = {"pa_caliber": 5, "pa_condition": 7}
        with patch("frappe.db.get_value", side_effect=_item_gv()), \
             patch("frappe.db.set_value"), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
             ) as mock_map:
            mock_map.return_value = {"sku": "X"}
            sync.push_item("X", client=client, attr_ids=attr_ids, _retries=0)
        # brand_id / shipping_class / listing joined the call in RSR P3 — all
        # "unset" on an ordinary retail push.  This case is about attr_ids.
        mock_map.assert_called_once_with(
            "X", attr_ids=attr_ids, site="retail", brand_id=None,
            shipping_class="", listing=False)

    def test_auto_fetches_attr_ids_when_none(self):
        """When attr_ids is not passed, client.get_attribute_ids() is called."""
        client = _make_client()
        with patch("frappe.db.get_value", side_effect=_item_gv()), \
             patch("frappe.db.set_value"), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value={"sku": "X"},
             ):
            sync.push_item("X", client=client, _retries=0)
        client.get_attribute_ids.assert_called_once()

    def test_5xx_retried_then_succeeds(self):
        """Transient 5xx errors are retried; success on second attempt is written back."""
        client = _make_client(create_id=55)
        # First call raises 5xx, second succeeds.
        client.create_product.side_effect = [
            WooCommerceError("Gateway Timeout", status_code=504),
            {"id": 55, "status": "publish"},
        ]
        with patch("frappe.db.get_value", side_effect=_item_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch("ffl_woo_sync.woocommerce.sync.time.sleep") as mock_sleep, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value={"sku": "X"},
             ):
            sync.push_item("X", client=client, attr_ids={}, _retries=1)
        # Retried (sleep called once for attempt 0).
        mock_sleep.assert_called_once_with(1)  # 2**0 = 1
        # Write-back happened.
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_product_id"], "55")

    def test_4xx_fails_fast_no_retry(self):
        """4xx errors are not retried — fail immediately."""
        client = _make_client()
        client.create_product.side_effect = WooCommerceError(
            "Unprocessable Entity", status_code=422
        )
        with patch("frappe.db.get_value", side_effect=_item_gv()), \
             patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.sync.time.sleep") as mock_sleep, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value={"sku": "X"},
             ):
            with self.assertRaises(WooCommerceError):
                sync.push_item("X", client=client, attr_ids={}, _retries=2)
        # No sleep — 4xx is not retried.
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# push_item — WC category = the Item's catalog tree node → the store's term (M3b)
# ---------------------------------------------------------------------------

class TestPushItemCategory(_PatchNow):
    def test_category_resolved_and_applied(self):
        """Item → its tree node → the store's linked term → payload['categories']=[{id}]."""
        client = _make_client(create_id=42)

        def _gv(doctype, name, field, *a, **k):
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: None for f in field})
            if field == "item_group":
                return "Handgun"
            if field == "standard_rate":
                return 100.0  # priced, so the unpriced-listing guard lets the push through
            return None  # woo_product_id blank → create path

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.categories.item_category",
                   return_value="firearms-pistols") as node, \
             patch("ffl_woo_sync.woocommerce.categories.term_for", return_value=17) as term, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value={"sku": "GLOCK-19"},
             ):
            sync.push_item("GLOCK-19", client=client, attr_ids={}, _retries=0)
        node.assert_called_once_with("GLOCK-19")
        term.assert_called_once_with(client, "retail", "firearms-pistols")
        sent = client.create_product.call_args[0][0]
        self.assertEqual(sent["categories"], [{"id": 17}])

    def test_no_category_when_item_group_blank(self):
        """No tree node for the Item → no 'categories' key added."""
        client = _make_client(create_id=42)

        def _gv(doctype, name, field, *a, **k):
            if isinstance(field, (list, tuple)):
                return frappe._dict({f: None for f in field})
            # priced, so the unpriced-listing guard lets the push through; everything else
            # (item_group, woo_product_id) stays blank as this test intends
            return 100.0 if field == "standard_rate" else None

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.categories.item_category", return_value=None), \
             patch("ffl_woo_sync.woocommerce.categories.term_for") as term, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.item_to_product_payload",
                 return_value={"sku": "X"},
             ):
            sync.push_item("X", client=client, attr_ids={}, _retries=0)
        term.assert_not_called()
        sent = client.create_product.call_args[0][0]
        self.assertNotIn("categories", sent)


# ---------------------------------------------------------------------------
# push_serial
# ---------------------------------------------------------------------------

class TestPushSerial(_PatchNow):
    def test_writeback_starts_a_fresh_snapshot_after_the_store_call(self):
        """Same race as the item push: commit between the store call and the write."""
        order = MagicMock()
        client = _make_client(update_id=55)
        client.update_product.side_effect = lambda *a, **k: (order.store(), {"id": 55})[1]
        with patch("frappe.db.get_value", side_effect=_serial_gv(woo_product_id="55")), \
             patch.object(sync, "safe_commit", side_effect=lambda: order.commit()), \
             patch("frappe.db.set_value", side_effect=lambda *a, **k: order.write(a[0])), \
             patch("ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                   return_value={"sku": "GLOCK-19::SN-001"}):
            sync.push_serial("SN-001", client=client, attr_ids={}, _retries=0)
        names = [c[0] for c in order.mock_calls]
        self.assertEqual(names[-3:], ["store", "commit", "write"])

    def test_creates_product_when_no_woo_id(self):
        client = _make_client(create_id=77)
        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "GLOCK-19::SN-001"},
             ):
            sync.push_serial("SN-001", client=client, attr_ids={}, _retries=0)
        client.create_product.assert_called_once()
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_product_id"], "77")

    def test_updates_product_when_woo_id_exists(self):
        client = _make_client(update_id=55)

        # push_serial reads the sold-state pair (list form → dict) before
        # Serial No.item_code (for the WC category) and woo_product_id;
        # return "" for item_code so no category is applied here.
        _gv = _serial_gv(woo_product_id="55")

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value"), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "GLOCK-19::SN-001"},
             ):
            sync.push_serial("SN-001", client=client, attr_ids={}, _retries=0)
        client.update_product.assert_called_once_with("55", {"sku": "GLOCK-19::SN-001"})

    def test_writes_back_woo_synced_at(self):
        client = _make_client(create_id=1)
        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "X::Y"},
             ):
            sync.push_serial("Y", client=client, attr_ids={}, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_synced_at"], _FAKE_NOW)

    def test_attr_ids_produce_real_taxonomy_ids(self):
        """attr_ids are threaded through to serial_to_product_payload."""
        client = _make_client()
        attr_ids = {"pa_caliber": 5, "pa_condition": 7}
        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value"), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
             ) as mock_map:
            mock_map.return_value = {"sku": "X::Y"}
            sync.push_serial("Y", client=client, attr_ids=attr_ids, _retries=0)
        mock_map.assert_called_once_with("Y", attr_ids=attr_ids, site="retail")

    def test_writes_back_woo_sync_error_blank_on_success(self):
        """A successful push clears woo_sync_error on the Serial No."""
        client = _make_client(create_id=99)
        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "X::Y"},
             ):
            sync.push_serial("Y", client=client, attr_ids={}, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_sync_error"], "")

    def test_only_an_explicit_relist_clears_woo_delisted(self):
        """INVERTED by the sentinel fix. This used to assert that ANY push clears
        woo_delisted — which is precisely how the sentinel destroyed itself: the
        first push forgot that a gun had been hidden, and the next sweep put it
        back on sale. Now only a caller that explicitly means "relist" may clear
        it; see tests/test_hidden_serial_guard.py."""
        client = _make_client(create_id=99)
        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "X::Y"},
             ):
            sync.push_serial("Y", client=client, attr_ids={}, _retries=0)
        self.assertNotIn("woo_delisted", mock_sv.call_args[0][2])

        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "X::Y"},
             ):
            sync.push_serial("Y", client=client, attr_ids={}, _retries=0, _relist=True)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_delisted"], 0)

    def test_error_writes_sync_error_and_reraises(self):
        """A push failure writes woo_sync_error to the Serial No and re-raises."""
        client = _make_client()
        boom = WooCommerceError("4xx bad payload", status_code=400)
        with patch("frappe.db.get_value", side_effect=_serial_gv()), \
             patch("frappe.db.set_value") as mock_sv, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 side_effect=boom,
             ):
            with self.assertRaises(WooCommerceError):
                sync.push_serial("BAD-SN", client=client, attr_ids={}, _retries=0)
        mock_sv.assert_called_once_with(
            "Serial No", "BAD-SN", "woo_sync_error", str(boom)[:1000]
        )


# ---------------------------------------------------------------------------
# push_serial — WC category from the serial's parent Item's tree node (M3b)
# ---------------------------------------------------------------------------

class TestPushSerialCategory(_PatchNow):
    def test_category_resolved_from_parent_item(self):
        """Serial → item_code → the Item's tree node → the store's term on the payload."""
        client = _make_client(create_id=77)

        # item_code is set HERE (unlike _serial_gv's blank default) — resolving
        # the parent Item is the whole point of this test. The price therefore
        # resolves through that parent, so the fixture states both sides:
        # a per-serial (non-uniform) item, priced from the serial's own tag.
        _gv = _serial_gv(item_code="GLOCK-19", item_group="Handgun",
                         uniform_sell_price=0, standard_rate=0)

        with patch("frappe.db.get_value", side_effect=_gv), \
             patch("frappe.db.set_value"), \
             patch("ffl_woo_sync.woocommerce.categories.item_category",
                   return_value="firearms-pistols") as node, \
             patch("ffl_woo_sync.woocommerce.categories.term_for", return_value=21), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.serial_to_product_payload",
                 return_value={"sku": "GLOCK-19::SN-1"},
             ):
            sync.push_serial("SN-1", client=client, attr_ids={}, _retries=0)
        node.assert_called_once_with("GLOCK-19")
        sent = client.create_product.call_args[0][0]
        self.assertEqual(sent["categories"], [{"id": 21}])


# ---------------------------------------------------------------------------
# push_all_serials
# ---------------------------------------------------------------------------

class TestPushAllSerials(_PatchNow):
    def test_pushes_each_active_serial(self):
        client = _make_client()
        serials = ["SN-A", "SN-B"]
        with patch("frappe.get_all", return_value=serials), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_ps:
            mock_ps.return_value = {"id": 1}
            results = sync.push_all_serials("ITEM-001", client=client, attr_ids={})
        self.assertEqual(len(results), 2)
        self.assertEqual(mock_ps.call_count, 2)

    def test_shared_attr_ids_not_refetched_per_serial(self):
        """Pre-passed attr_ids are reused; get_attribute_ids() is not called."""
        client = _make_client()
        with patch("frappe.get_all", return_value=["SN-X"]), \
             patch("ffl_woo_sync.woocommerce.sync.push_serial", return_value={"id": 1}):
            sync.push_all_serials("ITEM-001", client=client, attr_ids={"pa_caliber": 5})
        client.get_attribute_ids.assert_not_called()


# ---------------------------------------------------------------------------
# push_stock
# ---------------------------------------------------------------------------

class TestPushStock(_PatchNow):
    def test_pushes_current_qty_to_existing_product(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="55"), \
             patch("frappe.db.set_value"), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.stock_for_item",
                 return_value=8,
             ):
            sync.push_stock("AMMO-001", client=client, _retries=0)
        client.update_product.assert_called_once_with("55", {"stock_quantity": 8})

    def test_raises_when_no_woo_id(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value=""):
            with self.assertRaises(ValueError):
                sync.push_stock("NO-WOO", client=client)

    def test_raises_when_woo_id_none(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value=None):
            with self.assertRaises(ValueError):
                sync.push_stock("NO-WOO", client=client)

    def test_404_dangling_product_id_is_skipped_not_errored(self):
        """A WC product deleted on the store leaves a dangling product_id; its 404
        must be downgraded to a warning and skipped (NOT re-raised into
        push_stock_task's ERROR Error Log). Root cause of the recurring
        'Woo push_stock_task failed: CZ 75 Grade A' during incident PSINV-00035."""
        client = _make_client()
        client.update_product.side_effect = WooCommerceError(
            "PUT products/DEAD-99 → 404: Not Found", status_code=404)
        logger = MagicMock()
        with patch("frappe.db.get_value", return_value="DEAD-99"), \
             patch("frappe.db.set_value") as mock_sv, \
             patch("frappe.logger", return_value=logger), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.stock_for_item",
                 return_value=3,
             ):
            result = sync.push_stock("CZ 75 Grade A", client=client, _retries=0)
        # Did NOT raise; returned a skip marker instead.
        self.assertEqual(result.get("skipped"), "dangling_product_id")
        # A clear warning carrying the product id (not an ERROR Error Log).
        logger.warning.assert_called_once()
        self.assertIn("DEAD-99", logger.warning.call_args.args[0])
        # Not recorded as a successful sync — no synced_at write on the skip path.
        mock_sv.assert_not_called()

    def test_stamp_lost_to_a_concurrent_item_write_is_retried_on_a_fresh_snapshot(self):
        """MariaDB 1020 'Record has changed since last read' on the synced_at
        stamp: the Item row moved under this job's snapshot while the WooCommerce
        round-trip was in flight (an SLE hook stamping the same fields, a desk
        save). The store already has the quantity — filing that as
        'push_stock_task failed' (IRAY-RH25, 2026-08-26 15:23) misreported a push
        that landed. Roll the stale snapshot back and stamp again."""
        client = _make_client()
        attempts = []

        def _set_value(doctype, name, values, *a, **k):
            attempts.append(values)
            if len(attempts) == 1:
                raise frappe.QueryDeadlockError(
                    "(1020, \"Record has changed since last read in table 'tabItem'\")")

        with patch("frappe.db.get_value", return_value="55"), \
             patch("frappe.db.set_value", side_effect=_set_value), \
             patch("frappe.db.rollback") as rollback, \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.stock_for_item",
                 return_value=2,
             ):
            sync.push_stock("IRAY-RH25", client=client, _retries=0)
        client.update_product.assert_called_once_with("55", {"stock_quantity": 2})
        rollback.assert_called_once_with()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[1].get("woo_sync_error"), "")

    def test_non_404_error_still_raises(self):
        """Only a 404 (dangling id) is downgraded; other HTTP errors still raise
        so genuine sync failures aren't silently swallowed."""
        client = _make_client()
        client.update_product.side_effect = WooCommerceError("Forbidden", status_code=403)
        with patch("frappe.db.get_value", return_value="55"), \
             patch("frappe.db.set_value"), \
             patch(
                 "ffl_woo_sync.woocommerce.sync.mapping.stock_for_item",
                 return_value=3,
             ):
            with self.assertRaises(WooCommerceError):
                sync.push_stock("X", client=client, _retries=0)


# ---------------------------------------------------------------------------
# delist_item
# ---------------------------------------------------------------------------

class TestDelistItem(_PatchNow):
    def test_updates_to_draft_zero_stock(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="42"), \
             patch("frappe.db.set_value"):
            sync.delist_item("GLOCK-19", client=client, _retries=0)
        client.update_product.assert_called_once_with(
            "42", {"status": "draft", "stock_quantity": 0}
        )

    def test_writes_back_woo_listed_0(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="42"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.delist_item("GLOCK-19", client=client, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_listed"], 0)

    def test_writes_back_woo_synced_at(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="42"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.delist_item("GLOCK-19", client=client, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_synced_at"], _FAKE_NOW)

    def test_raises_when_no_woo_id(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value=None):
            with self.assertRaises(ValueError):
                sync.delist_item("NO-WOO", client=client)

    def test_raises_when_woo_id_blank(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value=""):
            with self.assertRaises(ValueError):
                sync.delist_item("NO-WOO", client=client)


# ---------------------------------------------------------------------------
# delist_serial
# ---------------------------------------------------------------------------

class TestDelistSerial(_PatchNow):
    def test_updates_to_draft_zero_stock(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value"):
            sync.delist_serial("SN-X", client=client, _retries=0)
        client.update_product.assert_called_once_with(
            "77", {"status": "draft", "stock_quantity": 0}
        )

    def test_raises_when_no_woo_id(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value=None):
            with self.assertRaises(ValueError):
                sync.delist_serial("SN-X", client=client)

    def test_writes_back_woo_synced_at(self):
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.delist_serial("SN-X", client=client, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_synced_at"], _FAKE_NOW)

    def test_sets_woo_delisted_1_on_success(self):
        """delist_serial sets woo_delisted=1 so Pass 3 won't re-delist every cycle."""
        client = _make_client()
        with patch("frappe.db.get_value", return_value="77"), \
             patch("frappe.db.set_value") as mock_sv:
            sync.delist_serial("SN-X", client=client, _retries=0)
        update = mock_sv.call_args[0][2]
        self.assertEqual(update["woo_delisted"], 1)


# ---------------------------------------------------------------------------
# relist_* (delegation smoke tests)
# ---------------------------------------------------------------------------

class TestRelist(_PatchNow):
    def test_relist_item_delegates_to_push_item(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_item") as mock_push:
            mock_push.return_value = {"id": 1}
            sync.relist_item("ITEM-001", client=MagicMock(), attr_ids={})
        mock_push.assert_called_once()
        _args, _kwargs = mock_push.call_args
        self.assertEqual(_args[0], "ITEM-001")

    def test_relist_serial_delegates_to_push_serial(self):
        with patch("ffl_woo_sync.woocommerce.sync.push_serial") as mock_push:
            mock_push.return_value = {"id": 2}
            sync.relist_serial("SN-001", client=MagicMock(), attr_ids={})
        mock_push.assert_called_once()
        _args, _kwargs = mock_push.call_args
        self.assertEqual(_args[0], "SN-001")


# ---------------------------------------------------------------------------
# _f — site validation
# ---------------------------------------------------------------------------

class TestSiteFieldLookup(FrappeTestCase):
    def test_f_raises_config_error_for_wrong_case_site(self):
        """'Dealer' (wrong case) must raise WooCommerceConfigError, not KeyError."""
        with self.assertRaises(WooCommerceConfigError) as cm:
            sync._f("Dealer", "product_id")
        self.assertIn("Dealer", str(cm.exception))

    def test_f_raises_config_error_for_unknown_site(self):
        """An unknown site name must raise WooCommerceConfigError, not KeyError."""
        with self.assertRaises(WooCommerceConfigError) as cm:
            sync._f("wholesale", "product_id")
        self.assertIn("wholesale", str(cm.exception))

    def test_f_retail_resolves_correctly(self):
        self.assertEqual(sync._f("retail", "product_id"), "woo_product_id")

    def test_f_refuses_the_retired_dealer_store(self):
        """1.11.0: the dealer store is gone; a stray caller fails loudly, not silently."""
        with self.assertRaises(WooCommerceConfigError):
            sync._f("dealer", "product_id")


# ---------------------------------------------------------------------------
# _push_with_retry
# ---------------------------------------------------------------------------

class TestPushWithRetry(FrappeTestCase):
    """Tests for the internal retry helper — no DB or Frappe context needed."""

    def test_returns_on_first_success(self):
        fn = MagicMock(return_value={"id": 1})
        result = sync._push_with_retry(fn, retries=2)
        self.assertEqual(result, {"id": 1})
        fn.assert_called_once()

    def test_retries_on_5xx_and_succeeds(self):
        fn = MagicMock(side_effect=[
            WooCommerceError("503", status_code=503),
            {"id": 2},
        ])
        with patch("ffl_woo_sync.woocommerce.sync.time.sleep") as mock_sleep:
            result = sync._push_with_retry(fn, retries=1)
        self.assertEqual(result, {"id": 2})
        mock_sleep.assert_called_once_with(1)  # 2**0 = 1

    def test_retries_on_network_error(self):
        """status_code=None (network failure) is also treated as transient."""
        fn = MagicMock(side_effect=[
            WooCommerceError("Connection refused", status_code=None),
            {"id": 3},
        ])
        with patch("ffl_woo_sync.woocommerce.sync.time.sleep"):
            result = sync._push_with_retry(fn, retries=1)
        self.assertEqual(result, {"id": 3})

    def test_4xx_fails_fast_no_retry(self):
        fn = MagicMock(side_effect=WooCommerceError("Bad Request", status_code=400))
        with patch("ffl_woo_sync.woocommerce.sync.time.sleep") as mock_sleep:
            with self.assertRaises(WooCommerceError):
                sync._push_with_retry(fn, retries=2)
        fn.assert_called_once()  # called exactly once — not retried
        mock_sleep.assert_not_called()

    def test_raises_after_exhausting_retries(self):
        fn = MagicMock(side_effect=WooCommerceError("504", status_code=504))
        with patch("ffl_woo_sync.woocommerce.sync.time.sleep"):
            with self.assertRaises(WooCommerceError):
                sync._push_with_retry(fn, retries=2)
        self.assertEqual(fn.call_count, 3)  # initial + 2 retries

    def test_exponential_backoff_timing(self):
        fn = MagicMock(side_effect=[
            WooCommerceError("500", status_code=500),
            WooCommerceError("500", status_code=500),
            WooCommerceError("500", status_code=500),
        ])
        with patch("ffl_woo_sync.woocommerce.sync.time.sleep") as mock_sleep:
            with self.assertRaises(WooCommerceError):
                sync._push_with_retry(fn, retries=2)
        # Sleeps: 2**0=1 after attempt 0, 2**1=2 after attempt 1; no sleep after final failure.
        self.assertEqual(mock_sleep.call_args_list, [call(1), call(2)])
