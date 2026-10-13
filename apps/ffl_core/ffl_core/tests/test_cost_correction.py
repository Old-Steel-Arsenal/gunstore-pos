"""Manager self-service firearm cost correction — GL/ledger acceptance tests.

Feature: correct the acquisition cost of ONE in-stock, owned firearm serial from
the Item page. Two-layer write:
  1. Stock Reconciliation (single serial, valuation only) — posts the delta to
     Dr Stock In Hand / Cr Stock Adjustment (or reverse for a down-valuation).
  2. Serial No.purchase_rate := new_cost (the firearm-COGS SSOT, sales_report.py).

These tests turn the accountant's blocking conditions into
executable ledger-level assertions. The crown jewel is
`SiblingSerialIsolation`: a single-serial revaluation must
NOT disturb any sibling serial of the same Item — proven at the ledger layer, on
a real 2+-serial owned Item.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate, nowtime

COMPANY_NAME = "FFL Test"

OWNED_ITEM = "CCOR-OWNED-GUN"        # owned firearm, single-serial happy path
MULTI_ITEM = "CCOR-MULTI-GUN"        # owned firearm, multi-serial
CONS_ITEM = "CCOR-CONS-GUN"          # firearm flagged consignment
ACC_ITEM = "CCOR-ACCESSORY"          # serialized NON-firearm (reject)


def _ensure_item(item_code: str, *, item_group: str) -> None:
    if frappe.db.exists("Item", item_code):
        return
    frappe.get_doc({
        "doctype": "Item", "item_code": item_code, "item_name": item_code,
        "item_group": item_group, "stock_uom": "Nos",
        "is_stock_item": 1, "has_serial_no": 1,
    }).insert(ignore_permissions=True)


class CostCorrectionFixtures(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, pay_on_sale
        dev_site.run()
        # PS-7: cost_correction now reads the pay-on-sale stamp fields off the
        # Serial No, so they must exist for every cost-correction test (prod
        # creates them at after_migrate; ensure here so a fresh CI DB has them too).
        pay_on_sale.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        frappe.defaults.set_user_default("Company", cls.company)
        cls.stores = frappe.db.get_value(
            "Warehouse", {"company": cls.company, "warehouse_name": "Stores"}, "name")
        cls.stock_adj = frappe.get_cached_value("Company", cls.company, "stock_adjustment_account")
        cls.srbnb = frappe.db.get_value("Company", cls.company, "stock_received_but_not_billed")

        _ensure_item(OWNED_ITEM, item_group="Pistol")
        _ensure_item(MULTI_ITEM, item_group="Pistol")
        _ensure_item(CONS_ITEM, item_group="Pistol")
        _ensure_item(ACC_ITEM, item_group="Optic")

    # --- helpers ---------------------------------------------------------

    def _receive(self, item: str, serial: str, cost: float, warehouse: str | None = None) -> None:
        from ffl_core.firearm import serialized_receipt_row
        wh = warehouse or self.stores
        if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
                "Serial No", serial, "status") == "Active":
            return
        row = serialized_receipt_row(
            item_code=item, qty=1, warehouse=wh, basic_rate=cost, serial=serial)
        if not cost:
            row["allow_zero_valuation_rate"] = 1  # a $0 / never-booked acquisition
        se = frappe.get_doc({
            "doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
            "company": self.company,
            "items": [row],
        })
        se.flags.ignore_permissions = True
        se.insert()
        se.submit()
        # Mirror production intake (compliance/purchase.py stamps the acquisition
        # cost onto Serial No.purchase_rate) — a raw Material Receipt does not.
        frappe.db.set_value("Serial No", serial, "purchase_rate", flt(cost, 2), update_modified=False)

    def _serial_valuation(self, item: str, warehouse: str, serial: str) -> float:
        """Independent ledger read of ONE serial's current book value."""
        from erpnext.stock.utils import get_incoming_rate
        return flt(get_incoming_rate({
            "item_code": item, "warehouse": warehouse,
            "posting_date": nowdate(), "posting_time": nowtime(),
            "qty": -1, "actual_qty": -1, "serial_no": serial,
            "company": self.company, "voucher_type": "Stock Entry",
        }, raise_error_if_no_rate=False), 2)

    def _bin(self, item: str, warehouse: str) -> dict:
        return frappe.db.get_value(
            "Bin", {"item_code": item, "warehouse": warehouse},
            ["actual_qty", "stock_value"], as_dict=True) or frappe._dict(actual_qty=0, stock_value=0)

    def _voucher_gl(self, voucher_no: str) -> list:
        return frappe.get_all(
            "GL Entry",
            filters={"voucher_no": voucher_no, "is_cancelled": 0},
            fields=["account", "debit", "credit"])


# ---------------------------------------------------------------------------
# Happy path (single owned serial)
# ---------------------------------------------------------------------------
class HappyPath(CostCorrectionFixtures):
    def test_upvaluation_sets_both_layers_and_posts_delta_gl(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-UP"
        self._receive(OWNED_ITEM, serial, 100.0)

        res = correct_serial_cost(serial, 900.0, "Invoice found: real cost was 900")

        # two layers land on the new value
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 900.0)
        self.assertAlmostEqual(self._serial_valuation(OWNED_ITEM, self.stores, serial), 900.0, places=2)

        # GL: exactly 2 lines, Cr Stock Adjustment 800 / Dr inventory 800
        sr = res["stock_reconciliation"]
        gl = self._voucher_gl(sr)
        self.assertEqual(len(gl), 2)
        adj = [g for g in gl if g.account == self.stock_adj]
        self.assertEqual(len(adj), 1)
        self.assertAlmostEqual(flt(adj[0].credit), 800.0, places=2)
        self.assertAlmostEqual(flt(adj[0].debit), 0.0, places=2)
        other = [g for g in gl if g.account != self.stock_adj]
        self.assertEqual(len(other), 1)
        self.assertAlmostEqual(flt(other[0].debit), 800.0, places=2)

        self.assertEqual(res["posting_date"], nowdate())
        self.assertAlmostEqual(res["old_cost"], 100.0, places=2)
        self.assertAlmostEqual(res["new_cost"], 900.0, places=2)

    def test_downvaluation_reverses_direction(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-DOWN"
        self._receive(OWNED_ITEM, serial, 900.0)

        res = correct_serial_cost(serial, 400.0, "Overstated at intake")

        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 400.0)
        gl = self._voucher_gl(res["stock_reconciliation"])
        self.assertEqual(len(gl), 2)
        adj = [g for g in gl if g.account == self.stock_adj]
        # down-valuation: Stock Adjustment is DEBITED (expense up), inventory credited
        self.assertAlmostEqual(flt(adj[0].debit), 500.0, places=2)
        self.assertAlmostEqual(flt(adj[0].credit), 0.0, places=2)

    def test_quantity_never_moves(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-QTY"
        self._receive(OWNED_ITEM, serial, 250.0)
        before = self._bin(OWNED_ITEM, self.stores).actual_qty
        res = correct_serial_cost(serial, 275.0, "recount")
        after = self._bin(OWNED_ITEM, self.stores).actual_qty
        self.assertEqual(after, before)
        # the reco row itself recorded a pure revaluation
        qd = frappe.db.get_value(
            "Stock Reconciliation Item",
            {"parent": res["stock_reconciliation"]}, "quantity_difference")
        self.assertAlmostEqual(flt(qd), 0.0, places=6)

    def test_two_layer_consistency_invariant_C5(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-C5"
        self._receive(OWNED_ITEM, serial, 333.33)
        correct_serial_cost(serial, 700.00, "reconcile to invoice")
        pr = flt(frappe.db.get_value("Serial No", serial, "purchase_rate"))
        val = self._serial_valuation(OWNED_ITEM, self.stores, serial)
        self.assertEqual(pr, 700.00)
        self.assertAlmostEqual(pr, val, places=2)

    def test_source_of_cost_hint_on_zero_to_large_jump_C1(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-HINT"
        self._receive(OWNED_ITEM, serial, 0.0)   # $0 / never-booked acquisition
        res = correct_serial_cost(serial, 5700.00, "FN FNC true cost")
        self.assertTrue(res["source_of_cost_hint"])

    def test_no_hint_on_ordinary_correction_C1(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-NOHINT"
        self._receive(OWNED_ITEM, serial, 480.0)
        res = correct_serial_cost(serial, 505.0, "minor fix")
        self.assertFalse(res["source_of_cost_hint"])

    def test_audit_comment_and_timeline_capture_real_user_C7(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-HP-AUDIT"
        self._receive(OWNED_ITEM, serial, 200.0)

        manager = "ccor-manager@example.com"
        if not frappe.db.exists("User", manager):
            u = frappe.get_doc({
                "doctype": "User", "email": manager, "first_name": "Cost Mgr",
                "send_welcome_email": 0,
            })
            u.flags.ignore_permissions = True
            u.insert()
            u.add_roles("Stock Manager")

        frappe.set_user(manager)
        try:
            res = correct_serial_cost(serial, 640.0, "trade-in revalue reason-XYZ")
        finally:
            frappe.set_user("Administrator")

        sr = res["stock_reconciliation"]
        # reason lives on a Comment against the reco (reco has no remarks field)
        reco_comments = frappe.get_all(
            "Comment",
            filters={"reference_doctype": "Stock Reconciliation", "reference_name": sr},
            fields=["content", "owner"])
        self.assertTrue(any("reason-XYZ" in (c.content or "") for c in reco_comments))
        self.assertTrue(any(c.owner == manager for c in reco_comments))

        # Serial No timeline cross-references the MAT-RECO no. and captures the real user
        sn_comments = frappe.get_all(
            "Comment",
            filters={"reference_doctype": "Serial No", "reference_name": serial},
            fields=["content", "owner"])
        self.assertTrue(any(sr in (c.content or "") for c in sn_comments),
                        "Serial No timeline must cross-reference the reconciliation no.")
        self.assertTrue(any(c.owner == manager for c in sn_comments))


# ---------------------------------------------------------------------------
# Sibling serials MUST be untouched (ledger-level)
# ---------------------------------------------------------------------------
class SiblingSerialIsolation(CostCorrectionFixtures):
    def test_correcting_one_serial_does_not_disturb_siblings(self):
        from ffl_core.api.cost_correction import correct_serial_cost

        serials = ["CCOR-SIB-1", "CCOR-SIB-2", "CCOR-SIB-3"]
        for s in serials:
            self._receive(MULTI_ITEM, s, 500.0)
        target = "CCOR-SIB-2"
        siblings = ["CCOR-SIB-1", "CCOR-SIB-3"]

        before_val = {s: self._serial_valuation(MULTI_ITEM, self.stores, s) for s in serials}
        before_pr = {s: flt(frappe.db.get_value("Serial No", s, "purchase_rate")) for s in serials}
        before_bin = self._bin(MULTI_ITEM, self.stores)

        new_cost = 900.0
        delta = new_cost - 500.0  # 400
        res = correct_serial_cost(target, new_cost, "physical recount, target only")

        after_bin = self._bin(MULTI_ITEM, self.stores)

        # Item quantity unchanged
        self.assertEqual(after_bin.actual_qty, before_bin.actual_qty)
        # Bin.stock_value rose by EXACTLY the target delta (no sibling contamination)
        self.assertAlmostEqual(
            flt(after_bin.stock_value) - flt(before_bin.stock_value), delta, places=2)

        # each sibling's valuation AND purchase_rate unchanged
        for s in siblings:
            self.assertAlmostEqual(
                self._serial_valuation(MULTI_ITEM, self.stores, s), before_val[s], places=2,
                msg=f"sibling {s} valuation moved")
            self.assertEqual(
                flt(frappe.db.get_value("Serial No", s, "purchase_rate")), before_pr[s],
                msg=f"sibling {s} purchase_rate moved")

        # target moved to new value on both layers
        self.assertAlmostEqual(
            self._serial_valuation(MULTI_ITEM, self.stores, target), new_cost, places=2)
        self.assertEqual(flt(frappe.db.get_value("Serial No", target, "purchase_rate")), new_cost)

        # the reconciliation's declared bundle names ONLY the target serial
        sr_doc = frappe.get_doc("Stock Reconciliation", res["stock_reconciliation"])
        bundle = sr_doc.items[0].serial_and_batch_bundle
        self.assertTrue(bundle)
        entries = frappe.get_all(
            "Serial and Batch Entry", filters={"parent": bundle}, fields=["serial_no"])
        self.assertEqual([e.serial_no for e in entries], [target])

        # (ledger-direct) EVERY Stock Ledger Entry this reconciliation posted
        # names the TARGET serial only — no sibling gets an SLE. A serial-scoped
        # revaluation is an outward-at-old + inward-at-new pair on the SAME serial,
        # so qty nets to zero and value nets to exactly the target delta. NOTE: in
        # v16 the serial identity lives in the Serial and Batch Bundle, not the
        # SLE.serial_no column (null here) — we resolve through the bundle, never by
        # filtering SLE.serial_no (that would pass vacuously for every serial).
        voucher_sles = frappe.get_all(
            "Stock Ledger Entry",
            filters={"voucher_no": res["stock_reconciliation"], "is_cancelled": 0},
            fields=["serial_and_batch_bundle", "actual_qty", "stock_value_difference"])
        self.assertTrue(voucher_sles, "revaluation posted no SLE")
        sle_serials = set()
        for e in voucher_sles:
            self.assertTrue(e.serial_and_batch_bundle, "revaluation SLE must carry a serial bundle")
            for be in frappe.get_all(
                    "Serial and Batch Entry",
                    filters={"parent": e.serial_and_batch_bundle}, fields=["serial_no"]):
                sle_serials.add(be.serial_no)
        self.assertEqual(sle_serials, {target},
                         f"reconciliation posted SLEs for {sle_serials}, expected only {{{target}}}")
        # qty nets to zero (pure revaluation); value nets to exactly the target delta
        self.assertAlmostEqual(sum(flt(e.actual_qty) for e in voucher_sles), 0.0, places=6)
        self.assertAlmostEqual(
            sum(flt(e.stock_value_difference) for e in voucher_sles), delta, places=2)

        # GL into Stock Adjustment for this voucher == exactly the target delta
        gl = self._voucher_gl(res["stock_reconciliation"])
        adj_net = sum(flt(g.credit) - flt(g.debit) for g in gl if g.account == self.stock_adj)
        self.assertAlmostEqual(adj_net, delta, places=2)  # up-valuation → net credit == delta


# ---------------------------------------------------------------------------
# Rejections (all zero-write) + permission
# ---------------------------------------------------------------------------
class Rejections(CostCorrectionFixtures):
    def test_rejects_new_cost_zero_C6(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-ZERO"
        self._receive(OWNED_ITEM, serial, 300.0)
        with self.assertRaises(frappe.ValidationError):
            correct_serial_cost(serial, 0, "zero it")
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)

    def test_rejects_negative_cost_C6(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-NEG"
        self._receive(OWNED_ITEM, serial, 300.0)
        with self.assertRaises(frappe.ValidationError):
            correct_serial_cost(serial, -50, "negative")

    def test_rejects_empty_reason(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-REASON"
        self._receive(OWNED_ITEM, serial, 300.0)
        with self.assertRaises(frappe.ValidationError):
            correct_serial_cost(serial, 400.0, "   ")

    def test_rejects_consignment_serial_server_side_C4(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-CONS"
        self._receive(CONS_ITEM, serial, 300.0)
        frappe.db.set_value("Serial No", serial, "is_consignment", 1, update_modified=False)
        try:
            with self.assertRaises(frappe.ValidationError):
                correct_serial_cost(serial, 800.0, "should be blocked")
        finally:
            frappe.db.set_value("Serial No", serial, "is_consignment", 0, update_modified=False)
        # no write happened
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)

    def test_rejects_non_firearm(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-ACC"
        self._receive(ACC_ITEM, serial, 40.0)
        with self.assertRaises(frappe.ValidationError):
            correct_serial_cost(serial, 60.0, "not a gun")

    def test_rejects_sold_serial(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-SOLD"
        self._receive(OWNED_ITEM, serial, 300.0)
        frappe.db.set_value("Serial No", serial, "status", "Delivered", update_modified=False)
        try:
            with self.assertRaises(frappe.ValidationError):
                correct_serial_cost(serial, 800.0, "already sold")
        finally:
            frappe.db.set_value("Serial No", serial, "status", "Active", update_modified=False)

    def test_rejects_unknown_serial(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        with self.assertRaises(frappe.ValidationError):
            correct_serial_cost("CCOR-NOPE-XYZ", 500.0, "ghost")

    def test_rejects_non_manager(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-PERM"
        self._receive(OWNED_ITEM, serial, 300.0)

        clerk = "ccor-clerk@example.com"
        if not frappe.db.exists("User", clerk):
            u = frappe.get_doc({
                "doctype": "User", "email": clerk, "first_name": "Clerk",
                "send_welcome_email": 0,
            })
            u.flags.ignore_permissions = True
            u.insert()
            u.add_roles("Sales User")   # deliberately NOT Stock/System Manager

        frappe.set_user(clerk)
        try:
            with self.assertRaises(frappe.PermissionError):
                correct_serial_cost(serial, 700.0, "clerk should be blocked")
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)

    def test_rejects_when_stock_period_frozen_C2(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-FROZEN"
        self._receive(OWNED_ITEM, serial, 300.0)

        frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", nowdate())
        try:
            with self.assertRaises(frappe.ValidationError):
                correct_serial_cost(serial, 800.0, "into frozen period")
        finally:
            frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", None)
        # zero-write
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)

    def test_rejects_when_accounts_period_frozen_C2(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-REJ-ACCFROZEN"
        self._receive(OWNED_ITEM, serial, 300.0)

        frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", nowdate())
        try:
            with self.assertRaises(frappe.ValidationError):
                correct_serial_cost(serial, 800.0, "into frozen accounts period")
        finally:
            frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)


# ---------------------------------------------------------------------------
# Atomicity — a layer-2 failure must leave NO report≠GL split state
# ---------------------------------------------------------------------------
class ConsignedOutStock(CostCorrectionFixtures):
    """A gun OUT on consignment at a dealer is still OUR asset — only its
    warehouse changed. Refusing to correct its cost created a dead end (it is
    exactly the stock whose valuation decides the settlement COGS), so the
    warehouse guard is gone and only `is_consignment=1` — a gun consigned TO us,
    which was never ours — is refused.

    The two conditions are independent and this class pins both, because the
    words look alike: `is_consignment` is a property of the GUN (whose is it),
    `consignment_dealer` is a property of the WAREHOUSE (where is it).
    """

    DEALER = "CCOR Consignee Dealer"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.warehouses import ensure_stock_at_consignee_account
        ensure_stock_at_consignee_account(cls.company)
        if not frappe.db.exists("Customer", cls.DEALER):
            frappe.get_doc({
                "doctype": "Customer", "customer_name": cls.DEALER,
                "customer_group": "Commercial", "territory": "All Territories",
                "is_ffl_dealer": 1,
            }).insert(ignore_permissions=True)
        from ffl_core.warehouses import ensure_consignment_warehouse
        cls.dealer_wh = ensure_consignment_warehouse(cls.DEALER, cls.company)

    def test_a_gun_out_on_consignment_can_be_corrected(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-OUT-OK"
        self._receive(OWNED_ITEM, serial, 100.0, warehouse=self.dealer_wh)

        res = correct_serial_cost(serial, 900.0, "Invoice found after it shipped to the dealer")

        self.assertTrue(res["ok"])
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 900.0)
        self.assertAlmostEqual(
            self._serial_valuation(OWNED_ITEM, self.dealer_wh, serial), 900.0, places=2)

    def test_the_stock_leg_lands_on_the_consignee_account_not_stock_in_hand(self):
        """Nothing in this module ever hard-coded an inventory
        account — the counter-account (Stock Adjustment / SRBNB) is ours, the
        stock leg is resolved by ERPNext from the warehouse. That is exactly why
        this needs an assertion rather than a code change: the behaviour is
        inherited, and an inherited behaviour is one nobody notices breaking."""
        from ffl_core.api.cost_correction import correct_serial_cost
        from ffl_core.warehouses import stock_at_consignee_account
        serial = "CCOR-OUT-GL"
        self._receive(OWNED_ITEM, serial, 100.0, warehouse=self.dealer_wh)

        res = correct_serial_cost(serial, 400.0, "cost correction while at the dealer")

        gl = self._voucher_gl(res["stock_reconciliation"])
        self.assertEqual(len(gl), 2)
        dr = [g for g in gl if flt(g.debit) > 0]
        cr = [g for g in gl if flt(g.credit) > 0]
        self.assertEqual(len(dr), 1)
        self.assertEqual(len(cr), 1)
        # the stock leg follows the WAREHOUSE, not the company's default
        self.assertEqual(dr[0].account, stock_at_consignee_account(self.company))
        self.assertEqual(dr[0].account, frappe.db.get_value("Warehouse", self.dealer_wh, "account"))
        self.assertAlmostEqual(flt(dr[0].debit), 300.0, places=2)
        # ...and the counter-account is still ours to choose
        self.assertEqual(cr[0].account, self.stock_adj)
        self.assertAlmostEqual(flt(cr[0].credit), 300.0, places=2)

    def test_the_picker_lists_a_gun_that_is_out_on_consignment(self):
        """The server guard and the picker have to move together. the requirement
        names only the server check, but the dialog fed by list_item_serials_for_cost
        applied the same warehouse exclusion — relaxing one and not the other
        would leave the UI dead end exactly where it was."""
        from ffl_core.api.cost_correction import list_item_serials_for_cost
        out_at_dealer = "CCOR-OUT-PICKER"
        not_ours = "CCOR-OUT-PICKER-THEIRS"
        self._receive(MULTI_ITEM, out_at_dealer, 500.0, warehouse=self.dealer_wh)
        self._receive(MULTI_ITEM, not_ours, 500.0, warehouse=self.dealer_wh)
        frappe.db.set_value("Serial No", not_ours, "is_consignment", 1, update_modified=False)
        try:
            names = {r["serial"] for r in list_item_serials_for_cost(MULTI_ITEM)["serials"]}
        finally:
            frappe.db.set_value("Serial No", not_ours, "is_consignment", 0, update_modified=False)

        self.assertIn(out_at_dealer, names, "our gun at a dealer must be pickable")
        self.assertNotIn(not_ours, names, "a gun consigned TO us must stay hidden")

    def test_a_gun_consigned_TO_us_is_still_refused_even_in_that_warehouse(self):
        """The relaxation must not open the other door: `is_consignment=1` is a
        gun that is not the store's to revalue, wherever it is standing."""
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-OUT-NOTOURS"
        self._receive(CONS_ITEM, serial, 300.0, warehouse=self.dealer_wh)
        frappe.db.set_value("Serial No", serial, "is_consignment", 1, update_modified=False)
        try:
            with self.assertRaises(frappe.ValidationError) as ctx:
                correct_serial_cost(serial, 800.0, "should still be blocked")
            self.assertIn("consignment firearm", str(ctx.exception))
        finally:
            frappe.db.set_value("Serial No", serial, "is_consignment", 0, update_modified=False)
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)


class Atomicity(CostCorrectionFixtures):
    def test_layer2_failure_rolls_back_reco_no_split_state(self):
        from ffl_core.api import cost_correction
        serial = "CCOR-ATOMIC"
        self._receive(OWNED_ITEM, serial, 300.0)
        before_val = self._serial_valuation(OWNED_ITEM, self.stores, serial)

        real_set_value = frappe.db.set_value

        def boom(doctype, name, fieldname, *args, **kwargs):
            if doctype == "Serial No" and fieldname == "purchase_rate":
                raise RuntimeError("injected layer-2 failure")
            return real_set_value(doctype, name, fieldname, *args, **kwargs)

        # neutralise any 3rd-party commit so the savepoint rollback is effective
        with patch("frappe.db.commit"), patch("frappe.db.set_value", side_effect=boom):
            with self.assertRaises(Exception):
                cost_correction.correct_serial_cost(serial, 900.0, "will fail on layer 2")

        # purchase_rate unchanged AND valuation unchanged → no split
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 300.0)
        self.assertAlmostEqual(
            self._serial_valuation(OWNED_ITEM, self.stores, serial), before_val, places=2)


# ---------------------------------------------------------------------------
# Read-only picker helper for the dialog
# ---------------------------------------------------------------------------
class ListSerialsHelper(CostCorrectionFixtures):
    def test_lists_only_active_owned_firearm_serials(self):
        from ffl_core.api.cost_correction import list_item_serials_for_cost
        active = "CCOR-LIST-ACTIVE"
        sold = "CCOR-LIST-SOLD"
        cons = "CCOR-LIST-CONS"
        self._receive(MULTI_ITEM, active, 500.0)
        self._receive(MULTI_ITEM, sold, 500.0)
        self._receive(MULTI_ITEM, cons, 500.0)
        frappe.db.set_value("Serial No", sold, "status", "Delivered", update_modified=False)
        frappe.db.set_value("Serial No", cons, "is_consignment", 1, update_modified=False)
        try:
            out = list_item_serials_for_cost(MULTI_ITEM)
        finally:
            frappe.db.set_value("Serial No", sold, "status", "Active", update_modified=False)
            frappe.db.set_value("Serial No", cons, "is_consignment", 0, update_modified=False)

        names = {r["serial"] for r in out["serials"]}
        self.assertIn(active, names)
        self.assertNotIn(sold, names)
        self.assertNotIn(cons, names)
        row = next(r for r in out["serials"] if r["serial"] == active)
        self.assertIn("purchase_rate", row)

    def test_non_firearm_item_lists_nothing(self):
        from ffl_core.api.cost_correction import list_item_serials_for_cost
        out = list_item_serials_for_cost(ACC_ITEM)
        self.assertFalse(out["is_firearm"])
        self.assertEqual(out["serials"], [])


# ---------------------------------------------------------------------------
# PS-7 — a pay-on-sale gun whose cost is still parked
# in SRBNB must book its correction delta to SRBNB, not Stock Adjustment, and
# keep srbnb_cost in lock-step with purchase_rate/valuation so the eventual sale
# settlement clears SRBNB to zero. Plain owned guns are untouched.
# ---------------------------------------------------------------------------
class PayOnSaleSRBNBCorrection(CostCorrectionFixtures):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if not frappe.db.exists("Supplier", "_Test SRBNB Supplier"):
            frappe.get_doc({
                "doctype": "Supplier", "supplier_name": "_Test SRBNB Supplier",
                "supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
                    or "All Supplier Groups",
                "supplier_type": "Company",
            }).insert(ignore_permissions=True)
        cls.supplier = "_Test SRBNB Supplier"

    def _stamp_pay_on_sale(self, serial: str, srbnb_cost: float, *, settled: int = 0) -> None:
        frappe.db.set_value("Serial No", serial, {
            "pay_on_sale": 1, "pay_on_sale_supplier": self.supplier,
            "srbnb_cost": srbnb_cost, "pay_on_sale_settled": settled,
        }, update_modified=False)

    def test_srbnb_gun_books_upvaluation_delta_to_srbnb_and_syncs_srbnb_cost(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-POS-UP"
        self._receive(OWNED_ITEM, serial, 400.0)
        self._stamp_pay_on_sale(serial, 400.0)

        res = correct_serial_cost(serial, 700.00, "true cost per Test Vendor invoice")

        # both value layers move to the new cost …
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 700.00)
        self.assertAlmostEqual(self._serial_valuation(OWNED_ITEM, self.stores, serial), 700.00, places=2)
        # … AND srbnb_cost tracks it (three-way invariant srbnb_cost==purchase_rate==valuation)
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "srbnb_cost")), 700.00)

        # GL: the delta (300.00) posts to SRBNB (Cr, up-valuation), never Stock Adjustment
        gl = self._voucher_gl(res["stock_reconciliation"])
        self.assertEqual(len(gl), 2)
        srbnb_lines = [g for g in gl if g.account == self.srbnb]
        self.assertEqual(len(srbnb_lines), 1)
        self.assertAlmostEqual(flt(srbnb_lines[0].credit), 300.00, places=2)
        self.assertAlmostEqual(flt(srbnb_lines[0].debit), 0.0, places=2)
        self.assertFalse([g for g in gl if g.account == self.stock_adj],
                         "SRBNB gun must not post to Stock Adjustment")

    def test_srbnb_gun_downvaluation_debits_srbnb(self):
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-POS-DOWN"
        self._receive(OWNED_ITEM, serial, 700.0)
        self._stamp_pay_on_sale(serial, 700.0)

        res = correct_serial_cost(serial, 500.0, "overstated at intake")

        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "srbnb_cost")), 500.0)
        gl = self._voucher_gl(res["stock_reconciliation"])
        srbnb_lines = [g for g in gl if g.account == self.srbnb]
        self.assertEqual(len(srbnb_lines), 1)
        # down-valuation reduces the liability: Dr SRBNB
        self.assertAlmostEqual(flt(srbnb_lines[0].debit), 200.0, places=2)
        self.assertFalse([g for g in gl if g.account == self.stock_adj])

    def test_srbnb_gun_suppresses_source_of_cost_hint(self):
        # An SRBNB gun's cost source IS the SRBNB liability — no source-of-cost JE
        # is ever owed, so the source-of-cost hint must NOT fire even on a ~0 -> large jump.
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-POS-HINT"
        self._receive(OWNED_ITEM, serial, 0.5)
        self._stamp_pay_on_sale(serial, 0.5)
        res = correct_serial_cost(serial, 5700.00, "FN true cost, cost was in SRBNB")
        self.assertFalse(res["source_of_cost_hint"])

    def test_plain_owned_gun_still_uses_stock_adjustment(self):
        # Discrimination proof: a non-pay-on-sale gun keeps the Stock Adjustment
        # path and never touches SRBNB.
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-POS-PLAIN"
        self._receive(OWNED_ITEM, serial, 400.0)  # NOT stamped pay_on_sale
        res = correct_serial_cost(serial, 500.0, "plain owned correction")
        gl = self._voucher_gl(res["stock_reconciliation"])
        self.assertTrue([g for g in gl if g.account == self.stock_adj])
        self.assertFalse([g for g in gl if g.account == self.srbnb])
        # srbnb_cost stays 0 for a plain gun
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "srbnb_cost") or 0), 0.0)

    def test_incompletely_stamped_srbnb_gun_blocks_correction(self):
        # A pay_on_sale, unsettled gun with a real purchase cost but NO persisted
        # srbnb_cost is incompletely stamped — its cost may still sit in SRBNB.
        # Routing the delta to Stock Adjustment (the srbnb_cost<=0 fallthrough)
        # would desync SRBNB once the gun is backfilled + sold. Fail closed,
        # mirroring the settle path's BLOCK — never silently mis-route.
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-POS-INCOMPLETE"
        self._receive(OWNED_ITEM, serial, 400.0)  # purchase_rate=400
        frappe.db.set_value("Serial No", serial, {
            "pay_on_sale": 1, "pay_on_sale_supplier": self.supplier,
            "srbnb_cost": 0, "pay_on_sale_settled": 0}, update_modified=False)
        with self.assertRaises(frappe.ValidationError):
            correct_serial_cost(serial, 500.0, "should block until stamp is fixed")
        # zero write
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "purchase_rate")), 400.0)

    def test_settled_pay_on_sale_gun_uses_stock_adjustment(self):
        # Once settled, the cost has left SRBNB (it's in AP), so a later correction
        # must go to Stock Adjustment, not SRBNB, and must NOT rewrite srbnb_cost.
        from ffl_core.api.cost_correction import correct_serial_cost
        serial = "CCOR-POS-SETTLED"
        self._receive(OWNED_ITEM, serial, 400.0)
        self._stamp_pay_on_sale(serial, 400.0, settled=1)
        res = correct_serial_cost(serial, 500.0, "corrected after settlement")
        gl = self._voucher_gl(res["stock_reconciliation"])
        self.assertTrue([g for g in gl if g.account == self.stock_adj])
        self.assertFalse([g for g in gl if g.account == self.srbnb])
        self.assertEqual(flt(frappe.db.get_value("Serial No", serial, "srbnb_cost")), 400.0)
