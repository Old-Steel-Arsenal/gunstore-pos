"""Backfill: seed Woo Online Order Serial rows from legacy line.serial."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestBackfillWooOrderSerials(FrappeTestCase):
	def _doc(self, lines, serials=None):
		doc = MagicMock()
		doc.lines = [frappe._dict(l) for l in lines]
		doc.serials = serials or []
		doc.get.side_effect = lambda k, d=None: {"serials": doc.serials}.get(k, d)
		def _append(table, row):
			getattr(doc, table).append(frappe._dict(row))
			return frappe._dict(row)
		doc.append.side_effect = _append
		return doc

	def test_seeds_unit0_confirmed_and_blanks_for_extra_units(self):
		from ffl_woo_sync.patches import backfill_woo_order_serials as patch_mod
		doc = self._doc([{"item_code": "GLOCK-19", "serial": "SN-1", "woo_line_id": "7", "qty": 2}])
		with patch("frappe.get_all", return_value=["WOO-1"]), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch.object(patch_mod, "is_regulated_firearm_item", return_value=True):
			patch_mod.execute()
		self.assertEqual(len(doc.serials), 2)
		self.assertEqual(doc.serials[0]["confirmed_serial"], "SN-1")
		self.assertEqual(doc.serials[0]["unit_index"], 0)
		self.assertEqual(doc.serials[1]["confirmed_serial"], "")
		self.assertEqual(doc.serials[1]["unit_index"], 1)
		doc.save.assert_called_once()

	def test_skips_orders_already_having_serial_rows(self):
		from ffl_woo_sync.patches import backfill_woo_order_serials as patch_mod
		doc = self._doc(
			[{"item_code": "GLOCK-19", "serial": "SN-1", "woo_line_id": "7", "qty": 1}],
			serials=[frappe._dict({"item_code": "GLOCK-19"})],
		)
		with patch("frappe.get_all", return_value=["WOO-1"]), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch.object(patch_mod, "is_regulated_firearm_item", return_value=True):
			patch_mod.execute()
		doc.save.assert_not_called()

	def test_ignores_non_firearm_lines(self):
		from ffl_woo_sync.patches import backfill_woo_order_serials as patch_mod
		doc = self._doc([{"item_code": "AMMO", "serial": "", "woo_line_id": "3", "qty": 2}])
		with patch("frappe.get_all", return_value=["WOO-1"]), \
			 patch("frappe.get_doc", return_value=doc), \
			 patch.object(patch_mod, "is_regulated_firearm_item", return_value=False):
			patch_mod.execute()
		self.assertEqual(doc.serials, [])
		doc.save.assert_not_called()
