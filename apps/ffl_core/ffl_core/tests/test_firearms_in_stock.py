"""Firearms In Stock report: in-stock firearms by serial, FastBound deep link,
acquisition enrichment, and status/warehouse/manufacturer filtering.

Builds Serial No rows directly and forces the stock columns (status/warehouse)
via db.set_value — the report reads those columns, so we don't need a full
stock-ledger lifecycle to exercise its query logic.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from ffl_core.ffl_core.report.firearms_in_stock.firearms_in_stock import execute


class FirearmsInStockReport(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls._ensure_root_groups()
		cls._ensure_company()
		cls._ensure_uom("Nos")
		cls._ensure_item_group("All Item Groups", is_group=1, parent=None)
		cls._ensure_item_group("Used Firearm", is_group=0, parent="All Item Groups")
		cls._ensure_item_group("FIS Optics", is_group=0, parent="All Item Groups")
		cls._ensure_caliber(".357 Magnum", "Pistol")
		cls._ensure_brand("FIS Mfg")
		cls._ensure_brand("FIS Unit Mfg")
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		cls.warehouse = cls._ensure_warehouse("FIS Test WH", cls.company)

		cls._ensure_firearm_item("FIS-GUN-1")
		cls._ensure_optic_item("FIS-OPTIC-1")

		# in-stock firearm, synced to FastBound
		cls._ensure_serial("FIS-SN-INSTOCK", "FIS-GUN-1", "Active", cls.warehouse, "9001")
		# in-stock firearm, not yet synced (no FastBound id)
		cls._ensure_serial("FIS-SN-NOFB", "FIS-GUN-1", "Active", cls.warehouse, "")
		# sold firearm — should not appear in the in-stock (Active) view
		cls._ensure_serial("FIS-SN-SOLD", "FIS-GUN-1", "Delivered", cls.warehouse, "9002")
		# non-firearm serialized item — should never appear
		cls._ensure_serial("FIS-SN-OPTIC", "FIS-OPTIC-1", "Active", cls.warehouse, "")
		# old/surplus gun under the shared SKU carrying its OWN maker / importer /
		# condition (the per-serial override the report must surface, not the Item's).
		cls._ensure_serial("FIS-SN-OVERRIDE", "FIS-GUN-1", "Active", cls.warehouse, "9003")
		frappe.db.set_value("Serial No", "FIS-SN-OVERRIDE", {
			"manufacturer": "FIS Unit Mfg", "importer": "Unit Importer", "condition_grade": "Good",
			"firearm_type": "Rifle",  # differs from Item's "Pistol" — tests COALESCE precedence
		}, update_modified=False)

		cls._ensure_acquisition("FIS-GUN-1", "FIS-SN-INSTOCK", "Jane Trade")

	# -------------------------------------------------------- helpers

	@staticmethod
	def _ensure_root_groups():
		for dt, root in [
			("Customer Group", "All Customer Groups"),
			("Territory", "All Territories"),
		]:
			if not frappe.db.exists(dt, root):
				field = f"{dt.lower().replace(' ', '_')}_name"
				frappe.get_doc({"doctype": dt, field: root, "is_group": 1}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_company():
		if frappe.db.exists("Company", {"company_name": "FFL Test"}):
			return
		frappe.get_doc({
			"doctype": "Company",
			"company_name": "FFL Test",
			"abbr": "FT",
			"default_currency": "USD",
			"country": "United States",
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
		doc = frappe.get_doc({
			"doctype": "Warehouse",
			"warehouse_name": name,
			"company": company,
		}).insert(ignore_permissions=True)
		return doc.name

	@classmethod
	def _ensure_firearm_item(cls, code):
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "Used Firearm",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
			"is_firearm": 1,
			"manufacturer": "FIS Mfg",
			"model_name": "Model X",
			"caliber": ".357 Magnum",
			"firearm_type": "Pistol",
		}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_optic_item(cls, code):
		if frappe.db.exists("Item", code):
			return
		frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "FIS Optics",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
			"is_firearm": 0,
		}).insert(ignore_permissions=True)

	@classmethod
	def _ensure_serial(cls, serial, item_code, status, warehouse, fb_id):
		if frappe.db.exists("Serial No", serial):
			frappe.delete_doc("Serial No", serial, force=1, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "Serial No",
			"serial_no": serial,
			"item_code": item_code,
			"company": cls.company,
			"fastbound_item_id": fb_id or None,
		}).insert(ignore_permissions=True)
		# Force the stock columns the report keys off — no SLE needed for a query test.
		frappe.db.set_value(
			"Serial No", doc.name,
			{"status": status, "warehouse": warehouse},
			update_modified=False,
		)

	@classmethod
	def _ensure_acquisition(cls, item_code, serial, source_name):
		acq = frappe.get_doc({
			"doctype": "FFL Acquisition",
			"acquisition_date": now_datetime(),
			"source_type": "Trade-in",
			"source_name": source_name,
			"source_id_type": "Driver License",
			"source_id_number": "DL-FIS-1",
			"company": cls.company,
			"item": item_code,
			"serial": serial,
			"cost": 300,
		})
		acq.insert(ignore_permissions=True)
		acq.submit()

	# -------------------------------------------------------- tests

	def test_columns_shape(self):
		columns, _ = execute({"status": "Active"})
		fieldnames = [c["fieldname"] for c in columns]
		self.assertEqual(len(columns), 13)
		self.assertEqual(fieldnames[0], "serial_no")
		self.assertIn("fastbound", fieldnames)
		self.assertIn("importer", fieldnames)
		self.assertIn("condition_grade", fieldnames)
		self.assertIn("firearm_type", fieldnames)
		self.assertIn("location", fieldnames)

	def test_per_serial_manufacturer_overrides_item(self):
		_, data = execute({"status": "Active"})
		by_serial = {r["serial_no"]: r for r in data}
		# Override serial reports ITS maker; a serial without an override falls
		# back to the model SKU's Item.manufacturer.
		self.assertEqual(by_serial["FIS-SN-OVERRIDE"]["manufacturer"], "FIS Unit Mfg")
		self.assertEqual(by_serial["FIS-SN-INSTOCK"]["manufacturer"], "FIS Mfg")

	def test_importer_and_condition_columns(self):
		_, data = execute({"status": "Active"})
		by_serial = {r["serial_no"]: r for r in data}
		self.assertEqual(by_serial["FIS-SN-OVERRIDE"]["importer"], "Unit Importer")
		self.assertEqual(by_serial["FIS-SN-OVERRIDE"]["condition_grade"], "Good")
		# Item has no importer and the serial sets none → empty (never "Unknown").
		self.assertFalse(by_serial["FIS-SN-INSTOCK"]["importer"])

	def test_manufacturer_filter_matches_per_serial(self):
		_, data = execute({"status": "Active", "manufacturer": "FIS Unit Mfg"})
		serials = {r["serial_no"] for r in data}
		self.assertIn("FIS-SN-OVERRIDE", serials)       # matched on its per-serial maker
		self.assertNotIn("FIS-SN-INSTOCK", serials)     # its maker is "FIS Mfg"

	def test_in_stock_firearms_only(self):
		_, data = execute({"status": "Active"})
		by_serial = {r["serial_no"]: r for r in data}
		self.assertIn("FIS-SN-INSTOCK", by_serial)
		self.assertIn("FIS-SN-NOFB", by_serial)
		# sold firearm and non-firearm optic must be excluded from the in-stock view
		self.assertNotIn("FIS-SN-SOLD", by_serial)
		self.assertNotIn("FIS-SN-OPTIC", by_serial)

	def test_item_columns_passthrough(self):
		_, data = execute({"status": "Active"})
		row = {r["serial_no"]: r for r in data}["FIS-SN-INSTOCK"]
		self.assertEqual(row["item_code"], "FIS-GUN-1")
		self.assertEqual(row["manufacturer"], "FIS Mfg")
		self.assertEqual(row["model_name"], "Model X")
		self.assertEqual(row["caliber"], ".357 Magnum")
		self.assertEqual(row["warehouse"], self.warehouse)

	def test_fastbound_link_rendered_when_synced(self):
		_, data = execute({"status": "Active"})
		by_serial = {r["serial_no"]: r for r in data}
		synced = by_serial["FIS-SN-INSTOCK"]["fastbound"]
		self.assertIn("<a", synced)
		self.assertIn("cloud.fastbound.com", synced)
		self.assertIn("Items/9001", synced)
		# unsynced serial gets a placeholder, not a link
		unsynced = by_serial["FIS-SN-NOFB"]["fastbound"]
		self.assertNotIn("<a", unsynced)

	def test_acquisition_enrichment(self):
		_, data = execute({"status": "Active"})
		row = {r["serial_no"]: r for r in data}["FIS-SN-INSTOCK"]
		self.assertEqual(row["source"], "Jane Trade")
		self.assertIsNotNone(row["acquired"])
		# a serial with no acquisition leaves the enrichment columns empty
		nofb = {r["serial_no"]: r for r in data}["FIS-SN-NOFB"]
		self.assertIsNone(nofb["source"])
		self.assertIsNone(nofb["acquired"])

	def test_status_filter_shows_sold(self):
		_, data = execute({"status": "Delivered"})
		serials = {r["serial_no"] for r in data}
		self.assertIn("FIS-SN-SOLD", serials)
		self.assertNotIn("FIS-SN-INSTOCK", serials)

	def test_warehouse_and_manufacturer_filters(self):
		_, in_wh = execute({"status": "Active", "warehouse": self.warehouse})
		self.assertIn("FIS-SN-INSTOCK", {r["serial_no"] for r in in_wh})

		_, other_wh = execute({"status": "Active", "warehouse": "Nonexistent WH - FT"})
		self.assertEqual(other_wh, [])

		_, by_mfg = execute({"status": "Active", "manufacturer": "FIS Mfg"})
		self.assertIn("FIS-SN-INSTOCK", {r["serial_no"] for r in by_mfg})

	def test_serial_inherits_manufacturer_but_never_importer(self):
		# A serial with no per-unit maker inherits manufacturer from the model Item
		# on save (the before_save hook). importer does NOT inherit (2026-08-20):
		# generic surplus buckets mix import batches under one SKU, so an
		# Item-level importer is never trustworthy as a per-gun fact — an importer
		# is stated per physical gun at receive, or stays blank.
		frappe.db.set_value("Item", "FIS-GUN-1", "importer", "Acme Importers")
		doc = frappe.get_doc({
			"doctype": "Serial No", "serial_no": "FIS-SN-INHERIT",
			"item_code": "FIS-GUN-1", "company": self.company,
		}).insert(ignore_permissions=True)
		self.assertEqual(doc.manufacturer, "FIS Mfg")        # inherited from Item
		self.assertFalse(doc.importer)                       # never inherited

	def test_report_importer_never_falls_back_to_item(self):
		# The Importer column shows only what the physical gun carries: a blank
		# per-serial importer stays blank even when the Item has one (2026-08-20 —
		# Item-level importer is no longer a per-gun default anywhere: not the
		# form, not the report, not the FastBound push).
		frappe.db.set_value("Item", "FIS-GUN-1", "importer", "Acme Importers")
		_, data = execute({"status": "Active"})
		by_serial = {r["serial_no"]: r for r in data}
		self.assertFalse(by_serial["FIS-SN-INSTOCK"]["importer"])

	def test_serial_keeps_its_own_manufacturer_override(self):
		# An explicit per-serial manufacturer is never overwritten by the inherit hook.
		doc = frappe.get_doc({
			"doctype": "Serial No", "serial_no": "FIS-SN-OWNMFG",
			"item_code": "FIS-GUN-1", "company": self.company,
			"manufacturer": "FIS Unit Mfg",
		}).insert(ignore_permissions=True)
		self.assertEqual(doc.manufacturer, "FIS Unit Mfg")

	def test_serial_inherits_model_caliber_from_item_when_blank(self):
		# model + caliber inherit from the model Item on save too, so the by-serial
		# form shows the gun's model/caliber even before a FastBound sync stamps them.
		doc = frappe.get_doc({
			"doctype": "Serial No", "serial_no": "FIS-SN-INHERIT-MC",
			"item_code": "FIS-GUN-1", "company": self.company,
		}).insert(ignore_permissions=True)
		self.assertEqual(doc.model_name, "Model X")          # inherited from Item
		self.assertEqual(doc.caliber, ".357 Magnum")         # inherited from Item

	def test_serial_keeps_its_own_model_override(self):
		# An explicit per-serial model is never overwritten by the inherit hook.
		doc = frappe.get_doc({
			"doctype": "Serial No", "serial_no": "FIS-SN-OWNMODEL",
			"item_code": "FIS-GUN-1", "company": self.company,
			"model_name": "Unit Model",
		}).insert(ignore_permissions=True)
		self.assertEqual(doc.model_name, "Unit Model")

	def test_firearm_type_coalesce_serial_overrides_item(self):
		# Per-serial firearm_type beats the Item-level value (COALESCE precedence).
		# FIS-SN-OVERRIDE has firearm_type="Rifle" stamped directly on the Serial No;
		# FIS-GUN-1 Item has firearm_type="Pistol". The report must show "Rifle" for
		# FIS-SN-OVERRIDE and fall back to "Pistol" for serials with no per-unit override.
		_, data = execute({"status": "Active"})
		by_serial = {r["serial_no"]: r for r in data}
		self.assertEqual(by_serial["FIS-SN-OVERRIDE"]["firearm_type"], "Rifle",
			"per-serial firearm_type must win over Item's 'Pistol'")
		self.assertEqual(by_serial["FIS-SN-INSTOCK"]["firearm_type"], "Pistol",
			"serial with no per-unit override must fall back to Item's firearm_type")


if __name__ == "__main__":
	unittest.main()
