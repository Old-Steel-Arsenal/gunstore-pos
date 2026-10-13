"""In-store storage locations: zones, one-gun slots, open locations, quantities,
"unassigned", the log, and the rule that a location change never creates a stock or
accounting entry.

Every test builds its own items and zone names (the class transaction is only rolled
back at the end), and reads stock per item so shared warehouse rows never leak between
tests.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core import storage
from ffl_core.api import storage as api
from ffl_core.firearm import issue_serial, serialized_receipt_row, transfer_serial
from ffl_core.warehouses import default_stock_warehouse


def _user(email, role):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0})
		u.flags.ignore_permissions = True
		u.insert()
	frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
	frappe.get_doc("User", email).add_roles(role)
	frappe.clear_cache(user=email)
	return email


class StorageFixtures(FrappeTestCase):
	"""Shared setup and builders -- and NOT ONE `def test_`, so unittest never collects
	a base class's tests again under a subclass (see test_ci_scripts.NoRerunTestClasses)."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.warehouse = default_stock_warehouse(cls.company)
		cls.clerk = _user("stg-clerk@example.com", "Sales User")
		cls.stocker = _user("stg-stocker@example.com", "Stock User")
		cls.manager = _user("stg-manager@example.com", "Stock Manager")
		cls.nobody = _user("stg-nobody@example.com", "Purchase User")

	def setUp(self) -> None:
		super().setUp()
		self.tag = frappe.generate_hash(length=5).upper()

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	# --- items and stock ----------------------------------------------------

	def item(self, *, group="Ammunition", serial=False) -> str:
		code = f"STG-{self.tag}-{frappe.generate_hash(length=4).upper()}"
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": f"Item {code}",
			"item_group": group, "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 1 if serial else 0}).insert(ignore_permissions=True)
		return code

	def _entry(self, doc):
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company, **doc})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return se

	def stock(self, item, qty, warehouse=None, rate=2.0):
		self._entry({"items": [{"item_code": item, "qty": qty,
			"t_warehouse": warehouse or self.warehouse, "basic_rate": rate}]})

	def gun(self, *, item=None, serial=None, warehouse=None, group="Pistol") -> str:
		item = item or self.item(group=group, serial=True)
		serial = serial or f"SN-{self.tag}-{frappe.generate_hash(length=4).upper()}"
		self._entry({"items": [serialized_receipt_row(
			item_code=item, qty=1, warehouse=warehouse or self.warehouse, basic_rate=400.0, serial=serial)]})
		return serial

	def warehouse_named(self, name, **extra) -> str:
		existing = frappe.db.get_value("Warehouse", {"warehouse_name": name, "company": self.company})
		if existing:
			return existing
		doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name,
			"company": self.company, **extra})
		doc.insert(ignore_permissions=True)
		return doc.name

	# --- zones --------------------------------------------------------------

	def slots(self, count=3, name=None, company=None) -> list[str]:
		"""A Slots zone; returns its location names in position order."""
		res = storage.create_zone(company or self.company, name or f"Z{self.tag}", "Slots", count)
		return self.zone_locations(res["zone"])

	def open_zone(self, count=1, name=None, company=None) -> list[str]:
		"""``count`` Open zones ("Rk<tag> 1"...): an Open zone is one location, so several
		locations are several zones. Returns the location names."""
		base = name or f"Rk{self.tag}"
		names = [base] if count == 1 else [f"{base} {i}" for i in range(1, count + 1)]
		return [self.zone_locations(storage.create_zone(company or self.company, n, "Open", 1)["zone"])[0]
			for n in names]

	@staticmethod
	def zone_locations(zone) -> list[str]:
		return frappe.get_all("Storage Location", filters={"zone": zone}, pluck="name",
			order_by="position asc")

	# --- assertions ---------------------------------------------------------

	def where(self, serial):
		return frappe.db.get_value("Serial No", serial, "storage_location")

	def ledger_counts(self):
		return (frappe.db.count("Stock Ledger Entry"), frappe.db.count("GL Entry"),
			frappe.db.count("Stock Entry"), frappe.db.count("Warehouse"))

	def moves(self, **filters):
		return frappe.get_all("Storage Move", filters=filters, order_by="creation asc, name asc",
			fields=["name", "kind", "reason", "serial_no", "item_code", "qty", "from_location",
				"to_location", "voucher_type", "voucher_no", "note"])


class StorageZones(StorageFixtures):
	def test_slots_zone_names_its_positions(self):
		locs = self.slots(3)
		abbr = frappe.db.get_value("Company", self.company, "abbr")
		self.assertEqual(locs, [f"{abbr}-Z{self.tag}{n}" for n in (1, 2, 3)])
		rows = frappe.get_all("Storage Location", filters={"name": ("in", locs)},
			fields=["label", "kind", "position", "disabled", "company"], order_by="position asc")
		self.assertEqual([r.label for r in rows], [f"Z{self.tag}{n}" for n in (1, 2, 3)])
		self.assertTrue(all(r.kind == "Slots" and not r.disabled and r.company == self.company for r in rows))
		self.assertEqual(frappe.db.get_value("Storage Zone", f"{abbr}-Z{self.tag}", "slot_count"), 3)

	def test_an_open_zone_is_exactly_one_location_named_like_the_zone(self):
		res = storage.create_zone(self.company, f"Rack {self.tag}", "Open", 7)   # the count is ignored
		locs = self.zone_locations(res["zone"])
		self.assertEqual(len(locs), 1)
		row = frappe.db.get_value("Storage Location", locs[0], ["label", "kind", "position"], as_dict=True)
		self.assertEqual((row.label, row.kind, row.position), (f"Rack {self.tag}", "Open", 1))
		self.assertEqual(res["positions"], 1)
		self.assertEqual(frappe.db.get_value("Storage Zone", res["zone"], "slot_count"), 1)
		# the count is not even needed
		self.assertEqual(api.create_zone(f"Case {self.tag}", "Open")["positions"], 1)

	def test_a_slots_zone_keeps_its_sides_and_numbering_for_the_map(self):
		res = storage.create_zone(self.company, f"S{self.tag}", "Slots", 30, sides=2, numbering="In order")
		self.assertEqual(frappe.db.get_value("Storage Zone", res["zone"], ["slot_lines", "numbering"]), (2, "In order"))
		z = next(z for z in api.get_map(self.company)["zones"] if z["name"] == res["zone"])
		self.assertEqual((z["slot_lines"], z["numbering"]), (2, "In order"))
		# defaults: fit to the shape, odd / even; an Open zone has no sides
		self.assertEqual(api.create_zone(f"V{self.tag}", "Slots", 4, sides="2")["positions"], 4)  # form posts strings
		res = storage.create_zone(self.company, f"T{self.tag}", "Slots", 4)
		self.assertEqual(frappe.db.get_value("Storage Zone", res["zone"], ["slot_lines", "numbering"]), (0, "Odd / even"))
		for bad, msg in (({"sides": 3}, "Only a Slots zone has sides"), ({"numbering": "odd-even"}, "Numbering is")):
			with self.assertRaises(frappe.ValidationError) as ctx:
				storage.create_zone(self.company, f"Rack {self.tag}", "Open", 1, **bad)
			self.assertIn(msg, str(ctx.exception))
		for bad, msg in (({"sides": 51}, "Sides go from 1 to 50"), ({"sides": -1}, "Sides go from 1 to 50"),
				({"sides": "2.7"}, "Sides go from 1 to 50"), ({"sides": "two"}, "Sides go from 1 to 50"),
				({"numbering": "Zigzag"}, "Numbering is")):
			with self.assertRaises(frappe.ValidationError) as ctx:
				storage.create_zone(self.company, f"U{self.tag}", "Slots", 4, **bad)
			self.assertIn(msg, str(ctx.exception))
		self.assertFalse(frappe.db.exists("Storage Zone", {"zone_name": f"U{self.tag}"}))

	def test_a_zones_sides_and_numbering_change_without_moving_anything(self):
		locs = self.slots(4)
		zone = frappe.db.get_value("Storage Location", locs[0], "zone")
		self.assertEqual(api.set_zone_layout(zone, "2", "In order"),
			{"zone": zone, "sides": 2, "numbering": "In order"})
		self.assertEqual(frappe.db.get_value("Storage Zone", zone, ["slot_lines", "numbering"]), (2, "In order"))
		self.assertEqual(storage.set_zone_layout(zone, 0, None)["numbering"], "Odd / even")  # back to the defaults
		self.assertEqual(self.zone_locations(zone), locs)                                    # the slots stay put
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.set_zone_layout(zone, "2.5", None)
		self.assertIn("Sides go from 1 to 50", str(ctx.exception))
		rack = storage.create_zone(self.company, f"Rack {self.tag}", "Open", 1)["zone"]
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.set_zone_layout(rack, 2, None)
		self.assertIn("Only a Slots zone has sides", str(ctx.exception))

	def test_an_open_zone_takes_no_more_positions(self):
		res = storage.create_zone(self.company, f"Rack {self.tag}", "Open", 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.add_positions(res["zone"], 2)
		self.assertIn("Open zones are one location", str(ctx.exception))
		self.assertEqual(len(self.zone_locations(res["zone"])), 1)

	def test_a_name_already_used_by_a_zone_or_a_location_is_refused(self):
		storage.create_zone(self.company, f"Rack {self.tag}", "Open", 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.create_zone(self.company, f"Rack {self.tag}", "Open", 1)
		self.assertIn("already exists", str(ctx.exception))
		# a Slots zone whose first slot is the Open zone's name (zone "Rack 7A", slot 1 -> "Rack 7A1")
		self.slots(3, name=f"Y{self.tag}")
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.create_zone(self.company, f"Y{self.tag}1", "Open", 1)   # = slot Y<tag>1
		self.assertIn("already exists", str(ctx.exception))

	def test_slots_zones_still_take_a_count_and_more_slots(self):
		locs = self.slots(30, name=f"S{self.tag}")
		self.assertEqual(len(locs), 30)
		self.assertEqual(frappe.db.get_value("Storage Location", locs[29], "label"), f"S{self.tag}30")
		zone = frappe.db.get_value("Storage Location", locs[0], "zone")
		self.assertEqual(storage.add_positions(zone, 2)["positions"], 32)

	def test_duplicate_zone_and_label_clash_refused(self):
		self.slots(11, name=f"Q{self.tag}")
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.create_zone(self.company, f"Q{self.tag}", "Slots", 2)
		self.assertIn("already exists", str(ctx.exception))
		# Q<tag>1 + position 1 would be "Q<tag>11" -- the same label as position 11 above.
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.create_zone(self.company, f"Q{self.tag}1", "Slots", 1)
		self.assertIn("already exists", str(ctx.exception))

	def test_zone_input_is_validated(self):
		for bad in (dict(zone_name="", kind="Slots", count=2), dict(zone_name="A/B", kind="Slots", count=2),
				dict(zone_name=f"Z{self.tag}", kind="Shelf", count=2),
				dict(zone_name=f"Z{self.tag}", kind="Slots", count=0),
				dict(zone_name=f"Z{self.tag}", kind="Slots", count=100000)):
			with self.assertRaises(frappe.ValidationError, msg=str(bad)):
				storage.create_zone(self.company, **bad)

	def test_add_positions_only_appends(self):
		locs = self.slots(2)
		zone = frappe.db.get_value("Storage Location", locs[0], "zone")
		storage.add_positions(zone, 2)
		after = self.zone_locations(zone)
		self.assertEqual(after[:2], locs)
		self.assertEqual(len(after), 4)
		self.assertEqual(frappe.db.get_value("Storage Zone", zone, "slot_count"), 4)
		self.assertEqual(frappe.db.get_value("Storage Location", after[3], "label"), f"Z{self.tag}4")

	def test_disable_needs_an_empty_place_and_blocks_new_things(self):
		a, b = self.slots(2)
		gun = self.gun()
		storage.assign_serial(gun, a)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.set_disabled(None, a, 1)
		self.assertIn("still holds something", str(ctx.exception))
		storage.set_disabled(None, b, 1)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.assign_serial(gun, b)
		self.assertIn("disabled", str(ctx.exception))
		storage.assign_serial(gun, None)
		zone = frappe.db.get_value("Storage Location", a, "zone")
		storage.set_disabled(zone, None, 1)
		self.assertEqual(frappe.db.get_value("Storage Zone", zone, "disabled"), 1)
		self.assertEqual(storage.location_row(a).disabled, 1)   # disabled through its zone
		self.assertEqual(frappe.db.get_value("Storage Location", a, "disabled"), 0)  # its own switch untouched
		storage.set_disabled(zone, None, 0)
		self.assertEqual(storage.location_row(a).disabled, 0)
		self.assertEqual(storage.location_row(b).disabled, 1)   # b was disabled on its own: still is

	def test_remove_slots_deletes_the_last_empty_ones(self):
		a, b, c, d = self.slots(4)
		zone = frappe.db.get_value("Storage Location", a, "zone")
		gun = self.gun()
		storage.assign_serial(gun, c)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.remove_positions(zone, 2)    # C3 holds a gun
		self.assertIn("still holds", str(ctx.exception))
		self.assertTrue(frappe.db.exists("Storage Location", d))  # nothing deleted on a refusal
		with self.assertRaises(frappe.ValidationError):
			storage.remove_positions(zone, 4)    # a zone keeps one slot
		res = storage.remove_positions(zone, 1)
		self.assertEqual((res["positions"], res["removed"]), (3, 1))
		self.assertFalse(frappe.db.exists("Storage Location", d))
		self.assertEqual(frappe.db.get_value("Storage Zone", zone, "slot_count"), 3)
		storage.assign_serial(gun, None)
		storage.remove_positions(zone, 2)
		self.assertEqual(self.zone_locations(zone), [a])
		self.assertEqual(storage.add_positions(zone, 1)["positions"], 2)  # the name comes back
		self.assertEqual(self.zone_locations(zone), [a, b])

	def test_delete_zone_needs_it_empty_and_keeps_the_history(self):
		loc, = self.open_zone()
		zone = frappe.db.get_value("Storage Location", loc, "zone")
		item = self.item()
		self.stock(item, 3)
		res = storage.put_qty(item, loc, 2)
		with self.assertRaises(frappe.ValidationError):
			storage.delete_zone(zone, self.company)
		storage.take_qty(item, loc, 2)
		with self.assertRaises(frappe.ValidationError):
			storage.remove_positions(zone, 1)    # an Open zone is one location
		storage.delete_zone(zone, self.company)
		self.assertFalse(frappe.db.exists("Storage Zone", zone))
		self.assertFalse(frappe.db.exists("Storage Location", loc))
		self.assertFalse(frappe.db.exists("Storage Location Qty", {"location": loc}))
		self.assertEqual(frappe.db.get_value("Storage Move", res.move, "to_location"), loc)
		self.assertEqual(storage.unassigned_qty(self.company, item), 3)

	def test_a_location_other_documents_name_is_never_deleted(self):
		a, b = self.slots(2)
		zone = frappe.db.get_value("Storage Location", a, "zone")
		gun = self.gun()
		storage.assign_serial(gun, b)
		# the gun left but the sweep has not cleared it yet: its Serial No still names B2
		frappe.db.set_value("Serial No", gun, "status", "Delivered", update_modified=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.remove_positions(zone, 1)
		self.assertIn("Disable it instead", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):
			storage.delete_zone(zone, self.company)
		self.assertTrue(frappe.db.exists("Storage Location", b))
		storage.release_departed([gun])
		storage.remove_positions(zone, 1)      # only our own log names it now
		self.assertFalse(frappe.db.exists("Storage Location", b))

	def test_nothing_is_ever_deleted_or_edited_by_hand(self):
		a, = self.slots(1)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Storage Location", a, ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Storage Zone", frappe.db.get_value("Storage Location", a, "zone"),
				ignore_permissions=True, force=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Storage Zone", "company": self.company, "zone_name": "Hand",
				"kind": "Slots"}).insert(ignore_permissions=True)
		gun = self.gun()
		res = storage.assign_serial(gun, a)
		log = frappe.get_doc("Storage Move", res.move)
		log.note = "tampered"
		with self.assertRaises(frappe.ValidationError):
			log.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Storage Move", res.move, ignore_permissions=True)

	def test_two_companies_keep_their_own_zones(self):
		abbr = "SG" + self.tag[:3]
		other = frappe.get_doc({"doctype": "Company", "company_name": f"_Test STG Co {self.tag}",
			"abbr": abbr, "default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True).name
		mine = self.slots(2, name="A" + self.tag)
		theirs = self.slots(2, name="A" + self.tag, company=other)
		self.assertTrue(set(mine).isdisjoint(theirs))
		self.assertEqual({frappe.db.get_value("Storage Location", l, "company") for l in theirs}, {other})
		gun = self.gun()
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.assign_serial(gun, theirs[0])
		self.assertIn("different company", str(ctx.exception))
		self.assertEqual({z["name"] for z in api.get_map(other)["zones"]}, {f"{abbr}-A{self.tag}"})
		self.assertEqual(len(api.get_map(self.company)["zones"]) >= 1, True)
		self.assertNotIn(f"{abbr}-A{self.tag}", {z["name"] for z in api.get_map(self.company)["zones"]})


class StorageSlotsAndOpen(StorageFixtures):
	def test_a_slot_holds_exactly_one_gun(self):
		a, b = self.slots(2)
		g1, g2 = self.gun(), self.gun()
		storage.assign_serial(g1, a)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.assign_serial(g2, a)
		self.assertIn("already holds", str(ctx.exception))
		self.assertIn(g1, str(ctx.exception))
		self.assertIsNone(self.where(g2))
		storage.assign_serial(g2, b)
		self.assertEqual((self.where(g1), self.where(g2)), (a, b))

	def test_placing_a_gun_where_it_already_is_does_nothing(self):
		a, = self.slots(1)
		g = self.gun()
		self.assertIsNotNone(storage.assign_serial(g, a))
		self.assertIsNone(storage.assign_serial(g, a))
		self.assertEqual(len(self.moves(serial_no=g)), 1)

	def test_a_slot_takes_firearms_only(self):
		a, = self.slots(1)
		optic = self.gun(group="Optic")  # serialized, but not a firearm
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.assign_serial(optic, a)
		self.assertIn("not a firearm", str(ctx.exception))
		ammo = self.item()
		self.stock(ammo, 5)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.put_qty(ammo, a, 1)
		self.assertIn("single-firearm slot", str(ctx.exception))

	def test_an_open_location_takes_guns_and_everything_else(self):
		rack, = self.open_zone(1)
		optic, g1, g2 = self.gun(group="Optic"), self.gun(), self.gun()
		for s in (optic, g1, g2):
			storage.assign_serial(s, rack)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, rack, 4)
		self.assertEqual({self.where(s) for s in (optic, g1, g2)}, {rack})
		self.assertEqual(storage.qty_at(rack, ammo), 4)

	def test_a_gun_that_is_not_in_our_stock_cannot_be_placed(self):
		a, = self.slots(1)
		g = self.gun()
		issue_serial(frappe.db.get_value("Serial No", g, "item_code"), g, self.warehouse)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.assign_serial(g, a)
		self.assertIn("not in the store's stock", str(ctx.exception))

	def test_a_slot_freed_by_a_departed_gun_is_free_again(self):
		a, = self.slots(1)
		g1, g2 = self.gun(), self.gun()
		storage.assign_serial(g1, a)
		# Sold behind our back (no hook): status flips, location stays until noticed.
		frappe.db.set_value("Serial No", g1, {"status": "Delivered", "warehouse": None}, update_modified=False)
		storage.assign_serial(g2, a)  # a Delivered gun never blocks a slot
		self.assertEqual(self.where(g2), a)

	def test_a_move_logs_where_it_came_from(self):
		a, b = self.slots(2)
		g = self.gun()
		storage.assign_serial(g, a)
		storage.assign_serial(g, b)
		storage.assign_serial(g, None)
		log = self.moves(serial_no=g)
		self.assertEqual([(m.from_location, m.to_location) for m in log], [(None, a), (a, b), (b, None)])
		self.assertEqual({m.kind for m in log}, {"serial"})
		self.assertEqual(frappe.db.get_value("Storage Move", log[0].name, "owner"), frappe.session.user)

	def test_a_line_of_several_guns_fills_the_next_free_slots(self):
		slots = self.slots(4)
		blocker = self.gun()
		storage.assign_serial(blocker, slots[1])
		guns = [self.gun() for _ in range(2)]
		item = frappe.db.get_value("Serial No", guns[0], "item_code")
		plan = storage.plan_placements(item_code=item, qty=2, serials=guns, storage_location=slots[0])
		self.assertEqual([p["location"] for p in plan], [slots[0], slots[2]])
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.plan_placements(item_code=item, qty=2, serials=guns, storage_location=slots[3])
		self.assertIn("free slot", str(ctx.exception))
		# An explicit per-gun location wins.
		plan = storage.plan_placements(item_code=item, qty=2, serials=guns, storage_location=slots[0],
			serial_locations={guns[1]: slots[3]})
		self.assertEqual([(p["serial"], p["location"]) for p in plan],
			[(guns[1], slots[3]), (guns[0], slots[0])])


class StorageQuantities(StorageFixtures):
	def test_unassigned_is_stock_minus_what_the_locations_hold(self):
		rack, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 10)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 10)
		storage.put_qty(ammo, rack, 4)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 6)
		info = storage.item_locations(self.company, [ammo])[ammo]
		self.assertEqual((info["unassigned"], info["total"]), (6, 10))
		self.assertEqual([(l["location"], l["qty"]) for l in info["locations"]], [(rack, 4)])

	def test_units_move_between_locations_and_never_go_negative(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 10)
		storage.put_qty(ammo, r1, 5)
		storage.move(reason="move", item_code=ammo, qty=3, from_location=r1, to_location=r2)
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (2, 3))
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.move(reason="move", item_code=ammo, qty=3, from_location=r1, to_location=r2)
		self.assertIn("holds only 2", str(ctx.exception))
		self.assertEqual((storage.qty_at(r1, ammo), storage.qty_at(r2, ammo)), (2, 3))
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 5)

	def test_taking_what_a_location_has_when_short_is_allowed_for_sales(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 5)
		storage.put_qty(ammo, r1, 2)
		res = storage.move(reason="sale", item_code=ammo, qty=5, from_location=r1, allow_short=True)
		self.assertEqual(res.qty, 2)
		self.assertEqual(storage.qty_at(r1, ammo), 0)
		self.assertIsNone(storage.move(reason="sale", item_code=ammo, qty=1, from_location=r1, allow_short=True))

	def test_a_hand_move_cannot_place_more_than_is_unassigned(self):
		r1, = self.open_zone(1)
		ammo = self.item()
		self.stock(ammo, 3)
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.move(reason="move", item_code=ammo, qty=4, to_location=r1, check_unassigned=True)
		self.assertIn("Only 3", str(ctx.exception))
		storage.move(reason="move", item_code=ammo, qty=3, to_location=r1, check_unassigned=True)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 0)

	def test_a_serialized_item_cannot_be_moved_by_quantity(self):
		r1, = self.open_zone(1)
		g = self.gun()
		item = frappe.db.get_value("Serial No", g, "item_code")
		with self.assertRaises(frappe.ValidationError) as ctx:
			storage.put_qty(item, r1, 1)
		self.assertIn("serial number", str(ctx.exception))

	def test_a_sale_the_locations_did_not_see_goes_to_confirm(self):
		r1, r2 = self.open_zone(2)
		ammo = self.item()
		self.stock(ammo, 6)
		storage.put_qty(ammo, r1, 3)
		storage.put_qty(ammo, r2, 3)
		# Two units leave the warehouse with no location named.
		self._entry({"stock_entry_type": "Material Issue", "items": [
			{"item_code": ammo, "qty": 2, "s_warehouse": self.warehouse}]})
		self.assertEqual(storage.unassigned_qty(self.company, ammo), -2)
		confirm = {r["item_code"]: r for r in storage.unassigned_summary(self.company)["to_confirm"]}
		self.assertEqual(confirm[ammo]["over"], 2)
		with self.assertRaises(frappe.ValidationError):
			api.confirm_taken(ammo, r1, 3)  # more than is waiting
		res = api.confirm_taken(ammo, r1, 2)
		self.assertEqual(res["over"], 0)
		self.assertEqual(storage.qty_at(r1, ammo), 1)
		self.assertEqual(storage.unassigned_qty(self.company, ammo), 0)
		self.assertNotIn(ammo, [r["item_code"] for r in storage.unassigned_summary(self.company)["to_confirm"]])
		self.assertEqual(self.moves(item_code=ammo)[-1].reason, "confirm")

	def test_unassigned_lists_guns_and_items_in_no_location(self):
		a, = self.slots(1)
		g_in, g_out = self.gun(), self.gun()
		storage.assign_serial(g_in, a)
		ammo = self.item()
		self.stock(ammo, 4)
		summary = storage.unassigned_summary(self.company)
		serials = {g["serial"] for g in summary["guns"]}
		self.assertIn(g_out, serials)
		self.assertNotIn(g_in, serials)
		self.assertEqual({i["item_code"]: i["unassigned"] for i in summary["items"]}[ammo], 4)

	def test_custody_and_consignment_stock_is_not_ours_to_locate(self):
		custody = self.warehouse_named(f"STG Custody {self.tag}", is_custody_warehouse=1)
		ammo = self.item()
		self.stock(ammo, 5, warehouse=custody)
		self.assertEqual(storage.stock_qty(self.company, [ammo]), {})


class StorageNeverTouchesStockOrBooks(StorageFixtures):
	def test_location_work_creates_no_stock_or_gl_entries(self):
		rack, = self.open_zone(1)
		a, b = self.slots(2)
		g1, g2 = self.gun(), self.gun()
		ammo = self.item()
		self.stock(ammo, 10)
		before = self.ledger_counts()
		bin_before = frappe.db.get_value("Bin", {"item_code": ammo, "warehouse": self.warehouse}, "actual_qty")
		# every kind of location change
		storage.create_zone(self.company, f"Y{self.tag}", "Slots", 2)
		storage.add_positions(f"{frappe.db.get_value('Company', self.company, 'abbr')}-Y{self.tag}", 1)
		storage.assign_serial(g1, a)
		storage.assign_serial(g2, b)
		storage.assign_serial(g1, rack)
		storage.assign_serial(g1, None)
		storage.put_qty(ammo, rack, 6)
		storage.move(reason="move", item_code=ammo, qty=2, from_location=rack)
		storage.take_qty(ammo, rack, 1, reason="sale")
		storage.set_disabled(None, a, 1)
		api.scan_move(rack, g1)
		api.scan_move(rack, ammo, 1)
		api.get_map(self.company)
		self.assertEqual(self.ledger_counts()[:3], before[:3])
		# Locations are not warehouses: only the Warehouse table itself is compared.
		self.assertEqual(self.ledger_counts()[3], before[3])
		self.assertEqual(frappe.db.get_value("Bin", {"item_code": ammo, "warehouse": self.warehouse}, "actual_qty"),
			bin_before)
		self.assertEqual(frappe.db.get_value("Serial No", g1, "warehouse"), self.warehouse)


class StorageGunLeaves(StorageFixtures):
	def test_a_disposition_style_issue_clears_the_location(self):
		a, = self.slots(1)
		g = self.gun()
		item = frappe.db.get_value("Serial No", g, "item_code")
		storage.assign_serial(g, a)
		issue_serial(item, g, self.warehouse)
		self.assertIsNone(self.where(g))
		last = self.moves(serial_no=g)[-1]
		self.assertEqual((last.reason, last.from_location, last.to_location), ("out", a, None))
		# and the slot is free for the next gun
		g2 = self.gun()
		storage.assign_serial(g2, a)
		self.assertEqual(self.where(g2), a)

	def test_a_transfer_into_a_consignment_or_custody_warehouse_clears_it(self):
		a, b = self.slots(2)
		dealer = f"_Test STG Dealer {self.tag}"
		if not frappe.db.exists("Customer", dealer):
			frappe.get_doc({"doctype": "Customer", "customer_name": dealer,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
			}).insert(ignore_permissions=True)
		from ffl_core.warehouses import ensure_consignment_warehouse
		consign_wh = ensure_consignment_warehouse(dealer, self.company)
		custody_wh = self.warehouse_named(f"STG Custody {self.tag}", is_custody_warehouse=1)
		g1, g2 = self.gun(), self.gun()
		storage.assign_serial(g1, a)
		storage.assign_serial(g2, b)
		for g, wh in ((g1, consign_wh), (g2, custody_wh)):
			transfer_serial(frappe.db.get_value("Serial No", g, "item_code"), g, self.warehouse, wh)
			self.assertIsNone(self.where(g), g)
		# ...and a gun that comes back is unassigned until someone places it
		transfer_serial(frappe.db.get_value("Serial No", g1, "item_code"), g1, consign_wh, self.warehouse)
		self.assertIsNone(self.where(g1))

	def test_a_sale_of_the_gun_clears_it(self):
		a, = self.slots(1)
		g = self.gun()
		item = frappe.db.get_value("Serial No", g, "item_code")
		storage.assign_serial(g, a)
		cust = f"_Test STG Buyer {self.tag}"
		frappe.get_doc({"doctype": "Customer", "customer_name": cust,
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
		}).insert(ignore_permissions=True)
		si = frappe.get_doc({"doctype": "Sales Invoice", "customer": cust, "company": self.company,
			"update_stock": 1, "set_warehouse": self.warehouse,
			"items": [{"item_code": item, "qty": 1, "rate": 500, "warehouse": self.warehouse,
				"use_serial_batch_fields": 1, "serial_no": g}]})
		si.flags.ignore_permissions = True
		si.flags.ffl_skip_firearm_gate = True
		from unittest.mock import patch
		with patch("ffl_core.compliance.firearm.require_4473_and_nics_for_firearm_sale"), \
				patch("ffl_core.compliance.firearm.require_serial_for_firearm"):
			si.insert()
			si.submit()
		self.assertEqual(frappe.db.get_value("Serial No", g, "status"), "Delivered")
		self.assertIsNone(self.where(g))
		self.assertEqual(self.moves(serial_no=g)[-1].reason, "out")

	def test_the_hourly_sweep_catches_anything_the_entry_hook_missed(self):
		a, b = self.slots(2)
		g1, g2 = self.gun(), self.gun()
		storage.assign_serial(g1, a)
		storage.assign_serial(g2, b)
		frappe.db.set_value("Serial No", g1, {"status": "Delivered", "warehouse": None}, update_modified=False)
		frappe.db.set_value("Serial No", g2, "warehouse", None, update_modified=False)
		storage.sweep_departed()
		self.assertIsNone(self.where(g1))
		self.assertIsNone(self.where(g2))

	def test_the_map_ignores_a_gun_that_left_even_before_the_sweep(self):
		a, = self.slots(1)
		g = self.gun()
		storage.assign_serial(g, a)
		frappe.db.set_value("Serial No", g, {"status": "Delivered", "warehouse": None}, update_modified=False)
		zone = next(z for z in api.get_map(self.company)["zones"] if a in [l["name"] for l in z["locations"]])
		self.assertEqual(zone["used"], 0)
		self.assertEqual(api.get_location(a)["guns"], [])


class StorageApi(StorageFixtures):
	def test_scan_move_places_a_gun_and_a_unit_and_undo_reverses_it(self):
		a, = self.slots(1)
		rack, = self.open_zone(1)
		g = self.gun()
		res = api.scan_move(a, g.lower())  # case is ignored
		self.assertEqual(res["result"], "serial")
		self.assertEqual(self.where(g), a)
		self.assertEqual(api.scan_move(a, g)["result"], "already")
		ammo = self.item()
		self.stock(ammo, 5)
		res = api.scan_move(rack, ammo, 2)
		self.assertEqual((res["result"], res["qty"]), ("item", 2))
		self.assertEqual(storage.qty_at(rack, ammo), 2)
		self.assertTrue(api.undo_move(res["move"])["ok"])
		self.assertEqual(storage.qty_at(rack, ammo), 0)
		self.assertFalse(api.undo_move(res["move"])["ok"])  # only once
		first = api.scan_move(rack, g)
		self.assertTrue(api.undo_move(first["move"])["ok"])
		self.assertEqual(self.where(g), a)

	def test_scan_move_says_what_is_wrong_in_plain_words(self):
		a, b = self.slots(2)
		g1, g2 = self.gun(), self.gun()
		api.scan_move(a, g1)
		res = api.scan_move(a, g2)
		self.assertEqual(res["result"], "error")
		self.assertIn("already holds", res["message"])
		self.assertIn("free slot", res["message"])
		self.assertEqual(api.scan_move(a, "NO-SUCH-CODE")["result"], "error")
		self.assertIn("Scan the location first", api.scan_move(a, b)["message"])
		self.assertEqual(self.where(g2), None)

	def test_scan_move_asks_where_to_take_units_from_when_it_could_be_two_places(self):
		r1, r2, r3 = self.open_zone(3)
		ammo = self.item()
		self.stock(ammo, 4)
		storage.put_qty(ammo, r1, 2)
		storage.put_qty(ammo, r2, 2)  # nothing unassigned any more
		res = api.scan_move(r3, ammo, 1)
		self.assertEqual(res["result"], "choose")
		self.assertEqual({o["location"] for o in res["options"]}, {r1, r2})
		res = api.scan_move(r3, ammo, 1, from_location=r2)
		self.assertEqual(res["result"], "item")
		self.assertEqual((storage.qty_at(r2, ammo), storage.qty_at(r3, ammo)), (1, 1))
		# with exactly one other place it just takes from there
		other = self.item()
		self.stock(other, 2)
		storage.put_qty(other, r1, 2)
		res = api.scan_move(r2, other, 1)
		self.assertEqual((res["result"], res["from_location"]), ("item", r1))

	def test_a_move_made_after_cannot_be_undone(self):
		a, b = self.slots(2)
		g = self.gun()
		first = api.scan_move(a, g)
		api.scan_move(b, g)
		res = api.undo_move(first["move"])
		self.assertFalse(res["ok"])
		self.assertEqual(self.where(g), b)

	def test_the_map_counts_slots_and_flags_unassigned(self):
		a, b, c = self.slots(3)
		g = self.gun()
		storage.assign_serial(g, b)
		zone = next(z for z in api.get_map(self.company)["zones"] if b in [l["name"] for l in z["locations"]])
		self.assertEqual((zone["positions"], zone["used"]), (3, 1))
		occupied = [l for l in zone["locations"] if l["guns"]]
		self.assertEqual([(l["name"], l["guns"][0]["serial"]) for l in occupied], [(b, g)])
		self.assertEqual(api.get_location(b)["guns"][0]["serial"], g)
		self.assertEqual(api.plan_slots([{"key": "n", "after": a, "count": 1}]), {"n": [c]})
		self.assertEqual(api.plan_slots([{"key": "n", "after": c, "count": 1}]), {"n": []})
		listed = {l["name"]: l["free"] for l in api.list_locations(self.company)}
		self.assertEqual((listed[a], listed[b], listed[c]), (True, False, True))

	def test_location_query_marks_a_taken_slot(self):
		a, b = self.slots(2)
		storage.assign_serial(self.gun(), a)
		rows = dict(api.location_query("Storage Location", f"Z{self.tag}", "name", 0, 20, {}))
		self.assertIn("taken by", rows[a])
		self.assertNotIn("taken by", rows[b])
		# Frappe validates a picked Link value by searching for its exact name through this query.
		self.assertEqual(set(dict(api.location_query("Storage Location", b, "name", 0, 20, {}))), {b})
		free_only = dict(api.location_query("Storage Location", f"Z{self.tag}", "name", 0, 20, {"only_free": 1}))
		self.assertEqual(set(free_only), {b})

	def test_who_may_do_what(self):
		a, = self.slots(1)
		for who in (self.clerk, self.stocker, self.manager):
			frappe.set_user(who)
			api.get_map(self.company)
			api.get_options(self.company)
		frappe.set_user(self.nobody)
		with self.assertRaises(frappe.PermissionError):
			api.get_map(self.company)
		with self.assertRaises(frappe.PermissionError):
			api.scan_move(a, "x")
		# zones are for stock managers
		for who in (self.clerk, self.stocker):
			frappe.set_user(who)
			with self.assertRaises(frappe.PermissionError):
				api.create_zone(f"N{self.tag}", "Slots", 2)
			with self.assertRaises(frappe.PermissionError):
				api.remove_positions(frappe.db.get_value("Storage Location", a, "zone"), 1)
			with self.assertRaises(frappe.PermissionError):
				api.delete_zone(frappe.db.get_value("Storage Location", a, "zone"))
		frappe.set_user(self.manager)
		self.assertTrue(api.get_options(self.company)["can_manage"])
		zone = api.create_zone(f"N{self.tag}", "Slots", 2)["zone"]
		self.assertEqual(api.remove_positions(zone, 1)["positions"], 1)
		api.delete_zone(zone)
		frappe.set_user(self.clerk)
		self.assertFalse(api.get_options(self.company)["can_manage"])


class StorageReport(StorageFixtures):
	def test_firearms_in_stock_shows_where_each_gun_sits(self):
		from ffl_core.ffl_core.report.firearms_in_stock.firearms_in_stock import execute
		a, = self.slots(1)
		placed, loose = self.gun(), self.gun()
		storage.assign_serial(placed, a)
		columns, rows = execute({})
		self.assertIn("location", [c["fieldname"] for c in columns])
		by_serial = {r["serial_no"]: r for r in rows}
		self.assertEqual(by_serial[placed]["location"], f"Z{self.tag}1")
		self.assertIsNone(by_serial[loose]["location"])


class StorageSlotPlan(StorageFixtures):
	def test_every_row_gets_its_own_slots_and_none_is_offered_twice(self):
		slots = self.slots(8)
		storage.assign_serial(self.gun(), slots[1])
		plan = api.plan_slots([
			{"key": "a", "start": slots[0], "count": 3},   # A1, then A3 (A2 is taken), A4
			{"key": "b", "start": slots[5], "count": 2},   # A6, A7
			{"key": "next", "after": slots[5], "count": 2},  # after A6 -> A8 only (A7 is row b's)
		])
		self.assertEqual(plan["a"], [slots[0], slots[2], slots[3]])
		self.assertEqual(plan["b"], [slots[5], slots[6]])
		self.assertEqual(plan["next"], [slots[7]])

	def test_open_locations_and_a_taken_start_need_no_slots(self):
		slots = self.slots(2)
		rack, = self.open_zone(1)
		storage.assign_serial(self.gun(), slots[0])
		plan = api.plan_slots(__import__("json").dumps([
			{"key": "o", "start": rack, "count": 4}, {"key": "t", "start": slots[0], "count": 1}]))
		self.assertEqual(plan, {"o": [], "t": []})


class StorageDeferredDeparture(StorageFixtures):
	def test_a_desk_transfer_between_own_warehouses_keeps_the_location(self):
		a, = self.slots(1)
		g = self.gun()
		item = frappe.db.get_value("Serial No", g, "item_code")
		other = self.warehouse_named(f"STG Back Room {self.tag}")
		storage.assign_serial(g, a)
		transfer_serial(item, g, self.warehouse, other)
		self.assertEqual(frappe.db.get_value("Serial No", g, "warehouse"), other)
		self.assertEqual(self.where(g), a)
		self.assertEqual([m.reason for m in self.moves(serial_no=g)], ["move"])

	def test_cancelling_the_transfer_keeps_it_too_and_a_real_departure_still_clears(self):
		a, = self.slots(1)
		g = self.gun()
		item = frappe.db.get_value("Serial No", g, "item_code")
		other = self.warehouse_named(f"STG Back Room {self.tag}")
		storage.assign_serial(g, a)
		se = frappe.get_doc("Stock Entry", transfer_serial(item, g, self.warehouse, other))
		se.cancel()
		self.assertEqual(self.where(g), a)
		issue_serial(item, g, self.warehouse)
		self.assertIsNone(self.where(g))
