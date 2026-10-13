"""Floor plans of the Storage Locations page: one drawing per room, each zone on at most one
plan, every shape checked on save, and a save refused when someone else saved first."""
from __future__ import annotations

import json

import frappe

from ffl_core import storage
from ffl_core.api import storage as api
from ffl_core.tests.test_storage import StorageFixtures


class StorageFloorPlans(StorageFixtures):
	def plan(self, name=None, width=60, height=40, company=None) -> frappe._dict:
		res = storage.create_floor_plan(company or self.company, name or f"Room {self.tag}", width, height)
		return self.load(res["plan"])

	@staticmethod
	def load(plan) -> frappe._dict:
		return frappe.db.get_value("Storage Floor Plan", plan,
			["name", "plan_name", "width", "height", "layout", "modified"], as_dict=True)

	def zone(self, kind="Slots", count=3, name=None) -> str:
		name = name or f"Q{self.tag}{frappe.generate_hash(length=3).upper()}"
		return storage.create_zone(self.company, name, kind, count)["zone"]

	def save(self, p, shapes, **kw) -> dict:
		return storage.save_floor_plan(kw.get("company", self.company), p.name, kw.get("plan_name", p.plan_name), kw.get("width", p.width),
			kw.get("height", p.height), shapes, kw.get("modified", str(p.modified)))

	@staticmethod
	def shape(type_="zone", **kw) -> dict:
		return {"type": type_, "x": 10, "y": 10, "w": 6, "h": 2, "r": 0, **kw}

	def map_plan(self, plan) -> dict:
		return next(p for p in api.get_map(self.company)["plans"] if p["name"] == plan)

	def test_a_plan_is_created_saved_renamed_and_deleted(self):
		p = self.plan()
		self.assertEqual(json.loads(p.layout), {"shapes": []})
		a = self.zone()
		res = self.save(p, [self.shape(zone=a, rows=2, r=-90, extra="dropped"),
			self.shape("wall", x=30, y=0.5, w=60, h=0.5), self.shape("door", x=5, y=0.5, w=3, h=0.5),
			self.shape("area", text="  Counter  ", x=40, y=20), self.shape("label", text="Front", x=30, y=38)],
			plan_name=f"Showroom {self.tag}", width=70)
		self.assertEqual((res["shapes"], res["dropped"]), (5, []))
		saved = self.map_plan(p.name)
		self.assertEqual((saved["plan_name"], saved["width"]), (f"Showroom {self.tag}", 70))
		zone = saved["layout"]["shapes"][0]
		self.assertEqual(zone, {"type": "zone", "x": 10, "y": 10, "w": 6, "h": 2, "r": 270, "zone": a, "rows": 2})
		self.assertEqual(saved["layout"]["shapes"][3]["text"], "Counter")
		self.assertNotIn("text", saved["layout"]["shapes"][1])  # a wall has no text
		self.assertEqual(str(saved["modified"]), res["modified"])
		# the version the save returned is the one the next save starts from
		self.save(self.load(p.name), [], modified=res["modified"])
		storage.delete_floor_plan(self.company, p.name)
		self.assertFalse(frappe.db.exists("Storage Floor Plan", p.name))
		with self.assertRaises(frappe.ValidationError):
			storage.delete_floor_plan(self.company, p.name)

	def test_plan_names_and_sizes_are_checked(self):
		p = self.plan(f"Vault {self.tag}")
		for bad in ("", " ", "x" * 41):
			with self.assertRaises(frappe.ValidationError):
				storage.create_floor_plan(self.company, bad, 60, 40)
		with self.assertRaises(frappe.ValidationError):  # name taken in this company
			storage.create_floor_plan(self.company, f"Vault {self.tag}", 60, 40)
		for w, h in ((3, 40), (60, 501), ("abc", 40), (float("nan"), 40), (float("inf"), 40)):
			with self.assertRaises(frappe.ValidationError):
				storage.create_floor_plan(self.company, f"Size {self.tag}", w, h)
		other = self.plan(f"Office {self.tag}")
		with self.assertRaises(frappe.ValidationError):  # renaming onto a taken name
			self.save(other, [], plan_name=f"Vault {self.tag}")
		self.save(p, [], plan_name=f"Vault {self.tag}")  # keeping its own name is fine

	def test_every_shape_is_checked(self):
		p = self.plan(width=20, height=10)
		bad = [
			"not a list",
			[["a list"]],
			[self.shape("table")],
			[self.shape("wall", x=21)],                 # centre outside the plan
			[self.shape("wall", y=-1)],
			[self.shape("wall", w=0.1)],                 # too thin
			[self.shape("wall", w=21)],                  # longer than the plan
			[self.shape("wall", x="ten")],
			[self.shape("wall", r=float("nan"))],
			[self.shape("label", text="x" * 41)],
			[self.shape("zone", zone=self.zone(), rows=0)],
			[self.shape("zone", zone=self.zone(), rows=51)],
			[self.shape("wall")] * (storage.MAX_SHAPES + 1),
		]
		for shapes in bad:
			with self.assertRaises(frappe.ValidationError, msg=repr(shapes)[:80]):
				self.save(p, shapes)
		self.assertEqual(self.map_plan(p.name)["layout"], {"shapes": []})

	def test_a_zone_sits_on_one_plan_only(self):
		front, back = self.plan(f"Front {self.tag}"), self.plan(f"Back {self.tag}")
		a = self.zone()
		with self.assertRaises(frappe.ValidationError):
			self.save(front, [self.shape(zone=a), self.shape(zone=a, x=20)])
		self.save(front, [self.shape(zone=a)])
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.save(back, [self.shape(zone=a)])
		self.assertIn(f"Front {self.tag}", str(ctx.exception))
		# taken off the front plan, it can go on the back one
		self.save(self.load(front.name), [])
		self.save(back, [self.shape(zone=a)])

	def test_a_zone_of_another_company_is_refused(self):
		abbr = "FP" + self.tag[:3]
		other = frappe.get_doc({"doctype": "Company", "company_name": f"_Test STG FP {self.tag}",
			"abbr": abbr, "default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True).name
		theirs = storage.create_zone(other, f"T{self.tag}", "Open", 1)["zone"]
		p = self.plan()
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.save(p, [self.shape(zone=theirs)])
		self.assertIn("another company", str(ctx.exception))
		# and their plans are theirs: same name is fine, and the map shows each company its own
		mine = self.plan(f"Shared {self.tag}")
		storage.create_floor_plan(other, f"Shared {self.tag}", 60, 40)
		self.assertIn(mine.name, {p["name"] for p in api.get_map(self.company)["plans"]})
		self.assertEqual({p["plan_name"] for p in api.get_map(other)["plans"]}, {f"Shared {self.tag}"})
		# a plan is only written under its own company's lock
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.save(mine, [], company=other)
		self.assertIn("another company", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):
			storage.delete_floor_plan(other, mine.name)
		self.assertTrue(frappe.db.exists("Storage Floor Plan", mine.name))

	def test_a_save_from_an_older_version_is_refused(self):
		p = self.plan()
		self.save(p, [self.shape("wall")])
		with self.assertRaises(frappe.ValidationError) as ctx:   # p still holds the first version
			self.save(p, [self.shape("door")])
		self.assertIn("Someone else saved", str(ctx.exception))
		for stale in ("", None, "not a date"):
			with self.assertRaises(frappe.ValidationError):
				self.save(self.load(p.name), [], modified=stale)
		self.assertEqual([s["type"] for s in self.map_plan(p.name)["layout"]["shapes"]], ["wall"])

	def test_deleting_a_zone_takes_it_off_its_plan_without_staling_an_open_draft(self):
		p = self.plan()
		a, b = self.zone(), self.zone()
		self.save(p, [self.shape(zone=a), self.shape(zone=b, x=20)])
		before = self.load(p.name)
		storage.delete_zone(a, self.company)
		stored = [s.get("zone") for s in storage.plan_shapes(self.load(p.name).layout)]
		self.assertEqual(stored, [b])                                    # gone from the stored layout
		self.assertEqual(self.load(p.name).modified, before.modified)  # an open draft can still save
		# that draft still has the deleted zone (twice, even): it is dropped, not refused
		res = self.save(before, [self.shape(zone=a), self.shape(zone=a, x=30), self.shape(zone=b, x=20)])
		self.assertEqual((res["dropped"], res["shapes"]), ([a], 1))

	def test_a_re_created_zone_of_the_same_name_starts_on_no_plan(self):
		p, q = self.plan(f"Old {self.tag}"), self.plan(f"New {self.tag}")
		name = f"W{self.tag}"
		a = self.zone(name=name)
		self.save(p, [self.shape(zone=a), self.shape("wall", x=30)])
		storage.delete_zone(a, self.company)
		again = self.zone(name=name)
		self.assertEqual(again, a)                                       # same name, a new zone
		self.assertEqual([s["type"] for s in self.map_plan(p.name)["layout"]["shapes"]], ["wall"])
		self.save(q, [self.shape(zone=again)])                          # free for any plan
		self.assertEqual([s["zone"] for s in self.map_plan(q.name)["layout"]["shapes"]], [again])

	def test_delete_zone_is_refused_for_another_companys_zone(self):
		abbr = "DZ" + self.tag[:3]
		other = frappe.get_doc({"doctype": "Company", "company_name": f"_Test STG DZ {self.tag}",
			"abbr": abbr, "default_currency": "USD", "country": "United States"}).insert(ignore_permissions=True).name
		theirs = storage.create_zone(other, f"T{self.tag}", "Open", 1)["zone"]
		with self.assertRaises(frappe.ValidationError):
			storage.delete_zone(theirs, self.company)
		self.assertTrue(frappe.db.exists("Storage Zone", theirs))

	def test_messages_name_zones_as_shown_and_typed_html_is_refused(self):
		p = self.plan(width=20, height=10)
		a = self.zone(name=f"Rk{self.tag}", kind="Open")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.save(p, [self.shape(zone=a, x=25)])
		self.assertIn(f"Rk{self.tag} is outside", str(ctx.exception))   # the zone name, not its id
		self.assertNotIn(f"-Rk{self.tag}", str(ctx.exception))
		bad = "<img src=x onerror=alert(1)>"
		with self.assertRaises(frappe.ValidationError):
			storage.create_floor_plan(self.company, bad, 60, 40)
		with self.assertRaises(frappe.ValidationError):
			self.save(p, [], plan_name=bad)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.save(p, [self.shape("label", text="<b>x</b>")])
		self.assertNotIn("<b>", str(ctx.exception))                       # echoed escaped

	def test_deleting_a_plan_leaves_its_zones_free_for_another(self):
		p, q = self.plan(f"One {self.tag}"), self.plan(f"Two {self.tag}")
		a = self.zone()
		self.save(p, [self.shape(zone=a)])
		storage.delete_floor_plan(self.company, p.name)
		self.assertTrue(frappe.db.exists("Storage Zone", a))
		self.save(q, [self.shape(zone=a)])

	def test_only_the_storage_page_writes_a_plan(self):
		p = self.plan()
		doc = frappe.get_doc("Storage Floor Plan", p.name)
		doc.plan_name = "Hand edit"
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Storage Floor Plan", p.name, ignore_permissions=True)

	def test_who_may_draw(self):
		frappe.set_user(self.manager)
		res = api.create_floor_plan(f"Mgr {self.tag}", 30, 20)
		p = self.load(res["plan"])
		api.save_floor_plan(p.name, p.plan_name, 30, 20, json.dumps([self.shape("wall")]), str(p.modified))
		for who in (self.clerk, self.stocker):
			frappe.set_user(who)
			self.assertIn(p.name, {x["name"] for x in api.get_map(self.company)["plans"]})
			with self.assertRaises(frappe.PermissionError):
				api.create_floor_plan(f"No {self.tag}")
			with self.assertRaises(frappe.PermissionError):
				api.save_floor_plan(p.name, p.plan_name, 30, 20, [], str(p.modified))
			with self.assertRaises(frappe.PermissionError):
				api.delete_floor_plan(p.name)
		frappe.set_user(self.manager)
		api.delete_floor_plan(p.name)
