"""Inventory Count (stocktake): scan resolution, scope, undo rules, variance
(including sales during the count) and finalize.

Every test builds its own items/serials so nothing leaks between tests (the class
transaction is only rolled back at the end), and counts are scoped to this module's
own warehouses so unrelated stock in "Stores" never shows up.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, flt, now_datetime

from ffl_core.api import inventory_count as ic

GUN = "INVC-GUN"            # serialized firearm model
GUN2 = "INVC-GUN-B"         # second model (other item group)
AMMO = "INVC-AMMO"          # non-serialized, in Ammunition
OPTIC = "INVC-OPTIC"        # non-serialized, in Optic
DEALER = "_Test INVC Consignment Dealer"


def _ensure_item(code, *, group, serial=False, upc=None, rate=0):
	if frappe.db.exists("Item", code):
		return
	doc = {
		"doctype": "Item", "item_code": code, "item_name": code, "item_group": group,
		"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1 if serial else 0,
	}
	if upc:
		doc["barcodes"] = [{"barcode": upc}]
	frappe.get_doc(doc).insert(ignore_permissions=True)


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


class Fixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.stores = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "warehouse_name": "Stores"}, "name")
		cls.back = cls._warehouse("INVC Back Room")
		cls.other = cls._warehouse("INVC Elsewhere")
		_ensure_item(GUN, group="Pistol", serial=True)
		_ensure_item(GUN2, group="Optic", serial=True)
		_ensure_item(AMMO, group="Ammunition", upc="INVC-UPC-AMMO")
		_ensure_item(OPTIC, group="Optic")  # no UPC: counted by typing a quantity
		cls.clerk = _user("invc-clerk@example.com", "Stock User")
		cls.clerk2 = _user("invc-clerk2@example.com", "Stock User")
		cls.manager = _user("invc-manager@example.com", "Stock Manager")

	@classmethod
	def _warehouse(cls, name, **extra):
		existing = frappe.db.get_value("Warehouse", {"warehouse_name": name, "company": cls.company})
		if existing:
			return existing
		doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name,
			"company": cls.company, **extra})
		doc.insert(ignore_permissions=True)
		return doc.name

	def setUp(self) -> None:
		super().setUp()
		# A shelf of its own per test: the class transaction is rolled back once, at
		# the end, so a shared one would carry one test's stock into the next.
		self.shelf = self._warehouse(f"INVC Shelf {frappe.generate_hash(length=6)}")

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	# --- stock helpers -----------------------------------------------------

	def _receive_gun(self, serial, warehouse=None, item=GUN, cost=500.0):
		from ffl_core.firearm import serialized_receipt_row
		if frappe.db.exists("Serial No", serial):
			return
		self._entry({"items": [serialized_receipt_row(
			item_code=item, qty=1, warehouse=warehouse or self.shelf, basic_rate=cost, serial=serial)]})

	def _receive_qty(self, item, qty, warehouse=None, rate=2.0, minutes_ago=0):
		"""``minutes_ago`` posts the receipt in the past, i.e. before a count that is
		then pushed back with _backdate."""
		doc = {"items": [{"item_code": item, "qty": qty,
			"t_warehouse": warehouse or self.shelf, "basic_rate": rate}]}
		if minutes_ago:
			when = add_to_date(now_datetime(), minutes=-minutes_ago)
			doc.update(set_posting_time=1, posting_date=when.date().isoformat(),
				posting_time=when.time().isoformat())
		self._entry(doc)

	def _issue_qty(self, item, qty, warehouse=None):
		self._entry({"stock_entry_type": "Material Issue", "items": [{
			"item_code": item, "qty": qty, "s_warehouse": warehouse or self.shelf}]})

	def _entry(self, doc):
		se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company, **doc})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		return se

	def _pos_sale(self, item, qty, warehouse=None, minutes_ago=0, consolidated=False):
		"""A submitted POS Invoice line that has NOT reached the stock ledger — what ERPNext
		holds until the nightly close. Written straight to the tables: a real sale needs a
		shift, a profile and payments, none of which this is about."""
		when = add_to_date(now_datetime(), minutes=-minutes_ago)
		inv = frappe.new_doc("POS Invoice")
		inv.update({"name": f"INVC-POS-{frappe.generate_hash(length=8)}", "company": self.company,
			"docstatus": 1, "posting_date": when.date().isoformat(), "posting_time": when.time().isoformat(),
			"consolidated_invoice": "SINV-X" if consolidated else None})
		inv.db_insert()
		line = frappe.new_doc("POS Invoice Item")
		line.update({"name": frappe.generate_hash(length=10), "parent": inv.name, "parenttype": "POS Invoice",
			"parentfield": "items", "idx": 1, "docstatus": 1, "item_code": item, "qty": qty,
			"stock_qty": qty, "warehouse": warehouse or self.shelf})
		line.db_insert()
		return inv.name

	def _bin_qty(self, item, warehouse=None):
		return flt(frappe.db.get_value(
			"Bin", {"item_code": item, "warehouse": warehouse or self.shelf}, "actual_qty"))

	def _new_count(self, warehouses=None, groups=None, **kw):
		r = ic.create_count(
			company=self.company, warehouses=warehouses if warehouses is not None else [self.shelf],
			item_groups=groups or [], **kw)
		return r["name"]

	def _backdate(self, count, minutes=60):
		"""Push the count and every scan into the past, so stock movements made after
		this point are unambiguously 'during the count'."""
		past = add_to_date(now_datetime(), minutes=-minutes)
		frappe.db.set_value("Inventory Count", count, "creation", past, update_modified=False)
		frappe.db.sql(
			"UPDATE `tabInventory Count Entry` SET creation = %s WHERE inventory_count = %s",
			(past, count))
		# Stock already on the books was recorded before the count began.
		frappe.db.sql("UPDATE `tabStock Ledger Entry` SET creation = DATE_SUB(creation, INTERVAL 2 HOUR) "
			"WHERE warehouse = %s", (self.shelf,))

	def _entries(self, count, **filters):
		return frappe.get_all("Inventory Count Entry", filters={"inventory_count": count, **filters},
			fields=["name", "kind", "serial_no", "item_code", "warehouse", "qty_delta", "code", "method"])


class ScanResolution(Fixtures):
	def test_serial_scan_is_case_and_space_insensitive(self):
		self._receive_gun("INVC-SN-CASE")
		count = self._new_count()
		r = ic.scan(count, "  invc-sn-case \n")
		self.assertEqual(r["result"], "serial")
		self.assertEqual(r["serial"], "INVC-SN-CASE")  # the record's own spelling
		self.assertTrue(r["expected"])
		(e,) = self._entries(count, kind="serial")
		self.assertEqual((e.serial_no, e.item_code, e.warehouse), ("INVC-SN-CASE", GUN, self.shelf))

	def test_repeat_serial_scan_changes_nothing(self):
		self._receive_gun("INVC-SN-TWICE")
		count = self._new_count()
		first = ic.scan(count, "INVC-SN-TWICE")
		again = ic.scan(count, "invc-sn-twice")
		self.assertEqual(again["result"], "already")
		self.assertEqual(again["entry"], first["entry"])
		self.assertEqual(len(self._entries(count, kind="serial")), 1)

	def test_the_unique_key_stops_a_second_row_for_the_same_gun(self):
		"""Two devices racing past the exists-check: the index is what refuses."""
		self._receive_gun("INVC-SN-RACE")
		count = self._new_count()
		ic.scan(count, "INVC-SN-RACE")
		with self.assertRaises(frappe.UniqueValidationError):
			ic._insert_entry(count, kind="serial", method="scan", serial_no="invc-sn-race ",
				item_code=GUN, warehouse=self.shelf)

	def test_upc_scan_adds_one_each_time(self):
		self._receive_qty(AMMO, 10)
		count = self._new_count()
		self.assertEqual(ic.scan(count, "INVC-UPC-AMMO")["counted"], 1)
		r = ic.scan(count, "INVC-UPC-AMMO")
		self.assertEqual((r["result"], r["counted"], r["warehouse"]), ("item", 2, self.shelf))
		rows = self._entries(count, kind="qty")
		self.assertEqual(sum(e.qty_delta for e in rows), 2)

	def test_serial_wins_over_upc(self):
		"""A code that is both a serial and a UPC is the gun."""
		_ensure_item("INVC-AMMO-CLASH", group="Ammunition", upc="INVC-CLASH-1")
		self._receive_gun("INVC-CLASH-1")
		count = self._new_count()
		self.assertEqual(ic.scan(count, "INVC-CLASH-1")["result"], "serial")

	def test_unknown_code_is_saved_for_the_report(self):
		count = self._new_count()
		r = ic.scan(count, "NOPE-123")
		self.assertEqual(r["result"], "unknown")
		(e,) = self._entries(count, kind="unknown")
		self.assertEqual(e.code, "NOPE-123")
		ic.scan(count, "NOPE-123")
		self.assertEqual(ic.variance(count)["serial"]["unknown"], [{"code": "NOPE-123", "times": 2}])

	def test_a_serialized_models_upc_is_refused_with_next_step(self):
		_ensure_item("INVC-GUN-UPC", group="Pistol", serial=True, upc="INVC-UPC-GUN")
		count = self._new_count()
		r = ic.scan(count, "INVC-UPC-GUN")
		self.assertEqual(r["result"], "error")
		self.assertIn("serial", r["message"].lower())
		self.assertEqual(self._entries(count), [])

	def test_an_item_outside_the_item_group_scope_is_refused(self):
		self._receive_qty(AMMO, 1)
		count = self._new_count(groups=["Optic"])
		r = ic.scan(count, "INVC-UPC-AMMO")
		self.assertEqual(r["result"], "error")
		self.assertEqual(self._entries(count), [])

	def test_upc_scan_needs_a_warehouse_when_the_count_spans_several(self):
		count = self._new_count(warehouses=[self.shelf, self.back])
		with self.assertRaises(frappe.ValidationError):
			ic.scan(count, "INVC-UPC-AMMO")
		self.assertEqual(ic.scan(count, "INVC-UPC-AMMO", self.back)["warehouse"], self.back)
		with self.assertRaises(frappe.ValidationError):
			ic.scan(count, "INVC-UPC-AMMO", self.other)  # not in the count

	def test_set_qty_is_saved_as_a_difference(self):
		count = self._new_count()
		ic.scan(count, "INVC-UPC-AMMO")
		self.assertEqual(ic.set_qty(count, AMMO, self.shelf, 7)["counted"], 7)
		self.assertEqual(sum(e.qty_delta for e in self._entries(count, kind="qty")), 7)
		self.assertIsNone(ic.set_qty(count, AMMO, self.shelf, 7)["entry"])  # no change, no row
		with self.assertRaises(frappe.ValidationError):
			ic.set_qty(count, AMMO, self.shelf, -1)
		with self.assertRaises(frappe.ValidationError):
			ic.set_qty(count, GUN, self.shelf, 1)  # serialized: count the serial

	def test_toggle_serial_ticks_and_unticks_by_hand(self):
		self._receive_gun("INVC-SN-HAND")
		count = self._new_count()
		self.assertTrue(ic.toggle_serial(count, "INVC-SN-HAND", 1)["found"])
		(e,) = self._entries(count, kind="serial")
		self.assertEqual(e.method, "manual")
		ic.toggle_serial(count, "INVC-SN-HAND", 1)  # idempotent
		self.assertEqual(len(self._entries(count, kind="serial")), 1)
		self.assertFalse(ic.toggle_serial(count, "INVC-SN-HAND", 0)["found"])
		self.assertEqual(self._entries(count, kind="serial"), [])


class Scope(Fixtures):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.warehouses import ensure_consignment_warehouse, ensure_custody_warehouse
		if not frappe.db.exists("Customer", DEALER):
			frappe.get_doc({"doctype": "Customer", "customer_name": DEALER,
				"customer_group": "Commercial", "territory": "All Territories",
				"is_ffl_dealer": 1}).insert(ignore_permissions=True)
		cls.consignment = ensure_consignment_warehouse(DEALER, cls.company)
		cls.custody = ensure_custody_warehouse(cls.company)
		cls.transit = cls._warehouse("INVC Transit", warehouse_type="Transit")

	def test_whole_store_leaves_out_consignment_and_transit(self):
		eligible = ic._eligible_warehouses(self.company, True)
		self.assertIn(self.shelf, eligible)
		self.assertIn(self.custody, eligible)
		self.assertNotIn(self.consignment, eligible)
		self.assertNotIn(self.transit, eligible)

	def test_custody_is_optional(self):
		self.assertNotIn(self.custody, ic._eligible_warehouses(self.company, False))

	def test_a_warehouse_that_cannot_be_counted_is_refused(self):
		for bad in (self.consignment, self.transit):
			with self.assertRaises(frappe.ValidationError):
				ic.create_count(company=self.company, warehouses=[bad])

	def test_scope_is_saved_and_listed(self):
		count = self._new_count(warehouses=[self.shelf, self.back], groups=["Ammunition"])
		row = next(c for c in ic.get_counts() if c["name"] == count)
		self.assertEqual(sorted(row["warehouses"]), sorted([self.shelf, self.back]))
		self.assertEqual(row["item_groups"], ["Ammunition"])
		self.assertEqual(row["status"], "Open")

	def test_item_group_scope_includes_child_groups(self):
		# "Firearms" is the parent of the gun-type leaf groups.
		parent = frappe.db.get_value("Item Group", "Pistol", "parent_item_group")
		count = self._new_count(groups=[parent])
		row = ic._count_row(count)
		self.assertIn("Pistol", ic._scope(row).groups)

	def test_scanning_outside_the_warehouse_scope_is_a_difference_not_a_match(self):
		self._receive_gun("INVC-SN-ELSEWHERE", warehouse=self.other)
		count = self._new_count()  # shelf only
		r = ic.scan(count, "INVC-SN-ELSEWHERE")
		self.assertEqual(r["result"], "serial")
		self.assertFalse(r["expected"])
		self.assertIn("warning", r)


class UndoPermissions(Fixtures):
	def test_owner_and_manager_can_undo_but_another_clerk_cannot(self):
		self._receive_qty(AMMO, 5)
		count = self._new_count()
		frappe.set_user(self.clerk)
		entry = ic.scan(count, "INVC-UPC-AMMO")
		frappe.set_user(self.clerk2)
		entry_id = frappe.get_all("Inventory Count Entry", filters={"inventory_count": count}, pluck="name")[0]
		with self.assertRaises(frappe.ValidationError):
			ic.undo(entry_id)
		frappe.set_user(self.manager)
		self.assertTrue(ic.undo(entry_id)["ok"])
		self.assertEqual(self._entries(count), [])
		self.assertTrue(ic.undo(entry_id)["ok"])  # already gone is fine
		# the owner, on a fresh scan
		frappe.set_user(self.clerk)
		ic.scan(count, "INVC-UPC-AMMO")
		(e,) = self._entries(count)
		ic.undo(e.name)
		self.assertEqual(self._entries(count), [])

	def test_unticking_someone_elses_scan_follows_the_same_rule(self):
		self._receive_gun("INVC-SN-OTHERS")
		count = self._new_count()
		frappe.set_user(self.clerk)
		ic.scan(count, "INVC-SN-OTHERS")
		frappe.set_user(self.clerk2)
		with self.assertRaises(frappe.ValidationError):
			ic.toggle_serial(count, "INVC-SN-OTHERS", 0)
		self.assertEqual(len(self._entries(count, kind="serial")), 1)

	def test_roleless_user_is_rejected_at_entry(self):
		nobody = "invc-nobody@example.com"
		if not frappe.db.exists("User", nobody):
			frappe.get_doc({"doctype": "User", "email": nobody, "first_name": "Nobody",
				"send_welcome_email": 0}).insert(ignore_permissions=True)
		frappe.db.delete("Has Role", {"parent": nobody, "parenttype": "User"})
		frappe.clear_cache(user=nobody)
		count = self._new_count()
		frappe.set_user(nobody)
		with self.assertRaises(frappe.PermissionError):
			ic.scan(count, "X")
		with self.assertRaises(frappe.PermissionError):
			ic.finalize(count)
		frappe.set_user(self.clerk)
		with self.assertRaises(frappe.PermissionError):
			ic.finalize(count)  # scanning is open to stock staff, adjusting is not


class State(Fixtures):
	def test_state_lists_the_shelf_and_what_was_found(self):
		self._receive_gun("INVC-SN-S1")
		self._receive_gun("INVC-SN-S2")
		self._receive_qty(AMMO, 4)
		count = self._new_count()
		ic.scan(count, "INVC-SN-S1")
		ic.scan(count, "INVC-UPC-AMMO")
		st = ic.get_state(count)
		self.assertEqual({g["serial"] for g in st["serials"]}, {"INVC-SN-S1", "INVC-SN-S2"})
		self.assertEqual(set(st["found"]), {"INVC-SN-S1"})
		ammo = next(r for r in st["items"] if r["item_code"] == AMMO)
		self.assertEqual((ammo["expected"], ammo["counted"], ammo["upc"]), (4, 1, "INVC-UPC-AMMO"))
		self.assertEqual(ammo["last_delta"], 1)
		light = ic.get_state(count, counts_only=1)
		self.assertNotIn("serials", light)
		self.assertEqual(set(light["found"]), {"INVC-SN-S1"})
		self.assertEqual(light["counted"][0]["counted"], 1)

	def test_an_undo_shows_in_the_next_poll(self):
		self._receive_gun("INVC-SN-POLL")
		count = self._new_count()
		ic.scan(count, "INVC-SN-POLL")
		ic.toggle_serial(count, "INVC-SN-POLL", 0)
		self.assertEqual(ic.get_state(count, counts_only=1)["found"], {})

	def test_held_and_customer_guns_carry_their_label(self):
		self._receive_gun("INVC-SN-CUSTOMER")
		frappe.db.set_value("Serial No", "INVC-SN-CUSTOMER", "is_transfer_gun", 1, update_modified=False)
		count = self._new_count()
		st = ic.get_state(count)
		g = next(g for g in st["serials"] if g["serial"] == "INVC-SN-CUSTOMER")
		self.assertEqual(g["reason"], "custody")
		self.assertTrue(g["label"])

	def test_item_group_scope_narrows_the_shelf_list(self):
		self._receive_gun("INVC-SN-G1", item=GUN)
		self._receive_gun("INVC-SN-G2", item=GUN2)
		count = self._new_count(groups=["Optic"])
		serials = {g["serial"] for g in ic.get_state(count)["serials"]}
		self.assertIn("INVC-SN-G2", serials)
		self.assertNotIn("INVC-SN-G1", serials)


class Variance(Fixtures):
	def test_missing_unexpected_and_unknown_guns(self):
		self._receive_gun("INVC-SN-FOUND")
		self._receive_gun("INVC-SN-MISSING")
		self._receive_gun("INVC-SN-HELD")
		frappe.db.set_value("Serial No", "INVC-SN-HELD", "is_transfer_gun", 1, update_modified=False)
		self._receive_gun("INVC-SN-SOLD")
		frappe.db.set_value("Serial No", "INVC-SN-SOLD", "status", "Delivered", update_modified=False)
		self._receive_gun("INVC-SN-ELSE", warehouse=self.other)
		count = self._new_count()
		for code in ("INVC-SN-FOUND", "INVC-SN-SOLD", "INVC-SN-ELSE", "BOGUS"):
			ic.scan(count, code)
		v = ic.variance(count)["serial"]
		missing = {g["serial"]: g for g in v["missing"]}
		self.assertEqual(set(missing), {"INVC-SN-MISSING", "INVC-SN-HELD"})
		self.assertEqual(missing["INVC-SN-HELD"]["reason"], "custody")
		self.assertEqual(missing["INVC-SN-MISSING"]["reason"], "")
		unexpected = {g["serial"]: g for g in v["unexpected"]}
		self.assertEqual(set(unexpected), {"INVC-SN-SOLD", "INVC-SN-ELSE"})
		self.assertIn("sold", unexpected["INVC-SN-SOLD"]["label"].lower())
		self.assertEqual(v["unknown"], [{"code": "BOGUS", "times": 1}])

	def test_item_difference_and_never_scanned_counts_as_zero(self):
		self._receive_qty(AMMO, 10)
		self._receive_qty(OPTIC, 3)
		count = self._new_count()
		for _ in range(8):
			ic.scan(count, "INVC-UPC-AMMO")
		rows = {r["item_code"]: r for r in ic.variance(count)["items"]}
		self.assertEqual((rows[AMMO]["expected"], rows[AMMO]["counted"], rows[AMMO]["adjust_to"],
			rows[AMMO]["difference"]), (10, 8, 8, -2))
		self.assertTrue(rows[OPTIC]["never_scanned"])
		self.assertEqual((rows[OPTIC]["adjust_to"], rows[OPTIC]["difference"]), (0, -3))

	def test_a_matching_count_is_not_a_difference(self):
		self._receive_qty(AMMO, 3)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 3)
		self.assertNotIn(AMMO, {r["item_code"] for r in ic.variance(count)["items"]})

	def test_a_sale_after_the_item_was_counted_is_not_written_off_twice(self):
		self._receive_qty(AMMO, 10, minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)
		self._backdate(count)
		self._issue_qty(AMMO, 3)  # sold at the counter after staff counted it
		self.assertEqual(self._bin_qty(AMMO), 7)
		row = next((r for r in ic.variance(count)["items"] if r["item_code"] == AMMO), None)
		self.assertIsNone(row, "10 counted - 3 sold since = 7 on the shelf = what the system says")

	def test_a_sale_before_the_item_was_counted_is_a_real_shortage(self):
		self._receive_qty(AMMO, 10)
		self._issue_qty(AMMO, 3)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 5)
		self._backdate(count, minutes=-1)  # counted after the sale: the sale is no excuse
		row = next(r for r in ic.variance(count)["items"] if r["item_code"] == AMMO)
		self.assertEqual((row["expected"], row["adjust_to"], row["difference"]), (7, 5, -2))

	def test_goods_received_during_the_count_are_not_a_surplus(self):
		self._receive_qty(AMMO, 4, minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 4)
		self._backdate(count)
		self._receive_qty(AMMO, 6)  # a delivery arrives mid-count
		self.assertNotIn(AMMO, {r["item_code"] for r in ic.variance(count)["items"]})

	def test_the_target_never_goes_below_zero(self):
		self._receive_qty(AMMO, 5, minutes_ago=120)
		count = self._new_count()
		self._backdate(count)
		self._issue_qty(AMMO, 5)  # never scanned, then all sold
		self.assertNotIn(AMMO, {r["item_code"] for r in ic.variance(count)["items"]})


class Finalize(Fixtures):
	def _sr_rows(self, name):
		return {(r.item_code, r.warehouse): r for r in frappe.get_doc("Stock Reconciliation", name).items}

	def test_finalize_posts_one_reconciliation_and_never_touches_a_gun(self):
		self._receive_qty(AMMO, 10, rate=2.0)
		self._receive_qty(OPTIC, 3, rate=40.0)
		self._receive_gun("INVC-SN-FIN-FOUND")
		self._receive_gun("INVC-SN-FIN-LOST")
		dispositions = frappe.db.count("FFL Disposition")
		count = self._new_count()
		ic.scan(count, "INVC-SN-FIN-FOUND")
		ic.set_qty(count, AMMO, self.shelf, 8)
		ic.set_qty(count, OPTIC, self.shelf, 5)  # found two extra

		frappe.set_user(self.manager)
		r = ic.finalize(count)
		frappe.set_user("Administrator")

		self.assertTrue(r["stock_reconciliation"])
		sr = frappe.get_doc("Stock Reconciliation", r["stock_reconciliation"])
		self.assertEqual(sr.docstatus, 1)
		rows = self._sr_rows(sr.name)
		self.assertEqual(set(rows), {(AMMO, self.shelf), (OPTIC, self.shelf)})
		self.assertEqual(rows[(AMMO, self.shelf)].qty, 8)
		self.assertEqual(rows[(OPTIC, self.shelf)].qty, 5)
		# Removing stock keeps the bin's own rate, so nothing is revalued.
		self.assertEqual(flt(rows[(AMMO, self.shelf)].valuation_rate), 2.0)
		self.assertEqual(self._bin_qty(AMMO), 8)
		self.assertEqual(self._bin_qty(OPTIC), 5)
		# No gun anywhere in it, the missing one is untouched and nothing was disposed.
		self.assertFalse([k for k in rows if k[0] in (GUN, GUN2)])
		self.assertEqual(frappe.db.get_value("Serial No", "INVC-SN-FIN-LOST", "status"), "Active")
		self.assertEqual(frappe.db.count("FFL Disposition"), dispositions)
		self.assertEqual([m["serial"] for m in r["serial"]["missing"]], ["INVC-SN-FIN-LOST"])
		# The books: shrinkage on AMMO, gain on OPTIC, both through Stock Adjustment.
		adj = frappe.get_cached_value("Company", self.company, "stock_adjustment_account")
		gl = frappe.get_all("GL Entry", filters={"voucher_no": sr.name, "is_cancelled": 0, "account": adj},
			fields=["debit", "credit"])
		self.assertEqual(round(sum(g.debit for g in gl) - sum(g.credit for g in gl), 2), 4.0 - 80.0)
		header = frappe.db.get_value("Inventory Count", count,
			["status", "stock_reconciliation", "finalized_by"], as_dict=True)
		self.assertEqual((header.status, header.stock_reconciliation, header.finalized_by),
			("Finalized", sr.name, self.manager))

	def test_found_stock_in_an_empty_bin_is_rated_from_the_item_cost(self):
		_ensure_item("INVC-FOUND", group="Ammunition", upc="INVC-UPC-FOUND")
		frappe.db.set_value("Item", "INVC-FOUND", "last_purchase_rate", 6.5)
		count = self._new_count()
		ic.scan(count, "INVC-UPC-FOUND")
		ic.scan(count, "INVC-UPC-FOUND")
		r = ic.finalize(count)
		rows = self._sr_rows(r["stock_reconciliation"])
		self.assertEqual(rows[("INVC-FOUND", self.shelf)].qty, 2)
		self.assertEqual(flt(rows[("INVC-FOUND", self.shelf)].valuation_rate), 6.5)
		self.assertEqual(self._bin_qty("INVC-FOUND"), 2)

	def test_only_the_ticked_rows_are_adjusted(self):
		self._receive_qty(AMMO, 10)
		self._receive_qty(OPTIC, 3)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 9)
		ic.set_qty(count, OPTIC, self.shelf, 1)
		r = ic.finalize(count, [{"item_code": AMMO, "warehouse": self.shelf}])
		self.assertEqual(set(self._sr_rows(r["stock_reconciliation"])), {(AMMO, self.shelf)})
		self.assertEqual(self._bin_qty(AMMO), 9)
		self.assertEqual(self._bin_qty(OPTIC), 3)

	def test_a_sale_during_the_count_lands_on_the_corrected_figure(self):
		self._receive_qty(AMMO, 10, minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 9)  # one is genuinely missing
		self._backdate(count)
		self._issue_qty(AMMO, 3)  # and three sold afterwards
		r = ic.finalize(count)
		self.assertEqual(self._bin_qty(AMMO), 6)  # 9 counted - 3 sold, not 9 and not 7

	def test_firearms_only_count_finalizes_without_a_reconciliation(self):
		self._receive_gun("INVC-SN-ONLY")
		count = self._new_count(groups=["Pistol"])
		ic.scan(count, "INVC-SN-ONLY")
		r = ic.finalize(count)
		self.assertIsNone(r["stock_reconciliation"])
		self.assertEqual(frappe.db.get_value("Inventory Count", count, "status"), "Finalized")

	def test_an_empty_count_cannot_be_finalized(self):
		self._receive_qty(AMMO, 10)
		count = self._new_count()
		with self.assertRaises(frappe.ValidationError):
			ic.finalize(count)
		self.assertEqual(self._bin_qty(AMMO), 10)
		self.assertEqual(frappe.db.get_value("Inventory Count", count, "status"), "Open")

	def test_a_finalized_count_is_closed(self):
		self._receive_gun("INVC-SN-CLOSED")
		count = self._new_count(groups=["Pistol"])
		ic.scan(count, "INVC-SN-CLOSED")
		ic.finalize(count)
		with self.assertRaises(frappe.ValidationError):
			ic.finalize(count)
		with self.assertRaises(frappe.ValidationError):
			ic.scan(count, "INVC-SN-CLOSED")
		(e,) = self._entries(count)
		with self.assertRaises(frappe.ValidationError):
			ic.undo(e.name)
		# the firearm report is what was true at the time
		self.assertEqual(ic.variance(count)["status"], "Finalized")
		self.assertIn("missing", ic.variance(count)["serial"])

	def test_a_failed_reconciliation_leaves_the_count_open(self):
		self._receive_qty(AMMO, 10)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 4)
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", frappe.utils.nowdate())
		try:
			with self.assertRaises(frappe.ValidationError):
				ic.finalize(count)
		finally:
			frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
		self.assertEqual(frappe.db.get_value("Inventory Count", count, "status"), "Open")
		self.assertEqual(self._bin_qty(AMMO), 10)

	def test_cancel_discards_without_adjusting(self):
		self._receive_qty(AMMO, 10)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 1)
		ic.cancel_count(count)
		self.assertEqual(frappe.db.get_value("Inventory Count", count, "status"), "Cancelled")
		with self.assertRaises(frappe.ValidationError):
			ic.scan(count, "INVC-UPC-AMMO")
		self.assertEqual(self._bin_qty(AMMO), 10)


class NeverScannedAndSkipped(Fixtures):
	def test_never_scanned_items_are_only_adjusted_when_asked_for(self):
		self._receive_qty(AMMO, 10)
		self._receive_qty(OPTIC, 3)
		count = self._new_count()
		ic.set_qty(count, OPTIC, self.shelf, 2)
		r = ic.finalize(count)  # no item_rows: the unscanned ammo is left alone
		self.assertEqual({a["item_code"] for a in r["adjusted"]}, {OPTIC})
		self.assertEqual(self._bin_qty(AMMO), 10)

	def test_ticking_a_never_scanned_row_sets_it_to_zero(self):
		self._receive_qty(AMMO, 10)
		self._receive_qty(OPTIC, 3)
		count = self._new_count()
		ic.set_qty(count, OPTIC, self.shelf, 2)
		ic.finalize(count, [{"item_code": AMMO, "warehouse": self.shelf},
			{"item_code": OPTIC, "warehouse": self.shelf}])
		self.assertEqual((self._bin_qty(AMMO), self._bin_qty(OPTIC)), (0, 2))

	def test_a_count_with_no_quantity_entries_adjusts_nothing_even_when_asked(self):
		self._receive_qty(AMMO, 10)
		self._receive_gun("INVC-SN-NOQTY")
		count = self._new_count()
		ic.scan(count, "INVC-SN-NOQTY")
		r = ic.finalize(count, [{"item_code": AMMO, "warehouse": self.shelf}])
		self.assertIsNone(r["stock_reconciliation"])
		self.assertEqual(self._bin_qty(AMMO), 10)
		self.assertEqual(frappe.db.get_value("Inventory Count", count, "status"), "Finalized")

	def test_a_first_zero_is_saved_and_counts_as_counted(self):
		self._receive_qty(OPTIC, 3)
		count = self._new_count()
		r = ic.set_qty(count, OPTIC, self.shelf, 0)
		self.assertTrue(r["entry"])
		(e,) = self._entries(count, kind="qty")
		self.assertEqual((e.qty_delta, e.method), (0, "manual"))
		self.assertIsNone(ic.set_qty(count, OPTIC, self.shelf, 0)["entry"])  # now it is a no-op
		row = next(r for r in ic.variance(count)["items"] if r["item_code"] == OPTIC)
		self.assertFalse(row["never_scanned"])
		self.assertEqual(row["adjust_to"], 0)

	def test_an_item_that_cannot_be_reconciled_is_set_aside_and_the_rest_posts(self):
		self._receive_qty(AMMO, 10)
		self._receive_qty(OPTIC, 3)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 9)
		ic.set_qty(count, OPTIC, self.shelf, 1)
		frappe.db.set_value("Item", OPTIC, "disabled", 1, update_modified=False)
		try:
			v = ic.variance(count)
			self.assertEqual([(s["item_code"], s["counted"]) for s in v["skipped"]], [(OPTIC, 1)])
			self.assertEqual({r["item_code"] for r in v["items"]}, {AMMO})
			r = ic.finalize(count)
		finally:
			frappe.db.set_value("Item", OPTIC, "disabled", 0, update_modified=False)
		self.assertEqual([s["item_code"] for s in r["skipped"]], [OPTIC])
		self.assertEqual((self._bin_qty(AMMO), self._bin_qty(OPTIC)), (9, 3))

	def test_a_batch_tracked_item_is_refused_at_the_scan(self):
		_ensure_item("INVC-BATCH", group="Ammunition", upc="INVC-UPC-BATCH")
		frappe.db.set_value("Item", "INVC-BATCH", "has_batch_no", 1, update_modified=False)
		count = self._new_count()
		self.assertEqual(ic.scan(count, "INVC-UPC-BATCH")["result"], "error")
		with self.assertRaises(frappe.ValidationError):
			ic.set_qty(count, "INVC-BATCH", self.shelf, 1)
		self.assertEqual(self._entries(count), [])


class PosSales(Fixtures):
	"""ERPNext keeps a POS sale out of stock until the register closes, and then dates
	the stock movement at the CLOSE, not at the sale. The count places every POS sale by
	the sale's own time."""

	def _close_sale(self, pos_invoice, minutes_ago=0):
		"""What consolidating a POS Invoice does to stock: one Sales Invoice, flagged
		consolidated, whose ledger entry is dated when it was closed."""
		lines = frappe.get_all("POS Invoice Item", filters={"parent": pos_invoice},
			fields=["item_code", "stock_qty", "warehouse"])
		si = f"INVC-SI-{frappe.generate_hash(length=8)}"
		for line in lines:
			se = self._entry({"stock_entry_type": "Material Issue" if line.stock_qty > 0 else "Material Receipt",
				"items": [{"item_code": line.item_code, "qty": abs(line.stock_qty),
					("s_warehouse" if line.stock_qty > 0 else "t_warehouse"): line.warehouse,
					**({} if line.stock_qty > 0 else {"basic_rate": 2.0})}]
				, **self._at(minutes_ago)})
			frappe.db.sql("UPDATE `tabStock Ledger Entry` SET voucher_type = 'Sales Invoice', voucher_no = %s "
				"WHERE voucher_no = %s", (si, se.name))
		inv = frappe.new_doc("Sales Invoice")
		inv.update({"name": si, "company": self.company, "docstatus": 1, "is_consolidated": 1})
		inv.db_insert()
		frappe.db.set_value("POS Invoice", pos_invoice, "consolidated_invoice", si, update_modified=False)

	def _at(self, minutes_ago):
		if not minutes_ago:
			return {}
		when = add_to_date(now_datetime(), minutes=-minutes_ago)
		return {"set_posting_time": 1, "posting_date": when.date().isoformat(),
			"posting_time": when.time().isoformat()}

	def _sale(self, item, qty, sold_minutes_ago, closed_minutes_ago=None):
		"""A POS sale rung up ``sold_minutes_ago`` and, if given, closed ``closed_minutes_ago``."""
		name = self._pos_sale(item, qty, minutes_ago=sold_minutes_ago)
		if closed_minutes_ago is not None:
			self._close_sale(name, closed_minutes_ago)
		return name

	def _row(self, count, item=AMMO):
		return next((r for r in ic.variance(count)["items"] if r["item_code"] == item), None)

	def test_a_sale_before_the_scan_is_not_deducted_again_when_the_register_closes_later(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		self._sale(AMMO, 2, sold_minutes_ago=120, closed_minutes_ago=30)  # closed AFTER the scan
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 8)  # scanned at -60: the 2 are already gone
		self._backdate(count)
		self.assertEqual(self._bin_qty(AMMO), 8)
		self.assertIsNone(self._row(count), "the close's ledger entry is dated after the scan, but the sale is not")

	def test_a_sale_after_the_scan_is_deducted_once(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)
		self._backdate(count)  # scanned at -60
		self._sale(AMMO, 2, sold_minutes_ago=30, closed_minutes_ago=20)
		self.assertEqual(self._bin_qty(AMMO), 8)
		self.assertIsNone(self._row(count), "10 counted, 2 sold since: 8 on the shelf and in the Bin")

	def test_a_return_after_the_scan_adds_back_once(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)
		self._backdate(count)
		self._sale(AMMO, -1, sold_minutes_ago=30, closed_minutes_ago=20)
		self.assertEqual(self._bin_qty(AMMO), 11)
		self.assertIsNone(self._row(count))

	def test_a_sale_not_yet_closed_blocks_adjusting_its_row_and_nothing_is_posted(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		self._receive_qty(OPTIC, 3, minutes_ago=180)
		pos = self._sale(AMMO, 2, sold_minutes_ago=120)  # open
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 7)
		ic.set_qty(count, OPTIC, self.shelf, 2)
		self._backdate(count)
		row = self._row(count)
		self.assertEqual([x["invoice"] for x in row["open_register_sales"]], [pos])
		with self.assertRaises(frappe.ValidationError) as cm:
			ic.finalize(count)
		msg = str(cm.exception)
		self.assertIn("1 register sales", msg)
		self.assertIn("not closed yet", msg)
		self.assertIn("11 PM", msg)
		self.assertIn(AMMO, msg)
		self.assertEqual(frappe.db.get_value("Inventory Count", count, "status"), "Open")
		self.assertEqual((self._bin_qty(AMMO), self._bin_qty(OPTIC)), (10, 3))  # not even the clean row

	def test_an_open_sale_on_an_item_that_is_not_being_adjusted_does_not_stop_finalize(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		self._receive_qty(OPTIC, 3, minutes_ago=180)
		self._sale(AMMO, 2, sold_minutes_ago=120)  # open
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)  # matches the Bin: nothing to adjust
		ic.set_qty(count, OPTIC, self.shelf, 2)
		self._backdate(count)
		r = ic.finalize(count)
		self.assertEqual({a["item_code"] for a in r["adjusted"]}, {OPTIC})
		self.assertEqual((self._bin_qty(AMMO), self._bin_qty(OPTIC)), (10, 2))

	def test_unticking_the_blocked_row_lets_the_rest_through(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		self._receive_qty(OPTIC, 3, minutes_ago=180)
		self._sale(AMMO, 2, sold_minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 7)
		ic.set_qty(count, OPTIC, self.shelf, 2)
		self._backdate(count)
		r = ic.finalize(count, [{"item_code": OPTIC, "warehouse": self.shelf}])
		self.assertEqual((self._bin_qty(AMMO), self._bin_qty(OPTIC)), (10, 2))
		self.assertTrue(r["stock_reconciliation"])

	def test_a_sale_in_another_warehouse_does_not_block(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		self._pos_sale(AMMO, 2, warehouse=self.back, minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 8)
		self._backdate(count)
		self.assertTrue(ic.finalize(count)["stock_reconciliation"])

	def test_a_sale_not_yet_closed_is_not_mixed_into_the_figures(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)
		self._backdate(count)
		self._sale(AMMO, 2, sold_minutes_ago=1)  # open, after the scan: still counted, not in the Bin
		self.assertIsNone(self._row(count))

	def test_the_frozen_period_check_speaks_before_the_register_check(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		self._sale(AMMO, 2, sold_minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 7)
		self._backdate(count)
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", frappe.utils.nowdate())
		try:
			with self.assertRaises(frappe.ValidationError) as cm:
				ic.finalize(count)
		finally:
			frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
		self.assertIn("frozen", str(cm.exception))  # the cheaper, earlier check spoke first


class BackdatedEntries(Fixtures):
	def test_a_back_dated_receipt_entered_after_the_scan_counts_as_arriving_after_it(self):
		self._receive_qty(AMMO, 10, minutes_ago=180)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)
		self._backdate(count)
		# a Purchase Receipt dated to the supplier's invoice, entered after the count: the goods
		# reached the shelf after the walk-through
		self._receive_qty(AMMO, 5, minutes_ago=240)
		self.assertEqual(self._bin_qty(AMMO), 15)
		self.assertIsNone(next((r for r in ic.variance(count)["items"] if r["item_code"] == AMMO), None),
			"10 counted + 5 recorded since = 15 = the Bin: nothing to write off")
		r = ic.finalize(count)
		self.assertEqual(self._bin_qty(AMMO), 15)
		self.assertIsNone(r["stock_reconciliation"])

	def test_an_entry_recorded_before_the_scan_is_not_added_again(self):
		count = self._new_count()
		self._receive_qty(AMMO, 4, minutes_ago=300)
		self._backdate(count, minutes=-1)  # the scan comes after the receipt was recorded
		ic.set_qty(count, AMMO, self.shelf, 4)
		self.assertIsNone(next((r for r in ic.variance(count)["items"] if r["item_code"] == AMMO), None))


class NoCostWriteUps(Fixtures):
	def test_found_stock_with_no_cost_is_set_aside_not_written_up_at_zero(self):
		_ensure_item("INVC-NOCOST", group="Ammunition", upc="INVC-UPC-NOCOST")
		self._receive_qty(AMMO, 5)
		count = self._new_count()
		ic.set_qty(count, "INVC-NOCOST", self.shelf, 3)
		ic.set_qty(count, AMMO, self.shelf, 4)
		v = ic.variance(count)
		self.assertEqual([(s["item_code"], s["counted"]) for s in v["skipped"]], [("INVC-NOCOST", 3)])
		self.assertIn("no cost on file", v["skipped"][0]["reason"])
		r = ic.finalize(count)
		self.assertEqual(self._bin_qty(AMMO), 4)  # the rest still posts
		self.assertEqual(self._bin_qty("INVC-NOCOST"), 0)
		rows = frappe.get_doc("Stock Reconciliation", r["stock_reconciliation"]).items
		self.assertEqual([x.item_code for x in rows], [AMMO])
		self.assertFalse(any(x.allow_zero_valuation_rate for x in rows))


class FirearmsOnly(Fixtures):
	def test_a_count_with_no_quantity_entry_says_so(self):
		self._receive_gun("INVC-SN-FO")
		count = self._new_count()
		ic.scan(count, "INVC-SN-FO")
		self.assertFalse(ic.variance(count)["quantity_counted"])
		ic.set_qty(count, OPTIC, self.shelf, 0)
		self.assertTrue(ic.variance(count)["quantity_counted"])


class CountsAreOnlyChangedThroughTheApi(Fixtures):
	def test_no_stock_role_can_create_or_write_a_count_or_an_entry(self):
		for user in (self.clerk, self.manager):
			for dt in ("Inventory Count", "Inventory Count Entry"):
				for ptype in ("create", "write"):
					self.assertFalse(frappe.has_permission(dt, ptype, user=user), (user, dt, ptype))
			self.assertTrue(frappe.has_permission("Inventory Count", "read", user=user))

	def test_a_save_that_does_not_come_from_the_api_is_refused(self):
		count = self._new_count()
		doc = frappe.get_doc("Inventory Count", count)
		doc.notes = "x"
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Inventory Count Entry", "inventory_count": count, "kind": "unknown",
				"code": "X"}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Inventory Count", "company": self.company}).insert(ignore_permissions=True)

	def test_scope_fields_are_set_only_once(self):
		meta = frappe.get_meta("Inventory Count")
		for f in ("company", "warehouses", "item_groups", "include_custody"):
			self.assertTrue(meta.get_field(f).set_only_once, f)


class SavedReportHasNoOrderRefs(Fixtures):
	def test_the_report_kept_with_the_count_leaves_order_references_out(self):
		self._receive_gun("INVC-SN-HELD-WEB")
		frappe.db.set_value("Serial No", "INVC-SN-HELD-WEB", {"woo_reserved": 1, "woo_order": "WC-9"},
			update_modified=False)
		self._receive_qty(AMMO, 1)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 1)
		r = ic.finalize(count)  # run as Administrator: sees refs
		live = next(g for g in r["serial"]["missing"] if g["serial"] == "INVC-SN-HELD-WEB")
		self.assertIn("WC-9", live["label"])
		saved = frappe.db.get_value("Inventory Count", count, "serial_report")
		self.assertNotIn("WC-9", saved)
		self.assertNotIn("public_label", saved)
		self.assertIn("INVC-SN-HELD-WEB", saved)


class ItemRow(Fixtures):
	def test_one_row_can_be_fetched_on_its_own(self):
		self._receive_qty(AMMO, 5)
		count = self._new_count()
		ic.scan(count, "INVC-UPC-AMMO")
		ic.scan(count, "INVC-UPC-AMMO")
		r = ic.get_item_row(count, AMMO, self.shelf)
		self.assertEqual((r["item_code"], r["expected"], r["counted"], r["upc"], r["last_delta"]),
			(AMMO, 5, 2, "INVC-UPC-AMMO", 1))
		self.assertEqual(ic.get_item_row(count, OPTIC, self.shelf)["counted"], 0)


class PostingMoment(Fixtures):
	def test_the_reconciliation_is_dated_when_the_figures_were_worked_out(self):
		from unittest.mock import patch

		self._receive_qty(AMMO, 10, minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 9)  # one is genuinely missing
		self._backdate(count)
		as_of = add_to_date(now_datetime(), minutes=-2)
		self._issue_qty(AMMO, 3)  # a sale AFTER that moment (posting now)
		with patch.object(ic, "now_datetime", return_value=as_of):
			v = ic.variance(count)  # preview: not pinned to as_of, only finalize is
			r = ic.finalize(count)
		self.assertEqual(r["adjusted"][0]["expected"], 10)  # the stock AT that moment, not the Bin now
		sr = frappe.get_doc("Stock Reconciliation", r["stock_reconciliation"])
		self.assertEqual(sr.posting_date, as_of.date())
		self.assertEqual(sr.set_posting_time, 1)
		# the later sale is not in the figures: it stacks on top of the reconciliation ...
		self.assertEqual((sr.items[0].qty, sr.items[0].current_qty), (9, 10))
		# ... so the shelf ends at 9 counted - 3 sold, not at 9 and not at 7
		self.assertEqual(self._bin_qty(AMMO), 6)

	def test_a_row_that_matches_the_stock_at_that_moment_needs_no_entry(self):
		from unittest.mock import patch

		self._receive_qty(AMMO, 10, minutes_ago=120)
		count = self._new_count()
		ic.set_qty(count, AMMO, self.shelf, 10)
		self._backdate(count)
		self._issue_qty(AMMO, 3)
		with patch.object(ic, "now_datetime", return_value=add_to_date(now_datetime(), minutes=-2)):
			r = ic.finalize(count)  # would raise "no change" if the row were posted
		self.assertIsNone(r["stock_reconciliation"])
		self.assertEqual(self._bin_qty(AMMO), 7)


class OrderReferences(Fixtures):
	HOLD = {"reason": "counter_hold", "detail": {"tooltip": "On Sales Invoice SI-0001", "ref": "SI-0001"}}
	CHANNEL = {"reason": "channel_reserved", "detail": {"channel": "woo", "ref": "WC-77"}}

	def test_refs_are_left_out_unless_asked_for(self):
		self.assertEqual(ic._shelf_label(self.HOLD), "On an open counter order")
		self.assertIn("SI-0001", ic._shelf_label(self.HOLD, True))
		self.assertNotIn("WC-77", ic._shelf_label(self.CHANNEL))
		self.assertIn("WC-77", ic._shelf_label(self.CHANNEL, True))

	def test_only_the_roles_that_see_the_pending_pill_see_refs(self):
		frappe.set_user(self.clerk)
		self.assertFalse(ic._can_see_order_refs())
		frappe.set_user(self.manager)
		self.assertTrue(ic._can_see_order_refs())


class UndoReturnsWhatIsLeft(Fixtures):
	def test_undo_of_a_quantity_entry_reports_the_new_tally(self):
		self._receive_qty(AMMO, 5)
		count = self._new_count()
		ic.scan(count, "INVC-UPC-AMMO")
		second = ic.scan(count, "INVC-UPC-AMMO")
		self.assertEqual((second["entry"] is not None, second["last_delta"]), (True, 1))
		r = ic.undo(second["entry"])
		self.assertEqual((r["kind"], r["item_code"], r["counted"], r["last_delta"]), ("qty", AMMO, 1, 1))
		self.assertTrue(r["last_entry"])
		r = ic.undo(r["last_entry"])
		self.assertEqual((r["counted"], r["last_entry"]), (0, None))

	def test_undo_of_a_gun_names_the_serial(self):
		self._receive_gun("INVC-SN-UNDO")
		count = self._new_count()
		e = ic.scan(count, "INVC-SN-UNDO")["entry"]
		self.assertEqual(ic.undo(e)["serial_no"], "INVC-SN-UNDO")
