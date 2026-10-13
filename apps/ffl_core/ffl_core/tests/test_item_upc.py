"""Tests for the Item.upc <-> Item Barcode (UPC) sync hook."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.item_hooks import sync_upc_barcode


def _new_item(item_code: str, **kw):
	frappe.delete_doc_if_exists("Item", item_code)
	doc = frappe.get_doc({
		"doctype": "Item",
		"item_code": item_code,
		"item_name": kw.pop("item_name", item_code),
		"item_group": kw.pop("item_group", "All Item Groups"),
		"stock_uom": "Nos",
		"is_stock_item": 1,
		**kw,
	})
	return doc


class ItemUpcSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.set_user("Administrator")

	# All test codes below are valid UPC-A (correct check digit) — ERPNext's
	# Item.validate_barcode rejects malformed UPCs. Barcodes are globally
	# unique, so each test uses a distinct (repunit-style) valid code.
	def test_upc_set_creates_single_upc_barcode_row(self):
		doc = _new_item("TEST-UPC-1", upc="111111111117")
		doc.insert(ignore_permissions=True)
		upc_rows = [r for r in doc.barcodes if (r.barcode_type or "").upper() == "UPC"]
		self.assertEqual(len(upc_rows), 1)
		self.assertEqual(upc_rows[0].barcode, "111111111117")

	def test_empty_upc_backfills_from_existing_row(self):
		doc = _new_item("TEST-UPC-2")
		doc.append("barcodes", {"barcode": "222222222224", "barcode_type": "UPC"})
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.upc, "222222222224")

	def test_non_upc_rows_untouched(self):
		doc = _new_item("TEST-UPC-3", upc="333333333331")
		doc.append("barcodes", {"barcode": "4006381333931", "barcode_type": "EAN"})
		doc.insert(ignore_permissions=True)
		types = sorted((r.barcode_type or "").upper() for r in doc.barcodes)
		self.assertEqual(types, ["EAN", "UPC"])
		ean = [r for r in doc.barcodes if (r.barcode_type or "").upper() == "EAN"]
		self.assertEqual(ean[0].barcode, "4006381333931")

	def test_changing_upc_replaces_stale_row(self):
		doc = _new_item("TEST-UPC-4", upc="444444444448")
		doc.insert(ignore_permissions=True)
		doc.upc = "555555555555"
		doc.save(ignore_permissions=True)
		upc_rows = [r for r in doc.barcodes if (r.barcode_type or "").upper() == "UPC"]
		self.assertEqual(len(upc_rows), 1)
		self.assertEqual(upc_rows[0].barcode, "555555555555")

	def test_thirteen_digit_ean_in_upc_field_saves_as_ean(self):
		# A 13-digit GTIN is an EAN-13, not a 12-digit UPC-A. Entering it in the
		# single UPC field must still save — stored as an EAN barcode row, which
		# ERPNext accepts — not be rejected as "not a valid UPC".
		doc = _new_item("TEST-UPC-EAN13", upc="8605003812418")
		doc.insert(ignore_permissions=True)
		rows = [r for r in doc.barcodes if (r.barcode or "").strip() == "8605003812418"]
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].barcode_type or "").upper(), "EAN")
		self.assertEqual(doc.upc, "8605003812418")

	def test_thirteen_digit_upc_row_retyped_to_ean_before_validate(self):
		# Reproduces the reported error: a 13-digit code entered (via the RSR
		# quick-entry helper or the Barcodes grid) as barcode_type "UPC".
		# ERPNext's validate_barcode rejects it as "not a valid UPC" unless the
		# hook re-types it to EAN *before* that core validation runs.
		doc = _new_item("TEST-UPC-EAN13-ROW")
		doc.append("barcodes", {"barcode": "4006381339926", "barcode_type": "UPC"})
		doc.insert(ignore_permissions=True)  # must not raise InvalidBarcode
		rows = [r for r in doc.barcodes if (r.barcode or "").strip() == "4006381339926"]
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].barcode_type or "").upper(), "EAN")
		self.assertEqual(doc.upc, "4006381339926")  # reverse-filled for display

	def test_changing_upc_from_upc_to_ean_retypes_and_replaces_row(self):
		# Editing the field from a 12-digit UPC to a 13-digit EAN must drop the
		# stale UPC row and leave exactly one EAN row (no orphaned UPC).
		doc = _new_item("TEST-UPC-6", upc="888888888886")
		doc.insert(ignore_permissions=True)
		doc.upc = "9780201379624"
		doc.save(ignore_permissions=True)
		gtin = [r for r in doc.barcodes if (r.barcode_type or "").upper() in ("UPC", "EAN")]
		self.assertEqual(len(gtin), 1)
		self.assertEqual(gtin[0].barcode, "9780201379624")
		self.assertEqual((gtin[0].barcode_type or "").upper(), "EAN")

	def test_eight_digit_code_saved_as_ean8(self):
		# An 8-digit GTIN is an EAN-8 — typed by length so ERPNext accepts it.
		doc = _new_item("TEST-UPC-EAN8", upc="96385074")
		doc.insert(ignore_permissions=True)
		rows = [r for r in doc.barcodes if (r.barcode or "").strip() == "96385074"]
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].barcode_type or "").upper(), "EAN-8")

	def test_independent_ean_barcode_preserved_when_setting_upc(self):
		# An EAN barcode that does not equal the UPC field (e.g. a Receive Goods
		# scan) must survive when a separate UPC is entered in the field.
		doc = _new_item("TEST-UPC-7")
		doc.append("barcodes", {"barcode": "5012345678900", "barcode_type": "EAN"})
		doc.upc = "666666666662"
		doc.insert(ignore_permissions=True)
		codes = sorted((r.barcode or "") for r in doc.barcodes)
		self.assertEqual(codes, ["5012345678900", "666666666662"])

	def test_hook_is_idempotent(self):
		# Running the hook twice should not duplicate the UPC row.
		doc = _new_item("TEST-UPC-5", upc="777777777779")
		doc.insert(ignore_permissions=True)
		sync_upc_barcode(doc)
		sync_upc_barcode(doc)
		upc_rows = [r for r in doc.barcodes if (r.barcode_type or "").upper() == "UPC"]
		self.assertEqual(len(upc_rows), 1)
