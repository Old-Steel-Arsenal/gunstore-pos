"""The by-hand cleanup for a store that never installs osa_dropship: it
must never drop a row a drop-ship store still needs."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from ffl_integrations.patches import drop_legacy_dropship_tables as cleanup


class DropLegacyDropshipTables(unittest.TestCase):
	def _run(self, *, installed=(), counts=None, apply=0):
		counts = counts if counts is not None else {t: 0 for t in cleanup.LEGACY_TABLES}
		ddl = []
		with patch("frappe.get_installed_apps", return_value=list(installed)), \
				patch("frappe.db.table_exists", side_effect=lambda t: t in counts), \
				patch("frappe.db.exists", return_value=False), \
				patch("frappe.db.count", side_effect=lambda t, *a, **k: 3 if t == "Singles" else counts[t]), \
				patch("frappe.db.sql_ddl", side_effect=ddl.append), \
				patch("frappe.db.delete") as delete, patch("frappe.db.commit"):
			out = cleanup.run(apply=apply)
		return out, ddl, delete

	def test_a_dry_run_reports_and_drops_nothing(self):
		out, ddl, delete = self._run()
		self.assertFalse(out["dropped"])
		self.assertEqual(out["singles"], 3)
		self.assertEqual(ddl, [])
		delete.assert_not_called()

	def test_apply_drops_every_empty_legacy_table_and_the_singles(self):
		out, ddl, delete = self._run(apply=1)
		self.assertTrue(out["dropped"])
		self.assertEqual(len(ddl), len(cleanup.LEGACY_TABLES))
		delete.assert_called_once_with("Singles", {"doctype": cleanup.LEGACY_SINGLE})

	def test_a_table_with_rows_is_refused(self):
		counts = {t: 0 for t in cleanup.LEGACY_TABLES} | {"Distributor Listing": 650}
		with self.assertRaises(frappe.ValidationError):
			self._run(counts=counts, apply=1)

	def test_seeded_pricing_rows_do_not_block_it(self):
		counts = {t: 0 for t in cleanup.LEGACY_TABLES} | {"Dropship Pricing Rule": 4, "Dropship Brand MAP Mode": 2}
		out, ddl, _delete = self._run(counts=counts, apply=1)
		self.assertTrue(out["dropped"])
		self.assertEqual(len(ddl), len(cleanup.LEGACY_TABLES))

	def test_a_store_with_the_extension_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(installed=("frappe", "osa_dropship"), apply=1)
