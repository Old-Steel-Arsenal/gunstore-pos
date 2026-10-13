"""Spike 1: firearm Items require a serial number on every sales transaction line.

The compliance rule lives in `ffl_core.compliance.firearm.require_serial_for_firearm`,
wired via `doc_events` (see `ffl_core.hooks`) onto Sales Invoice / Sales Order /
Delivery Note / POS Invoice `validate`.

This module verifies the rule itself with a unit test. A separate integration
test is filed for Phase 1 once ERPNext setup wizard is run on the dev site
(Price List / Currency Exchange / GL accounts must exist for full SI lifecycle).

Run inside the bench container:
    bench --site dev.localhost run-tests --module ffl_core.tests.test_firearm_compliance
"""
from __future__ import annotations

from types import SimpleNamespace
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.firearm import require_serial_for_firearm


class FirearmSerialEnforcement(FrappeTestCase):
	"""Unit tests over the validator with synthetic Item records.

	Avoids ERPNext's full Sales Invoice / Sales Order setup chain (price list,
	currency exchange, GL accounts) — that's covered in Phase 1's integration test.
	"""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls._ensure_item_group("All Item Groups", is_group=1, parent=None)
		cls._ensure_item_group("Firearm", is_group=0, parent="All Item Groups")
		cls._ensure_item_group("Accessory", is_group=0, parent="All Item Groups")
		cls._ensure_uom("Nos")
		cls._ensure_item("FFL-TEST-GUN", item_group="Firearm", has_serial_no=1)
		cls._ensure_item("FFL-TEST-ACC", item_group="Accessory", has_serial_no=0)
		# Used Firearm pattern: has_serial_no=0, firearm_serial carries the
		# manufacturer serial directly on the Item.
		cls._ensure_used_firearm_item("FFL-TEST-USED-GUN", manufacturer_serial="USED-MFG-12345")

	@staticmethod
	def _ensure_item_group(name: str, is_group: int, parent: str | None) -> None:
		if frappe.db.exists("Item Group", name):
			return
		doc = frappe.get_doc({
			"doctype": "Item Group",
			"item_group_name": name,
			"is_group": is_group,
		})
		if parent:
			doc.parent_item_group = parent
		doc.insert(ignore_permissions=True)

	@staticmethod
	def _ensure_uom(name: str) -> None:
		if not frappe.db.exists("UOM", name):
			frappe.get_doc({"doctype": "UOM", "uom_name": name, "must_be_whole_number": 1}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_item(code: str, item_group: str, has_serial_no: int) -> None:
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": has_serial_no,
			"serial_no_series": f"{code}-.####" if has_serial_no else "",
		}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_used_firearm_item(code: str, manufacturer_serial: str) -> None:
		if frappe.db.exists("Item", code):
			# Refresh firearm_serial in case test left it dirty.
			frappe.db.set_value("Item", code, "firearm_serial", manufacturer_serial, update_modified=False)
			return
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "Firearm",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 0,
			"firearm_serial": manufacturer_serial,
		})
		doc.insert(ignore_permissions=True)

	@staticmethod
	def _make_doc(*lines: tuple[str, str], docstatus: int = 1) -> SimpleNamespace:
		"""Cheap stand-in for a Sales Invoice / Delivery Note; matches what the
		validator reads. docstatus defaults to 1 — the gate fires at submit
		(stock leaves at submit; drafts may legitimately hold serial-less
		firearm rows, e.g. a pickup before the cashier picks
		the physical gun)."""
		return SimpleNamespace(docstatus=docstatus, items=[
			SimpleNamespace(idx=i + 1, item_code=code, serial_no=serial)
			for i, (code, serial) in enumerate(lines)
		])

	# ------------------------------------------------------------------ tests

	def test_firearm_without_serial_is_rejected(self):
		doc = self._make_doc(("FFL-TEST-GUN", ""))
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_serial_for_firearm(doc)
		self.assertIn("serial number", str(ctx.exception).lower())

	def test_firearm_with_blank_whitespace_serial_is_rejected(self):
		doc = self._make_doc(("FFL-TEST-GUN", "   "))
		with self.assertRaises(frappe.ValidationError):
			require_serial_for_firearm(doc)

	def test_non_firearm_without_serial_is_allowed(self):
		doc = self._make_doc(("FFL-TEST-ACC", ""))
		require_serial_for_firearm(doc)  # must not raise

	def test_firearm_with_serial_passes(self):
		doc = self._make_doc(("FFL-TEST-GUN", "FFL-TEST-GUN-0001"))
		require_serial_for_firearm(doc)  # must not raise

	# The "Used Firearm pulls serial from Item.firearm_serial" tests were
	# removed alongside the Used Firearm path unification — every firearm now
	# requires a line-level serial_no via ERPNext's Serial No table.

	def test_mixed_lines_only_reject_firearm_without_serial(self):
		doc = self._make_doc(
			("FFL-TEST-ACC", ""),
			("FFL-TEST-GUN", ""),
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			require_serial_for_firearm(doc)
		# Error must point at the firearm row (idx 2), not the accessory row (idx 1).
		self.assertIn("row #2", str(ctx.exception))

	def test_empty_doc_is_allowed(self):
		doc = SimpleNamespace(docstatus=1, items=None)
		require_serial_for_firearm(doc)
		doc = SimpleNamespace(docstatus=1, items=[])
		require_serial_for_firearm(doc)

	def test_draft_with_serialless_firearm_is_allowed(self):
		"""Drafts park legitimately serial-less firearm rows (a pickup
		before the cashier picks the physical gun); the gate only
		matters when stock leaves — at submit."""
		doc = self._make_doc(("FFL-TEST-GUN", ""), docstatus=0)
		require_serial_for_firearm(doc)  # must not raise


if __name__ == "__main__":
	unittest.main()
