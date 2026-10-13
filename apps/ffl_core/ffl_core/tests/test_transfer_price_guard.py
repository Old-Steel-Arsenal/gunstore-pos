"""A customer's transferred-in gun must not reprice the store's own SKU.

INCIDENT (2026-07-19).  A customer transfer brought in a used Glock 19.
Staff had to put *something* in the cost and price boxes to get the
receive through, so they typed placeholders: cost $10, price $35.  The Item —
GLP61950203, a **uniform-priced** SKU shared by every Glock 19 on the shelf —
took both:

* ``sell_price`` 35 was stamped onto ``Item.standard_rate`` (receive_goods), and
* ``rate`` 10 was mirrored onto ``Item.dealer_price`` (the purchase hook's
  "Cost" mirror),

so four brand-new $589.68 pistols went to WooCommerce at **$35**.  Reddit found
them within hours; three orders had to be cancelled and refunded.

Scope decision: **cut the transfer write-back only.**
A transfer gun is the CUSTOMER's property passing through — its numbers describe
one gun's paperwork, never the store's price or cost for that model.  Purchase
and manual receives keep today's behaviour deliberately; the residual risk
(a mistyped price on a real purchase still propagates) is accepted.

Per-SERIAL writes are untouched on purpose: ``Serial No.sell_price`` and
``purchase_rate`` still record what was typed, so the individual gun's paperwork
stays intact and auditable.  Only the SKU-level fields are protected.

Still true after the 2026-07-20 custody rework, and worth knowing why.  That run
routes transfers into a custody warehouse at rate 0 — no placeholder survives —
but ONLY when the Receive Goods "FFL Transfer" checkbox is ticked.  The soft
markers exercised here (``transfer_for_customer`` / ``acquisition_type``) drive
the price guard alone, deliberately: prod uses the "FFL Transfer" AcquireType for
real PURCHASES too (bought from another licensee), so letting
them erase a cost would delete real inventory value.  This module therefore keeps
testing exactly what it always tested; the custody behaviour is asserted against
the checkbox in ``tests/test_transfer_custody.py``.
"""
from __future__ import annotations

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from ffl_core.api.receive_goods import create_receive
from ffl_core.firearm import is_transfer_intake
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError


# ===========================================================================
# The classifier
# ===========================================================================

class TestTransferIntakeDetection(FrappeTestCase):
    """Two independent markers reach the receive, and either one means the gun
    belongs to a customer rather than the store."""

    def test_a_named_transfer_customer_marks_a_transfer(self):
        self.assertTrue(is_transfer_intake(None, "CUST-0001"))

    def test_an_explicit_transfer_acquisition_type_marks_a_transfer(self):
        self.assertTrue(is_transfer_intake("Transfer", None))

    def test_matching_is_case_and_space_insensitive(self):
        """``ffl_acquisition_type`` is a free-text Data field (it mirrors
        FastBound's vocabulary), so it arrives however the caller typed it."""
        for value in ("transfer", "TRANSFER", " Transfer "):
            with self.subTest(value=value):
                self.assertTrue(is_transfer_intake(value, None))

    def test_a_purchase_is_not_a_transfer(self):
        self.assertFalse(is_transfer_intake("Purchase", None))

    def test_a_bare_receive_is_not_a_transfer(self):
        """Neither marker set → ordinary stock. Must stay False: reading a plain
        receive as a transfer would silently stop legitimate price updates."""
        self.assertFalse(is_transfer_intake(None, None))
        self.assertFalse(is_transfer_intake("", ""))


# ===========================================================================
# The receive path
# ===========================================================================

class TransferReceiveDoesNotRepriceTheSku(FrappeTestCase):
    ITEM = "TPG-UNIFORM-GLOCK"
    REAL_PRICE = 589.68
    REAL_COST = 420.00

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import test_bootstrap
        test_bootstrap.run()

        cls.company = (frappe.new_doc("Purchase Receipt").company
                       or frappe.db.get_single_value("Global Defaults", "default_company"))
        from ffl_core.warehouses import default_stock_warehouse
        cls.warehouse = default_stock_warehouse(cls.company)

        if not frappe.db.exists("Supplier Group", "Distributor"):
            frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Distributor",
                            "parent_supplier_group": "All Supplier Groups",
                            "is_group": 0}).insert(ignore_permissions=True)
        if not frappe.db.exists("Supplier", "TPG-FFL"):
            frappe.get_doc({"doctype": "Supplier", "supplier_name": "TPG-FFL",
                            "supplier_group": "Distributor", "is_ffl_dealer": 1,
                            "ffl_number": "1-55-555-55-5X-55555",
                            "ez_check_status": "Verified"}).insert(ignore_permissions=True)
        if not frappe.db.exists("Customer", "TPG-Transfer-Customer"):
            frappe.get_doc({"doctype": "Customer",
                            "customer_name": "TPG-Transfer-Customer"}).insert(ignore_permissions=True)

    def setUp(self) -> None:
        super().setUp()
        p = patch.object(FastBoundClient, "from_settings",
                         side_effect=FastBoundConfigError("disabled in transfer-guard tests"))
        p.start()
        self.addCleanup(p.stop)
        # A uniform-priced firearm SKU carrying REAL numbers, rebuilt per test so
        # one test's write-back can never mask another's assertion.
        if not frappe.db.exists("Item", self.ITEM):
            frappe.get_doc({
                "doctype": "Item", "item_code": self.ITEM, "item_name": self.ITEM,
                "item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
                "has_serial_no": 1, "serial_no_series": f"{self.ITEM}-.####",
            }).insert(ignore_permissions=True)
        frappe.db.set_value("Item", self.ITEM, {
            "uniform_sell_price": 1,
            "standard_rate": self.REAL_PRICE,
            "dealer_price": self.REAL_COST,
        }, update_modified=False)

    def _receive(self, serial, *, transfer=False, rate=10.0, sell_price=35.0):
        payload = {
            "supplier": "TPG-FFL",
            "source": "Distributor",
            "warehouse": self.warehouse,
            "items": [{
                "item_code": self.ITEM, "qty": 1, "rate": rate,
                "serials": [serial], "sell_price": sell_price,
                "manufacturer": "Glock", "model_name": "19",
                "firearm_type": "Pistol", "caliber": "9mm",
            }],
        }
        if transfer:
            payload["transfer_for_customer"] = "TPG-Transfer-Customer"
            payload["acquisition_type"] = "Transfer"
        return create_receive(json.dumps(payload))

    # -- the guard ----------------------------------------------------------

    def test_transfer_placeholder_price_does_not_touch_standard_rate(self):
        """The $35 half of the incident."""
        res = self._receive("TPG-XFER-1", transfer=True)
        self.assertTrue(res["ok"])
        self.assertEqual(
            float(frappe.db.get_value("Item", self.ITEM, "standard_rate")),
            self.REAL_PRICE)

    def test_transfer_placeholder_cost_does_not_touch_dealer_price(self):
        """The $10 half — written by a different hook (compliance.purchase), so
        it needed its own guard and would otherwise have survived the fix."""
        res = self._receive("TPG-XFER-2", transfer=True)
        self.assertTrue(res["ok"])
        self.assertEqual(
            float(frappe.db.get_value("Item", self.ITEM, "dealer_price")),
            self.REAL_COST)

    def test_transfer_still_records_the_numbers_on_the_gun_itself(self):
        """The guard protects the SKU, not the paperwork. The individual gun must
        still carry what was typed, or the transfer's own record would be lost.

        Restored 2026-07-20 after briefly being superseded. The custody rework
        first treated any transfer marker as grounds to erase the cost, which
        would have made this assertion false; a prod audit
        then showed the soft markers also ride on ordinary purchases, so cost
        erasure was narrowed to the explicit checkbox and this behaviour —
        and this test — stands unchanged."""
        res = self._receive("TPG-XFER-3", transfer=True)
        self.assertTrue(res["ok"])
        self.assertEqual(
            float(frappe.db.get_value("Serial No", "TPG-XFER-3", "purchase_rate")), 10.0)
        self.assertEqual(
            float(frappe.db.get_value("Serial No", "TPG-XFER-3", "sell_price")), 35.0)

    def test_the_selling_item_price_is_not_repriced_either(self):
        """standard_rate and the POS Item Price are stamped together; guarding
        only the Item field would leave the POS still charging $35."""
        self._receive("TPG-XFER-4", transfer=True)
        price_list = frappe.db.get_value(
            "Price List", {"selling": 1, "enabled": 1}, "name")
        rate = frappe.db.get_value(
            "Item Price",
            {"item_code": self.ITEM, "price_list": price_list, "selling": 1},
            "price_list_rate")
        self.assertNotEqual(float(rate or 0), 35.0)

    # -- the other half: ordinary receives are untouched ---------------------

    def test_an_ordinary_purchase_still_sets_the_sku_price(self):
        """The scope is frozen at transfers. A real purchase re-pricing its SKU is
        existing, wanted behaviour — a guard that also blocked this would break
        every restock."""
        res = self._receive("TPG-BUY-1", transfer=False, rate=400.0, sell_price=650.0)
        self.assertTrue(res["ok"])
        self.assertEqual(
            float(frappe.db.get_value("Item", self.ITEM, "standard_rate")), 650.0)

    def test_an_ordinary_purchase_still_mirrors_the_cost(self):
        res = self._receive("TPG-BUY-2", transfer=False, rate=400.0, sell_price=650.0)
        self.assertTrue(res["ok"])
        self.assertEqual(
            float(frappe.db.get_value("Item", self.ITEM, "dealer_price")), 400.0)
