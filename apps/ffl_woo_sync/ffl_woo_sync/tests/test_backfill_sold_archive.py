"""One-time SOLD-archive backfill & zombie audit.

The four product-lifecycle states observed in prod all pre-date the
SOLD-archive lifecycle, so once the lifecycle ships the existing rows must be triaged:
some serials should gain a SOLD page, some should stay drafted (and get a 301
from the storefront plugin's redirect engine), some carry a sentinel that points at nothing.

This suite covers the enumerate → classify → report side, which is READ-ONLY,
and the deliberately narrow apply helper (it can only act on an explicit list of
serial names a human copied out of the report — there is no "apply everything
the query found" path by design).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.maintenance import backfill_sold_archive as backfill


def _row(**kw):
    base = {"name": "SN-1", "status": "Active", "woo_product_id": "",
            "woo_delisted": 0, "woo_sold_archived": 0}
    base.update(kw)
    return frappe._dict(base)


# ===========================================================================
# classify — the pure triage rule
# ===========================================================================

class TestClassify(FrappeTestCase):
    def test_sold_and_listed_needs_an_archive(self):
        """The SP000279 shape: sold, still has a WC product, no sold meta yet."""
        row = _row(status="Delivered", woo_product_id="279", woo_delisted=1)
        self.assertEqual(backfill.classify(row), backfill.ARCHIVE)

    def test_sold_and_listed_but_never_delisted_also_needs_an_archive(self):
        """A sold gun the sweep never caught is equally in scope."""
        row = _row(status="Delivered", woo_product_id="500")
        self.assertEqual(backfill.classify(row), backfill.ARCHIVE)

    def test_already_archived_is_done(self):
        row = _row(status="Delivered", woo_product_id="279", woo_sold_archived=1)
        self.assertEqual(backfill.classify(row), backfill.ALREADY_ARCHIVED)

    def test_delisted_but_not_sold_stays_drafted(self):
        """Removed, not sold → the page stays draft and wants a 301,
        NOT a SOLD archive that would claim a sale that never happened."""
        row = _row(status="Active", woo_product_id="123", woo_delisted=1)
        self.assertEqual(backfill.classify(row), backfill.DELISTED_NOT_SOLD)

    def test_sentinel_without_a_product_is_stale(self):
        """delisted=1 with no product id: the flag points at nothing."""
        row = _row(status="Inactive", woo_product_id="", woo_delisted=1)
        self.assertEqual(backfill.classify(row), backfill.STALE_SENTINEL)

    def test_sold_but_never_web_listed_is_out_of_scope(self):
        """POS-only inventory (e.g. the M1 Garands) never had a page, so
        there is no URL to preserve and nothing to do."""
        row = _row(status="Delivered", woo_product_id="")
        self.assertEqual(backfill.classify(row), backfill.NEVER_LISTED)

# ===========================================================================
# enumerate_candidates — the read-only sweep
# ===========================================================================

class TestEnumerateCandidates(FrappeTestCase):
    def test_unions_the_delisted_and_sold_queries(self):
        """The backfill population is `delisted=1` OR `Delivered AND listed` —
        two disjoint-ish queries unioned, deduped by serial name."""
        delisted = [_row(name="SN-A", woo_product_id="1", woo_delisted=1)]
        sold = [_row(name="SN-B", status="Delivered", woo_product_id="2")]

        def _get_all(doctype, **kwargs):
            return delisted if (kwargs.get("filters") or {}).get("woo_delisted") else sold

        with patch("frappe.get_all", side_effect=_get_all):
            rows = backfill.enumerate_candidates()
        self.assertEqual({r.name for r in rows}, {"SN-A", "SN-B"})

    def test_a_serial_in_both_queries_appears_once(self):
        both = _row(name="SN-DUP", status="Delivered", woo_product_id="9", woo_delisted=1)
        with patch("frappe.get_all", return_value=[both]):
            rows = backfill.enumerate_candidates()
        self.assertEqual(len(rows), 1)

    def test_reads_nothing_but_serial_no(self):
        """A prod audit must not wander into other doctypes."""
        seen = []

        def _get_all(doctype, **kwargs):
            seen.append(doctype)
            return []

        with patch("frappe.get_all", side_effect=_get_all):
            backfill.enumerate_candidates()
        self.assertEqual(set(seen), {"Serial No"})


# ===========================================================================
# build_report — what the operator reads before anything is written
# ===========================================================================

class TestBuildReport(FrappeTestCase):
    def _rows(self):
        return [
            _row(name="SN-ARCH", status="Delivered", woo_product_id="279", woo_delisted=1),
            _row(name="SN-KEEP", status="Active", woo_product_id="123", woo_delisted=1),
            _row(name="SN-DONE", status="Delivered", woo_product_id="9", woo_sold_archived=1),
            _row(name="SN-STALE", status="Inactive", woo_product_id="", woo_delisted=1),
        ]

    def test_buckets_every_serial_by_name(self):
        with patch.object(backfill, "enumerate_candidates", return_value=self._rows()):
            report = backfill.build_report()
        self.assertEqual(report["buckets"][backfill.ARCHIVE], ["SN-ARCH"])
        self.assertEqual(report["buckets"][backfill.DELISTED_NOT_SOLD], ["SN-KEEP"])
        self.assertEqual(report["buckets"][backfill.ALREADY_ARCHIVED], ["SN-DONE"])
        self.assertEqual(report["buckets"][backfill.STALE_SENTINEL], ["SN-STALE"])

    def test_counts_match_the_bucket_contents(self):
        with patch.object(backfill, "enumerate_candidates", return_value=self._rows()):
            report = backfill.build_report()
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["counts"][backfill.ARCHIVE], 1)

    def test_every_bucket_is_present_even_when_empty(self):
        """A report that silently omits an empty bucket reads as 'not checked'."""
        with patch.object(backfill, "enumerate_candidates", return_value=[]):
            report = backfill.build_report()
        self.assertEqual(set(report["buckets"]), set(backfill.BUCKETS))
        self.assertEqual(report["total"], 0)

    def test_report_records_the_site_it_audited(self):
        with patch.object(backfill, "enumerate_candidates", return_value=[]):
            self.assertEqual(backfill.build_report(site="retail")["site"], "retail")

    def test_format_report_names_every_bucket_and_the_counts(self):
        with patch.object(backfill, "enumerate_candidates", return_value=self._rows()):
            text = backfill.format_report(backfill.build_report())
        for bucket in backfill.BUCKETS:
            self.assertIn(bucket, text)
        self.assertIn("SN-ARCH", text)


# ===========================================================================
# apply_archive — narrow by design
# ===========================================================================

class TestApplyArchive(FrappeTestCase):
    def test_archives_each_named_serial(self):
        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as mock_a:
            result = backfill.apply_archive(["SN-1", "SN-2"])
        self.assertEqual(mock_a.call_count, 2)
        self.assertEqual(result["archived"], ["SN-1", "SN-2"])

    def test_passes_no_price(self):
        """A backfill cannot know the realized figure, so it shows none
        rather than an inexact one."""
        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as mock_a:
            backfill.apply_archive(["SN-1"])
        self.assertNotIn("sold_price", mock_a.call_args.kwargs)

    def test_empty_list_writes_nothing(self):
        """There is deliberately no 'apply everything the query found' path: the
        caller must paste in serials read from the report."""
        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold") as mock_a:
            result = backfill.apply_archive([])
        mock_a.assert_not_called()
        self.assertEqual(result["archived"], [])

    def test_one_failure_does_not_abort_the_rest(self):
        def _archive(serial_no, **kwargs):
            if serial_no == "SN-BAD":
                raise RuntimeError("WC 404")
            return {"id": 1}

        with patch("ffl_woo_sync.woocommerce.sync.archive_serial_sold",
                   side_effect=_archive):
            result = backfill.apply_archive(["SN-BAD", "SN-OK"])
        self.assertEqual(result["archived"], ["SN-OK"])
        self.assertIn("SN-BAD", result["errors"][0])


# ===========================================================================
# Prod-isolation guard
# ===========================================================================

class TestNotWiredToMigrate(FrappeTestCase):
    def test_backfill_is_absent_from_patches_txt(self):
        """This module performs PROD WRITES against a live storefront. Listing it
        in patches.txt would make `bench migrate` run it unattended on every
        deploy — an unreviewed prod write. It must stay operator-invoked."""
        import os

        import ffl_woo_sync
        patches = os.path.join(os.path.dirname(ffl_woo_sync.__file__), "patches.txt")
        with open(patches) as fh:
            self.assertNotIn("backfill_sold_archive", fh.read())


# ===========================================================================
# PRODUCTION GAP — the backfill must find web-sold serials too
# ===========================================================================

class TestConsumedIsSoldInBackfill(FrappeTestCase):
    """This module carried its OWN copy of the sold-status constant, so the
    hotfix to sync.SOLD_STATUS would not have reached it.

    That mattered more here than anywhere else: the backfill is the tool
    that surfaces historically mis-routed serials for owner approval, and the
    population this bug created is *entirely* `Consumed`. A backfill still
    looking only for `Delivered` would have reported a short list, the owner
    would have approved it, and every web-sold gun would have stayed drafted —
    the audit designed to catch the problem would have certified it as clean.
    """

    def test_uses_syncs_constant_rather_than_a_private_copy(self):
        """Root cause of the drift: two sources of truth for one fact."""
        from ffl_woo_sync.woocommerce import sync
        self.assertIs(backfill._SOLD_STATUS, sync.SOLD_STATUS)

    def test_classifies_a_web_sold_listed_serial_as_archive(self):
        row = _row(name="348S", status="Consumed", woo_product_id="WC-348")
        self.assertEqual(backfill.classify(row), backfill.ARCHIVE)

    def test_classifies_a_web_sold_drafted_serial_as_archive(self):
        """The prod shape: routed to delist by the bug, so it carries
        delisted=1 AND a sold status."""
        row = _row(name="348S", status="Consumed", woo_product_id="WC-348",
                   woo_delisted=1)
        self.assertEqual(backfill.classify(row), backfill.ARCHIVE)

    def test_classifies_an_unlisted_web_sold_serial_as_never_listed(self):
        row = _row(name="348S", status="Consumed", woo_product_id="")
        self.assertEqual(backfill.classify(row), backfill.NEVER_LISTED)

    def test_enumeration_selects_every_sold_status(self):
        captured = []

        def _get_all(doctype, **kwargs):
            captured.append(kwargs.get("filters") or {})
            return []

        with patch("frappe.get_all", side_effect=_get_all):
            backfill.enumerate_candidates()
        sold_query = [f for f in captured if "status" in f]
        self.assertTrue(sold_query, "the sold-status query was not issued")
        self.assertEqual(sold_query[0]["status"], ["in", list(backfill._SOLD_STATUS)])
