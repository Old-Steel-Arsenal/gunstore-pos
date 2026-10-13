"""Selling a gun with no recorded cost warns, and never blocks.

The blocking/not-blocking half is the whole point:
a hard stop at the register trades a wrong margin for a lost sale. So every test
here asserts the call RETURNS, and the alert is checked by capturing what the
hook printed rather than by whether it raised.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

COMPANY_NAME = "FFL Test"
GUN_ITEM = "ZCA-GUN"


class UnpricedSaleAlert(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from ffl_core.setup import dev_site, zero_cost
        dev_site.run()
        zero_cost.ensure()
        cls.company = frappe.db.get_value("Company", {"company_name": COMPANY_NAME}, "name")
        if not frappe.db.exists("Item", GUN_ITEM):
            frappe.get_doc({
                "doctype": "Item", "item_code": GUN_ITEM, "item_name": GUN_ITEM,
                "item_group": "Pistol", "stock_uom": "Nos",
                "is_stock_item": 1, "has_serial_no": 1,
            }).insert(ignore_permissions=True)

    @staticmethod
    def _serial(name: str, *, purchase_rate: float = 0.0, confirmed: int = 0,
            is_consignment: int = 0, is_transfer: int = 0) -> str:
        if not frappe.db.exists("Serial No", name):
            frappe.get_doc({
                "doctype": "Serial No", "serial_no": name, "item_code": GUN_ITEM,
            }).insert(ignore_permissions=True)
        frappe.db.set_value("Serial No", name, {
            "purchase_rate": purchase_rate,
            "zero_cost_confirmed": confirmed,
            "is_consignment": is_consignment,
            "is_transfer_gun": is_transfer,
        }, update_modified=False)
        return name

    @staticmethod
    def _doc(*serials: str, **over) -> SimpleNamespace:
        base = dict(doctype="Sales Invoice", name="ZCA-TEST", is_return=0,
            items=[frappe._dict({"idx": 1, "item_code": GUN_ITEM,
                "serial_no": "\n".join(serials), "qty": len(serials)})])
        base.update(over)
        return SimpleNamespace(**base)

    def _warn(self, doc) -> list:
        """Run the hook, returning whatever it printed. Never raises — if the
        hook ever starts throwing, THAT is the regression this returns to."""
        from ffl_core.compliance.zero_cost_sale_alert import warn_on_unpriced_firearm_sale
        with patch("frappe.msgprint") as mp:
            warn_on_unpriced_firearm_sale(doc)
        return mp.call_args_list

    # --- warns -------------------------------------------------------------

    def test_a_gun_with_no_recorded_cost_warns_without_blocking(self):
        serial = self._serial("ZCA-NOCOST", purchase_rate=0.0)
        calls = self._warn(self._doc(serial))
        self.assertEqual(len(calls), 1, "the sale must be flagged")
        self.assertIn(serial, str(calls[0]))
        self.assertIn("NOT blocked", str(calls[0]))

    def test_a_placeholder_penny_cost_still_warns(self):
        # the $0.01 migration guns — a placeholder, not a price
        serial = self._serial("ZCA-PENNY", purchase_rate=0.01)
        self.assertEqual(len(self._warn(self._doc(serial))), 1)

    def test_every_unpriced_gun_on_the_invoice_is_named_at_once(self):
        a = self._serial("ZCA-MULTI-A", purchase_rate=0.0)
        b = self._serial("ZCA-MULTI-B", purchase_rate=0.0)
        calls = self._warn(self._doc(a, b))
        self.assertEqual(len(calls), 1, "one message, not one per gun")
        printed = str(calls[0])
        self.assertIn(a, printed)
        self.assertIn(b, printed)

    # --- stays quiet -------------------------------------------------------

    def test_a_priced_gun_is_not_flagged(self):
        serial = self._serial("ZCA-PRICED", purchase_rate=500.0)
        self.assertEqual(self._warn(self._doc(serial)), [])

    def test_a_confirmed_free_gun_is_not_flagged(self):
        """Feature A already recorded WHY this one was free. Warning again is how
        an alert teaches people to dismiss it."""
        serial = self._serial("ZCA-CONFIRMED", purchase_rate=0.0, confirmed=1)
        self.assertEqual(self._warn(self._doc(serial)), [])

    def test_a_gun_consigned_to_us_is_not_flagged(self):
        serial = self._serial("ZCA-CONS", purchase_rate=0.0, is_consignment=1)
        self.assertEqual(self._warn(self._doc(serial)), [])

    def test_a_customers_gun_in_custody_is_not_flagged(self):
        serial = self._serial("ZCA-CUSTODY", purchase_rate=0.0, is_transfer=1)
        self.assertEqual(self._warn(self._doc(serial)), [])

    def test_a_return_is_not_flagged(self):
        serial = self._serial("ZCA-RETURN", purchase_rate=0.0)
        self.assertEqual(self._warn(self._doc(serial, is_return=1)), [])

    def test_a_consolidated_invoice_is_not_flagged_twice(self):
        """The POS Invoice it rolls up already warned at the register."""
        serial = self._serial("ZCA-CONSOLIDATED", purchase_rate=0.0)
        self.assertEqual(self._warn(self._doc(serial, is_consolidated=1)), [])

    def test_a_serialized_non_firearm_is_not_flagged(self):
        """The firearm filter, pinned. Without it this alert would fire on a $0
        serialized accessory — which carries no bound-book cost story and is not
        the gap being warned about. Asserting it keeps the filter from silently
        becoming a no-op (e.g. if is_firearm_item's input ever changes shape)."""
        if not frappe.db.exists("Item", "ZCA-ACC-SERIAL"):
            frappe.get_doc({
                "doctype": "Item", "item_code": "ZCA-ACC-SERIAL",
                "item_name": "ZCA-ACC-SERIAL", "item_group": "Optic",
                "stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1,
            }).insert(ignore_permissions=True)
        serial = "ZCA-ACC-1"
        if not frappe.db.exists("Serial No", serial):
            frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
                "item_code": "ZCA-ACC-SERIAL"}).insert(ignore_permissions=True)
        frappe.db.set_value("Serial No", serial, "purchase_rate", 0.0,
            update_modified=False)
        doc = SimpleNamespace(doctype="Sales Invoice", name="ZCA-ACC-INV",
            is_return=0, items=[frappe._dict({"idx": 1,
                "item_code": "ZCA-ACC-SERIAL", "serial_no": serial, "qty": 1})])
        self.assertEqual(self._warn(doc), [])

    def test_a_line_with_no_serial_is_ignored(self):
        doc = SimpleNamespace(doctype="Sales Invoice", name="ZCA-NOSERIAL",
            is_return=0, items=[frappe._dict(
                {"idx": 1, "item_code": "ZCA-ACCESSORY", "serial_no": ""})])
        self.assertEqual(self._warn(doc), [])


class HookWiring(FrappeTestCase):
    def test_the_alert_is_wired_onto_both_invoice_doctypes(self):
        """Open Question 3: the owner asked for it at the POS register AND on
        Sales Invoice. One of the two silently missing is invisible in testing —
        you only find out when a gun sells through the other door."""
        from ffl_core import hooks
        path = "ffl_core.compliance.zero_cost_sale_alert.warn_on_unpriced_firearm_sale"
        for doctype in ("Sales Invoice", "POS Invoice"):
            self.assertIn(path, hooks.doc_events[doctype]["validate"], doctype)


if __name__ == "__main__":
    unittest.main()
