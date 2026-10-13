"""Phase 1 integration test: full Used Firearm acquisition → disposition cycle.

Skips the ERPNext Sales Invoice lifecycle (still flaky on a dev site without
the full Setup Wizard run) and exercises our domain doctypes directly. SI/SO
end-to-end coverage lives in `test_listing_status_sync.py` (sync helpers) and
will be promoted to a real SI test once Phase 1's setup_dev_site bootstrap
is wired into CI.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, nowdate


class FFLAcquisitionDispositionCycle(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls._ensure_root_groups()
		cls._ensure_company()
		cls._ensure_uom("Nos")
		cls._ensure_item_group("All Item Groups", is_group=1, parent=None)
		cls._ensure_item_group("Used Firearm", is_group=0, parent="All Item Groups")
		cls._ensure_caliber(".357 Magnum", "Pistol")
		cls._ensure_item(
			code="UF-INT-TEST-1",
			item_group="Used Firearm",
			serial="UF-INT-TEST-1-SN",
			caliber=".357 Magnum",
		)
		cls._ensure_customer("FFL-INT-TEST-CUSTOMER")
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

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
		if not frappe.db.exists("Customer Group", "Individual"):
			frappe.get_doc({
				"doctype": "Customer Group",
				"customer_group_name": "Individual",
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Territory", "United States"):
			frappe.get_doc({
				"doctype": "Territory",
				"territory_name": "United States",
				"parent_territory": "All Territories",
				"is_group": 0,
			}).insert(ignore_permissions=True)

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
		if frappe.db.exists("Caliber", name):
			return
		frappe.get_doc({"doctype": "Caliber", "caliber_name": name, "category": category}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_item(code, item_group, serial, caliber=None):
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": item_group,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_serial_no": 1,
				"serial_no_series": f"{code}-.####",
				"caliber": caliber,
				"firearm_type": "Pistol",
				"firearm_serial": serial,
			}).insert(ignore_permissions=True)
		# All firearms are has_serial_no=1 now (the legacy Used Firearm path is
		# unified — see patches/unify_used_firearm_serial.py). FFL Disposition
		# auto-picks the single in-stock Serial No, so the test fixture needs
		# the matching Serial No row to mirror that behaviour.
		if serial and not frappe.db.exists("Serial No", serial):
			frappe.get_doc({
				"doctype": "Serial No",
				"serial_no": serial,
				"item_code": code,
				"status": "Active",
			}).insert(ignore_permissions=True)

	@staticmethod
	def _ensure_customer(name):
		if frappe.db.exists("Customer", name):
			return
		frappe.get_doc({
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Individual",
			"customer_group": "Individual",
			"territory": "United States",
		}).insert(ignore_permissions=True)

	# -------------------------------------------------------- tests

	def test_caliber_doctype_basic_crud(self):
		self.assertTrue(frappe.db.exists("Caliber", ".357 Magnum"))
		c = frappe.get_doc("Caliber", ".357 Magnum")
		self.assertEqual(c.category, "Pistol")

	def test_item_has_firearm_custom_fields(self):
		# Custom fields should be present on the meta even before any value set.
		meta = frappe.get_meta("Item")
		fields = {f.fieldname for f in meta.fields}
		expected = {
			"manufacturer", "model_name", "caliber", "firearm_type",
			"barrel_length", "finish", "year_made", "condition_grade",
			"dealer_price", "listing_status", "image_gallery",
		}
		missing = expected - fields
		self.assertFalse(missing, f"missing custom fields on Item: {missing}")

	def test_customer_has_ffl_fields(self):
		meta = frappe.get_meta("Customer")
		fields = {f.fieldname for f in meta.fields}
		expected = {
			"is_ffl_dealer", "ffl_number", "ffl_license_type",
			"ffl_document", "ffl_expiry_date", "ez_check_status",
			"ez_check_last_run",
		}
		missing = expected - fields
		self.assertFalse(missing, f"missing custom fields on Customer: {missing}")

	def test_acquisition_individual_requires_id(self):
		acq = frappe.get_doc({
			"doctype": "FFL Acquisition",
			"acquisition_date": now_datetime(),
			"source_type": "Individual",
			"source_name": "John Doe",
			"company": self.company,
			"item": "UF-INT-TEST-1",
			"cost": 350,
		})
		with self.assertRaises(frappe.ValidationError) as ctx:
			acq.insert(ignore_permissions=True)
		self.assertIn("ID", str(ctx.exception))

	def test_acquisition_dealer_requires_ffl(self):
		acq = frappe.get_doc({
			"doctype": "FFL Acquisition",
			"acquisition_date": now_datetime(),
			"source_type": "FFL Dealer",
			"source_name": "Acme Distributors",
			"company": self.company,
			"item": "UF-INT-TEST-1",
			"cost": 250,
		})
		with self.assertRaises(frappe.ValidationError):
			acq.insert(ignore_permissions=True)

	def test_acquisition_full_individual_path_succeeds(self):
		acq = frappe.get_doc({
			"doctype": "FFL Acquisition",
			"acquisition_date": now_datetime(),
			"source_type": "Trade-in",
			"source_name": "Jane Buyer",
			"source_id_type": "Driver License",
			"source_id_number": "DL-NY-9999",
			"company": self.company,
			"item": "UF-INT-TEST-1",
			"cost": 300,
		})
		acq.insert(ignore_permissions=True)
		self.assertTrue(acq.name.startswith("ACQ-"))
		# Serial must auto-fetch from Item.serial_no
		self.assertEqual(acq.serial, "UF-INT-TEST-1-SN")
		acq.submit()
		self.assertEqual(acq.docstatus, 1)

	def test_disposition_retail_sale_requires_4473(self):
		# Retail Sale dispositions arrive from the FastBound 4473 webhook and
		# carry a fastbound_id; the gate fires at submit so the bound-book
		# record can't go A&D-final without 4473. Use a synthetic fastbound_id
		# to bypass the "no manual Retail Sale" reject and exercise the gate.
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "Retail Sale",
			"customer": "FFL-INT-TEST-CUSTOMER",
			"company": self.company,
			"item": "UF-INT-TEST-1",
			"form_4473_status": "Pending",
			"nics_status": "Proceed",
			"fastbound_id": "FB-TEST-4473-1",
		})
		disp.insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			disp.submit()
		self.assertIn("4473", str(ctx.exception))

	def test_disposition_retail_sale_requires_nics_proceed(self):
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "Retail Sale",
			"customer": "FFL-INT-TEST-CUSTOMER",
			"company": self.company,
			"item": "UF-INT-TEST-1",
			"form_4473_status": "Completed",
			"nics_status": "Delayed",
			"fastbound_id": "FB-TEST-4473-2",
		})
		disp.insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			disp.submit()
		self.assertIn("NICS", str(ctx.exception))

	def test_disposition_retail_sale_full_path_succeeds(self):
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "Retail Sale",
			"customer": "FFL-INT-TEST-CUSTOMER",
			"company": self.company,
			"item": "UF-INT-TEST-1",
			"form_4473_status": "Completed",
			"nics_status": "Proceed",
			"fastbound_id": "FB-TEST-4473-3",
		})
		disp.insert(ignore_permissions=True)
		self.assertTrue(disp.name.startswith("DISP-"))
		self.assertEqual(disp.serial, "UF-INT-TEST-1-SN")
		disp.submit()
		self.assertEqual(disp.docstatus, 1)

	def test_disposition_ffl_transfer_requires_destination_ffl(self):
		# Draft saves are intentionally allowed (manager may stage the
		# transfer while looking up the receiving dealer); the gate fires at
		# submit. Mirrors _require_witnesses_for_destruction.
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "FFL Transfer",
			"customer": "FFL-INT-TEST-CUSTOMER",
			"company": self.company,
			"item": "UF-INT-TEST-1",
		})
		disp.insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as ctx:
			disp.submit()
		self.assertIn("destination ffl", str(ctx.exception).lower())


	def test_disposition_dealer_transfer_issues_stock_on_submit(self):
		"""Submit a Dealer Transfer → ERPNext should auto-Material-Issue the
		firearm out of inventory so Serial No.status flips away from Active and
		the firearm disappears from the in-stock pickers immediately. Cancel
		should reverse it.

		Need a Serial No that's actually in a warehouse for the Material Issue
		to have something to consume — set one up just for this test."""
		# Find any active warehouse on the test company; receive a stock-entry
		# to land the test serial in it.
		warehouse = frappe.get_all(
			"Warehouse",
			filters={"company": self.company, "is_group": 0, "disabled": 0},
			pluck="name", limit=1,
		)[0]
		# Use a fresh Item code so we don't trip over the shared UF-INT-TEST-1
		# serial that other tests already exercise.
		from ffl_core.firearm import used_firearm_item_doc
		if not frappe.db.exists("Item", "UF-STOCK-ISSUE-TEST"):
			frappe.get_doc(used_firearm_item_doc(
				item_code="UF-STOCK-ISSUE-TEST",
				item_name="UF Stock Issue Test",
				description="UF stock-issue test",
				caliber=".357 Magnum",
				firearm_type="Pistol",
			)).insert(ignore_permissions=True)
		if not frappe.db.exists("Serial No", "STK-ISSUE-001"):
			se = frappe.get_doc({
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{
					"item_code": "UF-STOCK-ISSUE-TEST",
					"qty": 1,
					"t_warehouse": warehouse,
					"basic_rate": 100,
					"use_serial_batch_fields": 1,
					"serial_no": "STK-ISSUE-001",
				}],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()

		# Pre-condition: serial is on hand.
		self.assertEqual(
			frappe.db.get_value("Serial No", "STK-ISSUE-001", "status"),
			"Active",
		)

		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "Dealer Transfer",
			"destination_ffl": frappe.db.get_value(
				"ATF FFL Record", {}, "name") or "TEST-FFL-RECORD",
			"company": self.company,
			"item": "UF-STOCK-ISSUE-TEST",
			"serial": "STK-ISSUE-001",
		})
		# If no ATF FFL Record exists in the test DB, create a stub so the
		# Link field validates.
		if not frappe.db.exists("ATF FFL Record", disp.destination_ffl):
			frappe.get_doc({
				"doctype": "ATF FFL Record",
				"ffl_number": disp.destination_ffl,
				"loaded_from": "test-fixture",
			}).insert(ignore_permissions=True)
		disp.insert(ignore_permissions=True)
		disp.submit()
		disp.reload()

		# Post-condition: Material Issue was created + linked, serial is no
		# longer Active.
		self.assertTrue(disp.stock_entry, "stock_entry link missing on submitted disposition")
		self.assertEqual(
			frappe.db.get_value("Stock Entry", disp.stock_entry, "docstatus"), 1,
		)
		self.assertNotEqual(
			frappe.db.get_value("Serial No", "STK-ISSUE-001", "status"),
			"Active",
		)

		# Cancel reverses it: Stock Entry cancelled, serial back to Active.
		disp.cancel()
		disp.reload()
		self.assertFalse((disp.stock_entry or "").strip())
		self.assertEqual(
			frappe.db.get_value("Serial No", "STK-ISSUE-001", "status"),
			"Active",
		)


class ReacquisitionDedupe(FrappeTestCase):
	"""record_firearm_acquisition's dedupe must be OWNERSHIP-CYCLE aware.

	The 2026-08-21 JV-return incident: three rifles sold on 07-28 came back on
	08-19, and the receive hook created NO acquisitions — the (item, serial,
	docstatus=1) dedupe matched the PREVIOUS cycle's record and silently
	returned it. A dealer/consignment return is a REacquisition and must book
	fresh; only an OPEN cycle (no disposition since) may collapse to the
	existing record.

	Reuses FFLAcquisitionDispositionCycle's static fixture ensures WITHOUT
	inheriting the class — inheriting a test-bearing TestCase re-runs all of
	its cases under this class's name."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		base = FFLAcquisitionDispositionCycle
		base._ensure_root_groups()
		base._ensure_company()
		base._ensure_uom("Nos")
		base._ensure_item_group("All Item Groups", is_group=1, parent=None)
		base._ensure_item_group("Used Firearm", is_group=0, parent="All Item Groups")
		base._ensure_caliber(".357 Magnum", "Pistol")
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

	def _stocked_firearm(self, code: str, serial: str) -> None:
		from ffl_core.firearm import used_firearm_item_doc
		if not frappe.db.exists("Item", code):
			frappe.get_doc(used_firearm_item_doc(
				item_code=code, item_name=code, description=code,
				caliber=".357 Magnum", firearm_type="Pistol",
			)).insert(ignore_permissions=True)
		if not frappe.db.exists("Serial No", serial):
			warehouse = frappe.get_all(
				"Warehouse",
				filters={"company": self.company, "is_group": 0, "disabled": 0},
				pluck="name", limit=1,
			)[0]
			se = frappe.get_doc({
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [{
					"item_code": code, "qty": 1, "t_warehouse": warehouse,
					"basic_rate": 100, "use_serial_batch_fields": 1,
					"serial_no": serial,
				}],
			})
			se.flags.ignore_permissions = True
			se.insert()
			se.submit()

	def _submitted_dealer_transfer(self, code: str, serial: str):
		dest = frappe.db.get_value("ATF FFL Record", {}, "name") or "TEST-FFL-RECORD"
		if not frappe.db.exists("ATF FFL Record", dest):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": dest,
				"loaded_from": "test-fixture",
			}).insert(ignore_permissions=True)
		disp = frappe.get_doc({
			"doctype": "FFL Disposition",
			"disposition_date": now_datetime(),
			"disposition_type": "Dealer Transfer",
			"destination_ffl": dest,
			"company": self.company,
			"item": code,
			"serial": serial,
		})
		disp.insert(ignore_permissions=True)
		disp.submit()
		return disp

	def _acquire(self, code: str, serial: str) -> str:
		from ffl_core.acquisition import record_firearm_acquisition
		return record_firearm_acquisition(
			item_code=code, serial=serial, source_type="FFL Dealer",
			source_name="EXAMPLE SUPPLY LLC", source_ffl="9-99-999-99-9X-99999",
			cost=100, company=self.company,
		)

	def test_open_cycle_still_dedupes(self):
		# Same receive processed twice (hook replay / API retry) must not book
		# the gun twice — the un-disposed cycle collapses to the existing record.
		code, serial = "UF-REACQ-OPEN", "REACQ-OPEN-1"
		self._stocked_firearm(code, serial)
		first = self._acquire(code, serial)
		self.assertEqual(self._acquire(code, serial), first)

	def test_reacquisition_after_disposition_books_fresh(self):
		# Sold and returned: the old cycle is CLOSED by the disposition, so the
		# return's receive must create a brand-new acquisition record.
		code, serial = "UF-REACQ-RET", "REACQ-RET-1"
		self._stocked_firearm(code, serial)
		first = self._acquire(code, serial)
		self._submitted_dealer_transfer(code, serial)
		second = self._acquire(code, serial)
		self.assertNotEqual(second, first)
		self.assertEqual(frappe.db.get_value("FFL Acquisition", second, "docstatus"), 1)

	def test_cancelled_disposition_keeps_cycle_open(self):
		# A cancelled disposition means the gun never actually left — the cycle
		# stays open and the dedupe must still collapse to the existing record.
		code, serial = "UF-REACQ-CXL", "REACQ-CXL-1"
		self._stocked_firearm(code, serial)
		first = self._acquire(code, serial)
		disp = self._submitted_dealer_transfer(code, serial)
		disp.cancel()
		self.assertEqual(self._acquire(code, serial), first)

	def test_replay_after_return_dedupes_to_new_cycle(self):
		# The date half of the cycle test: after acquire → dispose → REacquire,
		# a replayed call (hook + API both firing for the same receive) must
		# collapse to the NEW cycle's record — not book a third. This is the
		# only scenario where `disposition_date >= acquisition_date` bites:
		# drop that condition and every returned gun's replay double-books.
		code, serial = "UF-REACQ-RPL", "REACQ-RPL-1"
		self._stocked_firearm(code, serial)
		first = self._acquire(code, serial)
		self._submitted_dealer_transfer(code, serial)
		second = self._acquire(code, serial)
		self.assertNotEqual(second, first)
		self.assertEqual(self._acquire(code, serial), second)  # replay → same

	def test_disposition_under_other_item_still_closes_cycle(self):
		# The serial-only half of the design: a Serial No is
		# unique by name, so a disposition recorded under a DIFFERENT item —
		# the gun was reassigned between Items (the Mosin two-Items pattern) —
		# is still this physical gun leaving, and it closes the cycle.
		from ffl_core.firearm import used_firearm_item_doc
		code_a, code_b, serial = "UF-REACQ-XI-A", "UF-REACQ-XI-B", "REACQ-XI-1"
		for code in (code_a, code_b):
			if not frappe.db.exists("Item", code):
				frappe.get_doc(used_firearm_item_doc(
					item_code=code, item_name=code, description=code,
					caliber=".357 Magnum", firearm_type="Pistol",
				)).insert(ignore_permissions=True)
		# Bare Serial No (no stock): the dispose hook skips the Material Issue
		# for a serial with no warehouse, which keeps this test about the
		# dedupe, not the stock ledger.
		frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
			"item_code": code_a, "company": self.company}).insert(ignore_permissions=True)
		first = self._acquire(code_a, serial)
		# Reassign to the other Item (what reassign_serial_to_item effects),
		# then dispose under it.
		frappe.db.set_value("Serial No", serial, "item_code", code_b,
			update_modified=False)
		self._submitted_dealer_transfer(code_b, serial)
		second = self._acquire(code_a, serial)
		self.assertNotEqual(second, first)


if __name__ == "__main__":
	unittest.main()
