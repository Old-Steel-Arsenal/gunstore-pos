"""Storage locations at the door: Receive Goods lines, a used-firearm intake and a
trade-in say where the units go; a taken slot refuses the whole receive up front, and
a deferred trade-in never fails its checkout over a slot."""
from __future__ import annotations

import json

import frappe
from frappe.utils import flt

from ffl_core import storage
from ffl_core.api import firearm_intake, trade_in
from ffl_core.api.receive_goods import create_receive
from ffl_core.tests.test_receive_goods import ReceiveGoodsFixtures
from ffl_core.tests.test_storage import StorageFixtures


class ReceiveAndStorage(ReceiveGoodsFixtures, StorageFixtures):
	def gun_line(self, serials, **extra):
		return {"item_code": "RG-FIREARM", "qty": len(serials), "rate": 400, "serials": serials,
			"manufacturer": "Glock", "model_name": "19", "firearm_type": "Pistol",
			"caliber": "9mm", **extra}

	def serial(self, n=1):
		return [f"RSTG-{self.tag}-{i}" for i in range(n)]

	def test_a_received_gun_lands_in_its_slot(self):
		a, b = self.slots(2)
		sn, = self.serial()
		res = create_receive(self._payload(items=[self.gun_line([sn], storage_location=a)]))
		self.assertEqual(self.where(sn), a)
		log = self.moves(serial_no=sn)[0]
		self.assertEqual((log.reason, log.to_location, log.voucher_type, log.voucher_no),
			("receive", a, "Purchase Receipt", res["name"]))

	def test_a_line_of_guns_fills_the_next_free_slots_and_per_gun_overrides_win(self):
		slots = self.slots(4)
		storage.assign_serial(self.gun(), slots[1])
		s1, s2, s3 = self.serial(3)
		create_receive(self._payload(items=[
			self.gun_line([s1, s2], storage_location=slots[0]),
			self.gun_line([s3], storage_location=slots[0], serial_locations={s3: slots[3]}),
		]))
		self.assertEqual([self.where(s) for s in (s1, s2, s3)], [slots[0], slots[2], slots[3]])

	def test_accessories_land_in_an_open_location_and_leave_with_a_cancelled_receipt(self):
		rack, = self.open_zone(1)
		res = create_receive(self._payload(items=[
			{"item_code": "RG-AMMO", "qty": 10, "rate": 1, "storage_location": rack}]))
		self.assertEqual(storage.qty_at(rack, "RG-AMMO"), 10)
		pr = frappe.get_doc("Purchase Receipt", res["name"])
		pr.flags.ignore_permissions = True
		pr.cancel()
		self.assertEqual(storage.qty_at(rack, "RG-AMMO"), 0)

	def test_a_taken_slot_refuses_the_receive_before_anything_is_written(self):
		a, = self.slots(1)
		storage.assign_serial(self.gun(), a)
		sn, = self.serial()
		before = frappe.db.count("Purchase Receipt")
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(items=[self.gun_line([sn], storage_location=a)]))
		self.assertIn("already holds", str(ctx.exception))
		self.assertEqual(frappe.db.count("Purchase Receipt"), before)
		self.assertFalse(frappe.db.exists("Serial No", sn))

	def test_a_slot_for_loose_stock_and_two_guns_in_one_slot_are_refused(self):
		a, = self.slots(1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(items=[
				{"item_code": "RG-AMMO", "qty": 2, "rate": 1, "storage_location": a}]))
		self.assertIn("single-firearm slot", str(ctx.exception))
		s1, s2 = self.serial(2)
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(items=[self.gun_line(
				[s1, s2], storage_location=a, serial_locations={s1: a, s2: a})]))
		self.assertIn("used for two guns", str(ctx.exception))

	def test_no_location_means_unassigned_and_the_old_payload_still_works(self):
		sn, = self.serial()
		create_receive(self._payload(items=[self.gun_line([sn]),
			{"item_code": "RG-AMMO", "qty": 3, "rate": 1}]))
		self.assertIsNone(self.where(sn))
		self.assertIn(sn, [g["serial"] for g in storage.unassigned_summary(self.company)["guns"]])
		self.assertEqual(self.moves(serial_no=sn), [])

	def test_receiving_is_still_one_stock_entry_and_no_extra_ledger_rows(self):
		a, = self.slots(1)
		sn, = self.serial()
		before = frappe.db.count("Stock Ledger Entry")
		create_receive(self._payload(items=[self.gun_line([sn], storage_location=a)]))
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), before + 1)  # the gun's receipt only


class IntakeAndTradeIn(StorageFixtures):
	def setUp(self) -> None:
		super().setUp()
		# A pay-now buy pushes its acquisition to the bound book on receive; these tests are not about FastBound.
		# A site where another module saved credentials (they persist) would otherwise reach the live client.
		from unittest.mock import patch

		from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError

		p = patch.object(FastBoundClient, "from_settings", side_effect=FastBoundConfigError("off in storage trade-in tests"))
		p.start()
		self.addCleanup(p.stop)

	def intake_payload(self, serial, **extra):
		return {"serial": serial, "source_type": "Individual", "source_name": "Jane Intake",
			"source_id_type": "Driver License", "source_id_number": f"DL-{serial}",
			"manufacturer": "Smith & Wesson", "model_name": "Model 19", "caliber": ".357 Magnum",
			"firearm_type": "Revolver", "acquisition_cost": 350, "condition_grade": "Excellent", **extra}

	def test_an_intake_puts_the_gun_in_the_chosen_slot(self):
		a, = self.slots(1)
		sn = f"INTK-{self.tag}"
		firearm_intake.create_intake(self.intake_payload(sn, storage_location=a))
		self.assertEqual(self.where(sn), a)

	def test_a_taken_slot_refuses_an_intake_up_front(self):
		a, = self.slots(1)
		storage.assign_serial(self.gun(), a)
		sn = f"INTK2-{self.tag}"
		with self.assertRaises(frappe.ValidationError):
			firearm_intake.create_intake(self.intake_payload(sn, storage_location=a))
		self.assertFalse(frappe.db.exists("Serial No", sn))

	def test_a_deferred_trade_in_parks_the_location_and_books_it_at_checkout(self):
		a, = self.slots(1)
		sn = f"TI-{self.tag}"
		res = firearm_intake.create_intake(self.intake_payload(sn, storage_location=a), defer_booking=True)
		self.assertIsNone(frappe.db.get_value("Serial No", sn, "name"))  # not booked yet
		parked = json.loads(frappe.db.get_value("FFL Acquisition", res["acquisition"], "pending_intake_payload"))
		self.assertEqual(parked["storage_location"], a)
		firearm_intake.complete_intake(res["acquisition"])
		self.assertEqual(self.where(sn), a)

	def test_a_slot_taken_before_checkout_never_fails_the_checkout(self):
		a, = self.slots(1)
		sn = f"TI2-{self.tag}"
		res = firearm_intake.create_intake(self.intake_payload(sn, storage_location=a), defer_booking=True)
		storage.assign_serial(self.gun(), a)  # someone fills the slot meanwhile
		out = firearm_intake.complete_intake(res["acquisition"])
		self.assertTrue(out["booked"])
		self.assertIsNone(self.where(sn))
		self.assertIn(sn, [g["serial"] for g in storage.unassigned_summary(self.company)["guns"]])

	def test_the_trade_in_endpoint_carries_the_location(self):
		a, = self.slots(1)
		cust = f"_Test STG Seller {self.tag}"
		frappe.get_doc({"doctype": "Customer", "customer_name": cust, "customer_group": "Individual",
			"territory": "All Territories"}).insert(ignore_permissions=True)
		sn = f"TI3-{self.tag}"
		frappe.set_user("Administrator")
		trade_in.create_trade_in_intake({
			"source_name": cust, "source_first_name": "Jo", "source_last_name": "Seller",
			"source_address_line1": "1 Main St", "source_city": "Springfield", "source_state": "TX",
			"source_zip": "75024", "id_type": "Driver License", "id_number": "D1", "serial_no": sn,
			"manufacturer": "Colt", "model_name": "1911", "caliber": ".45 ACP", "condition_grade": "Good",
			"acquisition_cost": 300, "firearm_type": "Pistol", "payout_method": "Cash", "storage_location": a})
		self.assertEqual(self.where(sn), a)
