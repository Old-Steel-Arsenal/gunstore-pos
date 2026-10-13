"""Storage locations: only move() writes a gun's location, company checks,
serialized Undo and confirm, disabled places, one definition of our shelf, returns that put
back only what the sale took, and the cheaper slot lookup."""
from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from ffl_core import storage
from ffl_core.api import firearm_intake, receive_goods
from ffl_core.api import storage as api
from ffl_core.firearm import transfer_serial
from ffl_core.tests.test_storage_counter import CounterStorageFixtures


class OnlyMoveWritesTheLocation(CounterStorageFixtures):
	def test_a_serial_no_save_cannot_change_the_location(self):
		a, b = self.slots(2)
		g = self.gun()
		doc = frappe.get_doc("Serial No", g)
		doc.storage_location = a
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save(ignore_permissions=True)
		self.assertIn("Storage Locations page", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):
			frappe.client.set_value("Serial No", g, "storage_location", a)
		self.assertIsNone(self.where(g))

	def test_move_still_writes_it_and_an_unrelated_save_is_fine(self):
		a, = self.slots(1)
		g = self.gun()
		storage.assign_serial(g, a)
		doc = frappe.get_doc("Serial No", g)
		doc.description = "edited"
		doc.save(ignore_permissions=True)        # location unchanged: allowed
		self.assertEqual(self.where(g), a)
		doc.reload()
		doc.storage_location = None
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)    # clearing by hand is a change too
		self.assertEqual(self.where(g), a)

	def test_a_serial_no_cannot_be_inserted_already_placed(self):
		a, = self.slots(1)
		item = self.item(group="Pistol", serial=True)
		doc = frappe.get_doc({"doctype": "Serial No", "serial_no": f"INS-{self.tag}", "item_code": item,
			"storage_location": a})
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)


class OtherCompaniesLocations(CounterStorageFixtures):
	def other_company(self):
		abbr = "RV" + self.tag[:3]
		name = frappe.get_doc({"doctype": "Company", "company_name": f"_Test STG R2 {self.tag}", "abbr": abbr,
			"default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True).name
		return name, self.open_zone(1, name="Other" + self.tag, company=name)[0]

	def test_validate_open_location_refuses_another_companys_place(self):
		other, theirs = self.other_company()
		ammo = self.item()
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.validate_open_location(ammo, theirs, self.company)
		self.assertIn("different company", str(ctx.exception))
		self.assertEqual(storage.validate_open_location(ammo, theirs, other), theirs)

	def test_create_order_refuses_a_foreign_location(self):
		other, theirs = self.other_company()
		ammo = self.item()
		self.stock(ammo, 3)
		from ffl_core.api import manual_order
		with self.assertRaises(frappe.ValidationError) as ctx:
			manual_order.create_order({"buyer_type": "individual", "customer": self.buyer,
				"pos_profile": self.pos_profile, "dispose_now": 1, "payment_received": 1, "mode_of_payment": "Cash",
				"items": [{"item_code": ammo, "qty": 1, "rate": 5, "storage_location": theirs}]})
		self.assertIn("different company", str(ctx.exception))

	def test_a_document_never_takes_from_another_companys_location(self):
		other, theirs = self.other_company()
		ammo = self.item()
		self.stock(ammo, 3)
		storage.put_qty(ammo, theirs, 1)          # the other company's shelf happens to hold some
		si = self.sale([(ammo, 1, theirs)])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(storage.qty_at(theirs, ammo), 1)       # untouched by this company's sale
		self.assertEqual(self.moves(item_code=ammo, voucher_no=si.name), [])

	def test_receive_refuses_a_foreign_location(self):
		other, theirs = self.other_company()
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.validate_placements([{"item_code": "X", "qty": 1, "location": theirs}], self.company)
		self.assertIn("different company", str(ctx.exception))


class UndoAndConfirmAreSerialized(CounterStorageFixtures):
	def test_a_double_undo_reverses_once_and_locks_the_original_row(self):
		a, b = self.slots(2)
		g = self.gun()
		api.scan_move(a, g)
		moved = api.scan_move(b, g)
		locked = []
		real = frappe.db.get_value

		def spy(doctype, *args, **kw):
			if doctype == "Storage Move" and kw.get("for_update"):
				locked.append(args[0])
			return real(doctype, *args, **kw)

		with patch.object(frappe.db, "get_value", side_effect=spy):
			self.assertTrue(api.undo_move(moved["move"])["ok"])
			second = api.undo_move(moved["move"])
		self.assertFalse(second["ok"])
		self.assertIn("already undone", second["message"])
		self.assertEqual(locked, [moved["move"], moved["move"]])   # each call locks the row first
		self.assertEqual(len(self.moves(note=f"undo of {moved['move']}")), 1)
		self.assertEqual(self.where(g), a)

	def test_two_confirms_cannot_confirm_the_same_units(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 6)
		storage.put_qty(ammo, r1, 3)
		storage.put_qty(ammo, r2, 3)
		self._entry({"stock_entry_type": "Material Issue", "items": [
			{"item_code": ammo, "qty": 2, "s_warehouse": self.warehouse}]})
		self.assertEqual(api.confirm_taken(ammo, r1, 2)["over"], 0)
		with self.assertRaises(frappe.ValidationError) as ctx:
			api.confirm_taken(ammo, r2, 1)
		self.assertIn("nothing waiting", str(ctx.exception))
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (1, 3))

	def test_confirm_takes_the_item_lock_before_it_reads(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 4)
		storage.put_qty(ammo, r1, 4)
		self._entry({"stock_entry_type": "Material Issue", "items": [
			{"item_code": ammo, "qty": 1, "s_warehouse": self.warehouse}]})
		order, real_get_value, real_unassigned = [], frappe.db.get_value, storage.unassigned_qty

		def spy_get(doctype, *a, **kw):
			if doctype == "Item" and kw.get("for_update"):
				order.append("item-lock")
			return real_get_value(doctype, *a, **kw)

		def spy_unassigned(*a, **kw):
			order.append("read-fresh" if kw.get("lock") else "read-stale")
			return real_unassigned(*a, **kw)

		with patch.object(frappe.db, "get_value", side_effect=spy_get), \
				patch.object(storage, "unassigned_qty", side_effect=spy_unassigned):
			api.confirm_taken(ammo, r1, 1)
		self.assertEqual(order[:2], ["item-lock", "read-fresh"])


class DisabledPlaces(CounterStorageFixtures):
	def test_a_disabled_place_is_never_offered_or_auto_picked(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 4)
		storage.put_qty(ammo, r2, 2)
		storage.take_qty(ammo, r2, 2, reason="adjust")   # empty, so it may be disabled
		storage.set_disabled(None, r2, 1)
		storage.put_qty(ammo, r1, 4)
		info = storage.item_locations(self.company, [ammo])[ammo]
		self.assertEqual([l["location"] for l in info["locations"]], [r1])
		self.assertEqual(info["sole"], r1)

	def test_units_in_a_disabled_place_are_not_offered_and_block_the_auto_pick(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 2)
		storage.put_qty(ammo, r2, 3)
		# r2 turns disabled while it still holds units (a zone disabled with stock cannot happen
		# through set_disabled, but a return may land in one)
		frappe.db.set_value("Storage Location", r2, "disabled", 1)
		info = storage.item_locations(self.company, [ammo])[ammo]
		self.assertEqual([l["location"] for l in info["locations"]], [r1])
		self.assertEqual(info["unassigned"], 0)          # r2's units are still counted as located
		self.assertEqual(info["sole"], r1)

	def test_a_zone_disabled_means_every_position_is_disabled_everywhere(self):
		r1, r2 = self.open_zone(2)
		zone = frappe.db.get_value("Storage Location", r1, "zone")
		storage.set_disabled(zone, None, 1)
		names = {l["name"] for l in api.list_locations(self.company, 0)}
		self.assertNotIn(r1, names)
		rows = dict(api.location_query("Storage Location", "", "name", 0, 500, {}))
		self.assertNotIn(r1, rows)
		with self.assertRaises(frappe.ValidationError):
			storage.assign_serial(self.gun(), r1)
		zones = {z["name"]: z for z in api.get_map(self.company)["zones"]}
		self.assertTrue(all(l["disabled"] for l in zones[zone]["locations"]))
		storage.set_disabled(zone, None, 0)
		self.assertIn(r1, {l["name"] for l in api.list_locations(self.company, 0)})

	def test_re_enabling_a_zone_keeps_a_position_disabled_on_its_own(self):
		a, b, c = self.slots(3)
		zone = frappe.db.get_value("Storage Location", a, "zone")
		storage.set_disabled(None, b, 1)
		storage.set_disabled(zone, None, 1)
		storage.set_disabled(zone, None, 0)
		self.assertEqual([storage.location_row(x).disabled for x in (a, b, c)], [0, 1, 0])
		self.assertEqual(storage.plan_row_slots([{"key": "k", "start": a, "count": 3}])["k"], [a, c])


class OurShelfHasOneDefinition(CounterStorageFixtures):
	def test_a_gun_moved_to_transit_or_a_disabled_warehouse_releases_its_slot(self):
		a, b = self.slots(2)
		g1, g2 = self.gun(), self.gun()
		storage.assign_serial(g1, a)
		storage.assign_serial(g2, b)
		transit = self.warehouse_named(f"STG Transit {self.tag}", warehouse_type="Transit")
		shut = self.warehouse_named(f"STG Shut {self.tag}")
		for g, wh in ((g1, transit), (g2, shut)):
			transfer_serial(frappe.db.get_value("Serial No", g, "item_code"), g, self.warehouse, wh)
		frappe.db.set_value("Warehouse", shut, "disabled", 1)
		storage.sweep_departed()
		self.assertIsNone(self.where(g1))   # Transit: released at once (the Stock Entry hook)
		self.assertIsNone(self.where(g2))   # disabled warehouse: released by the sweep

	def test_the_helpers_agree(self):
		transit = self.warehouse_named(f"STG Transit {self.tag}", warehouse_type="Transit")
		self.assertIn(transit, storage.excluded_warehouses())
		self.assertNotIn(transit, storage.sellable_warehouses(self.company))
		self.assertIn(self.warehouse, storage.sellable_warehouses(self.company))
		self.assertFalse(storage.on_our_shelf("Active", transit))
		self.assertTrue(storage.on_our_shelf("Active", self.warehouse))
		self.assertFalse(storage.on_our_shelf("Delivered", self.warehouse))
		self.assertFalse(storage.on_our_shelf("Active", self.warehouse, consigned_out=1))


class ReturnsPutBackOnlyWhatWasTaken(CounterStorageFixtures):
	def make_return(self, si, qty):
		from erpnext.controllers.sales_and_purchase_return import make_return_doc
		ret = make_return_doc("Sales Invoice", si.name)
		ret.items[0].qty = -qty
		ret.items[0].stock_qty = -qty
		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		return ret

	def test_a_short_pick_returns_only_the_units_it_took(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 6)
		storage.put_qty(ammo, r1, 2)
		si = self.sale([(ammo, 5, r1)])          # the location only had 2
		self.assertEqual(storage.qty_at(r1, ammo), 0)
		self.make_return(si, 5)
		self.assertEqual(storage.qty_at(r1, ammo), 2)   # not 5: the other 3 stay unassigned
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 4)

	def test_several_returns_never_put_back_more_than_was_taken(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 5)
		si = self.sale([(ammo, 4, r1)])
		self.make_return(si, 3)
		self.assertEqual(storage.qty_at(r1, ammo), 4)
		self.make_return(si, 1)
		self.assertEqual(storage.qty_at(r1, ammo), 5)

	def test_a_sale_that_named_no_location_puts_nothing_back(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 2)
		si = self.sale([(ammo, 2, None)])
		frappe.db.set_value("Sales Invoice Item", si.items[0].name, "ffl_storage_location", r1)  # a stray copy
		self.make_return(si, 2)
		self.assertEqual(storage.qty_at(r1, ammo), 2)


class SlotLookupIsCheap(CounterStorageFixtures):
	def test_planning_many_slots_costs_two_queries_not_one_per_slot(self):
		slots = self.slots(40)
		for i in range(0, 40, 2):
			storage.assign_serial(self.gun(), slots[i])
		start = storage.location_row(slots[0])
		calls = {"get_all": 0, "sql": 0}
		real_get_all, real_sql = frappe.get_all, frappe.db.sql

		def get_all(*a, **k):
			calls["get_all"] += 1
			return real_get_all(*a, **k)

		def sql(*a, **k):
			calls["sql"] += 1
			return real_sql(*a, **k)

		with patch("frappe.get_all", side_effect=get_all), patch.object(frappe.db, "sql", side_effect=sql):
			free = storage._next_free_slots(start, 15, set())
		self.assertEqual(free, [slots[i] for i in range(1, 30, 2)])
		# the zone's positions (one get_all, itself one query) and which of them hold a gun
		self.assertEqual(calls["get_all"], 1)
		self.assertLessEqual(calls["sql"], 2)


class DeferredTradeInStaysQuiet(CounterStorageFixtures):
	def test_a_slot_taken_before_checkout_leaves_no_red_error_on_the_request(self):
		a, = self.slots(1)
		sn = f"TQ-{self.tag}"
		res = firearm_intake.create_intake({"serial": sn, "source_type": "Individual", "source_name": "Jane",
			"source_id_type": "Driver License", "source_id_number": "D1", "manufacturer": "Smith & Wesson",
			"model_name": "Model 19", "caliber": ".357 Magnum", "firearm_type": "Revolver",
			"acquisition_cost": 350, "condition_grade": "Excellent", "storage_location": a}, defer_booking=True)
		storage.assign_serial(self.gun(), a)
		frappe.local.message_log = []
		out = firearm_intake.complete_intake(res["acquisition"])
		self.assertTrue(out["booked"])
		red = [m for m in frappe.local.message_log if "raise_exception" in str(m) and "already holds" in str(m)]
		self.assertEqual(red, [])
		self.assertTrue(any("not in a location" in str(m) for m in frappe.local.message_log))
