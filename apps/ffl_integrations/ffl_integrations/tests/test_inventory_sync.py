"""Bound-book -> ERPNext stock reconcile: safety invariants.

The selection + real Material Issue path is validated against a live FastBound
dev account; these guard the invariants that protect production inventory:
a FastBound fetch failure must remove nothing, and a dry run must never mutate.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import inventory_sync

_PATH = "ffl_integrations.fastbound.inventory_sync"


class ParseItemIds(FrappeTestCase):
    """Only a genuinely absent param (None) means 'remove all'. An explicit empty
    selection ('' or []) must remove NOTHING — a UI posting an empty approval can
    never mass-issue every disposed gun out of stock."""

    def test_none_means_no_filter(self):
        self.assertIsNone(inventory_sync._parse_item_ids(None))

    def test_empty_string_removes_nothing(self):
        self.assertEqual(inventory_sync._parse_item_ids(""), set())

    def test_empty_list_removes_nothing(self):
        self.assertEqual(inventory_sync._parse_item_ids([]), set())
        self.assertEqual(inventory_sync._parse_item_ids("[]"), set())

    def test_parses_json_and_list(self):
        self.assertEqual(inventory_sync._parse_item_ids('["A", "B"]'), {"A", "B"})
        self.assertEqual(inventory_sync._parse_item_ids(["A", " B "]), {"A", "B"})


class BoundBookStockSync(FrappeTestCase):
    def test_fastbound_error_removes_nothing(self):
        """A FastBound fetch failure must abort — never remove on partial data."""
        with patch(f"{_PATH}._fb_disposed_items", side_effect=RuntimeError("FB down")), \
                patch(f"{_PATH}.issue_serial") as mi:
            with self.assertRaises(RuntimeError):
                inventory_sync.sync_in_stock_from_boundbook(dry_run=0)
            mi.assert_not_called()

    def test_no_disposed_means_no_removal(self):
        with patch(f"{_PATH}._fb_disposed_items", return_value={}), \
                patch(f"{_PATH}.issue_serial") as mi:
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=0)
            self.assertEqual(res["removed_count"], 0)
            mi.assert_not_called()

    def test_dry_run_never_issues(self):
        with patch(f"{_PATH}._fb_disposed_items", return_value={"NO-SUCH-ID": "X"}), \
                patch(f"{_PATH}.issue_serial") as mi:
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=1)
            self.assertTrue(res["dry_run"])
            mi.assert_not_called()


class BoundBookStockSyncReal(FrappeTestCase):
    """Real ERPNext stock: issue_serial must submit a valid Material Issue
    (the Stock Entry company is mandatory), and the disposed-match must key off
    the FastBound item id, not the bare serial string."""

    COMPANY_NAME = "FFL Test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ensure_root_groups()
        cls._ensure_company()
        cls.company = frappe.db.get_value("Company", {"company_name": cls.COMPANY_NAME}, "name")
        cls._ensure_uom("Nos")
        cls._ensure_item_group("All Item Groups", 1, None)
        cls._ensure_item_group("BBS Firearms", 0, "All Item Groups")
        cls._ensure_caliber("9mm Luger", "Pistol")
        cls._ensure_brand("BBS Mfg")
        cls.warehouse = cls._ensure_warehouse("BBS Test WH", cls.company)
        cls._ensure_firearm_item("BBS-GUN")

    # -------------------------------------------------------- fixture helpers

    @staticmethod
    def _ensure_root_groups():
        for dt, root in [("Customer Group", "All Customer Groups"), ("Territory", "All Territories")]:
            if not frappe.db.exists(dt, root):
                field = f"{dt.lower().replace(' ', '_')}_name"
                frappe.get_doc({"doctype": dt, field: root, "is_group": 1}).insert(ignore_permissions=True)

    @classmethod
    def _ensure_company(cls):
        if not frappe.db.exists("Company", {"company_name": cls.COMPANY_NAME}):
            frappe.get_doc({
                "doctype": "Company", "company_name": cls.COMPANY_NAME, "abbr": "FT",
                "default_currency": "USD", "country": "United States",
            }).insert(ignore_permissions=True)

    @staticmethod
    def _ensure_uom(name):
        if not frappe.db.exists("UOM", name):
            frappe.get_doc({"doctype": "UOM", "uom_name": name, "must_be_whole_number": 1}).insert(ignore_permissions=True)

    @staticmethod
    def _ensure_item_group(name, is_group, parent):
        if frappe.db.exists("Item Group", name):
            return
        doc = frappe.get_doc({"doctype": "Item Group", "item_group_name": name, "is_group": is_group})
        if parent:
            doc.parent_item_group = parent
        doc.insert(ignore_permissions=True)

    @staticmethod
    def _ensure_caliber(name, category):
        if not frappe.db.exists("Caliber", name):
            frappe.get_doc({"doctype": "Caliber", "caliber_name": name, "category": category}).insert(ignore_permissions=True)

    @staticmethod
    def _ensure_brand(name):
        if not frappe.db.exists("Brand", name):
            frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)

    @staticmethod
    def _ensure_warehouse(name, company):
        existing = frappe.db.get_value("Warehouse", {"warehouse_name": name, "company": company})
        if existing:
            return existing
        return frappe.get_doc({
            "doctype": "Warehouse", "warehouse_name": name, "company": company,
        }).insert(ignore_permissions=True).name

    @staticmethod
    def _ensure_firearm_item(code):
        if frappe.db.exists("Item", code):
            return
        frappe.get_doc({
            "doctype": "Item", "item_code": code, "item_name": code,
            "item_group": "BBS Firearms", "stock_uom": "Nos",
            "is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
            "manufacturer": "BBS Mfg", "model_name": "Model B", "caliber": "9mm Luger",
            "firearm_type": "Pistol",
        }).insert(ignore_permissions=True)

    def _receive(self, serial, fb_id=None):
        """Material-Receipt one unit of BBS-GUN under `serial` into stock."""
        if frappe.db.exists("Serial No", serial):
            frappe.delete_doc("Serial No", serial, force=1, ignore_permissions=True)
        se = frappe.get_doc({
            "doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": self.company,
            "items": [{
                "item_code": "BBS-GUN", "qty": 1, "t_warehouse": self.warehouse,
                "basic_rate": 200, "use_serial_batch_fields": 1, "serial_no": serial,
            }],
        })
        se.flags.ignore_permissions = True
        se.insert()
        se.submit()
        if fb_id is not None:
            frappe.db.set_value("Serial No", serial, "fastbound_item_id", fb_id, update_modified=False)
        return se.name

    # -------------------------------------------------------- tests

    @staticmethod
    def _fake_fb(items):
        """Patch FastBoundClient.from_settings with a client whose bound book is `items`."""
        fake = MagicMock()
        fake.iter_items.return_value = [list(items)]  # one page, re-iterable
        return patch(
            "ffl_integrations.fastbound.client.FastBoundClient.from_settings",
            return_value=fake,
        )

    def test_issue_serial_removes_serial_from_stock(self):
        serial = "BBS-MI-1"
        self._receive(serial)
        self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")

        se_name = inventory_sync.issue_serial("BBS-GUN", serial, self.warehouse)

        self.assertTrue(se_name)
        self.assertNotEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")

    def test_dry_run_matches_by_fastbound_item_id_not_serial(self):
        """Match on the FastBound item id, not the serial string: an in-stock gun
        whose serial happens to equal a disposed serial — but a different FB item —
        must NOT be flagged for removal."""
        self._receive("BBS-DISP-1", fb_id="FBID-DISP-1")   # disposed by id → remove
        self._receive("DUP-SER", fb_id="FBID-KEEP")         # serial collides, id differs → keep
        fb_items = [
            {"id": "FBID-DISP-1", "serial": "BBS-DISP-1", "disposed": True, "deleted": False},
            {"id": "FBID-OTHER", "serial": "DUP-SER", "disposed": True, "deleted": False},
        ]
        with self._fake_fb(fb_items), patch(f"{_PATH}.issue_serial") as mi:
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=1)
        serials = {t["serial"] for t in res["would_remove"]}
        self.assertIn("BBS-DISP-1", serials)
        self.assertNotIn("DUP-SER", serials)
        mi.assert_not_called()

    def test_deleted_fastbound_item_is_not_removed(self):
        """A voided (deleted=true) FastBound record must never trigger a removal,
        even though it carries disposed=true."""
        self._receive("BBS-DEL-1", fb_id="FBID-DEL-1")
        fb_items = [{"id": "FBID-DEL-1", "serial": "BBS-DEL-1", "disposed": True, "deleted": True}]
        with self._fake_fb(fb_items), patch(f"{_PATH}.issue_serial") as mi:
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=1)
        self.assertEqual(res["would_remove"], [])
        mi.assert_not_called()

    def test_dry_run_flag_coercion(self):
        """dry_run accepts string inputs and never crashes on garbage (cint)."""
        with self._fake_fb([]), patch(f"{_PATH}.issue_serial") as mi:
            self.assertTrue(inventory_sync.sync_in_stock_from_boundbook(dry_run="1")["dry_run"])
            self.assertFalse(inventory_sync.sync_in_stock_from_boundbook(dry_run="nonsense")["dry_run"])
            mi.assert_not_called()

    def test_boundbook_mismatches_is_dry_run_only(self):
        """The read-only endpoint returns the dry-run shape and never removes
        stock, however it is called (no flag exists to turn writing on)."""
        self._receive("BBS-MM-1", fb_id="FBID-MM-1")
        fb_items = [{"id": "FBID-MM-1", "serial": "BBS-MM-1", "disposed": True, "deleted": False}]
        with self._fake_fb(fb_items), patch(f"{_PATH}.issue_serial") as mi:
            res = inventory_sync.boundbook_mismatches()
            dry = inventory_sync.sync_in_stock_from_boundbook(dry_run=1)
        mi.assert_not_called()
        self.assertEqual(
            set(res),
            {"dry_run", "fb_disposed_count", "would_remove", "would_remove_count",
             "consignment_held", "consignment_held_count"},
        )
        self.assertTrue(res["dry_run"])
        self.assertEqual([t["serial"] for t in res["would_remove"]], ["BBS-MM-1"])
        self.assertEqual(res, dry)
        self.assertEqual(frappe.db.get_value("Serial No", "BBS-MM-1", "status"), "Active")

    def test_boundbook_mismatches_takes_no_params_and_is_post_only(self):
        import inspect
        self.assertEqual(list(inspect.signature(inventory_sync.boundbook_mismatches).parameters), [])
        self.assertEqual(
            frappe.allowed_http_methods_for_whitelisted_func[inventory_sync.boundbook_mismatches], ["POST"])

    def _as_roles(self, roles):
        """Run as a non-Administrator (frappe.only_for lets Administrator through) holding `roles`."""
        from contextlib import contextmanager

        @contextmanager
        def cm():
            frappe.set_user("Guest")
            try:
                with patch("frappe.get_roles", return_value=roles):
                    yield
            finally:
                frappe.set_user("Administrator")
        return cm()

    def test_boundbook_mismatches_refuses_roles_outside_overview_roles(self):
        with self._as_roles(["Guest"]), patch(f"{_PATH}._fb_disposed_items") as fb:
            with self.assertRaises(frappe.PermissionError):
                inventory_sync.boundbook_mismatches()
            fb.assert_not_called()

    def test_boundbook_mismatches_allows_stock_and_accounts_user(self):
        """The AI service / cpa connector user holds Sales User + Stock User +
        Accounts User, no manager role."""
        for role in ("Stock User", "Accounts User"):
            with self.subTest(role=role), self._as_roles([role]), \
                    patch(f"{_PATH}._fb_disposed_items", return_value={}), \
                    patch(f"{_PATH}.in_stock_targets", return_value=[]), \
                    patch(f"{_PATH}.consignment_held_targets", return_value=[]):
                self.assertTrue(inventory_sync.boundbook_mismatches()["dry_run"])

    def test_sync_in_stock_stays_manager_only(self):
        with self._as_roles(["Stock User", "Accounts User"]), \
                patch(f"{_PATH}._fb_disposed_items") as fb:
            with self.assertRaises(frappe.PermissionError):
                inventory_sync.sync_in_stock_from_boundbook(dry_run=1)
            fb.assert_not_called()

    def test_real_run_removes_disposed_firearm(self):
        """End-to-end: a disposed-in-FB in-stock firearm is Material-Issued out.
        frappe.db.commit is patched so the removal stays in the test transaction."""
        self._receive("BBS-E2E-1", fb_id="FBID-E2E-1")
        fb_items = [{"id": "FBID-E2E-1", "serial": "BBS-E2E-1", "disposed": True, "deleted": False}]
        with self._fake_fb(fb_items), patch("frappe.db.commit"):
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=0)
        self.assertEqual(res["removed_count"], 1)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["removed"][0]["serial"], "BBS-E2E-1")
        self.assertNotEqual(frappe.db.get_value("Serial No", "BBS-E2E-1", "status"), "Active")

    def test_real_run_only_removes_approved_ids(self):
        """The operator's confirmation is authoritative: with item_ids supplied,
        only those FB item ids are removed even if more are disposed in FB."""
        self._receive("BBS-APPR-A", fb_id="IDA")
        self._receive("BBS-APPR-B", fb_id="IDB")
        fb_items = [
            {"id": "IDA", "serial": "BBS-APPR-A", "disposed": True, "deleted": False},
            {"id": "IDB", "serial": "BBS-APPR-B", "disposed": True, "deleted": False},
        ]
        with self._fake_fb(fb_items), patch("frappe.db.commit"):
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=0, item_ids=["IDA"])
        self.assertEqual(res["removed_count"], 1)
        self.assertEqual(res["removed"][0]["serial"], "BBS-APPR-A")
        self.assertNotEqual(frappe.db.get_value("Serial No", "BBS-APPR-A", "status"), "Active")
        self.assertEqual(frappe.db.get_value("Serial No", "BBS-APPR-B", "status"), "Active")

    def test_real_run_accepts_json_string_item_ids(self):
        """frappe.call sends list args as a JSON string — it must parse."""
        self._receive("BBS-JSON-A", fb_id="JIDA")
        fb_items = [{"id": "JIDA", "serial": "BBS-JSON-A", "disposed": True, "deleted": False}]
        with self._fake_fb(fb_items), patch("frappe.db.commit"):
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=0, item_ids='["JIDA"]')
        self.assertEqual(res["removed_count"], 1)

    def test_real_run_skips_approved_id_no_longer_disposed(self):
        """An approved id that FB no longer reports disposed is not removed."""
        self._receive("BBS-GONE", fb_id="GONE")
        with self._fake_fb([]), patch("frappe.db.commit"):  # FB reports nothing disposed now
            res = inventory_sync.sync_in_stock_from_boundbook(dry_run=0, item_ids=["GONE"])
        self.assertEqual(res["removed_count"], 0)
        self.assertEqual(frappe.db.get_value("Serial No", "BBS-GONE", "status"), "Active")
