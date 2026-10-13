"""Local catalog: the Sports South driver. The parsers and the classification are pure
and tested here; the lanes follow later in this file. Feeds are synthetic, from
tests/ss_synth.py.
"""
from __future__ import annotations

import inspect
import os
import tempfile
import tracemalloc
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.catalog_contract import check
from ffl_integrations.catalog_contract.transports import StubTransport
from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.backend import LocalCatalog
from ffl_integrations.catalog_local.drivers import rsr, ss, ss_feed
from ffl_integrations.catalog_local.sync import FeedAuthError, FeedFormatError, FeedUnavailable
from ffl_integrations.distributor.util import PRODUCT_CLASSES
from ffl_integrations.sports_south.api_client import DEFAULT_BASE_URL
from ffl_integrations.tests import catalog_stub, ss_synth
from ffl_integrations.tests.ss_synth import (
	FakeSSTransport,
	categories_text,
	daily_items,
	dataset,
	envelope,
	error_payload,
	onhand_full,
	onhand_incremental,
	record,
	servertime,
	sku,
	standard_feed,
	text_rows,
	upc,
	write_daily_items,
)


def _write(text: str) -> str:
	fd, path = tempfile.mkstemp(prefix="ss-test-", suffix=".xml")
	with os.fdopen(fd, "w", encoding="utf-8") as f:
		f.write(text)
	return path


def _rows(text: str, **kw):
	path = _write(text)
	return path, list(ss_feed.iter_catalog(path, brands=kw.pop("brands", {}),
		department_names=kw.pop("department_names", {}), caliber_slots=kw.pop("caliber_slots", {}), **kw))


# ---- parsers (pure)


class TestSsFeedParsers(unittest.TestCase):

	def test_error_payloads_are_refused_before_parsing(self):
		path = _write(error_payload("USER NOT AUTHENTICATED!"))
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedAuthError):
			ss_feed.assert_no_error(path)

		path = _write(error_payload("Authentication Failed For User", table1=False))
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedAuthError):
			ss_feed.assert_no_error(path)

		path = _write(error_payload("SOMETHING BROKE"))
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedFormatError) as ctx:
			ss_feed.assert_no_error(path)
		self.assertNotIsInstance(ctx.exception, FeedAuthError)

		path = _write(daily_items([1]))
		self.addCleanup(os.unlink, path)
		ss_feed.assert_no_error(path)

	def test_unwrap_survives_illegal_char_refs_and_inner_entities(self):
		# The inner text carries an XML-illegal reference and an escaped ampersand. The envelope
		# escapes it once more on the wire, so the wire carries the double-escaped form.
		path = _write(daily_items([1], over={1: {"SHDESC": "Ti&#x2;REX 500 S&amp;W"}}))
		self.addCleanup(os.unlink, path)
		rows = list(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={}))
		self.assertEqual(rows[0]["short_description"], "TiREX 500 S&W")

	def test_unwrap_streams_across_chunk_boundaries(self):
		pad = "X" * ((1 << 20) - 200)
		path = _write(daily_items([1, 2], over={1: {"SHDESC": pad + "A&amp;B"}}))
		self.addCleanup(os.unlink, path)
		rows = list(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={}))
		self.assertEqual(len(rows), 2)
		self.assertTrue(rows[0]["short_description"].endswith("A&B"))

	def test_a_truncated_document_raises(self):
		doc = daily_items([1, 2])
		path = _write(doc[: len(doc) // 2])
		self.addCleanup(os.unlink, path)
		with self.assertRaises(ET.ParseError):
			list(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={}))

	def test_catalog_row_maps_prices_policy_flags_and_department(self):
		cats = _write(categories_text())
		self.addCleanup(os.unlink, cats)
		names, slots = ss_feed.parse_categories(cats)
		self.assertEqual(names["26"], "TEST HANDGUNS (TEST FIREARMS)")
		# n=3 is ammo (category 5) with a manufacturer-minimum price and a hazmat flag.
		path, rows = _rows(daily_items([1, 2, 3], over={2: {"MFPRTYP": "U"}}),
			brands={"9001": "Acme Test Arms"}, department_names=names, caliber_slots=slots)
		self.addCleanup(os.unlink, path)
		ammo = rows[2]
		self.assertEqual(ammo["cost"], 103.5)
		self.assertEqual(ammo["map"], 153.0)
		self.assertEqual(ammo["msrp"], 0.0)
		self.assertEqual(ammo["map_policy"], "M")
		self.assertTrue(ammo["ships_ground_only"])
		self.assertEqual(rows[1]["msrp"], 152.0)
		self.assertEqual(rows[1]["map"], 0.0)
		self.assertEqual(ammo["manufacturer"], "Acme Test Arms")
		first = rows[0]
		self.assertEqual(first["department"], "26")
		self.assertEqual(first["department_name"], "TEST HANDGUNS (TEST FIREARMS)")
		self.assertEqual(first["caliber"], "9MM")
		self.assertTrue(first["image_url"].endswith(f"/large/{sku(1)}.jpg"))
		self.assertNotIn("long_description", first)
		self.assertEqual(first["product_class"], "Handgun")

	def test_ffl_comes_from_the_item_type_and_black_powder_guns_stay_ffl(self):
		# The Data Service releases a confirmed antique from a list it keeps. There is no such
		# list here, so every black-powder gun stays FFL-required: a deliberate, stricter deviation.
		path, rows = _rows(daily_items([1, 2, 3], over={
			1: {"ITYPE": "1", "CATID": "13"}, 2: {"ITYPE": "2"}, 3: {"ITYPE": "3"}}))
		self.addCleanup(os.unlink, path)
		self.assertEqual([r["ffl_required"] for r in rows], [True, True, False])

	def test_rejected_rows_are_counted_kept_present_and_gated(self):
		text = daily_items([1, 2, 3], over={2: {"CPRC": ""}, 3: {"ITUPC": None}})
		path = _write(text)
		self.addCleanup(os.unlink, path)
		stats, present = {}, set()
		rows = list(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={},
			stats=stats, present=present))
		self.assertEqual((stats["total"], stats["rejected"]), (3, 2))
		self.assertEqual(present, {sku(1), sku(2), sku(3)})
		self.assertEqual(len(rows), 1)

		text = daily_items(range(1, 6), over={1: {"CPRC": ""}, 2: {"ITUPC": None}})
		path = _write(text)
		self.addCleanup(os.unlink, path)
		with patch.object(ss_feed, "REJECT_RATIO_FLOOR", 4):
			with self.assertRaises(FeedFormatError):
				list(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={}))

	def test_categories_and_brands(self):
		cats = _write(categories_text())
		self.addCleanup(os.unlink, cats)
		names, slots = ss_feed.parse_categories(cats)
		self.assertEqual(slots["26"], ("ITATR2",))
		self.assertNotIn("28", slots)
		# A category with a blank DEP falls back to its DEPID in the name.
		blank = envelope(dataset(record("Table", {"CATID": "77", "CATDES": "TEST ODD", "DEPID": "9", "DEP": ""})))
		blank_path = _write(blank)
		self.addCleanup(os.unlink, blank_path)
		names_blank, _ = ss_feed.parse_categories(blank_path)
		self.assertEqual(names_blank["77"], "TEST ODD (9)")
		brands_path = _write(ss_synth.brands_text())
		self.addCleanup(os.unlink, brands_path)
		self.assertEqual(ss_feed.parse_brands(brands_path), {"9001": "Acme Test Arms"})
		empty_path = _write(envelope(dataset()))
		self.addCleanup(os.unlink, empty_path)
		with self.assertRaises(FeedFormatError):
			ss_feed.parse_categories(empty_path)

	def test_onhand_incremental_shapes(self):
		stamp = servertime()
		path = _write(onhand_incremental(stamp, [(sku(1), 5, 10.5), (sku(2), 0, None)]))
		self.addCleanup(os.unlink, path)
		self.assertEqual(ss_feed.parse_onhand_incremental(path),
			(stamp, [(sku(1), 5, 10.5), (sku(2), 0, None)]))

		path = _write(onhand_incremental(stamp, []))
		self.addCleanup(os.unlink, path)
		self.assertEqual(ss_feed.parse_onhand_incremental(path), (stamp, []))

		path = _write(onhand_incremental("", []))
		self.addCleanup(os.unlink, path)
		self.assertEqual(ss_feed.parse_onhand_incremental(path), ("", []))

		no_stamp = envelope(dataset(record("Onhand", {"I": sku(1), "Q": "5", "P": "", "C": "1.00"})))
		path = _write(no_stamp)
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedFormatError):
			ss_feed.parse_onhand_incremental(path)

		blank_q = envelope(dataset(
			"<ServerDateTime><SERVERTIME>" + stamp + "</SERVERTIME></ServerDateTime>",
			record("Onhand", {"I": sku(1), "Q": "", "P": "", "C": "1.00"}),
			record("Onhand", {"I": sku(2), "Q": "", "P": "", "C": "1.00"}),
		))
		path = _write(blank_q)
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedFormatError):
			ss_feed.parse_onhand_incremental(path)

	def test_onhand_full_streams_and_refuses_empty_or_wholly_rejected(self):
		path = _write(onhand_full([(sku(1), 1, 10.0), (sku(2), 2, 20.0), (sku(3), 3, 30.0)]))
		self.addCleanup(os.unlink, path)
		gen = ss_feed.iter_onhand_full(path)
		self.assertTrue(inspect.isgenerator(gen))
		self.assertEqual(len(list(gen)), 3)

		path = _write(envelope(dataset()))
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedFormatError):
			list(ss_feed.iter_onhand_full(path))

		no_q = envelope(dataset(
			record("Table", {"I": sku(1), "Q": "", "P": "", "C": "1"}),
			record("Table", {"I": sku(2), "Q": "", "P": "", "C": "1"}),
		))
		path = _write(no_q)
		self.addCleanup(os.unlink, path)
		with self.assertRaises(FeedFormatError):
			list(ss_feed.iter_onhand_full(path))

	def test_text_rows_skip_empty_text(self):
		path = _write(text_rows([(sku(1), "Copy one."), (sku(2), "")]))
		self.addCleanup(os.unlink, path)
		self.assertEqual(list(ss_feed.iter_text_rows(path)), [(sku(1), "Copy one.")])


# ---- classification parity (pure)

# (department, ITYPE, expected FFL flag, expected class), with a neutral title.
DEPARTMENT_CASES = [
	("26", "1", True, "Handgun"),
	("96", "1", True, "Handgun"),
	("40", "2", True, "Long Gun"),
	("94", "2", True, "Long Gun"),
	("63", "1", True, "Other Firearm"),
	("63", "3", False, "Gun Parts"),
	("66", "1", True, "Other Firearm"),
	("67", "3", False, "Gun Parts"),
	("65", "1", True, "Other Firearm"),
	("65", "3", False, "Other Firearm"),
	("93", "1", True, "NFA"),
	("93", "3", False, "Gun Parts"),
	("8", "3", False, "Gun Parts"),
	("12", "1", True, "Other Firearm"),
	("5", "3", False, "Ammunition"),
	("91", "3", False, "Ammunition"),
	("12", "3", False, "Accessory"),
	("31", "3", False, "Accessory"),
]

# (department, title, ITYPE, expected FFL flag, expected class), from the Data Service's own test titles.
TITLE_CASES = [
	("8", 'SilencerCo AC669 Bravo Direct Thread Mount 5/8"-24', "3", False, "Gun Parts"),
	("93", 'Tactical Solutions PLTSSMB Pac-Lite TSS 22 LR 1.00" Black Titanium', "1", True, "NFA"),
	("93", 'PLIVTSSMB Pac-Lite 22 LR 1.00" Black Titanium', "1", True, "NFA"),
	("93", "SILENCERCO OMEGA 36M SUPPRESSOR", "1", True, "NFA"),
	("93", "Archangel AA118 10/22 Faux Suppressor 22 LR Black Polymer", "3", False, "Gun Parts"),
	("93", "ATI 411300002 1911 Firefly Faux Suppressor", "3", False, "Gun Parts"),
	("93", "HUXWRX SUPPRESSOR COVER", "3", False, "Gun Parts"),
	("93", "SILENCERCO HYBRID SILENCER W/ PISTON", "1", True, "NFA"),
	("94", "CMMG BANSHEE SBR 5.7X28 8IN", "2", True, "NFA"),
	("93", "PLIVTSSMB PAC-LITE 22 LR BLACK TITANIUM", "1", True, "NFA"),
	("63", "RUGER 10/22 TRIGGER GROUP", "3", False, "Gun Parts"),
	("8", "GREEN MOUNTAIN 10/22 BARREL", "3", False, "Gun Parts"),
	("13", "PEDERSOLI 1861 SPRINGFIELD", "1", True, "Other Firearm"),
	("13", "CVA WOLF 50CAL MUZZLELOADER", "3", False, "Accessory"),
	("74", "ACME AR-15 STRIPPED LOWER", "1", True, "Other Firearm"),
	# The Data Service's curated category tree files this under rifles. The department rule here
	# gives Other Firearm: the same legal answer, a firearm.
	("37", "TACSOL X-RING 10/22 TAKEDOWN RIFLE", "1", True, "Other Firearm"),
]

# Rows the Data Service's owner rules touch. The expected class is the class the Data Service
# serves for them, after its owner rule, not its raw per-row fallback. The legal answer for
# lights is tested with the driver later.
OWNER_RULE_ROWS = [
	("31", "OLIGHT BALDR PRO WEAPON LIGHT", "1", True, "Accessory"),
	("31", "OLIGHT ODIN", "1", True, "Accessory"),
	("31", "ACME 9MM SUPPRESSOR", "1", True, "Accessory"),
	("31", "OLIGHT ODIN", "3", False, "Accessory"),
	("93", "MAXIM DEFENSE PDX 300BLK PISTOL 8.5IN", "1", True, "Handgun"),
	("93", "ACME 9MM PISTOLS TWIN PACK", "1", True, "Handgun"),
	("93", "ACME 9MM SUPPRESSOR", "1", True, "NFA"),
	("93", "ACME SBR PISTOL 9MM", "1", True, "NFA"),
	# The title names a suppressor word, so the pistol rule does not apply (the Data Service checks the bare NFA words here).
	("93", "ACME PISTOL SUPPRESSOR READY", "1", True, "NFA"),
	# Pistol must be a whole word.
	("93", "ACME PISTOLGRIP COVER", "1", True, "NFA"),
	# The pistol rule needs the flag, so an unflagged row stays a part.
	("93", "ACME PISTOL 9MM", "3", False, "Gun Parts"),
]


def _iter_class(self, dept, item_type, title):
	"""The class and FFL flag the parser assigns to one row."""
	path = _write(daily_items([1], over={1: {
		"CATID": dept, "ITYPE": item_type, "SHDESC": title.replace("&", "&amp;")}}))
	self.addCleanup(os.unlink, path)
	rows = list(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={}))
	return rows[0]["product_class"], rows[0]["ffl_required"]


class TestSsClassParity(unittest.TestCase):

	def _check(self, dept, title, item_type, ffl, expected):
		self.assertEqual(ss_feed.product_class(dept, title, ffl), expected, (dept, title))
		got_class, got_ffl = _iter_class(self, dept, item_type, title)
		self.assertEqual(got_ffl, ffl, (dept, title))
		self.assertEqual(got_class, expected, (dept, title))

	def test_department_cases_match_the_data_service(self):
		for dept, item_type, ffl, expected in DEPARTMENT_CASES:
			with self.subTest(dept=dept, item_type=item_type):
				self._check(dept, "ACME TEST ITEM", item_type, ffl, expected)
		self.assertEqual(ss_feed.product_class(" 8 ", "ACME TEST ITEM", False), "Gun Parts")
		self.assertEqual(ss_feed.product_class(" 91 ", "ACME TEST ITEM", False), "Ammunition")

	def test_title_cases_match_the_data_service(self):
		for dept, title, item_type, ffl, expected in TITLE_CASES:
			with self.subTest(dept=dept, title=title):
				self._check(dept, title, item_type, ffl, expected)

	def test_owner_rule_rows_match_the_class_the_data_service_serves(self):
		for dept, title, item_type, ffl, expected in OWNER_RULE_ROWS:
			with self.subTest(dept=dept, title=title):
				self._check(dept, title, item_type, ffl, expected)

	def test_category_sets_are_the_data_service_lists(self):
		from ffl_integrations.distributor import ss_provider as sp
		self.assertEqual(sp.HANDGUN_CATEGORIES, {"26", "64", "96"})
		self.assertEqual(sp.LONG_GUN_CATEGORIES, {"40", "48", "57", "58", "59", "94"})
		self.assertEqual(sp.RECEIVER_CATEGORIES, {"63", "66", "67"})
		self.assertEqual(sp.OTHER_FIREARM_CATEGORIES, {"65"})
		self.assertEqual(sp.SUPPRESSOR_CATEGORIES, {"93"})
		self.assertEqual(sp.AMMO_CATEGORIES, {"4", "5", "6", "49", "69", "70", "71", "72", "90", "91"})
		self.assertEqual(ss_feed.NEVER_FIREARM_CATEGORIES, {"31"})

	def test_ss_classes_are_service_classes(self):
		for table in (DEPARTMENT_CASES, TITLE_CASES, OWNER_RULE_ROWS):
			for row in table:
				self.assertIn(row[-1], PRODUCT_CLASSES)


# ---- lanes

TCODE = "sstest"


def _purge() -> None:
	for table in ("dc_offer", "dc_product_move", "dc_restriction", "dc_meta"):
		frappe.db.sql(f"DELETE FROM {table} WHERE distributor = %s", (TCODE,))
	frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("99992%",))
	frappe.db.sql("DELETE FROM dc_sync_run WHERE source LIKE %s", (TCODE + "\\_%",))
	frappe.db.commit()


def _age_all(hours: int) -> None:
	frappe.db.sql("""UPDATE dc_sync_run SET started_at = started_at - INTERVAL %s HOUR,
		finished_at = finished_at - INTERVAL %s HOUR WHERE source LIKE %s""",
		(hours, hours, TCODE + "\\_%"))
	frappe.db.commit()


def _runs() -> list:
	return [tuple(r) for r in frappe.db.sql("""SELECT source, status, error_class FROM dc_sync_run
		WHERE source LIKE %s ORDER BY id""", (TCODE + "\\_%",))]


def _offer_seqs() -> dict:
	return dict(frappe.db.sql("SELECT sku, change_seq FROM dc_offer WHERE distributor = %s", (TCODE,)))


def _offer(s: str):
	return frappe.db.sql("""SELECT cost, qty, discontinued, status_flag, ffl_required, product_class,
		department, department_name, caliber, long_description FROM dc_offer
		WHERE distributor = %s AND sku = %s""", (TCODE, s), as_dict=True)[0]


class _SsBase(FrappeTestCase):
	def setUp(self):
		catalog_stub.uninstall()
		_purge()
		self.transport = FakeSSTransport(standard_feed())
		self.driver = sync.register(ss.SsDriver(code=TCODE, transport=self.transport))
		self.addCleanup(sync.unregister, TCODE)
		self.addCleanup(_purge)
		# The test feeds hold 12 rows; the production floor is far above that.
		floor = patch.object(ss, "SNAPSHOT_MIN_ROWS", 0)
		floor.start()
		self.addCleanup(floor.stop)
		self.addCleanup(self._no_temp_files_left)

	def _no_temp_files_left(self):
		self.assertEqual([p for p in self.transport.left_behind if os.path.exists(p)], [],
			"a lane left its downloaded file behind")

	def methods(self) -> list:
		return [m for m, _p in self.transport.calls]

	def params(self, method: str) -> dict:
		return [p for m, p in self.transport.calls if m == method][-1]

	def _other_connection(self):
		from frappe.database import get_db

		conf = frappe.local.conf
		other = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
			user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
		other.connect()
		self.addCleanup(other.close)
		return other


class TestSsLanes(_SsBase):

	def test_first_run_loads_the_snapshot_then_the_text_and_rewinds_the_cursor(self):
		stamp = servertime()
		self.transport.files["IncrementalOnhandUpdate"] = onhand_incremental(stamp, [])
		out = sync.run_house(TCODE)
		self.assertEqual(out["_failed"], [])
		self.assertIsNone(out["catalog"])
		self.assertEqual(self.methods(), ["IncrementalOnhandUpdate", "CategoryUpdate",
			"BrandUpdate", "DailyItemUpdate", "ListNewText"])
		self.assertEqual(self.params("DailyItemUpdate"), {"LastUpdate": "1/1/1990", "LastItem": "-1"})
		self.assertEqual(self.params("ListNewText"), {"DateFrom": "1/1/1990"})
		self.assertEqual(_runs(), [(TCODE + "_quantity", "success", ""),
			(TCODE + "_snapshot", "success", ""), (TCODE + "_text", "success", "")])
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor = %s",
			(TCODE,))[0][0], 12)
		one = _offer(sku(1))
		self.assertEqual(one["department"], "26")
		self.assertEqual(one["department_name"], "TEST HANDGUNS (TEST FIREARMS)")
		self.assertEqual(one["ffl_required"], 1)
		self.assertEqual(one["product_class"], "Handgun")
		self.assertEqual(one["caliber"], "9MM")
		self.assertEqual(float(one["cost"]), 101.5)
		self.assertEqual(one["qty"], 1)
		self.assertEqual(one["long_description"], "Synthetic copy 1.")
		five = _offer(sku(5))
		self.assertEqual(five["product_class"], "Accessory")
		self.assertEqual(five["ffl_required"], 0)
		cursor = ss.qty_cursor(TCODE)
		self.assertTrue(cursor)
		self.assertLess(cursor, stamp)
		self.assertGreater(cursor, servertime(-25 * 60)[:19])

	def test_next_day_the_delta_and_text_run_with_a_one_day_overlap(self):
		sync.run_house(TCODE)
		_age_all(21)
		self.transport.calls.clear()
		cursor, snapshot_base, text_base = (ss.qty_cursor(TCODE),
			sync.last_success(TCODE + "_snapshot"), sync.last_success(TCODE + "_text"))
		sync.run_house(TCODE)
		self.assertNotIn(("DailyItemUpdate", {"LastUpdate": "1/1/1990", "LastItem": "-1"}),
			self.transport.calls)
		d = (snapshot_base - timedelta(days=1)).date()
		self.assertEqual(self.params("DailyItemUpdate")["LastUpdate"], f"{d.month}/{d.day}/{d.year}")
		t = (text_base - timedelta(days=1)).date()
		self.assertEqual(self.params("ListNewText")["DateFrom"], f"{t.month}/{t.day}/{t.year}")
		self.assertEqual(self.params("IncrementalOnhandUpdate")["SinceDateTime"], cursor)

	def test_the_delta_keeps_its_own_daily_cadence_after_a_snapshot(self):
		sync.run_house(TCODE)
		self.assertIsInstance(sync.run_lane(TCODE, "catalog", skip_schedule=True), dict)
		_age_all(19)
		self.assertIsInstance(sync.run_lane(TCODE, "snapshot", force=True), dict)
		_age_all(2)  # the last delta is now 21 h old, the last snapshot 2 h old
		self.transport.calls.clear()
		out = sync.run_lane(TCODE, "catalog")
		self.assertIsInstance(out, dict)
		self.assertNotEqual(out, {"skipped": "not_due"})
		d = (sync.last_success(TCODE + "_snapshot") - timedelta(days=1)).date()
		self.assertEqual(self.params("DailyItemUpdate")["LastUpdate"], f"{d.month}/{d.day}/{d.year}")
		self.assertEqual(sync.run_lane(TCODE, "catalog"), {"skipped": "not_due"})

	def test_an_unchanged_delta_bumps_nothing_and_a_new_title_bumps_one_row(self):
		sync.run_house(TCODE)
		seqs = _offer_seqs()
		self.assertEqual(sync.run_lane(TCODE, "catalog", skip_schedule=True)["updated"], 0)
		self.assertEqual(_offer_seqs(), seqs)
		self.transport.files["DailyItemUpdate"] = daily_items(range(1, 13),
			over={3: {"SHDESC": "ACME TEST AMMO RENAMED"}})
		self.assertEqual(sync.run_lane(TCODE, "catalog", skip_schedule=True)["updated"], 1)
		after = _offer_seqs()
		self.assertEqual([s for s in seqs if seqs[s] != after[s]], [sku(3)])

	def test_quantity_moves_qty_and_cost_and_advances_the_cursor(self):
		sync.run_house(TCODE)
		stamp = servertime(1)
		self.transport.files["IncrementalOnhandUpdate"] = onhand_incremental(
			stamp, [(sku(1), 9, 111.11), (sku(2), 4, None)])
		prev = ss.qty_cursor(TCODE)
		sync.run_lane(TCODE, "quantity")
		self.assertEqual(_offer(sku(1))["qty"], 9)
		self.assertEqual(float(_offer(sku(1))["cost"]), 111.11)
		self.assertEqual(_offer(sku(2))["qty"], 4)
		self.assertEqual(float(_offer(sku(2))["cost"]), 102.5)
		self.assertEqual(ss.qty_cursor(TCODE), stamp)
		self.assertEqual(self.params("IncrementalOnhandUpdate")["SinceDateTime"], prev)
		# qty and cost are insert-only for the daily delta: the quantity lane's values stand.
		self.transport.files["DailyItemUpdate"] = daily_items(range(1, 13),
			over={1: {"QTYOH": "77", "CPRC": "5.00"}})
		sync.run_lane(TCODE, "catalog", skip_schedule=True)
		self.assertEqual(_offer(sku(1))["qty"], 9)
		self.assertEqual(float(_offer(sku(1))["cost"]), 111.11)

	def test_rows_without_a_servertime_are_refused_and_the_cursor_holds(self):
		sync.run_house(TCODE)
		before_qty, prev = _offer(sku(1))["qty"], ss.qty_cursor(TCODE)
		self.transport.files["IncrementalOnhandUpdate"] = onhand_incremental("", [(sku(1), 9, 111.11)])
		try:
			sync.run_lane(TCODE, "quantity")
		except FeedFormatError:
			pass  # whether the lane raises or records the failure, nothing may be applied
		self.assertEqual(_offer(sku(1))["qty"], before_qty)
		self.assertEqual(ss.qty_cursor(TCODE), prev)

	def test_an_idle_buffer_falls_back_to_the_full_onhand_at_most_every_30_minutes(self):
		sync.run_house(TCODE)
		cursor = ss.qty_cursor(TCODE)
		self.transport.files["IncrementalOnhandUpdate"] = onhand_incremental("", [])
		self.transport.files["OnhandUpdate"] = onhand_full([(sku(1), 6, 120.0)])
		out = sync.run_lane(TCODE, "quantity")
		self.assertEqual(out["mode"], "full")
		self.assertEqual(_offer(sku(1))["qty"], 6)
		self.assertIn((TCODE + "_onhand_full", "success", ""), _runs())
		self.assertEqual(ss.qty_cursor(TCODE), cursor)
		self.assertEqual(self.transport.timeouts["OnhandUpdate"], ss.TIMEOUT_ONHAND_FULL)
		self.assertEqual(ss.TIMEOUT_ONHAND_FULL, 900)
		self.assertEqual(sync.run_lane(TCODE, "quantity")["mode"], "idle")
		self.assertEqual(self.methods().count("OnhandUpdate"), 1)

	def test_a_snapshot_discontinues_vanished_rows_below_the_gate_and_refuses_a_mass_vanish(self):
		sync.run_house(TCODE)
		with patch.object(ss, "DISAPPEARED_FRACTION", 0.5):
			self.transport.files["DailyItemUpdate"] = daily_items(range(1, 12))
			out = sync.run_lane(TCODE, "snapshot", force=True)
		self.assertEqual(out["disappeared"], 1)
		gone = _offer(sku(12))
		self.assertEqual((gone["discontinued"], gone["status_flag"], gone["qty"]),
			(1, "Discontinued", 0))
		self.transport.files["DailyItemUpdate"] = daily_items(range(1, 7))
		self.assertIsNone(sync.run_lane(TCODE, "snapshot", force=True))
		self.assertEqual(_runs()[-1], (TCODE + "_snapshot", "failed", "ValueError"))
		for n in range(7, 12):
			self.assertEqual(_offer(sku(n))["discontinued"], 0, n)

	def test_an_error_payload_fails_the_snapshot_and_the_delta_waits(self):
		self.transport.files["CategoryUpdate"] = error_payload("USER NOT AUTHENTICATED!")
		out = sync.run_house(TCODE)
		self.assertEqual(out["_failed"], [TCODE + "_snapshot"])
		self.assertIn((TCODE + "_snapshot", "failed", "FeedAuthError"), _runs())
		self.assertIn((TCODE + "_quantity", "success", ""), _runs())
		self.assertNotIn("DailyItemUpdate", self.methods())
		self.assertNotIn("ListNewText", self.methods())
		self.assertFalse(any(src.endswith(("_catalog", "_text")) for src, _s, _e in _runs()))
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor = %s",
			(TCODE,))[0][0], 0)
		self.transport.files["CategoryUpdate"] = error_payload("SOMETHING BROKE")
		self.assertIsNone(sync.run_lane(TCODE, "snapshot", force=True))
		self.assertIn((TCODE + "_snapshot", "failed", "FeedFormatError"), _runs())

	def test_sync_now_skips_the_schedule_but_not_the_weekly_snapshot_or_its_guards(self):
		sync.run_house(TCODE)
		self.transport.calls.clear()
		sync.run_house(TCODE, skip_schedule=True)
		self.assertNotEqual(self.params("DailyItemUpdate")["LastUpdate"], "1/1/1990")
		self.assertEqual(self.methods().count("DailyItemUpdate"), 1)
		self.assertIn("ListNewText", self.methods())
		self.assertEqual(sync.run_lane(TCODE, "snapshot", skip_schedule=True), {"skipped": "not_due"})
		self.transport.calls.clear()
		sync.run_lane(TCODE, "snapshot", force=True)
		self.assertEqual(self.params("DailyItemUpdate")["LastUpdate"], "1/1/1990")
		with patch.object(ss, "SNAPSHOT_MIN_ROWS", 100):
			self.assertIsNone(sync.run_lane(TCODE, "snapshot", force=True))
		self.assertIn((TCODE + "_snapshot", "failed", "ValueError"), _runs())
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor = %s",
			(TCODE,))[0][0], 12)

	def test_a_lock_skipped_quantity_run_keeps_the_cursor(self):
		sync.run_house(TCODE)
		cursor = ss.qty_cursor(TCODE)
		runs_before = len(_runs())
		other = self._other_connection()
		self.assertEqual(other.sql("SELECT GET_LOCK(%s, 0)", (store.lock_name(TCODE),))[0][0], 1)
		self.transport.files["IncrementalOnhandUpdate"] = onhand_incremental(servertime(2),
			[(sku(1), 8, 99.0)])
		try:
			out = sync.run_lane(TCODE, "quantity")
		finally:
			other.sql("SELECT RELEASE_LOCK(%s)", (store.lock_name(TCODE),))
		self.assertTrue(out.get("lock_skipped"))
		self.assertEqual(ss.qty_cursor(TCODE), cursor)
		self.assertEqual(len(_runs()), runs_before)

	def test_text_lane_fills_long_descriptions_and_skips_empty_text(self):
		sync.run_house(TCODE)
		seqs = _offer_seqs()
		self.transport.files["ListNewText"] = text_rows([(sku(1), "New copy"), (sku(2), "")])
		out = sync.run_lane(TCODE, "text", skip_schedule=True)
		self.assertEqual(out["updated"], 1)
		self.assertEqual(_offer(sku(1))["long_description"], "New copy")
		self.assertEqual(LocalCatalog().products_batch([upc(1)])["products"][0]["long_description"],
			"New copy")
		self.assertEqual(_offer(sku(2))["long_description"], "Synthetic copy 2.")
		after = _offer_seqs()
		self.assertEqual([s for s in seqs if seqs[s] != after[s]], [sku(1)])

	def test_the_shipped_driver_registers_with_its_four_lanes(self):
		shipped = sync.load_drivers()["ss"]
		self.assertIsInstance(shipped, ss.SsDriver)
		lanes = shipped.lanes()
		self.assertEqual([ln.name for ln in lanes], ["quantity", "snapshot", "catalog", "text"])
		self.assertEqual([ln.heavy for ln in lanes], [False, True, True, True])
		for ln in lanes:  # every lane, the catalog delta included, may run a long download and load
			self.assertEqual(ln.timeout, sync.HEAVY_TIMEOUT_SECONDS, ln.name)
		self.assertGreater(shipped.lane("quantity").timeout, ss.TIMEOUT_ONHAND_FULL)
		self.assertEqual(shipped.stale_after("snapshot"), 777600)
		self.assertEqual(shipped.never_firearm_departments, {"31"})


# ---- transport, settings, legal answer and contract


LOCAL = "ffl_integrations.catalog_local.is_local"
SETTINGS = ss.SETTINGS
FEED_LABEL = "Catalog feed (inventory.asmx)"
NOT_A_FIREARM = {"is_firearm": False, "nfa_type": "none", "is_antique": False}


def _mark(tag: str) -> str:
	return f"MARK-{tag}-must-not-leak"


def _creds(**over):
	fields = {"customer_number": "99999", "username": "apiuser", "_password": _mark("ss"),
		"base_url": "https://ss.example.com/smart"}
	return ss.FeedCredentials(**{**fields, **over})


def _clear_memo() -> None:
	frappe.local._remote_catalog_memo = {}


def _load(path: str) -> dict:
	return store.load_catalog(ss_feed.iter_catalog(path, brands={}, department_names={}, caliber_slots={}),
		distributor=TCODE, mode="snapshot", exclude_cols=ss.EXCLUDE_COLS,
		insert_only_cols=ss.INSERT_ONLY_COLS)


class _Resp:
	def __init__(self, status_code, body: bytes):
		self.status_code = status_code
		self._body = body

	def iter_content(self, n):
		yield self._body

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


class _Session:
	def __init__(self, resp=None, exc=None):
		self.resp = resp
		self.exc = exc
		self.calls = []

	def __repr__(self):
		# A real session does not print its request bodies, so neither does the double.
		return "_Session()"

	def post(self, url, **kw):
		self.calls.append((url, kw))
		if self.exc is not None:
			raise self.exc
		return self.resp


class TestSsTransport(FrappeTestCase):

	def test_posts_to_inventory_asmx_with_the_credentials_in_the_body_only(self):
		session = _Session(_Resp(200, categories_text().encode()))
		path = ss.InventoryTransport(_creds(), session=session).download("CategoryUpdate")
		self.addCleanup(ss_feed.unlink_quiet, path)
		url, kw = session.calls[0]
		self.assertEqual(url, "https://ss.example.com/smart/inventory.asmx/CategoryUpdate")
		self.assertNotIn(_mark("ss"), url)
		self.assertTrue(kw["stream"])
		self.assertEqual(kw["data"]["Password"], _mark("ss"))
		self.assertEqual(repr(kw["data"]["Password"]), "<redacted>")
		self.assertEqual(kw["data"]["Source"], "99999")
		with open(path, encoding="utf-8") as f:
			self.assertEqual(f.read(), categories_text())
		probe = ss.InventoryTransport(_creds(), session=_Session(_Resp(200, categories_text().encode())))
		self.assertEqual(probe.probe(), {"categories": 8})

	def test_http_and_network_failures_carry_no_request_details(self):
		before = set(os.listdir(tempfile.gettempdir()))
		with self.assertRaises(FeedUnavailable) as cm:
			ss.InventoryTransport(_creds(), session=_Session(_Resp(500, b""))).download("CategoryUpdate")
		self.assertEqual(str(cm.exception), "Sports South CategoryUpdate returned HTTP 500")
		leftovers = {n for n in set(os.listdir(tempfile.gettempdir())) - before
			if n.startswith("ss-feed-CategoryUpdate-")}
		self.assertEqual(leftovers, set())
		with self.assertRaises(FeedUnavailable) as cm:
			ss.InventoryTransport(_creds(), session=_Session(
				exc=requests.ConnectionError(f"boom {_mark('net')}"))).download("CategoryUpdate")
		self.assertEqual(str(cm.exception), "Sports South CategoryUpdate failed: ConnectionError")
		self.assertNotIn(_mark("net"), str(cm.exception))

	def test_the_disk_check_guards_only_the_large_downloads(self):
		creds = _creds()
		before = set(os.listdir(tempfile.gettempdir()))
		with patch.object(rsr, "check_disk", side_effect=FeedUnavailable("not enough free disk")) as chk:
			session = _Session(_Resp(200, b"x"))
			with self.assertRaises(FeedUnavailable):
				ss.InventoryTransport(creds, session=session).download(
					"DailyItemUpdate", {"LastUpdate": "1/1/1990", "LastItem": "-1"})
			self.assertEqual(session.calls, [], "a request went out past a failed disk check")
			self.assertEqual(chk.call_count, 1)
			self.assertEqual(chk.call_args.args[2], "DailyItemUpdate")
			leftovers = {n for n in set(os.listdir(tempfile.gettempdir())) - before
				if n.startswith("ss-feed-DailyItemUpdate-")}
			self.assertEqual(leftovers, set())

			chk.side_effect = None
			path = ss.InventoryTransport(creds, session=_Session(
				_Resp(200, categories_text().encode()))).download("CategoryUpdate")
			ss_feed.unlink_quiet(path)
			self.assertEqual(chk.call_count, 1, "a small download ran the disk check")
			for method in ("OnhandUpdate", "ListNewText"):
				path = ss.InventoryTransport(creds, session=_Session(_Resp(200, b"x"))).download(method)
				ss_feed.unlink_quiet(path)
			self.assertEqual(chk.call_count, 3)
			self.assertEqual(chk.call_args.args[2], "ListNewText")

	def test_a_run_on_a_fake_transport_never_reaches_the_disk_check(self):
		_purge()
		self.addCleanup(_purge)
		sync.register(ss.SsDriver(code=TCODE, transport=FakeSSTransport(standard_feed())))
		self.addCleanup(sync.unregister, TCODE)
		with patch.object(ss, "SNAPSHOT_MIN_ROWS", 0), \
				patch.object(rsr, "check_disk", side_effect=AssertionError("disk check on a fake transport")):
			self.assertEqual(sync.run_house(TCODE)["_failed"], [])

	def test_credentials_never_show_in_a_repr_a_traceback_or_a_run_row(self):
		creds = _creds()
		self.assertNotIn(_mark("ss"), repr(creds))
		# The traceback leg uses the HTTP 500 session: its failure text carries no mark.
		try:
			ss.InventoryTransport(creds, session=_Session(_Resp(500, b""))).download("CategoryUpdate")
		except FeedUnavailable:
			tb = frappe.get_traceback(with_context=True)
		else:
			self.fail("expected FeedUnavailable")
		self.assertIn("in download", tb)
		self.assertNotIn(_mark("ss"), tb)

		_purge()
		self.addCleanup(_purge)
		transport = ss.InventoryTransport(creds, session=_Session(
			exc=requests.ConnectionError(f"boom {_mark('net')}")))
		sync.register(ss.SsDriver(code=TCODE, transport=transport))
		self.addCleanup(sync.unregister, TCODE)
		sync.run_lane(TCODE, "quantity")
		row = frappe.db.sql("""SELECT error_class, error_msg FROM dc_sync_run WHERE source = %s
			ORDER BY id DESC LIMIT 1""", (f"{TCODE}_quantity",))[0]
		self.assertEqual(row[0], "FeedUnavailable")
		self.assertNotIn(_mark("net"), row[1] or "")
		self.assertNotIn(_mark("ss"), row[1] or "")


class TestSsSettingsFeed(FrappeTestCase):
	"""Sports South Settings › the catalog feed: credentials, configured(), Test Connection."""

	def setUp(self):
		from frappe.utils.password import get_decrypted_password

		names = ("customer_number", "username", "base_url")
		before = {f: frappe.db.get_single_value(SETTINGS, f) for f in names}
		pw_before = get_decrypted_password(SETTINGS, SETTINGS, "password", raise_exception=False)
		self.addCleanup(self._restore, before, pw_before)

	def _restore(self, before, pw_before):
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		for field, value in before.items():
			frappe.db.set_single_value(SETTINGS, field, value)
		if pw_before:
			set_encrypted_password(SETTINGS, SETTINGS, pw_before, "password")
		else:
			remove_encrypted_password(SETTINGS, SETTINGS, "password")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		frappe.db.commit()

	def _save(self, customer, username, password, base_url=""):
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		frappe.db.set_single_value(SETTINGS, "customer_number", customer)
		frappe.db.set_single_value(SETTINGS, "username", username)
		frappe.db.set_single_value(SETTINGS, "base_url", base_url)
		if password:
			set_encrypted_password(SETTINGS, SETTINGS, password, "password")
		else:
			remove_encrypted_password(SETTINGS, SETTINGS, "password")
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _checks(self, local: bool) -> list:
		from ffl_integrations.distributor.ss_provider import SSProvider

		with patch.object(SSProvider, "catalog_health_check",
				return_value={"label": "Catalog", "ok": True, "detail": "fine"}), \
				patch("ffl_integrations.sports_south.api_client.probe_connection", return_value=[]), \
				patch("ffl_integrations.sports_south.api_client.SSOrdersClient.from_settings",
					return_value=None), \
				patch(LOCAL, return_value=local):
			return SSProvider().connection_test()["checks"]

	def test_credentials_read_sports_south_settings_with_defaults(self):
		self._save("", "", _mark("ss"))
		self.assertIsNone(ss.credentials())
		self._save("99999", "", "")
		self.assertIsNone(ss.credentials())
		self._save("99999", "", _mark("ss"))
		creds = ss.credentials()
		self.assertEqual((creds.username, creds.base_url), ("99999", DEFAULT_BASE_URL))
		self.assertEqual(creds.password, _mark("ss"))

	def test_configured_needs_an_enabled_distributor_and_the_account_number(self):
		with patch.object(sync.Driver, "configured", return_value=True):
			self._save("", "", _mark("ss"))
			self.assertFalse(ss.SsDriver().configured())
			self._save("99999", "", _mark("ss"))
			self.assertTrue(ss.SsDriver().configured())
		with patch.object(sync.Driver, "configured", return_value=False):
			self.assertFalse(ss.SsDriver().configured())

	def test_an_undecryptable_password_is_a_failed_run_not_a_skip(self):
		self._save("99999", "", _mark("ss"))
		frappe.db.sql("""UPDATE `__Auth` SET `password` = %s WHERE `doctype` = %s
			AND `name` = %s AND `fieldname` = 'password'""", ("gAAAAABnot-a-valid-fernet-token", SETTINGS, SETTINGS))
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		_purge()
		self.addCleanup(_purge)
		sync.register(ss.SsDriver(code=TCODE))  # the real transport over Sports South Settings
		self.addCleanup(sync.unregister, TCODE)
		with patch.object(sync.Driver, "configured", return_value=True):
			self.assertIsNone(sync.run_lane(TCODE, "quantity"))
		row = frappe.db.sql("""SELECT status, error_class, error_msg FROM dc_sync_run WHERE source = %s
			ORDER BY id DESC LIMIT 1""", (f"{TCODE}_quantity",))[0]
		self.assertEqual(row[:2], ("failed", "FeedAuthError"))
		self.assertIn("cannot be decrypted", row[2])

	def test_connection_test_adds_the_feed_row_only_in_local_mode(self):
		def feed_row(checks):
			return [c for c in checks if c.get("label") == FEED_LABEL]

		self._save("99999", "", _mark("ss"))
		with patch.object(ss.InventoryTransport, "probe", return_value={"categories": 8}):
			checks = self._checks(local=True)
			self.assertIn({"label": FEED_LABEL, "ok": True, "detail": "Logged in; 8 categories."}, checks)
			self.assertEqual(feed_row(self._checks(local=False)), [])
		with patch.object(ss.InventoryTransport, "probe",
				side_effect=FeedAuthError("Sports South rejected the login (USER NOT AUTHENTICATED!)")):
			(row,) = feed_row(self._checks(local=True))
		self.assertFalse(row["ok"])
		self.assertEqual(row["detail"], "Sports South rejected the login (USER NOT AUTHENTICATED!)")
		self.assertNotIn(_mark("ss"), row["detail"])
		self._save("", "", "")
		(row,) = feed_row(self._checks(local=True))
		self.assertFalse(row["ok"])
		self.assertIn("no account number", row["detail"])


class TestSsLegalAndContract(_SsBase):

	def test_a_flagged_light_is_never_a_firearm_but_ships_only_to_a_dealer(self):
		from ffl_integrations.distributor.util import dealer_only_part, legal_firearm

		self.transport.files["DailyItemUpdate"] = daily_items(range(1, 13), over={
			5: {"ITYPE": "1"},
			7: {"CATID": "93", "ITYPE": "1", "SHDESC": "ACME TEST PDX PISTOL 300BLK"},
			8: {"CATID": "93", "ITYPE": "1", "SHDESC": "ACME TEST 9MM SUPPRESSOR"}})
		sync.run_house(TCODE)
		with patch(LOCAL, return_value=True):
			_clear_memo()
			rows = LocalCatalog().rows(TCODE, [sku(5), sku(7), sku(8), sku(1), sku(2)])
			light, pistol, nfa = rows[sku(5)], rows[sku(7)], rows[sku(8)]
			handgun, holster = rows[sku(1)], rows[sku(2)]
			self.assertEqual((light["ffl_required"], light["product_class"]), (True, "Accessory"))
			self.assertFalse(legal_firearm(light, "ss"))
			self.assertTrue(dealer_only_part(light, "ss"))
			self.assertEqual((pistol["product_class"], pistol["ffl_required"]), ("Handgun", True))
			self.assertTrue(legal_firearm(pistol, "ss"))
			self.assertFalse(dealer_only_part(pistol, "ss"))
			self.assertEqual(nfa["product_class"], "NFA")
			self.assertTrue(legal_firearm(nfa, "ss"))
			self.assertTrue(legal_firearm(handgun, "ss"))
			self.assertFalse(dealer_only_part(handgun, "ss"))
			self.assertFalse(holster["ffl_required"])
			self.assertFalse(legal_firearm(holster, "ss"))
			self.assertFalse(dealer_only_part(holster, "ss"))
			_clear_memo()
			self.assertEqual(LocalCatalog().products_batch([upc(5)])["products"][0]["legal"], NOT_A_FIREARM)
			for n in (1, 7, 2):
				_clear_memo()
				product = LocalCatalog().products_batch([upc(n)])["products"][0]
				self.assertIsNone(product["legal"], n)

	def test_legal_needs_every_flagged_offer_owner_ruled(self):
		from ffl_integrations.catalog_local import backend

		def offer(dept, flagged=True, house=TCODE):
			return {"distributor": house, "department": dept, "ffl_required": flagged}

		self.assertEqual(backend._legal([offer("31")]), NOT_A_FIREARM)
		self.assertIsNone(backend._legal([offer("31"), offer("26")]))
		self.assertEqual(backend._legal([offer("31"), offer("28", flagged=False)]), NOT_A_FIREARM)
		self.assertIsNone(backend._legal([offer("31", flagged=False)]))
		self.assertIsNone(backend._legal([offer("31", house="nosuchhouse")]))
		self.assertEqual(backend._legal([offer(" 31 ")]), NOT_A_FIREARM)

	def test_contract_check_passes_against_an_ss_fed_local_catalog(self):
		sync.run_house(TCODE)
		results = check(StubTransport(LocalCatalog()), TCODE, sku(1))
		failed = [r for r in results if not r["ok"]]
		self.assertEqual(failed, [], "\n".join(f"{r['name']}: {r['detail']}" for r in failed))

	def test_the_snapshot_lane_streams(self):
		"""Traced memory of the parser and the load barely grows from 2k to 20k rows, so the
		snapshot is never held as a list."""
		fd, small = tempfile.mkstemp(suffix="-ss-stream-2k")
		os.close(fd)
		fd, big = tempfile.mkstemp(suffix="-ss-stream-20k")
		os.close(fd)
		self.addCleanup(os.unlink, small)
		self.addCleanup(os.unlink, big)
		write_daily_items(small, 2_000)
		write_daily_items(big, 20_000)
		_load(small)  # warm imports and caches
		peaks = {}
		for name, path in (("small", small), ("big", big)):
			_purge()
			tracemalloc.start()
			try:
				out = _load(path)
				peaks[name] = tracemalloc.get_traced_memory()[1]
			finally:
				tracemalloc.stop()
		self.assertEqual(out["created"], 20_000)
		growth = peaks["big"] - peaks["small"]
		self.assertLess(growth, 16 << 20, f"traced peak grew {growth >> 20} MiB for 18k more rows")
