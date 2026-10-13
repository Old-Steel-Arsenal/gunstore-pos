"""Receive Goods page backend (create_receive) tests.

Covers the firearm enhancements wired through the receiving flow:
  - Source is required (per-receipt internal channel).
  - Firearm lines must carry Manufacturer / Model / Type / Caliber — a blank
    one is rejected so nothing reaches FastBound as "Unknown".
  - Confirmed firearm details are written back onto the Item before submit.
  - The per-line Service-need flag lands on the Purchase Receipt Item.
  - The resulting FFL Acquisition carries the receive source + transaction type.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from ffl_core.api.receive_goods import create_receive
from ffl_core.firearm import toggle_serial_service_need
from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfigError, FastBoundError


def _ensure_supplier_group(name):
	if not frappe.db.exists("Supplier Group", name):
		frappe.get_doc({
			"doctype": "Supplier Group", "supplier_group_name": name,
			"parent_supplier_group": "All Supplier Groups", "is_group": 0,
		}).insert(ignore_permissions=True)


def _ensure_supplier(name, **fields):
	if frappe.db.exists("Supplier", name):
		for k, v in fields.items():
			frappe.db.set_value("Supplier", name, k, v, update_modified=False)
		return
	frappe.get_doc({
		"doctype": "Supplier", "supplier_name": name,
		"supplier_group": "Distributor", **fields,
	}).insert(ignore_permissions=True)


def _ensure_item(code, item_group, has_serial_no):
	if frappe.db.exists("Item", code):
		return
	frappe.get_doc({
		"doctype": "Item", "item_code": code, "item_name": code,
		"item_group": item_group, "stock_uom": "Nos", "is_stock_item": 1,
		"has_serial_no": has_serial_no,
		"serial_no_series": f"{code}-.####" if has_serial_no else "",
	}).insert(ignore_permissions=True)


class ReceiveGoodsFixtures(FrappeTestCase):
	"""Shared setup + payload builders, and NOT ONE `def test_`.

	A *Fixtures / *Mixin base that carries no test methods is the repo idiom
	(AtfLinkFixtures, CancelOrderFixtures, BuildOrderMixin …) and the reason is
	written down in test_cancel_order.py:17 — unittest collects a base class's
	test methods AGAIN under every subclass. Inheriting the 32-test
	ReceiveGoodsCreate to reuse `_payload` would have re-run all 32 (commit-heavy
	receives, FastBound mocks, antique flips) a second time and made the module's
	test count 72 while every report said 40.
	"""
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		# create_receive builds the PR without an explicit company, so it uses
		# the site default company — pick a warehouse on THAT company to avoid an
		# InvalidWarehouseCompany mismatch (mirrors how the page runs in prod).
		cls.company = (frappe.new_doc("Purchase Receipt").company
			or frappe.db.get_single_value("Global Defaults", "default_company"))
		# Resolve a KNOWN-GOOD stock warehouse via the production helper
		# (prefers "Stores", excludes per-dealer consignment / Transit / disabled
		# warehouses). The old "first non-group warehouse" pick was unordered and
		# unfiltered — on a warm dev DB carrying leftover consignment / cross-
		# company warehouses it could land on one, which now (correctly) hides the
		# received gun from the POS pickers or trips the receive's SLE
		# valuation. On a fresh CI DB this resolves the same "Stores" it always
		# did, so it's a no-op there.
		from ffl_core.warehouses import default_stock_warehouse
		cls.warehouse = default_stock_warehouse(cls.company) or frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0, "disabled": 0,
				"consignment_dealer": ("is", "not set")},
			pluck="name", order_by="creation asc", limit=1,
		)[0]
		_ensure_supplier_group("Distributor")
		# Unique ffl_number per test module — the column is UNIQUE, and _ensure_supplier
		# de-dupes only by name, so a shared number collides across modules on a fresh
		# DB (test_4473_nics_gating keeps ...77777, test_consignment_capture uses ...77779).
		_ensure_supplier("RG-ACME-FFL", is_ffl_dealer=1, ffl_number="1-77-777-77-7X-77778",
			ez_check_status="Verified")
		# A firearm SKU (new, serial-tracked) starting with NO details (the
		# non-RSR / blank case) and a non-firearm consumable.
		_ensure_item("RG-FIREARM", item_group="Rifle", has_serial_no=1)
		_ensure_item("RG-AMMO", item_group="Ammunition", has_serial_no=0)

	def setUp(self) -> None:
		super().setUp()
		# Receive now pushes to FastBound synchronously. These tests exercise
		# receive LOGIC, not the bound-book API, so disable FastBound (push
		# no-ops) — the push itself is covered in test_fastbound_acquisition_push.
		# The two atomic-behaviour tests below re-patch this with a live mock.
		p = patch.object(FastBoundClient, "from_settings",
			side_effect=FastBoundConfigError("disabled in receive tests"))
		p.start()
		self.addCleanup(p.stop)

	def _payload(self, **over):
		base = {
			"supplier": "RG-ACME-FFL",
			"source": "Distributor",
			"warehouse": self.warehouse,
			"items": [],
		}
		base.update(over)
		return json.dumps(base)

	def _ensure_customer(self, name: str) -> str:
		if not frappe.db.exists("Customer", name):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": name,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
			}).insert(ignore_permissions=True)
		return name

	def _indiv_payload(self, suffix: str, **over) -> str:
		base = {
			"acquisition_source": "Individual",
			"source": "Individual",
			"seller_customer": None,
			"seller_first_name": "Jane",
			"seller_last_name": "Doe",
			"id_type": "Driver License",
			"id_number": f"DL-{suffix}",
			"seller_address_line1": "12 Receive Rd",
			"seller_city": "Springfield",
			"seller_state": "TX",
			"seller_zip": "75025",
			# How the seller was paid: what the page's Pay the seller dialog sends (Cash is its default).
			"seller_payment_method": "Cash",
			"warehouse": self.warehouse,
			"items": [{
				"item_code": "RG-FIREARM", "qty": 1, "rate": 250,
				"serials": [f"INDIV-{suffix}"], "sell_price": 450,
				"manufacturer": "Colt", "model_name": "1911",
				"firearm_type": "Pistol", "caliber": ".45 ACP",
			}],
		}
		base.update(over)
		return json.dumps(base)


class ReceiveGoodsCreate(ReceiveGoodsFixtures):
	"""The receive path itself: sources, firearm detail, pricing, antiques,
	individual-source acquisitions, FastBound atomicity."""

	def test_requires_source(self):
		payload = self._payload(source="", items=[
			{"item_code": "RG-AMMO", "qty": 5, "rate": 1},
		])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("Source", str(ctx.exception))

	def test_firearm_missing_details_rejected(self):
		# Non-RSR firearm with blank details must be rejected (no "Unknown").
		payload = self._payload(items=[
			{"item_code": "RG-FIREARM", "qty": 1, "rate": 500, "serials": ["RG-SN-NODETAIL"]},
		])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("missing required detail", str(ctx.exception))

	def test_firearm_details_written_back_and_acquisition_carries_source(self):
		payload = self._payload(acquisition_type="Purchase", items=[
			{
				"item_code": "RG-FIREARM", "qty": 1, "rate": 500, "sell_price": 750,
				"serials": ["RG-SN-001"], "service_need": 1,
				"manufacturer": "Glock", "model_name": "19", "firearm_type": "Pistol",
				"caliber": "9mm", "importer": "Glock Inc",
			},
		])
		res = create_receive(payload)
		self.assertTrue(res["ok"])
		pr = frappe.get_doc("Purchase Receipt", res["name"])
		# Source on the receipt; per-line service-need flag set.
		self.assertEqual(pr.ffl_source, "Distributor")
		self.assertEqual(int(pr.items[0].ffl_service_need or 0), 1)
		# Confirmed firearm details written onto the Item (so the FastBound
		# acquisition push reads real values, not the "Unknown" fallback).
		item = frappe.db.get_value("Item", "RG-FIREARM",
			["model_name", "firearm_type", "caliber", "importer"], as_dict=True)
		self.assertEqual(item.model_name, "19")
		self.assertEqual(item.firearm_type, "Pistol")
		# importer never lands on the Item (2026-08-20): it is a per-gun fact,
		# and an Item-level value comes back as a prefilled "default" on every
		# later receive into the SKU. It lives on the Serial No only.
		self.assertFalse(item.importer)
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-SN-001", "importer"), "Glock Inc")
		# Acquisition carries the receive source + default transaction type.
		acq = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": res["name"], "docstatus": 1},
			fields=["receive_source", "transaction_type", "serial"])
		self.assertEqual(len(acq), 1)
		self.assertEqual(acq[0].receive_source, "Distributor")
		# Acquisition Type chosen on the receive page lands on each acquisition.
		self.assertEqual(acq[0].transaction_type, "Purchase")
		self.assertEqual(acq[0].serial, "RG-SN-001")
		# Per-serial Service Needed flag must land on the Serial No (drives the
		# gunsmith queue surfaced on the Item form's Serial Numbers tab).
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-SN-001", "needs_service"), 1,
		)
		# And a service ToDo gets seeded for that serial so the gunsmith has
		# a visible task in Frappe's standard /app/todo queue.
		self.assertTrue(
			frappe.db.exists("ToDo", {
				"reference_type": "Serial No",
				"reference_name": "RG-SN-001",
				"status": "Open",
			}),
			msg="Expected an Open ToDo for the service-flagged Serial No",
		)
		# Per-gun Sell Price stamped on the Serial No drives POS price selection
		# (cheapest in-stock unit auto-picked at scan).
		self.assertEqual(
			float(frappe.db.get_value("Serial No", "RG-SN-001", "sell_price") or 0), 750,
		)

	def test_per_serial_maker_importer_stamped_without_clobbering_item(self):
		"""Old / surplus / imported guns under a shared model SKU: the unit's
		own manufacturer + importer land on its Serial No, and the shared Item's
		model-level manufacturer is NOT overwritten (it only seeds when empty)."""
		for b in ("Springfield Armory", "Winchester"):
			if not frappe.db.exists("Brand", b):
				frappe.get_doc({"doctype": "Brand", "brand": b}).insert(ignore_permissions=True)
		code = "RG-SHARED-OLD"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		frappe.db.set_value("Item", code, {
			"manufacturer": "Springfield Armory", "model_name": "M1 Garand",
			"firearm_type": "Rifle", "caliber": ".30-06", "importer": "",
		}, update_modified=False)
		payload = self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 600, "serials": ["RG-SN-OLD-1"],
			"manufacturer": "Winchester", "model_name": "M1 Garand",
			"firearm_type": "Rifle", "caliber": ".30-06", "importer": "CAI",
		}])
		res = create_receive(payload)
		self.assertTrue(res["ok"])
		# Per-unit maker / importer land on the Serial No (FastBound prefers these).
		sn = frappe.db.get_value("Serial No", "RG-SN-OLD-1",
			["manufacturer", "importer"], as_dict=True)
		self.assertEqual(sn.manufacturer, "Winchester")
		self.assertEqual(sn.importer, "CAI")
		# The shared Item's model-level manufacturer is NOT clobbered by the unit.
		self.assertEqual(
			frappe.db.get_value("Item", code, "manufacturer"), "Springfield Armory")
		# Item.importer stays empty — the unit's importer is NOT seeded back
		# (2026-08-20): one unit's import marking must never become the shared
		# SKU's default for every later receive (that seeding is how the 98-8MM
		# surplus bucket ended up with a bogus bucket-wide importer).
		self.assertFalse(frappe.db.get_value("Item", code, "importer"))

	def test_model_and_caliber_land_on_serial(self):
		"""model + caliber are per-unit Serial No fields (the bound book records
		the physical gun, not just the model SKU). ERPNext materialises Serial No
		rows via frappe.db.bulk_insert on PR submit, which bypasses the
		inherit_serial_firearm_attrs before_save hook — so create_receive must
		stamp them onto the serial explicitly, exactly like manufacturer/importer.

		Regression for received firearms (e.g. 08SM13461) showing a blank
		model/caliber on their Serial No while the model SKU Item carried them."""
		code = "RG-MODELCAL"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		payload = self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 500, "serials": ["RG-SN-MC-1"],
			"manufacturer": "Accuracy International", "model_name": "AXSR",
			"firearm_type": "Rifle", "caliber": ".338 Lapua", "importer": "AINA",
		}])
		res = create_receive(payload)
		self.assertTrue(res["ok"])
		sn = frappe.db.get_value("Serial No", "RG-SN-MC-1",
			["manufacturer", "importer", "model_name", "caliber"], as_dict=True)
		self.assertEqual(sn.model_name, "AXSR")
		self.assertEqual(sn.caliber, ".338 Lapua")
		# the per-unit maker / importer still land too (unchanged behaviour)
		self.assertEqual(sn.manufacturer, "Accuracy International")
		self.assertEqual(sn.importer, "AINA")

	def test_non_firearm_needs_no_details(self):
		payload = self._payload(items=[
			{"item_code": "RG-AMMO", "qty": 20, "rate": 0.5, "service_need": 0},
		])
		res = create_receive(payload)
		self.assertTrue(res["ok"])

	def test_receive_antique_stamps_item_and_skips_bound_book(self):
		"""Receiving a firearm with is_antique=1 flags the Item, still creates
		the Serial No (serialized inventory), but books NO FFL Acquisition.
		A year_made sent alongside is recorded as optional provenance."""
		code = "RG-ANTIQUE"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		payload = self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-ANTIQUE-1"],
			"is_antique": 1, "year_made": 1885,
			"manufacturer": "Winchester", "model_name": "1873",
			"firearm_type": "Rifle", "caliber": ".44-40",
		}])
		res = create_receive(payload)
		self.assertTrue(res["ok"])
		# Item flagged antique; the year lands on the gun and seeds the Item's
		# model-level default; serial created + in stock; no bound-book acquisition.
		self.assertEqual(frappe.db.get_value("Item", code, "is_antique"), 1)
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-ANTIQUE-1", "year_made"), 1885)
		self.assertEqual(frappe.db.get_value("Item", code, "year_made"), 1885)
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-ANTIQUE-1", "status"), "Active")
		self.assertEqual(
			frappe.db.count("FFL Acquisition", {"purchase_receipt": res["name"]}), 0)

	def test_antique_flip_needs_no_year_evidence(self):
		"""The Antique checkbox alone decides — a receive line flagged Antique
		with NO year_made anywhere goes through and is treated as an antique
		(Item flagged, serial in stock, no bound-book acquisition).

		Supersedes the old pre-1899 gate: the Receive Goods
		card carried no year input at all, so the gate was unsatisfiable from
		the UI and blocked every antique intake. year_made stays available as
		OPTIONAL provenance, not a precondition."""
		code = "RG-ANTIQUE-NOYEAR"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		res = create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-ANOYR-1"],
			"is_antique": 1,
			"manufacturer": "Winchester", "model_name": "1894",
			"firearm_type": "Rifle", "caliber": ".30-30",
		}]))
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("Item", code, "is_antique"), 1)
		self.assertFalse(frappe.db.get_value("Item", code, "year_made"))
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-ANOYR-1", "status"), "Active")
		self.assertEqual(
			frappe.db.count("FFL Acquisition", {"purchase_receipt": res["name"]}), 0)

	def test_antique_accepts_post_1898_year(self):
		"""A year_made the old gate would have rejected (>1898) no longer blocks
		the receive — the checkbox is the decision, the year is just recorded."""
		code = "RG-ANTIQUE-MODERNYEAR"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		res = create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-AMY-1"],
			"is_antique": 1, "year_made": 1950,
			"manufacturer": "Winchester", "model_name": "1894",
			"firearm_type": "Rifle", "caliber": ".30-30",
		}]))
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("Item", code, "is_antique"), 1)
		self.assertEqual(frappe.db.get_value("Item", code, "year_made"), 1950)
		self.assertEqual(
			frappe.db.count("FFL Acquisition", {"purchase_receipt": res["name"]}), 0)

	def test_each_gun_records_its_own_year(self):
		"""Two guns of ONE model, made seven years apart: each Serial No keeps its
		own year. The Item holds only the model-level default it was first seeded
		with — a later gun never restates it (it is seeded into
		Item.year_made only when the Item's value is empty).

		This is why year_made is a per-unit Serial No field and not just an Item
		one: on an antique the year IS the GCA-exemption provenance, so it has to
		describe the gun that skipped the bound book, not its model."""
		code = "RG-ANTIQUE-PERGUN"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		fa = {"manufacturer": "Winchester", "model_name": "1873",
			"firearm_type": "Rifle", "caliber": ".44-40", "is_antique": 1}
		self.assertTrue(create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 300,
			"serials": ["RG-PERGUN-A"], "year_made": 1885, **fa,
		}]))["ok"])
		self.assertTrue(create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 300,
			"serials": ["RG-PERGUN-B"], "year_made": 1892, **fa,
		}]))["ok"])
		# Each gun carries its own year...
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-PERGUN-A", "year_made"), 1885)
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-PERGUN-B", "year_made"), 1892)
		# ...and the shared SKU keeps the first one, never the second.
		self.assertEqual(frappe.db.get_value("Item", code, "year_made"), 1885)

	def test_year_on_a_non_antique_line_stays_off_the_item(self):
		"""A regulated (non-antique) firearm records its year on the gun too —
		year of manufacture is ordinary used-gun provenance. Only the Item seed is
		antique-scoped, so the shared SKU's model-level default is not written by
		a regulated receive."""
		code = "RG-REGULATED-YEAR"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		self.assertTrue(create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-REGYR-1"],
			"is_antique": 0, "year_made": 1885,
			"manufacturer": "Colt", "model_name": "AR-15",
			"firearm_type": "Rifle", "caliber": "5.56",
		}]))["ok"])
		self.assertEqual(
			frappe.db.get_value("Serial No", "RG-REGYR-1", "year_made"), 1885)
		self.assertFalse(frappe.db.get_value("Item", code, "year_made"))

	def test_impossible_year_is_dropped_not_rejected(self):
		"""A year that cannot be a year is discarded — from the gun and from the
		Item — but never fails the receive: the owner's rule is that the Antique
		box alone decides. Guards the one field the GCA-exemption claim rests on
		against a fat-fingered entry."""
		code = "RG-ANTIQUE-BADYEAR"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		fa = {"manufacturer": "Winchester", "model_name": "1894",
			"firearm_type": "Rifle", "caliber": ".30-30", "is_antique": 1}
		# The upper bound is next year, so pin BOTH sides of it — a stray sign
		# flip there ("+1" → "-1") would silently start rejecting this year's
		# guns, and a far-future probe like 99999 would never notice.
		next_year = frappe.utils.now_datetime().year + 1
		for i, bad_year in enumerate((-5, 0, 42, 999, next_year + 1, 18855, 99999)):
			with self.subTest(year_made=bad_year):
				serial = f"RG-BADYR-{i}"
				res = create_receive(self._payload(items=[{
					"item_code": code, "qty": 1, "rate": 300,
					"serials": [serial], "year_made": bad_year, **fa,
				}]))
				self.assertTrue(res["ok"])
				self.assertFalse(frappe.db.get_value("Serial No", serial, "year_made"))
				self.assertFalse(frappe.db.get_value("Item", code, "year_made"))
		self.assertEqual(frappe.db.get_value("Item", code, "is_antique"), 1)
		# ...and the years just inside each edge are kept.
		for i, ok_year in enumerate((1000, next_year)):
			with self.subTest(year_made=ok_year):
				serial = f"RG-OKYR-{i}"
				self.assertTrue(create_receive(self._payload(items=[{
					"item_code": code, "qty": 1, "rate": 300,
					"serials": [serial], "year_made": ok_year, **fa,
				}]))["ok"])
				self.assertEqual(
					frappe.db.get_value("Serial No", serial, "year_made"), ok_year)

	def test_antique_flip_is_audited_exactly_once(self):
		"""Flagging a SKU Antique leaves a durable record on the Item — who did
		it, on what serials, with what year — because the receive path writes the
		flag with update_modified=False and so leaves no Version row of its own.

		Exactly once: two lines of one SKU both ticked Antique are ONE decision,
		and a later receive of an already-antique item flips nothing at all."""
		code = "RG-ANTIQUE-AUDIT"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		fa = {"manufacturer": "Winchester", "model_name": "1873",
			"firearm_type": "Rifle", "caliber": ".44-40", "is_antique": 1}
		trail = {"reference_doctype": "Item", "reference_name": code,
			"content": ("like", "%Flagged Antique by%")}

		self.assertTrue(create_receive(self._payload(items=[
			{"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-AUD-1"], **fa},
			{"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-AUD-2"], **fa},
		]))["ok"])
		self.assertEqual(frappe.db.count("Comment", trail), 1)

		# Already antique — nothing flipped, so nothing new to record.
		self.assertTrue(create_receive(self._payload(items=[
			{"item_code": code, "qty": 1, "rate": 300, "serials": ["RG-AUD-3"], **fa},
		]))["ok"])
		self.assertEqual(frappe.db.count("Comment", trail), 1)

	def test_a_refused_antique_flip_leaves_no_audit_trail(self):
		"""The record follows the decision, not the attempt: a flip rejected by
		the bound-book lock must not leave a note saying the gun was flagged."""
		code = "RG-ANTIQUE-NOAUDIT"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		fa = {"manufacturer": "Colt", "model_name": "AR-15",
			"firearm_type": "Rifle", "caliber": "5.56"}
		# Book it first, so the antique flip below is refused.
		create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 400, "serials": ["RG-NOAUD-1"], **fa}]))
		with self.assertRaises(frappe.ValidationError):
			create_receive(self._payload(items=[{
				"item_code": code, "qty": 1, "rate": 400, "serials": ["RG-NOAUD-2"],
				"is_antique": 1, **fa}]))
		self.assertEqual(frappe.db.count("Comment", {
			"reference_doctype": "Item", "reference_name": code,
			"content": ("like", "%Flagged Antique by%")}), 0)

	def test_create_item_antique_flag(self):
		"""create_item (quick-create) accepts + stamps is_antique and echoes it back."""
		from ffl_core.api.receive_goods import create_item
		code = "RG-CREATE-ANTIQUE"
		if frappe.db.exists("Item", code):
			frappe.delete_doc("Item", code, force=1, ignore_permissions=True)
		res = create_item(json.dumps({
			"item_code": code, "item_group": "Rifle", "has_serial_no": 1, "is_antique": 1,
		}))
		self.assertTrue(res["is_antique"])
		self.assertEqual(frappe.db.get_value("Item", code, "is_antique"), 1)

	def test_receive_blocks_antique_on_booked_item(self):
		"""Once an item has bound-book guns, a later receive can't flip it antique."""
		code = "RG-BOOKED"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		# First receive as a NORMAL firearm → creates an FFL Acquisition (booked).
		create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 400, "serials": ["RG-BOOKED-1"],
			"manufacturer": "Colt", "model_name": "AR-15",
			"firearm_type": "Rifle", "caliber": "5.56",
		}]))
		# A second receive marking it antique must be rejected.
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(items=[{
				"item_code": code, "qty": 1, "rate": 400, "serials": ["RG-BOOKED-2"],
				"is_antique": 1,
				"manufacturer": "Colt", "model_name": "AR-15",
				"firearm_type": "Rifle", "caliber": "5.56",
			}]))
		self.assertIn("bound book", str(ctx.exception).lower())

	def test_receive_sets_standard_rate_for_non_firearm(self):
		"""Per-SKU Sell Price on a non-firearm receive lands on Item.standard_rate."""
		code = "RG-PRICE-ACC"
		_ensure_item(code, item_group="Ammunition", has_serial_no=0)
		res = create_receive(self._payload(items=[
			{"item_code": code, "qty": 5, "rate": 2.0, "sell_price": 9.99},
		]))
		self.assertTrue(res["ok"])
		self.assertEqual(float(frappe.db.get_value("Item", code, "standard_rate")), 9.99)

	def test_receive_firearm_price_stays_per_serial(self):
		"""A per-gun firearm's Sell Price goes to the Serial No, NOT Item.standard_rate."""
		code = "RG-PERGUN-PRICE"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		res = create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 400, "serials": ["PERGUN-1"], "sell_price": 1200,
			"manufacturer": "Colt", "model_name": "AR", "firearm_type": "Rifle", "caliber": "5.56",
		}]))
		self.assertTrue(res["ok"])
		self.assertEqual(float(frappe.db.get_value("Serial No", "PERGUN-1", "sell_price")), 1200)
		# Per-gun price must NOT leak to the SKU-level standard_rate.
		self.assertEqual(float(frappe.db.get_value("Item", code, "standard_rate") or 0), 0)

	def test_rereceive_without_is_antique_preserves_flag(self):
		"""A re-receive whose payload OMITS is_antique must not silently un-flag an
		antique Item back to regulated (only an explicit value changes it)."""
		code = "RG-PRESERVE"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		frappe.db.set_value("Item", code, "is_antique", 1)
		res = create_receive(self._payload(items=[{
			"item_code": code, "qty": 1, "rate": 100, "serials": ["PRESERVE-1"],
			"manufacturer": "Colt", "model_name": "X", "firearm_type": "Rifle", "caliber": "5.56",
		}]))  # note: no is_antique key in the line
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("Item", code, "is_antique"), 1)

	def test_conflicting_antique_lines_rejected(self):
		"""Same SKU on two lines with conflicting Antique settings is rejected — it
		would otherwise resolve last-write-wins and silently mis-book a serial."""
		code = "RG-CONFLICT"
		_ensure_item(code, item_group="Rifle", has_serial_no=1)
		fa = {"manufacturer": "Colt", "model_name": "X", "firearm_type": "Rifle", "caliber": "5.56"}
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._payload(items=[
				{"item_code": code, "qty": 1, "rate": 100, "serials": ["CONF-A"], "is_antique": 0, **fa},
				{"item_code": code, "qty": 1, "rate": 100, "serials": ["CONF-B"],
					"is_antique": 1, **fa},
			]))
		self.assertIn("conflicting", str(ctx.exception).lower())

	def test_per_sku_price_updates_existing_item_price(self):
		"""Receiving a non-firearm with a Sell Price updates an EXISTING POS Item
		Price (the receive value is authoritative, not just create-if-missing)."""
		from ffl_core.item_hooks import _default_selling_price_list
		code = "RG-PRICE-UPD"
		_ensure_item(code, item_group="Ammunition", has_serial_no=0)
		pl = _default_selling_price_list()
		frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": pl,
			"selling": 1, "price_list_rate": 5.0}).insert(ignore_permissions=True)
		res = create_receive(self._payload(items=[
			{"item_code": code, "qty": 3, "rate": 1.0, "sell_price": 12.50},
		]))
		self.assertTrue(res["ok"])
		self.assertEqual(float(frappe.db.get_value("Item", code, "standard_rate")), 12.50)
		self.assertEqual(float(frappe.db.get_value("Item Price",
			{"item_code": code, "price_list": pl, "selling": 1}, "price_list_rate")), 12.50)

	def test_in_stock_firearm_serials_excludes_antiques(self):
		"""The customer-transfer picker offers regulated firearms but NOT antiques
		(antiques need no transfer/4473)."""
		from ffl_core.firearm import in_stock_firearm_serials
		_ensure_item("RG-PICK-REG", item_group="Rifle", has_serial_no=1)
		_ensure_item("RG-PICK-ANT", item_group="Rifle", has_serial_no=1)
		frappe.db.set_value("Item", "RG-PICK-ANT", "is_antique", 1)
		fa = {"manufacturer": "Colt", "model_name": "X", "firearm_type": "Rifle", "caliber": "5.56"}
		create_receive(self._payload(items=[{"item_code": "RG-PICK-REG", "qty": 1, "rate": 100,
			"serials": ["PICK-REG-1"], **fa}]))
		create_receive(self._payload(items=[{"item_code": "RG-PICK-ANT", "qty": 1, "rate": 100,
			"serials": ["PICK-ANT-1"], "is_antique": 1, **fa}]))
		offered = {r["serial"] for r in in_stock_firearm_serials()}
		self.assertIn("PICK-REG-1", offered)
		self.assertNotIn("PICK-ANT-1", offered)

	def test_toggle_service_need_numeric_serial(self):
		"""Regression: a numeric Serial No name must toggle even when non-numeric
		serials coexist in the table.

		The Service-Needed pill reads the serial from a DOM data attribute; jQuery
		coerces a numeric-looking name (e.g. "1393829") to a JS Number, so the
		serial reaches the server as an int. Without coercion back to str,
		frappe.db.exists compares an integer against the varchar `name` column,
		forcing MySQL to numerify every row — which errors (1292) on the first
		non-numeric serial. We seed BOTH a numeric and a non-numeric serial so the
		int path would blow up, then call the toggle with an int and expect success.
		"""
		for sn in ("F01086", "1393829"):
			if not frappe.db.exists("Serial No", sn):
				frappe.get_doc({
					"doctype": "Serial No", "serial_no": sn, "item_code": "RG-FIREARM",
				}).insert(ignore_permissions=True)
		res = toggle_serial_service_need(int("1393829"), 1)  # int — mimics jQuery coercion
		self.assertTrue(res["ok"])
		self.assertEqual(res["needs_service"], 1)
		self.assertEqual(frappe.db.get_value("Serial No", "1393829", "needs_service"), 1)

	def test_individual_receive_records_individual_acquisition(self):
		"""Receiving from an individual: no FFL supplier; acquisition is
		source_type=Individual, linked to the Customer, with the ID captured."""
		cust = self._ensure_customer("_Test Indiv Seller")
		res = create_receive(self._indiv_payload(
			self._testMethodName, seller_customer=cust, seller_suffix="Jr"))
		self.assertTrue(res["ok"])
		pr = frappe.get_doc("Purchase Receipt", res["name"])
		self.assertEqual(pr.ffl_acquisition_source, "Individual")
		self.assertEqual(  # generic placeholder supplier (name-rule agnostic)
			frappe.db.get_value("Supplier", pr.supplier, "supplier_name"), "Individual Seller")
		acq = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": res["name"], "docstatus": 1},
			fields=["source_type", "source_customer", "source_id_number", "source_name",
				"source_first_name", "source_last_name", "source_suffix",
				"source_address", "serial"])
		self.assertEqual(len(acq), 1)
		self.assertEqual(acq[0].source_type, "Individual")
		self.assertEqual(acq[0].source_customer, cust)
		self.assertEqual(acq[0].source_id_number, f"DL-{self._testMethodName}")
		self.assertEqual(acq[0].source_name, cust)
		# Structured name captured at receive (FastBound contact uses these).
		self.assertEqual(acq[0].source_first_name, "Jane")
		self.assertEqual(acq[0].source_last_name, "Doe")
		self.assertEqual(acq[0].source_suffix, "Jr")
		# §478.125(e): the composed seller address lands on the acquisition.
		self.assertEqual(acq[0].source_address, "12 Receive Rd, Springfield TX 75025")

	def test_individual_receive_requires_address(self):
		"""An individual receive without a seller address must
		be rejected — placeholder addresses no longer reach the bound book."""
		cust = self._ensure_customer("_Test Indiv Seller")
		payload = json.loads(self._indiv_payload(
			self._testMethodName, seller_customer=cust))
		for k in ("seller_address_line1", "seller_city", "seller_state", "seller_zip"):
			payload.pop(k, None)
		with self.assertRaises(frappe.ValidationError):
			create_receive(json.dumps(payload))

	def test_consignment_receive_stamps_commission_and_consignor(self):
		"""End-to-end (Part A): a Consignment receive with a per-line commission %
		flows payload consignment_commission_pct -> PR Item ffl_consignment_commission_pct
		-> intake, stamping the Serial No's is_consignment / commission % (the
		per-line value, NOT the store default) / consignor (from the seller)."""
		from ffl_core.setup import consignment
		consignment.ensure()  # PR Item field + FFL Settings default may not exist yet
		cust = self._ensure_customer("_Test Consignor Bob")
		serial = f"CONSIGN-{self._testMethodName}"
		res = create_receive(self._indiv_payload(
			self._testMethodName, seller_customer=cust, acquisition_type="Consignment",
			items=[{
				"item_code": "RG-FIREARM", "qty": 1, "rate": 0,
				"serials": [serial], "sell_price": 900,
				"consignment_commission_pct": 15,
				"manufacturer": "Colt", "model_name": "1911",
				"firearm_type": "Pistol", "caliber": ".45 ACP",
			}],
		))
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value("Serial No", serial, "is_consignment"), 1)
		self.assertEqual(
			float(frappe.db.get_value("Serial No", serial, "consignment_commission_pct")), 15.0)
		consignor = frappe.db.get_value("Serial No", serial, "consignor")
		self.assertTrue(consignor and frappe.db.exists("Supplier", consignor))

	def test_consignment_receive_blank_pct_uses_store_default(self):
		"""A Consignment receive that omits the per-line commission falls back to
		the store default (FFL Settings, seeded to 20) — the receipt-level default
		path when the operator doesn't override a gun."""
		from ffl_core.setup import consignment
		consignment.ensure()
		cust = self._ensure_customer("_Test Consignor Bob")
		serial = f"CONSIGN-DEF-{self._testMethodName}"
		res = create_receive(self._indiv_payload(
			self._testMethodName, seller_customer=cust, acquisition_type="Consignment",
			items=[{
				"item_code": "RG-FIREARM", "qty": 1, "rate": 0, "serials": [serial],
				"manufacturer": "Colt", "model_name": "1911",
				"firearm_type": "Pistol", "caliber": ".45 ACP",
			}],
		))
		self.assertTrue(res["ok"])
		self.assertEqual(
			float(frappe.db.get_value("Serial No", serial, "consignment_commission_pct")), 20.0)

	def test_individual_receive_requires_id(self):
		cust = self._ensure_customer("_Test Indiv Seller")
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, id_number=""))
		self.assertIn("ID", str(ctx.exception))

	def test_individual_receive_accepts_other_id_type(self):
		"""Every seller-ID picker (receive page, trade-in dialog) offers "Other",
		and the Purchase Receipt field accepts it — but FFL Acquisition's
		source_id_type Select was missing it, so the whole receive died at
		acquisition creation with Frappe's Select-options error. The ID-type
		vocabulary must stay in lockstep across all three."""
		cust = self._ensure_customer("_Test Indiv Seller")
		res = create_receive(self._indiv_payload(
			self._testMethodName, seller_customer=cust, id_type="Other"))
		self.assertTrue(res["ok"])
		acq = frappe.get_all("FFL Acquisition",
			filters={"purchase_receipt": res["name"], "docstatus": 1},
			fields=["source_id_type"])
		self.assertEqual(len(acq), 1)
		self.assertEqual(acq[0].source_id_type, "Other")

	def test_individual_receive_requires_first_and_last_name(self):
		cust = self._ensure_customer("_Test Indiv Seller")
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._indiv_payload(
				self._testMethodName, seller_customer=cust, seller_last_name=""))
		self.assertIn("name", str(ctx.exception).lower())

	# ------------------------------------ Atomic FastBound push on receive

	def test_receive_syncs_acquisition_to_fastbound(self):
		"""A successful FastBound push stamps the acquisition's fastbound_id within
		the same transaction — the gun is in stock AND the bound book."""
		with patch.object(FastBoundClient, "from_settings") as factory:
			factory.return_value.post_acquisition.return_value = {
				"acquisitionId": "fb-recv-ok", "items": []}
			res = create_receive(self._payload(items=[{
				"item_code": "RG-FIREARM", "qty": 1, "rate": 300,
				"serials": [f"SYNC-{self._testMethodName}"],
				"manufacturer": "Colt", "model_name": "AR", "firearm_type": "Rifle",
				"caliber": "5.56",
			}]))
		fb_id = frappe.db.get_value("FFL Acquisition",
			{"purchase_receipt": res["name"], "docstatus": 1}, "fastbound_id")
		self.assertEqual(fb_id, "fb-recv-ok")

	def test_receive_aborts_when_fastbound_rejects(self):
		"""A FastBound rejection raises so create_receive fails — the gun must not
		be received (atomic: in both systems or neither)."""
		serial = f"REJECT-{self._testMethodName}"
		with patch.object(FastBoundClient, "from_settings") as factory:
			factory.return_value.post_acquisition.side_effect = FastBoundError(
				"bad contact", status_code=400)
			with self.assertRaises(frappe.ValidationError) as ctx:
				create_receive(self._payload(items=[{
					"item_code": "RG-FIREARM", "qty": 1, "rate": 300,
					"serials": [serial],
					"manufacturer": "Colt", "model_name": "AR", "firearm_type": "Rifle",
					"caliber": "5.56",
				}]))
		self.assertIn("FastBound", str(ctx.exception))

	def test_individual_receive_requires_customer(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._indiv_payload(self._testMethodName, seller_customer=""))
		self.assertIn("ustomer", str(ctx.exception))


class SellerPayment(ReceiveGoodsFixtures):
	"""Ammo bought from a private seller for cash, and nothing recorded
	the cash. An individual receive now books how the seller was paid, in its own transaction, before the bound-book
	push. The booking rules (accounts, once per receipt, undo) are in test_goods_payout."""

	def _ammo(self, qty=2, rate=10):
		return {"item_code": "RG-AMMO", "qty": qty, "rate": rate}

	def _gun(self, serial, rate=250):
		return {"item_code": "RG-FIREARM", "qty": 1, "rate": rate, "serials": [serial], "manufacturer": "Colt",
			"model_name": "1911", "firearm_type": "Pistol", "caliber": ".45 ACP"}

	def _paid(self, receipt):
		return frappe.db.get_value("Cash Drawer Entry", {"purchase_receipt": receipt, "status": "Posted"},
			["name", "amount", "payment_method", "reference", "journal_entry", "customer"], as_dict=True)

	def test_an_individual_receive_pays_the_seller_from_the_drawer(self):
		from ffl_core import cash_drawer as cd

		cust = self._ensure_customer("_Test Indiv Seller")
		before = cd.drawer_expected(self.company).expected
		res = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, items=[self._ammo()]))
		paid = self._paid(res["name"])
		self.assertEqual((flt(paid.amount), paid.payment_method, paid.customer), (20.0, "Cash", cust))
		self.assertEqual(res["seller_payment"]["entry"], paid.name)
		self.assertEqual((res["seller_payment"]["amount"], res["seller_payment"]["method"]), (20.0, "Cash"))
		self.assertEqual(cd.drawer_expected(self.company).expected, flt(before - 20, 2))

	def test_the_method_is_required_and_a_bank_one_needs_its_reference(self):
		cust = self._ensure_customer("_Test Indiv Seller")
		receipts = frappe.db.count("Purchase Receipt")
		for method, reference in ((None, None), ("", None), ("Venmo", None), ("Zelle", ""), ("ACH", "  ")):
			with self.assertRaises(frappe.ValidationError, msg=f"{method} {reference!r}"):
				create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, items=[self._ammo()],
					seller_payment_method=method, seller_payment_reference=reference))
		self.assertEqual(frappe.db.count("Purchase Receipt"), receipts)  # refused before anything was written
		res = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, items=[self._ammo()],
			seller_payment_method="Check", seller_payment_reference=" CHK-9 "))
		paid = self._paid(res["name"])
		self.assertEqual((paid.payment_method, paid.reference), ("Check", "CHK-9"))

	def test_a_gun_and_ammo_from_one_seller_are_paid_once(self):
		from ffl_core import cash_drawer as cd

		cust = self._ensure_customer("_Test Indiv Seller")
		res = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust,
			items=[self._gun(f"PAY-{self._testMethodName}"), self._ammo()]))
		self.assertEqual((res["seller_payment"]["amount"], res["seller_payment"]["excluded"]), (270.0, []))
		acq = frappe.db.get_value("FFL Acquisition", {"purchase_receipt": res["name"], "docstatus": 1}, "name")
		with self.assertRaises(frappe.ValidationError):  # the gun is not paid again through its acquisition
			cd.record_payout(acq, "Cash", replace=True)

	def test_an_individual_type_gunbroker_buy_is_paid_by_check_from_the_bank(self):
		"""Type "Individual": a gun bought from a private
		seller and paid by check. It is paid with the receipt; the drawer does not move."""
		from ffl_core import cash_drawer as cd

		cust = self._ensure_customer("_Test Indiv Seller")
		before = cd.drawer_expected(self.company).expected
		res = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, acquisition_type="Individual",
			seller_payment_method="Check", seller_payment_reference="1042",
			items=[self._gun(f"GB-{self._testMethodName}", rate=650.00)]))
		paid = self._paid(res["name"])
		self.assertEqual((flt(paid.amount), paid.payment_method, paid.reference), (650.00, "Check", "1042"))
		self.assertEqual(frappe.db.get_value("Cash Drawer Entry", paid.name, "credit_account"), cd.bank_account(self.company))
		self.assertEqual(cd.drawer_expected(self.company).expected, before)

	def test_each_exempt_receipt_needs_no_method_pays_nobody_and_receives_as_before(self):
		"""Consignment (paid when it sells), Gunsmithing (the customer's gun), a transfer (by its type, with its pickup
		customer), a customer-custody receipt, an unknown type: received exactly as before, no method asked, nothing
		booked to the drawer or the bank -- even when the lines carry a cost."""
		from ffl_core.setup import consignment

		consignment.ensure()
		cust = self._ensure_customer("_Test Indiv Seller")
		cases = {
			"Consignment": {"acquisition_type": "Consignment"},
			"Gunsmithing": {"acquisition_type": "Gunsmithing"},
			"FFL Transfer": {"acquisition_type": "FFL Transfer", "transfer_for_customer": cust},
			"custody": {"is_ffl_transfer": 1},
			"Return": {"acquisition_type": "Return"},
		}
		for n, (what, extra) in enumerate(cases.items()):
			with self.subTest(what):
				serial = f"EXEMPT-{n}-{self._testMethodName}"[:40]
				res = create_receive(self._indiv_payload(f"{self._testMethodName}-{n}", seller_customer=cust,
					seller_payment_method=None, items=[self._gun(serial, rate=275)], **extra))
				self.assertIsNone(res["seller_payment"])
				self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"purchase_receipt": res["name"]}))
				self.assertTrue(frappe.db.exists("FFL Acquisition", {"purchase_receipt": res["name"], "docstatus": 1}))
		self.assertEqual(frappe.db.get_value("Serial No", f"EXEMPT-0-{self._testMethodName}"[:40], "is_consignment"), 1)

	def test_a_pay_on_sale_dealer_purchase_stays_pay_on_sale(self):
		"""A pay-on-sale supplier's gun parks its cost in SRBNB until it sells (compliance/pay_on_sale.py): no payment
		at receipt, and the stamp the sale settles from is still written."""
		_ensure_supplier("RG-POS-FFL", is_ffl_dealer=1, ffl_number="1-77-777-77-7X-77790",
			ez_check_status="Verified", pay_on_sale=1)
		serial = f"POSALE-{self._testMethodName}"[:40]
		res = create_receive(self._payload(supplier="RG-POS-FFL", seller_payment_method="Cash",
			items=[self._gun(serial, rate=450.00)]))
		self.assertIsNone(res["seller_payment"])
		self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"purchase_receipt": res["name"]}))
		self.assertEqual(int(frappe.db.get_value("Serial No", serial, "pay_on_sale") or 0), 1)

	def test_nobody_is_paid_for_a_consignment_a_dealer_purchase_or_free_goods(self):
		cust = self._ensure_customer("_Test Consignor Bob")
		from ffl_core.setup import consignment
		consignment.ensure()
		consigned = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust,
			acquisition_type="Consignment", seller_payment_method=None,
			items=[{**self._gun(f"CONS-PAY-{self._testMethodName}"), "rate": 0}]))
		dealer = create_receive(self._payload(items=[self._ammo()], seller_payment_method="Cash"))
		free = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, seller_payment_method=None,
			items=[{**self._ammo(qty=1, rate=0), "zero_cost_confirmed": 1, "zero_cost_reason": "Sample"}]))
		for res in (consigned, dealer, free):
			self.assertIsNone(res["seller_payment"], res["name"])
			self.assertFalse(frappe.db.exists("Cash Drawer Entry", {"purchase_receipt": res["name"]}))

	def test_a_refused_payment_stops_the_receive_before_the_bound_book(self):
		"""The payment is checked and booked before the synchronous FastBound push: when it is refused, nothing reaches
		the bound book."""
		cust = self._ensure_customer("_Test Indiv Seller")
		with patch.object(FastBoundClient, "from_settings") as factory, patch(
				"ffl_core.cash_drawer._refuse_goods_payout", side_effect=frappe.ValidationError("frozen period")):
			factory.return_value.post_acquisition.return_value = {"acquisitionId": "fb-never", "items": []}
			with self.assertRaises(frappe.ValidationError):
				create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust,
					items=[self._gun(f"STOP-{self._testMethodName}")]))
			factory.return_value.post_acquisition.assert_not_called()

	def test_r2_7_the_payment_is_booked_before_the_push_and_nothing_fails_after_it(self):
		"""The payment (and the drawer lock it takes, with a bounded wait) comes BEFORE the FastBound push,
		so a busy drawer refuses while nothing has reached the bound book, and no step that can fail runs after the push.
		A push that fails propagates out of the receive with nothing committed (the request rolls back the payment with
		the receipt)."""
		from ffl_core import cash_drawer as cd

		cust = self._ensure_customer("_Test Indiv Seller")
		order = []
		real_record, real_lock_drawer = cd.record_goods_payout, cd.lock_drawer
		receipts = lambda: frappe.db.count("Purchase Receipt", {"ffl_seller_customer": cust})
		before = receipts()
		# The drawer lock comes first, before the receipt writes (and locks) any stock row -- the same
		# order as a count, so the two never deadlock -- then the payment, then the push.
		with patch.object(FastBoundClient, "from_settings") as factory, patch(
				"ffl_core.cash_drawer.lock_drawer", side_effect=lambda company, wait=None: (wait and order.append(
					("lock", wait, receipts() - before))) or real_lock_drawer(company, wait)), patch(
				"ffl_core.cash_drawer.record_goods_payout", side_effect=lambda *a, **k: order.append(("pay", None, None))
				or real_record(*a, **k)):
			factory.return_value.post_acquisition.side_effect = lambda *a, **k: order.append(("push", None, None)) or {
				"acquisitionId": "fb-order", "items": []}
			create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust,
				items=[self._gun(f"ORD-{self._testMethodName}")]))
			self.assertEqual(order, [("lock", 5, 0), ("pay", None, None), ("push", None, None)])
		real_lock = cd.lock_drawer
		busy = lambda company, wait=None: (_ for _ in ()).throw(frappe.ValidationError("drawer busy")) if wait \
			else real_lock(company, wait)
		with patch.object(FastBoundClient, "from_settings") as factory, patch("ffl_core.cash_drawer.lock_drawer",
				side_effect=busy):
			with self.assertRaises(frappe.ValidationError):
				create_receive(self._indiv_payload(f"{self._testMethodName}-2", seller_customer=cust,
					items=[self._gun(f"ORD2-{self._testMethodName}")]))
			factory.return_value.post_acquisition.assert_not_called()
		with patch.object(FastBoundClient, "from_settings") as factory, patch.object(frappe.db, "commit") as commit:
			factory.return_value.post_acquisition.side_effect = FastBoundError("bad contact", status_code=400)
			with self.assertRaises(frappe.ValidationError):
				create_receive(self._indiv_payload(f"{self._testMethodName}-3", seller_customer=cust,
					items=[self._gun(f"ORD3-{self._testMethodName}")]))
			commit.assert_not_called()

	def test_r3_7_a_booked_amount_other_than_the_dialogs_stops_the_receive(self):
		"""The receive books only the amount the dialog showed; anything else is refused before the push,
		with nothing saved."""
		from types import SimpleNamespace

		from ffl_core import cash_drawer as cd

		cust = self._ensure_customer("_Test Indiv Seller")
		real = cd.seller_payment_at_receipt
		off = lambda *a, **k: SimpleNamespace(pays=real(*a, **k).pays, amount=real(*a, **k).amount - 1)
		with patch.object(FastBoundClient, "from_settings") as factory, patch(
				"ffl_core.cash_drawer.seller_payment_at_receipt", side_effect=off):
			factory.return_value.post_acquisition.return_value = {"acquisitionId": "fb-never", "items": []}
			with self.assertRaises(frappe.ValidationError):
				create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust,
					items=[self._gun(f"AMT-{self._testMethodName}")]))
			factory.return_value.post_acquisition.assert_not_called()

	def test_r2_7_the_drawer_lock_waits_a_bounded_time(self):
		from ffl_core import cash_drawer as cd

		cd.lock_drawer(self.company, wait=5)  # free: taken at once
		calls = []
		with patch.object(frappe.db, "sql", side_effect=lambda *a, **k: calls.append(a[0])):
			cd.lock_drawer(self.company, wait=5)
		self.assertTrue(calls and "FOR UPDATE WAIT 5" in calls[0])

	def test_r2_1_a_gun_bought_back_from_someone_else_is_paid_in_full(self):
		"""Bought from one person and paid through record_payout, sold, then bought back from
		someone else on Receive Goods. Before the receipt exists nothing on it is paid: the dialog asks for the full
		price, create_receive takes the same decision, and the receipt books that amount."""
		from frappe.utils import now_datetime

		from ffl_core import cash_drawer as cd
		from ffl_core.acquisition import record_firearm_acquisition
		from ffl_core.api.receive_goods import seller_payment_preview
		from ffl_core.tests.test_cash_drawer_pos import legacy_payout

		serial = f"BACK-{self._testMethodName}"[:40]
		if not frappe.db.exists("Serial No", serial):
			frappe.get_doc({"doctype": "Serial No", "serial_no": serial, "item_code": "RG-FIREARM"}).insert(
				ignore_permissions=True)
		first_seller = self._ensure_customer("_Test First Seller")
		first = record_firearm_acquisition(item_code="RG-FIREARM", serial=serial, source_type="Trade-in",
			source_name=first_seller, source_customer=first_seller, cost=180, company=self.company,
			source_id_type="Driver License", source_id_number="DL-FIRST")
		legacy_payout(first, "Cash")
		sold = frappe.get_doc({"doctype": "FFL Disposition", "disposition_date": now_datetime(),
			"disposition_type": "Retail Sale", "customer": first_seller, "company": self.company, "item": "RG-FIREARM",
			"serial": serial, "form_4473_status": "Completed", "nics_status": "Proceed", "fastbound_id": f"FB-{serial}"})
		sold.insert(ignore_permissions=True)
		sold.submit()
		second = self._ensure_customer("_Test Second Seller")
		payload = self._indiv_payload(self._testMethodName, seller_customer=second, items=[self._gun(serial, rate=240)])
		preview = seller_payment_preview(payload)
		self.assertEqual((preview["pays"], preview["amount"]), (True, 240.0))
		res = create_receive(payload)
		self.assertEqual(res["seller_payment"]["amount"], 240.0)
		self.assertNotEqual(frappe.db.get_value("FFL Acquisition", {"purchase_receipt": res["name"]}, "name"), first)

	def test_r2_2_a_missing_payment_names_the_fields_to_send(self):
		cust = self._ensure_customer("_Test Indiv Seller")
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, items=[self._ammo()],
				seller_payment_method=None))
		self.assertIn("seller_payment_method", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, items=[self._ammo()],
				seller_payment_method="Zelle"))
		self.assertIn("seller_payment_reference", str(ctx.exception))

	def test_the_dialog_shows_what_the_receipt_books(self):
		"""seller_payment_preview answers with the server's rule, amount and ways to pay; the receipt
		then books exactly that amount."""
		from ffl_core import cash_drawer as cd
		from ffl_core.api.receive_goods import seller_payment_preview

		cust = self._ensure_customer("_Test Indiv Seller")
		items = [self._gun(f"PRE-{self._testMethodName}", rate=250.55), self._ammo(qty=3, rate=0.35)]
		payload = self._indiv_payload(self._testMethodName, seller_customer=cust, items=items)
		preview = seller_payment_preview(payload)
		self.assertEqual((preview["pays"], preview["amount"], preview["methods"]), (True, 251.6, list(cd.PAYOUT_METHODS)))
		self.assertEqual(create_receive(payload)["seller_payment"]["amount"], preview["amount"])
		for what, over in (("Consignment", {"acquisition_type": "Consignment"}), ("Gunsmithing", {"acquisition_type": "Gunsmithing"}),
				("dealer", {"acquisition_source": "FFL Dealer"}), ("free", {"items": [{**self._ammo(qty=1, rate=0)}]})):
			with self.subTest(what):
				self.assertFalse(seller_payment_preview(self._indiv_payload(f"{self._testMethodName}-x", seller_customer=cust,
					**{"items": items, **over}))["pays"])

	def test_receiving_the_same_goods_again_after_a_cancel_is_refused(self):
		"""The first receipt was cancelled but its seller payment stands; receiving the same goods (same
		seller, same amount) again is refused before anything reaches the bound book, another amount goes through. Its
		own seller: this class does not roll back between tests, and the standing payment must not meet the others."""
		cust = self._ensure_customer("_Test Indiv Reseller")
		first = create_receive(self._indiv_payload(self._testMethodName, seller_customer=cust, items=[self._ammo()]))
		pr = frappe.get_doc("Purchase Receipt", first["name"])
		pr.cancel()
		with self.assertRaises(frappe.ValidationError):
			create_receive(self._indiv_payload(f"{self._testMethodName}-2", seller_customer=cust, items=[self._ammo()]))
		other = create_receive(self._indiv_payload(f"{self._testMethodName}-3", seller_customer=cust,
			items=[self._ammo(qty=3)]))
		self.assertEqual(other["seller_payment"]["amount"], 30.0)


class ZeroCostGuard(ReceiveGoodsFixtures):
	"""A $0 line must state WHICH kind of $0 it is.

	`rate = 0.0 if is_custody else float(line.get("rate") or 0)` collapsed two
	different facts into one number — "I didn't fill it in" (None) and "I
	confirm it was free" — and once written they can never be told apart. That
	is why 11 real purchases went in at $0 across three months and why every
	later audit had to ask the owner gun by gun.

	The two legitimately-free kinds keep working untouched (customer custody,
	consignor-owned stock); a real purchase must now either carry a cost or say
	out loud that it was free, with a reason.
	"""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		# A SKU with NO purchase history — the supplier-gift-of-a-new-model case.
		# Kept apart from RG-FIREARM (which every priced test buys at $500) so the
		# zero-valuation path is exercised on an Item that genuinely has no rate
		# anywhere for ERPNext to fall back on.
		_ensure_item("RG-FIREARM-FREE", item_group="Rifle", has_serial_no=1)

	def _gun(self, serial, **over):
		line = {"item_code": "RG-FIREARM", "qty": 1, "serials": [serial],
			"manufacturer": "Colt", "model_name": "1911", "caliber": "45ACP",
			"firearm_type": "Pistol"}
		line.update(over)
		return line

	# ---------------------------------------------------------------- refused

	def test_a_purchase_with_no_cost_at_all_is_refused(self):
		"""No `rate` key at all — the payload shape an API/MCP caller sends, and
		(since the bridge stopped inventing 0) a distributor line whose unit cost
		nobody captured. The receive page cannot produce it: an empty cost box is
		submitted as 0, which the next test covers."""
		payload = self._payload(items=[self._gun("RG-ZC-1")])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("RG-ZC-1", str(ctx.exception))

	def test_a_purchase_with_an_unconfirmed_zero_cost_is_refused(self):
		"""Typing 0 is not the same as confirming it was free. THIS is the exact
		shape of the 11 bad receipts — they came from the page, which always
		sends a number."""
		payload = self._payload(items=[self._gun("RG-ZC-2", rate=0)])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("RG-ZC-2", str(ctx.exception))

	def test_a_blank_cost_string_is_refused_not_parsed(self):
		"""A whitespace-only cost is "nobody filled it in", not zero — and it must
		not reach float() and 500 the endpoint."""
		payload = self._payload(items=[self._gun("RG-ZC-BLANK", rate="   ")])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("RG-ZC-BLANK", str(ctx.exception))

	def test_an_unparseable_cost_is_refused_with_a_message_not_a_500(self):
		"""The function's whole job is a friendly refusal; a typed "$500" must
		not become an uncaught ValueError. flt() reads it as 0, which lands on
		the confirm-or-enter-a-cost arm — refused, named, no traceback."""
		payload = self._payload(items=[self._gun("RG-ZC-TYPO", rate="$500")])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("RG-ZC-TYPO", str(ctx.exception))

	def test_confirmed_zero_cost_without_a_reason_is_refused(self):
		"""The reason is the whole point — it is what a later audit reads."""
		payload = self._payload(items=[
			self._gun("RG-ZC-3", rate=0, zero_cost_confirmed=1)])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("reason", str(ctx.exception).lower())

	def test_an_unknown_reason_is_refused(self):
		"""The Select is a vocabulary, not free text: a caller that invents a
		reason gets refused rather than storing something no report groups by."""
		payload = self._payload(items=[self._gun(
			"RG-ZC-BADREASON", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Felt like it")])
		with self.assertRaises(frappe.ValidationError):
			create_receive(payload)

	def test_other_without_a_written_explanation_is_refused(self):
		""""Other" is the hatch for a case nobody foresaw, not a way to
		skip the question. Stored alone it tells a later audit exactly as much as
		the empty cost this guard exists to kill."""
		payload = self._payload(items=[self._gun(
			"RG-ZC-OTHER-BARE", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Other")])
		with self.assertRaises(frappe.ValidationError) as ctx:
			create_receive(payload)
		self.assertIn("RG-ZC-OTHER-BARE", str(ctx.exception))

	def test_a_stringy_falsy_confirmation_does_not_open_the_hatch(self):
		"""JSON sends "0" and "false" as strings, and both are truthy in Python.
		This is the guard's own bypass switch — it takes cint, not truthiness."""
		for value in ("0", "false"):
			with self.subTest(value=value):
				payload = self._payload(items=[self._gun(
					f"RG-ZC-STR-{value}", rate=0, zero_cost_confirmed=value,
					zero_cost_reason="Supplier gift")])
				with self.assertRaises(frappe.ValidationError):
					create_receive(payload)

	def test_a_cost_and_a_free_tick_together_are_refused(self):
		"""They contradict each other; trust neither."""
		payload = self._payload(items=[self._gun(
			"RG-ZC-BOTH", rate=500, zero_cost_confirmed=1,
			zero_cost_reason="Supplier gift")])
		with self.assertRaises(frappe.ValidationError):
			create_receive(payload)

	def test_non_firearm_lines_are_guarded_too(self):
		"""A $0 accessory is the same trap; the rule is not firearm-only."""
		payload = self._payload(items=[{"item_code": "RG-AMMO", "qty": 5, "rate": 0}])
		with self.assertRaises(frappe.ValidationError):
			create_receive(payload)

	# ---------------------------------------------------------------- allowed

	def test_confirmed_zero_cost_with_a_reason_is_accepted_and_recorded(self):
		"""The escape hatch, and it must leave a queryable fact behind — not a
		comment someone has to read. This is what lets a later stock-cost audit
		skip the gun instead of asking the owner about it again."""
		payload = self._payload(items=[self._gun(
			"RG-ZC-OK", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Supplier gift")])
		res = create_receive(payload)
		sn = frappe.db.get_value("Serial No", "RG-ZC-OK",
			["zero_cost_confirmed", "zero_cost_reason", "purchase_rate"], as_dict=True)
		self.assertTrue(sn.zero_cost_confirmed)
		self.assertEqual(sn.zero_cost_reason, "Supplier gift")
		# The gun's recorded cost really is 0 — i.e. ERPNext did not re-derive a
		# rate from the price list / last purchase rate behind the confirmation.
		# (This asserts the OUTCOME, not the explicit stamp in
		# compliance/purchase.py: purchase_rate is a NOT NULL column, so an
		# unstamped row reads 0 too. The stamp earns its place on amendments,
		# where a previously-priced line must not keep its old cost.)
		self.assertEqual(flt(sn.purchase_rate), 0.0)
		# ...and the receipt row carries the same trio as the audit source (the
		# only home a line with no Serial No — ammo, accessories — would have).
		row = frappe.get_all("Purchase Receipt Item",
			filters={"parent": res["name"]},
			fields=["rate", "allow_zero_valuation_rate", "is_free_item",
				"ffl_zero_cost_confirmed", "ffl_zero_cost_reason"])[0]
		self.assertEqual(flt(row.rate), 0.0)
		self.assertTrue(row.ffl_zero_cost_confirmed)
		self.assertEqual(row.ffl_zero_cost_reason, "Supplier gift")
		# The two ERPNext levers a $0 stock row needs, both of them: is_free_item
		# stops the rate being re-derived from the price list, and
		# allow_zero_valuation_rate is what lets a $0 receipt submit at all.
		# test_transfer_custody asserts the identical pair on a custody line.
		self.assertTrue(row.is_free_item)
		self.assertTrue(row.allow_zero_valuation_rate)

	def test_other_with_an_explanation_is_accepted_and_the_note_is_kept(self):
		payload = self._payload(items=[self._gun(
			"RG-ZC-OTHER-OK", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Other",
			zero_cost_note="Range demo unit the rep left behind")])
		res = create_receive(payload)
		sn = frappe.db.get_value("Serial No", "RG-ZC-OTHER-OK",
			["zero_cost_reason", "zero_cost_note"], as_dict=True)
		self.assertEqual(sn.zero_cost_reason, "Other")
		self.assertIn("Range demo", sn.zero_cost_note or "")
		row = frappe.get_all("Purchase Receipt Item", filters={"parent": res["name"]},
			fields=["ffl_zero_cost_note"])[0]
		self.assertIn("Range demo", row.ffl_zero_cost_note or "")

	def test_a_never_purchased_sku_can_be_received_free(self):
		"""The headline case: a supplier gifts a model the store has never bought,
		so there is no valuation rate anywhere to fall back on. Without
		allow_zero_valuation_rate ERPNext refuses the $0 row at submit."""
		res = create_receive(self._payload(items=[self._gun(
			"RG-ZC-NEWSKU", item_code="RG-FIREARM-FREE", rate=0,
			zero_cost_confirmed=1, zero_cost_reason="Supplier gift")]))
		self.assertTrue(res.get("name"))
		self.assertEqual(flt(frappe.db.get_value(
			"Serial No", "RG-ZC-NEWSKU", "purchase_rate")), 0.0)

	def test_confirming_a_zero_cost_leaves_an_audit_trail(self):
		"""An audit RECORD is wanted, not only a field. The flag is written
		with update_modified=False (no Version row), so without this the claim
		"this gun was free" would have no author and no date — the exact hole
		firearm.log_antique_flip was written to fill for the antique flag."""
		create_receive(self._payload(items=[self._gun(
			"RG-ZC-AUDIT", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Warranty replacement")]))
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Serial No", "reference_name": "RG-ZC-AUDIT"},
			fields=["content"])
		self.assertTrue(comments, "no Comment recorded the zero-cost confirmation")
		body = "\n".join(c.content or "" for c in comments)
		self.assertIn("Warranty replacement", body)
		self.assertIn(frappe.session.user, body)

	def test_a_confirmed_free_gun_drops_out_of_the_zero_cost_audit(self):
		"""The point of the whole feature: a $0 nobody can
		account for stays on the sweep, a $0 someone stated on purpose comes off
		it. Without the field wired into custody.audit_legacy_custody_candidates
		the confirmation would be a fact only code that knows the new column can
		see, and the human would be asked about this gun again at every audit."""
		from ffl_core.custody import audit_legacy_custody_candidates
		create_receive(self._payload(items=[self._gun(
			"RG-ZC-SWEEP", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Promotional item")]))
		flagged = {r["serial"] for r in audit_legacy_custody_candidates()["zero_cost"]}
		self.assertNotIn("RG-ZC-SWEEP", flagged)

	# ------------------------------------------------- counter-examples (regression)

	def test_customer_custody_still_receives_at_zero_without_any_confirmation(self):
		"""Transfer-in guns are forced to $0 by the server and must NOT be asked
		to confirm anything — the store never bought them."""
		cust = self._ensure_customer("_Test ZC Custody")
		payload = self._payload(is_ffl_transfer=1, transfer_for_customer=cust,
			items=[self._gun("RG-ZC-CUSTODY")])
		create_receive(payload)
		self.assertEqual(flt(frappe.db.get_value(
			"Serial No", "RG-ZC-CUSTODY", "purchase_rate")), 0.0)
		# A custody gun is $0 by force, never by confirmation — it must not be
		# marked as an asserted free acquisition (nor skipped by the sweep on
		# that basis; is_transfer_gun is what excludes it).
		self.assertFalse(frappe.db.get_value(
			"Serial No", "RG-ZC-CUSTODY", "zero_cost_confirmed"))

	def test_consignor_owned_stock_still_receives_at_zero(self):
		"""A consignment line is legitimately $0 and must stay submittable.
		This is the rule the original code declined to add a blanket cost>0 for —
		it must survive the new guard."""
		from ffl_core.setup import consignment
		consignment.ensure()
		cust = self._ensure_customer("_Test ZC Consignor")
		payload = self._indiv_payload(
			self._testMethodName, seller_customer=cust, acquisition_type="Consignment",
			items=[self._gun("RG-ZC-CONSIGN", rate=0)])
		res = create_receive(payload)
		# What this guards is that the guard did NOT fire — a consignor line with
		# rate 0 and no confirmation still receives. (Deliberately not asserting
		# the resulting purchase_rate: that is ERPNext's own valuation fallback,
		# untouched by this change and different on a test Item that carries a
		# last purchase rate.)
		self.assertTrue(res.get("name"))
		sn = frappe.db.get_value("Serial No", "RG-ZC-CONSIGN",
			["is_consignment", "zero_cost_confirmed"], as_dict=True)
		self.assertTrue(sn.is_consignment)
		# ...and it was NOT pushed through the confirmed-free path.
		self.assertFalse(sn.zero_cost_confirmed)

	def test_consignment_is_matched_case_and_whitespace_insensitively(self):
		"""acquisition_type is free-text Data mirroring FastBound's runtime
		SmartList, and an earlier dead `== "transfer"` arm is the standing lesson
		(firearm.py:775). An equality here would refuse a consignor's legitimate
		$0 gun at the door AND leave nobody recorded as owning it — so both
		consumers answer to firearm.is_consignment_acquisition_type."""
		from ffl_core.setup import consignment
		consignment.ensure()
		cust = self._ensure_customer("_Test ZC Consignor Case")
		res = create_receive(self._indiv_payload(
			self._testMethodName, seller_customer=cust,
			acquisition_type="  consignment ",
			items=[self._gun("RG-ZC-CONSIGN-CASE", rate=0)]))
		self.assertTrue(res.get("name"))
		# Both halves must have matched: the guard let the $0 through, and the
		# Purchase Receipt hook stamped whose gun it is.
		self.assertTrue(frappe.db.get_value(
			"Serial No", "RG-ZC-CONSIGN-CASE", "is_consignment"))

	def test_an_ordinary_priced_purchase_is_untouched(self):
		payload = self._payload(items=[self._gun("RG-ZC-PRICED", rate=500)])
		create_receive(payload)
		self.assertEqual(flt(frappe.db.get_value(
			"Serial No", "RG-ZC-PRICED", "purchase_rate")), 500.0)

	def test_a_cost_typed_with_a_thousands_separator_still_receives(self):
		"""flt strips the comma — the receiver typed a real number, and refusing
		it would be the guard getting in the way of the case it exists to protect."""
		create_receive(self._payload(items=[self._gun("RG-ZC-COMMA", rate="1,200")]))
		self.assertEqual(flt(frappe.db.get_value(
			"Serial No", "RG-ZC-COMMA", "purchase_rate")), 1200.0)


if __name__ == "__main__":
	unittest.main()


class ZeroCostStampIsClearable(ReceiveGoodsFixtures):
	"""The stamp must be undoable, and unprovable claims refused.

	Both fields are read_only on both doctypes and carry no no_copy, so if the
	on_submit stamp only ever writes on the truthy branch, one mis-tick leaves a
	$500 gun permanently marked "confirmed free" — hidden from the stock-cost
	sweep, with no form control able to clear it and cancel+amend unable to
	reach it. needs_service and is_transfer_gun in the same function each spell
	out this rule; the trio has to follow it.
	"""

	def _gun(self, serial, **over):
		line = {"item_code": "RG-FIREARM", "qty": 1, "serials": [serial],
			"manufacturer": "Colt", "model_name": "1911", "caliber": "45ACP",
			"firearm_type": "Pistol"}
		line.update(over)
		return line

	def test_an_ordinary_purchase_clears_the_stamp_rather_than_leaving_it(self):
		"""The correction path, end to end: a gun mis-marked "confirmed free" is
		put right by cancelling and amending the receipt WITHOUT the tick.

		Asserting on a fresh serial proves nothing here — an untouched Serial No
		reads 0/None whatever the stamping code does, so the old "write the trio
		only on the truthy branch" would pass too. The gun must therefore carry a
		REAL confirmation first, and the amendment must be shown to erase it AND
		to put the real cost back.
		"""
		serial = "RG-ZC-CLEAR"
		res = create_receive(self._payload(items=[self._gun(
			serial, rate=0, zero_cost_confirmed=1, zero_cost_reason="Supplier gift")]))
		stamped = frappe.db.get_value("Serial No", serial,
			["zero_cost_confirmed", "zero_cost_reason", "purchase_rate"], as_dict=True)
		self.assertTrue(stamped.zero_cost_confirmed, "precondition: the gun is marked free")
		self.assertEqual(stamped.zero_cost_reason, "Supplier gift")
		self.assertEqual(flt(stamped.purchase_rate), 0.0)

		# The operator's correction: cancel, amend, drop the tick, enter the cost.
		original = frappe.get_doc("Purchase Receipt", res["name"])
		original.flags.ignore_permissions = True
		original.cancel()
		# ignore_no_copy=False is what an amendment does: frappe.copy_doc's
		# DEFAULT keeps no_copy fields (its docstring says so out loud), while
		# Desk's amend strips them — parent and child rows alike.
		amended = frappe.copy_doc(original, ignore_no_copy=False)
		amended.amended_from = original.name
		amended.docstatus = 0
		for row in amended.items:
			row.rate = 500.0
			row.is_free_item = 0
			row.allow_zero_valuation_rate = 0
			# ERPNext marks serial_no / serial_and_batch_bundle no_copy too, so an
			# amendment arrives with NO gun on the row and would re-stamp nothing.
			# Re-entering the serial is therefore part of the correction, not an
			# artefact of the test — see setup/zero_cost._fields.
			row.serial_no = serial
			row.use_serial_batch_fields = 1
			# The trio is gone already (no_copy); asserted separately in
			# test_the_amended_receipt_row_does_not_inherit_the_free_marking.
		amended.flags.ignore_permissions = True
		amended.insert()
		amended.submit()

		sn = frappe.db.get_value("Serial No", serial,
			["zero_cost_confirmed", "zero_cost_reason", "zero_cost_note",
			 "purchase_rate"], as_dict=True)
		self.assertEqual(int(sn.zero_cost_confirmed or 0), 0, "the stamp survived the amendment")
		self.assertIsNone(sn.zero_cost_reason)
		self.assertIsNone(sn.zero_cost_note)
		# ...and the real cost is back, so the gun re-enters the stock-cost sweep
		# it had been hidden from.
		self.assertEqual(flt(sn.purchase_rate), 500.0)

	def test_the_amended_receipt_row_does_not_inherit_the_free_marking(self):
		"""no_copy on the three PR Item fields is what makes the correction above
		an OPERATOR-level action: amend must hand back a row with no tick, rather
		than reproducing the mis-tick the operator is trying to undo (the fields
		are read_only, so there is no form control to clear it by hand)."""
		res = create_receive(self._payload(items=[self._gun(
			"RG-ZC-NOCOPY", rate=0, zero_cost_confirmed=1,
			zero_cost_reason="Supplier gift")]))
		original = frappe.get_doc("Purchase Receipt", res["name"])
		original.flags.ignore_permissions = True
		original.cancel()

		# What Desk's amend does: copy WITHOUT the no_copy fields (client side
		# builds the same exclusion list from the docfields — model.js
		# get_no_copy_list). The default copy_doc keeps them, which is why this
		# has to be spelled out rather than left implicit.
		amended = frappe.copy_doc(original, ignore_no_copy=False)

		row = amended.items[0]
		self.assertFalse(row.get("ffl_zero_cost_confirmed"))
		self.assertFalse(row.get("ffl_zero_cost_reason"))
		self.assertFalse(row.get("ffl_zero_cost_note"))

	def _hand_built_receipt(self, serial, *, rate, confirmed=1,
			reason="Supplier gift", supplier="RG-ACME-FFL"):
		"""A Purchase Receipt built WITHOUT the receive API — the REST / import /
		script path the API-door guard cannot see. This is the only way to
		produce the contradictory row the on_submit invariant exists to catch."""
		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"ffl_source": "Distributor",
			"set_warehouse": self.warehouse,
			"items": [{
				"item_code": "RG-FIREARM", "qty": 1, "rate": rate,
				"warehouse": self.warehouse,
				"serial_no": serial, "use_serial_batch_fields": 1,
				"ffl_zero_cost_confirmed": 1 if confirmed else 0,
				"ffl_zero_cost_reason": reason,
			}],
		})
		pr.flags.ignore_permissions = True
		pr.insert()
		pr.submit()
		return pr

	def test_a_row_claiming_zero_cost_while_carrying_a_real_rate_is_refused(self):
		"""confirmed=1 AND rate>0 is a contradiction the API door already refuses,
		but a hand-built receipt walks straight past that door. By on_submit the
		number has settled (the very next branch stamps purchase_rate from it, and
		ERPNext has already zeroed any genuine is_free_item row), so the two facts
		can be compared here — and disagreeing means one of them is a lie."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._hand_built_receipt("RG-ZC-CONTRA", rate=500.0)
		message = str(ctx.exception)
		self.assertIn("RG-ZC-CONTRA", message)  # names the gun, not just the item
		self.assertIn("500", message)           # ...and the cost it contradicts

	def test_a_genuinely_free_hand_built_receipt_still_submits(self):
		"""The invariant must not cost the legitimate path anything: the same
		hand-built shape with a real $0 goes through and stamps as usual."""
		self._hand_built_receipt("RG-ZC-CONTRA-OK", rate=0.0)
		sn = frappe.db.get_value("Serial No", "RG-ZC-CONTRA-OK",
			["zero_cost_confirmed", "zero_cost_reason", "purchase_rate"], as_dict=True)
		self.assertTrue(sn.zero_cost_confirmed)
		self.assertEqual(sn.zero_cost_reason, "Supplier gift")
		self.assertEqual(flt(sn.purchase_rate), 0.0)

	def test_the_contradiction_can_never_reach_the_pay_on_sale_stamp(self):
		"""Why the invariant is worth a throw rather than a log line.

		The pay-on-sale parking block a few lines below keys off `row.rate > 0`,
		while the zero-cost block keys off the tick. Left alone, one row would
		stamp BOTH `purchase_rate = 0` ("free") and `srbnb_cost = 500` ("I owe the
		supplier 500") onto the same gun — and `zero_cost_confirmed = 1` drops it
		out of the stock-cost sweep that would have caught it. Selling it then
		books a real 500 payable against a gun the books call free.
		"""
		_ensure_supplier("RG-POS-FFL", is_ffl_dealer=1,
			ffl_number="1-77-777-77-7X-77780", ez_check_status="Verified")
		frappe.db.set_value("Supplier", "RG-POS-FFL", "pay_on_sale", 1,
			update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			self._hand_built_receipt("RG-ZC-POS", rate=500.0, supplier="RG-POS-FFL")
		# Nothing was stamped: no half-written gun carrying both stories.
		sn = frappe.db.get_value("Serial No", "RG-ZC-POS",
			["zero_cost_confirmed", "purchase_rate", "srbnb_cost", "pay_on_sale"],
			as_dict=True)
		if sn:  # the rollback may or may not leave the Serial No behind
			self.assertFalse(flt(sn.srbnb_cost))
			self.assertFalse(int(sn.pay_on_sale or 0))

	def test_a_receipt_claiming_zero_cost_without_a_reason_is_refused(self):
		"""The invariant now also lives at the Purchase Receipt door, because the
		stamp is copied FROM THE ROW — a hand-built receipt never passes the
		receive API. Narrow on purpose: only ticked rows are constrained."""
		from ffl_core.compliance.purchase import validate_zero_cost_confirmation
		doc = frappe._dict(items=[frappe._dict({
			"item_code": "RG-FIREARM", "idx": 1, "rate": 0,
			"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": None,
			"ffl_zero_cost_note": None})])
		with self.assertRaises(frappe.ValidationError):
			validate_zero_cost_confirmation(doc)

	def test_a_receipt_whose_reason_is_off_the_list_is_refused(self):
		"""The receipt door enforces the SAME vocabulary as the API door — a
		hand-built receipt cannot invent its own reason. (The API side is covered
		by test_an_unknown_reason_is_refused; this is the other function.)"""
		from ffl_core.compliance.purchase import validate_zero_cost_confirmation
		doc = frappe._dict(items=[frappe._dict({
			"item_code": "RG-FIREARM", "idx": 1, "rate": 0,
			"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": "Because I said so",
			"ffl_zero_cost_note": None})])
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_zero_cost_confirmation(doc)
		self.assertIn("Because I said so", str(ctx.exception))

	def test_a_receipt_choosing_other_without_an_explanation_is_refused(self):
		"""`Other` is the escape hatch from the vocabulary, so it has to carry the
		words that replace it — otherwise it IS the un-auditable $0 the whole
		feature exists to end."""
		from ffl_core.compliance.purchase import validate_zero_cost_confirmation
		from ffl_core.setup.zero_cost import OTHER_REASON
		doc = frappe._dict(items=[frappe._dict({
			"item_code": "RG-FIREARM", "idx": 1, "rate": 0,
			"ffl_zero_cost_confirmed": 1, "ffl_zero_cost_reason": OTHER_REASON,
			"ffl_zero_cost_note": "   "})])
		with self.assertRaises(frappe.ValidationError):
			validate_zero_cost_confirmation(doc)

	def test_an_untouched_row_is_not_constrained_at_all(self):
		"""The counter-example that keeps the door narrow: a row with no tick is
		none of this check's business, which is why no other receive path
		(4473 / custody / trade-in / distributor / backfill) needs an exemption."""
		from ffl_core.compliance.purchase import validate_zero_cost_confirmation
		doc = frappe._dict(items=[frappe._dict({
			"item_code": "RG-FIREARM", "idx": 1, "rate": 0,
			"ffl_zero_cost_confirmed": 0})])
		validate_zero_cost_confirmation(doc)  # must not raise
