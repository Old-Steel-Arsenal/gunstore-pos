"""Tests for maintenance/clear_ghost_serial_refs.py.

Post-incident cleanup: 126 ghost per-serial
WooCommerce products were trashed by lead.  The Serial No rows that had pushed
them still carry the now-dangling ``woo_product_id`` / ``woo_delisted`` — this
module clears those references.  Like backfill_sold_archive it is dry-run by
default, apply is explicit, and it is idempotent and operator-invoked (a prod
DB write that lead runs, never a deploy patch).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.maintenance import clear_ghost_serial_refs as ghost


# ===========================================================================
# The embedded incident set
# ===========================================================================

class TestIncidentSet(FrappeTestCase):
    def test_the_incident_set_is_126_unique_ids(self):
        ids = ghost.INCIDENT_GHOST_PRODUCT_IDS
        self.assertEqual(len(ids), 126)
        self.assertEqual(len(set(ids)), 126)

    def test_the_kept_product_2865_is_not_in_the_set(self):
        """The kept product (German P226 Grade B::U413254) is held for an in-flight order
        and was deliberately NOT trashed — it must never be scheduled to clear."""
        self.assertNotIn(2865, ghost.INCIDENT_GHOST_PRODUCT_IDS)
        self.assertNotIn("2865", {str(i) for i in ghost.INCIDENT_GHOST_PRODUCT_IDS})

    def test_u413254_is_the_excluded_serial(self):
        self.assertIn("U413254", ghost.EXCLUDED_SERIALS)


# ===========================================================================
# build_report — classification (READ-ONLY)
# ===========================================================================

class TestBuildReport(FrappeTestCase):
    def test_a_referencing_serial_is_scheduled_to_clear(self):
        with patch.object(ghost, "find_serials", return_value={"2553": ["SN-GHOST"]}), \
            patch.object(ghost, "audit_uniform_legacy", return_value=[]):
            report = ghost.build_report(product_ids=[2553])
        self.assertIn("SN-GHOST", report["will_clear"])
        self.assertEqual(report["not_found"], [])

    def test_an_excluded_serial_is_bucketed_apart_not_cleared(self):
        with patch.object(ghost, "find_serials", return_value={"2999": ["U413254"]}), \
            patch.object(ghost, "audit_uniform_legacy", return_value=[]):
            report = ghost.build_report(product_ids=[2999])
        self.assertIn("U413254", report["excluded"])
        self.assertNotIn("U413254", report["will_clear"])

    def test_an_id_with_no_referencing_serial_is_not_found(self):
        """Idempotency's read half: an already-cleared (or never-written-back) id
        has no Serial No pointing at it and lands in not_found, not an error."""
        with patch.object(ghost, "find_serials", return_value={}), \
            patch.object(ghost, "audit_uniform_legacy", return_value=[]):
            report = ghost.build_report(product_ids=[2553])
        self.assertEqual(report["not_found"], ["2553"])
        self.assertEqual(report["will_clear"], [])

    def test_report_defaults_to_the_incident_set(self):
        captured = {}

        def _fake_find(ids, site):
            captured["ids"] = list(ids)
            return {}

        with patch.object(ghost, "find_serials", side_effect=_fake_find), \
            patch.object(ghost, "audit_uniform_legacy", return_value=[]):
            ghost.build_report()
        self.assertEqual(len(captured["ids"]), 126)


# ===========================================================================
# apply_clear — the write (CONFIRMED PROD WRITE)
# ===========================================================================

class TestApplyClear(FrappeTestCase):
    def test_it_clears_product_id_and_delisted_on_a_referencing_serial(self):
        with patch.object(ghost, "find_serials", return_value={"2553": ["SN-GHOST"]}), \
            patch("frappe.db.set_value") as sv:
            result = ghost.apply_clear(product_ids=[2553])
        sv.assert_called_once_with(
            "Serial No", "SN-GHOST", {"woo_product_id": "", "woo_delisted": 0})
        self.assertEqual(result["cleared"], ["SN-GHOST"])

    def test_it_never_writes_an_excluded_serial(self):
        with patch.object(ghost, "find_serials", return_value={"2999": ["U413254"]}), \
            patch("frappe.db.set_value") as sv:
            result = ghost.apply_clear(product_ids=[2999])
        sv.assert_not_called()
        self.assertIn("U413254", result["skipped_excluded"])
        self.assertEqual(result["cleared"], [])

    def test_it_is_idempotent_when_nothing_references_the_ids(self):
        with patch.object(ghost, "find_serials", return_value={}), \
            patch("frappe.db.set_value") as sv:
            result = ghost.apply_clear(product_ids=[2553])
        sv.assert_not_called()
        self.assertEqual(result["cleared"], [])
        self.assertEqual(result["not_found_ids"], ["2553"])

    def test_it_clears_multiple_serials_across_ids(self):
        with patch.object(ghost, "find_serials",
                          return_value={"2553": ["SN-A"], "2561": ["SN-B", "SN-C"]}), \
            patch("frappe.db.set_value") as sv:
            result = ghost.apply_clear(product_ids=[2553, 2561])
        self.assertEqual(set(result["cleared"]), {"SN-A", "SN-B", "SN-C"})
        self.assertEqual(sv.call_count, 3)


# ===========================================================================
# find_serials — the lookup uses the SITE's product-id field
# ===========================================================================

class TestFindSerials(FrappeTestCase):
    def test_it_queries_by_the_retail_product_id_field(self):
        with patch("frappe.get_all", return_value=[
                {"name": "SN-A", "woo_product_id": "2553"}]) as ga:
            found = ghost.find_serials(["2553"], "retail")
        filters = ga.call_args.kwargs["filters"]
        self.assertIn("woo_product_id", filters)
        self.assertEqual(found, {"2553": ["SN-A"]})

    def test_it_groups_multiple_serials_under_the_same_id(self):
        with patch("frappe.get_all", return_value=[
                {"name": "SN-A", "woo_product_id": "2553"},
                {"name": "SN-B", "woo_product_id": "2553"}]):
            found = ghost.find_serials(["2553"], "retail")
        self.assertEqual(found, {"2553": ["SN-A", "SN-B"]})


# ===========================================================================
# audit_uniform_legacy — OUTPUT ONLY, never written
# ===========================================================================

class TestUniformLegacyAudit(FrappeTestCase):
    def test_it_lists_serials_with_products_under_uniform_firearms(self):
        def _ga(doctype, *a, **k):
            if doctype == "Item":
                return [{"name": "UNI-GUN", "is_firearm": 1, "item_group": "Pistol"}]
            if doctype == "Serial No":
                return [{"name": "SN-LEGACY", "item_code": "UNI-GUN",
                         "woo_product_id": "2865"}]
            return []

        with patch("frappe.get_all", side_effect=_ga):
            rows = ghost.audit_uniform_legacy("retail")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "SN-LEGACY")

    def test_the_audit_never_writes(self):
        """It is a report, not a fix — a human decides what to do with the
        surviving legacy per-serial products."""
        def _ga(doctype, *a, **k):
            if doctype == "Item":
                return [{"name": "UNI-GUN", "is_firearm": 1, "item_group": "Pistol"}]
            return [{"name": "SN-LEGACY", "item_code": "UNI-GUN",
                     "woo_product_id": "2865"}]

        with patch("frappe.get_all", side_effect=_ga), \
            patch("frappe.db.set_value") as sv:
            ghost.audit_uniform_legacy("retail")
        sv.assert_not_called()


# ===========================================================================
# Never wired into patches.txt (a prod write must stay operator-invoked)
# ===========================================================================

class TestNotWiredToMigrate(FrappeTestCase):
    def test_module_is_absent_from_patches_txt(self):
        import os

        import ffl_woo_sync
        patches = os.path.join(os.path.dirname(ffl_woo_sync.__file__), "patches.txt")
        with open(patches) as fh:
            self.assertNotIn("clear_ghost_serial_refs", fh.read())
