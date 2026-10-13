"""Firearm Intake API tests.

Covers lookup_serial + atomic create_intake. Uses unique serials per test to
keep ahead of the auto-commit chain in Stock Entry / Stock Ledger.
"""
from __future__ import annotations

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import firearm_intake


class FirearmIntake(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", company)

	def _payload(self, suffix: str, **overrides) -> dict:
		base = {
			"serial": f"INTAKE-{suffix}",
			"source_type": "Trade-in",
			"source_name": "Jane Doe",
			"source_id_type": "Driver License",
			"source_id_number": f"DL-{suffix}",
			"manufacturer": "Smith & Wesson",
			"model_name": "Model 19",
			"caliber": ".357 Magnum",
			"firearm_type": "Pistol",
			"acquisition_cost": 350,
			"retail_price": 600,
			"dealer_price": 480,
			"condition_grade": "Excellent",
		}
		base.update(overrides)
		return base

	# --------------------------------------------------------- lookup

	def test_lookup_unknown_serial(self):
		result = firearm_intake.lookup_serial("does-not-exist-zz")
		self.assertFalse(result["found"])

	def test_lookup_blank_serial(self):
		result = firearm_intake.lookup_serial("")
		self.assertFalse(result["found"])

	# --------------------------------------------------------- create

	def test_create_intake_round_trip(self):
		result = firearm_intake.create_intake(self._payload(self._testMethodName))
		self.assertTrue(result["ok"])
		self.assertTrue(result["item_code"].startswith("UF-"))
		# Item exists, listed for retail.
		item = frappe.get_doc("Item", result["item_code"])
		self.assertEqual(item.listing_status, "Listed")
		# The retail price stays a reference on the Item, and a per-gun priced firearm carries no Item Price
		# (item_hooks.sync_standard_selling_price; ERPNext's after_insert would have made one).
		self.assertEqual(float(item.standard_rate), 600.0)
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": result["item_code"]}))
		# The intake Stock Entry materialises the Serial No record — canonical
		# now that the Used Firearm / Item.firearm_serial path is unified.
		expected_serial = f"INTAKE-{self._testMethodName}"
		self.assertTrue(
			frappe.db.exists("Serial No", expected_serial),
			f"Expected Serial No {expected_serial} to be created by the intake Stock Entry",
		)
		self.assertEqual(
			frappe.db.get_value("Serial No", expected_serial, "item_code"),
			result["item_code"],
		)
		# The gun's model-level facts land on the Serial No itself. ERPNext
		# bulk-inserts the serial past the inherit_serial_firearm_attrs hook, so
		# _stamp_serial must write these explicitly — without it the per-gun
		# record (bound book source of truth) shows a blank model/caliber.
		sn = frappe.db.get_value("Serial No", expected_serial,
			["manufacturer", "model_name", "caliber"], as_dict=True)
		self.assertEqual(sn.manufacturer, "Smith & Wesson")
		self.assertEqual(sn.model_name, "Model 19")
		self.assertEqual(sn.caliber, ".357 Magnum")
		# Acquisition exists and is submitted
		acq = frappe.get_doc("FFL Acquisition", result["acquisition"])
		self.assertEqual(acq.docstatus, 1)
		self.assertEqual(acq.serial, expected_serial)
		self.assertEqual(acq.item, result["item_code"])

	def test_create_intake_rejects_missing_required(self):
		bad = self._payload(self._testMethodName)
		del bad["manufacturer"]
		with self.assertRaises(frappe.ValidationError) as ctx:
			firearm_intake.create_intake(bad)
		self.assertIn("manufacturer", str(ctx.exception).lower())

	def test_create_intake_individual_requires_id(self):
		bad = self._payload(self._testMethodName, source_id_type="", source_id_number="")
		with self.assertRaises(frappe.ValidationError) as ctx:
			firearm_intake.create_intake(bad)
		self.assertIn("ID", str(ctx.exception))

	def test_create_intake_dealer_requires_ffl(self):
		bad = self._payload(
			self._testMethodName,
			source_type="FFL Dealer",
			source_id_type=None,
			source_id_number=None,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			firearm_intake.create_intake(bad)
		self.assertIn("FFL", str(ctx.exception))

	def test_create_intake_rejects_duplicate_serial(self):
		first = firearm_intake.create_intake(self._payload(self._testMethodName))
		self.assertTrue(first["ok"])
		# Second time with same serial — must be rejected before any creation.
		with self.assertRaises(frappe.ValidationError) as ctx:
			firearm_intake.create_intake(self._payload(self._testMethodName))
		self.assertIn("already on file", str(ctx.exception))

	# ----------------------------------------- Phase 2: shared model SKU intake

	def test_intake_with_item_code_uses_that_sku(self):
		"""When the operator supplies item_code (a shared model SKU), the gun is
		filed under THAT Item — not an auto-minted UF- code."""
		suffix = self._testMethodName
		sku = f"SHARED-{suffix}"
		res = firearm_intake.create_intake(self._payload(suffix, item_code=sku))
		self.assertTrue(res["ok"])
		self.assertEqual(res["item_code"], sku)
		item = frappe.get_doc("Item", sku)
		self.assertTrue(item.is_firearm)
		self.assertEqual(int(item.has_serial_no), 1)

	def test_intake_reuses_existing_shared_sku_for_second_gun(self):
		"""Two guns into the same shared SKU → one Item, two Serial Nos."""
		suffix = self._testMethodName
		sku = f"SHARED-{suffix}"
		s1, s2 = f"S1-{suffix}", f"S2-{suffix}"
		firearm_intake.create_intake(self._payload(
			suffix, item_code=sku, serial=s1, source_id_number=f"A-{suffix}"))
		firearm_intake.create_intake(self._payload(
			suffix, item_code=sku, serial=s2, source_id_number=f"B-{suffix}"))
		serials = frappe.get_all("Serial No", filters={"item_code": sku}, pluck="name")
		self.assertCountEqual(serials, [s1, s2])

	def test_intake_shared_sku_stamps_per_serial_data(self):
		"""Per-gun condition / sell price / year / description land on the Serial
		No, not the shared Item.

		year_made rides the shared serial_firearm_attr_updates helper, so intake
		gained per-gun years when Receive Goods did (2026-07-24) — two trade-ins
		of one model can be decades apart and each keeps its own."""
		suffix = self._testMethodName
		sku = f"SHARED-{suffix}"
		serial = f"SN-{suffix}"
		firearm_intake.create_intake(self._payload(
			suffix, item_code=sku, serial=serial, condition_grade="Good",
			retail_price=777, year_made=1961))
		sn = frappe.db.get_value(
			"Serial No", serial,
			["condition_grade", "sell_price", "description", "year_made"], as_dict=True)
		self.assertEqual(sn.condition_grade, "Good")
		self.assertEqual(float(sn.sell_price or 0), 777)
		self.assertEqual(sn.year_made, 1961)
		self.assertTrue((sn.description or "").strip(), "per-gun description should be on the Serial No")

	def test_intake_shared_sku_preserves_individual_id_on_acquisition(self):
		"""Compliance guard: the individual source ID still lands on the FFL
		Acquisition even when filing under a shared SKU."""
		suffix = self._testMethodName
		sku = f"SHARED-{suffix}"
		serial = f"SN-{suffix}"
		res = firearm_intake.create_intake(self._payload(
			suffix, item_code=sku, serial=serial,
			source_id_type="Driver License", source_id_number=f"DL-{suffix}"))
		acq = frappe.get_doc("FFL Acquisition", res["acquisition"])
		self.assertEqual(acq.docstatus, 1)
		self.assertEqual(acq.serial, serial)
		self.assertEqual(acq.item, sku)
		self.assertEqual(acq.source_id_number, f"DL-{suffix}")


if __name__ == "__main__":
	unittest.main()
