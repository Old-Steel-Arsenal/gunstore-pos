"""Local catalog: the Lipsey's driver. The parsers, the class rule, the attempt gate and
the feed GET are pure and tested first; the lanes run against a fake session (the API is
IP-allowlisted). Every feed is synthetic, from tests/lipseys_synth.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.catalog_contract import check
from ffl_integrations.catalog_contract.transports import StubTransport
from ffl_integrations.catalog_local import schema, store, sync
from ffl_integrations.catalog_local.backend import LocalCatalog
from ffl_integrations.catalog_local.drivers import lipseys, lipseys_feed
from ffl_integrations.catalog_local.sync import FeedAuthError, FeedFormatError, FeedUnavailable
from ffl_integrations.distributor.util import PRODUCT_CLASSES
from ffl_integrations.lipseys import api_client
from ffl_integrations.lipseys.api_client import (
	LipseysAuthError,
	LipseysClient,
	LipseysConfig,
	LipseysError,
)
from ffl_integrations.tests import catalog_stub
from ffl_integrations.tests import lipseys_synth as syn
from ffl_integrations.tests.lipseys_synth import (
	UNAUTHORIZED,
	FakeSession,
	Resp,
	catalog_feed,
	catalog_item,
	envelope,
	login_ok,
	login_refused,
	pricing_feed,
	pricing_item,
	sku,
	upc,
)

TCODE = "liptest"
CATALOG = "Items/CatalogFeed"
PRICING = "Items/PricingQuantityFeed"
LOGIN = "Authentication/Login"
LOCAL = "ffl_integrations.catalog_local.is_local"


def _rows(items, stats=None):
	return list(lipseys_feed.iter_catalog(items, stats))


def _config(**over):
	return LipseysConfig(**{"email": "dealer@example.com", "password": "pw-test-not-real",
		"base_url": "https://api.example.invalid/api/Integration", **over})


# ---- parsers and class rule (pure)


class TestLipseysFeedParsers(unittest.TestCase):

	def test_department_key_folds_case_and_spacing(self):
		self.assertEqual(lipseys_feed.department_key("  Semi-Auto   PISTOL "), "semi-auto pistol")
		self.assertEqual(lipseys_feed.department_key(None), "")

	def test_catalog_row_maps_prices_flags_and_content(self):
		row = _rows([catalog_item(1, currentPrice=280.0, price=300.0, retailMap=349.0,
			canDropship=False, description2="  9MM 4IN ", shippingWeight=2.25)])[0]
		self.assertEqual(row["sku"], sku(1))
		self.assertEqual(row["upc"], upc(1))
		self.assertEqual(float(row["cost"]), 280.0)
		self.assertEqual(float(row["map"]), 349.0)
		self.assertEqual(row["map_policy"], "M")
		self.assertTrue(row["dropship_blocked"])
		self.assertEqual(row["short_description"], "ACME TEST PISTOL 1 9MM 4IN")
		self.assertEqual(row["caliber"], "9MM")
		self.assertEqual(row["department"], "semi-auto pistol")
		self.assertEqual(row["department_name"], "Semi-Auto Pistol")
		self.assertEqual(row["manufacturer"], syn.BRAND)
		self.assertEqual(row["mfg_number"], "ATS-1")
		self.assertEqual(float(row["weight_lb"]), 2.25)
		self.assertTrue(row["ffl_required"])
		self.assertEqual(row["product_class"], "Handgun")
		self.assertEqual((row["qty"], row["status_flag"]), (5, ""))

	def test_cost_falls_back_to_price_and_a_zero_map_is_no_map(self):
		row = _rows([catalog_item(1, currentPrice=None, price=300.0, retailMap=0)])[0]
		self.assertEqual(float(row["cost"]), 300.0)
		self.assertEqual((float(row["map"]), row["map_policy"]), (0.0, ""))
		row = _rows([catalog_item(1, currentPrice=-1, price="junk", msrp=None)])[0]
		self.assertEqual((float(row["cost"]), float(row["msrp"])), (0.0, 0.0))

	def test_allocated_rows_have_no_stock(self):
		row = _rows([catalog_item(1, allocated=True, quantity=12)])[0]
		self.assertEqual((row["qty"], row["status_flag"]), (0, "Allocated"))
		self.assertEqual(_rows([catalog_item(1, quantity=-3)])[0]["qty"], 0)
		self.assertEqual(_rows([catalog_item(1, quantity=True)])[0]["qty"], 0)

	def test_a_numeric_upc_gets_its_leading_zeros_back(self):
		self.assertEqual(_rows([catalog_item(1, upc=99993000042)])[0]["upc"], "099993000042")
		self.assertEqual(_rows([catalog_item(1, upc="099993000042")])[0]["upc"], "099993000042")
		self.assertEqual(_rows([catalog_item(1, upc=None)])[0]["upc"], "")

	def test_image_url_is_lipseys_own_with_the_name_quoted_whole(self):
		row = _rows([catalog_item(1, imageName="ZZL 00001/a&b.jpg")])[0]
		want = "https://www.lipseyscloud.com/images/ZZL%2000001%2Fa%26b.jpg"
		self.assertEqual((row["image_url"], row["image_url_hires"]), (want, want))
		self.assertEqual(row["image_name"], "ZZL 00001/a&b.jpg")
		row = _rows([catalog_item(1, imageName=None)])[0]
		self.assertEqual((row["image_url"], row["image_url_hires"], row["image_name"]), ("", "", ""))

	def test_an_nfa_row_outside_the_nfa_types_is_rekeyed_and_stays_nfa(self):
		rows = _rows([
			catalog_item(1, type="Rifle", fflRequired=False, sotRequired=True),
			catalog_item(2, type=" nfa -  SILENCER ", sotRequired=True),
			catalog_item(3, type="", itemType="NFA", sotRequired=True)])
		self.assertEqual([r["department"] for r in rows],
			["nfa - other", "nfa - silencer", "nfa - other"])
		self.assertEqual(rows[0]["department_name"], "NFA - Other")
		self.assertEqual([r["product_class"] for r in rows], ["NFA", "NFA", "NFA"])
		self.assertTrue(all(r["ffl_required"] for r in rows))

	def test_rows_without_a_legal_flag_or_a_type_are_rejected_and_gated(self):
		items = [catalog_item(1), catalog_item(2, fflRequired=None), catalog_item(3, sotRequired="no"),
			catalog_item(4, type=None, itemType=None), catalog_item(5, itemNo=" "), "junk"]
		stats = {}
		rows = _rows(items, stats)
		self.assertEqual([r["sku"] for r in rows], [sku(1)])
		self.assertEqual((stats["total"], stats["rejected"]), (6, 5))
		self.assertEqual(lipseys_feed.catalog_skus(items), {sku(1), sku(2), sku(3), sku(4)})
		with patch.object(lipseys_feed, "REJECT_RATIO_FLOOR", 4), self.assertRaises(FeedFormatError):
			_rows(items)

	def test_muzzleloaders_and_revolvers_are_always_ffl_and_never_antique(self):
		rows = _rows([catalog_item(1, type="Muzzleloader", fflRequired=False),
			catalog_item(2, type="Revolver", fflRequired=False),
			catalog_item(3, type="Rifle", fflRequired=False)])
		self.assertEqual([r["ffl_required"] for r in rows], [True, True, False])
		self.assertEqual([r["product_class"] for r in rows], ["Other Firearm", "Handgun", "Accessory"])

	def test_pricing_rows(self):
		stats = {}
		rows = lipseys_feed.pricing_rows({"nextUpdate": "2026-01-01T02:00:00", "items": [
			pricing_item(1, qty=4, price=250.0, current=240.0),
			pricing_item(2, qty=9, allocated=True),
			pricing_item(3, qty=2, price=0),
			{"itemNumber": "  "}, "junk"]}, stats)
		self.assertEqual(rows[0][:3], (sku(1), 4, 240))
		self.assertEqual(rows[1], (sku(2), 0, 250, "Allocated"))
		self.assertEqual(rows[2], (sku(3), 2, None, ""))
		self.assertEqual((stats["rejected"], stats["next_update"]), (2, "2026-01-01T02:00:00"))
		for bad in ({"items": []}, {"items": None}, [], None):
			with self.assertRaises(FeedFormatError):
				lipseys_feed.pricing_rows(bad)


# (type, flagged class, unflagged class): the class the reviewed type map gives.
CLASS_CASES = (
	("Rifle", "Long Gun", "Accessory"),
	("Semi-Auto Pistol", "Handgun", "Accessory"),
	("Shotgun", "Long Gun", "Accessory"),
	("Specialty Handgun", "Handgun", "Accessory"),
	("Combo", "Long Gun", "Accessory"),
	("Other Firearm", "Other Firearm", "Accessory"),
	("Revolver", "Handgun", "Accessory"),
	("Muzzleloader", "Other Firearm", "Accessory"),
	("NFA - Silencer", "NFA", "Accessory"),
	("NFA - Short Barrel Rifle", "NFA", "Accessory"),
	("NFA - Short Barrel Shotgun", "NFA", "Accessory"),
	("NFA - Other", "NFA", "Accessory"),
	("Accessory-Lowers", "Other Firearm", "Gun Parts"),
	("Accessory-Frames", "Other Firearm", "Gun Parts"),
	("Accessory-Receiver Sets", "Other Firearm", "Gun Parts"),
	("Accessory-Actions", "Other Firearm", "Gun Parts"),
	("Accessory-Uppers", "Other Firearm", "Gun Parts"),
	("Accessory-Barrels", "Other Firearm", "Gun Parts"),
	("Accessory-Triggers", "Other Firearm", "Gun Parts"),
	("Accessory-Braces", "Other Firearm", "Gun Parts"),
	("Accessory-Slide Assemblies", "Other Firearm", "Gun Parts"),
	("Accessory-Kits", "Other Firearm", "Gun Parts"),
	("Accessory-Magazines", "Other Firearm", "Accessory"),
	("Accessory-Scopes", "Other Firearm", "Accessory"),
	("Accessory-Lights", "Other Firearm", "Accessory"),
	("Accessory-Holsters", "Other Firearm", "Accessory"),
	("Non-Lethal", "Other Firearm", "Accessory"),
	("Accessory-Ammunition", "Other Firearm", "Ammunition"),
	("A Type Nobody Mapped", "Other Firearm", "Accessory"),
)


class TestLipseysClassRule(unittest.TestCase):

	def test_the_type_map_is_the_reviewed_one(self):
		self.assertEqual(len(lipseys_feed.TYPE_MAP), 69)
		self.assertEqual(len(lipseys_feed.DEPARTMENTS), 69)
		self.assertLessEqual(lipseys_feed.NFA_KEYS, set(lipseys_feed.DEPARTMENTS))
		self.assertLessEqual(lipseys_feed.ALWAYS_FFL_KEYS, set(lipseys_feed.DEPARTMENTS))

	def test_mapped_types_get_their_class(self):
		for kind, flagged, unflagged in CLASS_CASES:
			self.assertEqual(lipseys_feed.product_class(kind, "ACME TEST ITEM", True), flagged, kind)
			self.assertEqual(lipseys_feed.product_class(kind, "ACME TEST ITEM", False), unflagged, kind)

	def test_every_mapped_type_answers_a_service_class(self):
		for kind, _node, _firearm in lipseys_feed.TYPE_MAP:
			for ffl in (True, False):
				self.assertIn(lipseys_feed.product_class(kind, "ACME TEST", ffl), PRODUCT_CLASSES, kind)

	def test_an_nfa_title_marks_a_flagged_row_only(self):
		self.assertEqual(lipseys_feed.product_class("Rifle", "ACME TEST 300BLK SBR", True), "NFA")
		self.assertEqual(lipseys_feed.product_class("Accessory-Silencer Accessories",
			"ACME TEST SUPPRESSOR MOUNT", False), "Gun Parts")
		self.assertEqual(lipseys_feed.product_class("Rifle", "ACME TEST SUPPRESSOR READY", True),
			"Long Gun")


class TestAttemptGate(unittest.TestCase):
	NOW = datetime(2026, 1, 10, 12, 0)
	EVERY = timedelta(hours=4)

	def due(self, attempts, *, force=False, saved=None):
		return lipseys.attempt_due(attempts, self.NOW, every=self.EVERY,
			max_backoff=timedelta(hours=24), force=force, force_floor=timedelta(minutes=30),
			creds_saved_at=saved)

	def ago(self, **kw):
		return self.NOW - timedelta(**kw)

	def test_first_attempt_and_the_cadence_after_a_success(self):
		self.assertTrue(self.due([]))
		self.assertFalse(self.due([("success", self.ago(hours=3), "")]))
		self.assertTrue(self.due([("success", self.ago(hours=4), "")]))

	def test_failures_back_off_doubling_and_capped(self):
		ok = ("success", self.ago(hours=20), "")
		self.assertTrue(self.due([("failed", self.ago(hours=4), "FeedUnavailable"), ok]))
		self.assertFalse(self.due([("failed", self.ago(hours=7), "FeedUnavailable"),
			("failed", self.ago(hours=12), "FeedUnavailable"), ok]))  # 2 failures: 8 hours
		self.assertTrue(self.due([("failed", self.ago(hours=8), "Interrupted"),
			("failed", self.ago(hours=12), "FeedUnavailable"), ok]))
		many = [("failed", self.ago(hours=23), "FeedUnavailable")] * 8
		self.assertFalse(self.due(many))
		many[0] = ("failed", self.ago(hours=24), "FeedUnavailable")
		self.assertTrue(self.due(many))  # capped at max_backoff

	def test_force_waits_the_floor_only(self):
		recent = [("success", self.ago(minutes=20), "")]
		self.assertFalse(self.due(recent, force=True))
		self.assertTrue(self.due([("failed", self.ago(minutes=31), "FeedUnavailable")] * 3, force=True))

	def test_an_auth_failure_holds_until_the_settings_are_saved_after_it(self):
		held = [("failed", self.ago(days=3), "FeedAuthError")]
		self.assertFalse(self.due(held))
		self.assertFalse(self.due(held, force=True))
		self.assertFalse(self.due(held, saved=self.ago(days=4)))
		self.assertTrue(self.due(held, saved=self.ago(days=2)))


# ---- the feed GET on the ordering client (pure)


class TestFeedGet(unittest.TestCase):

	def client(self, routes):
		session = FakeSession({LOGIN: login_ok(), **routes})
		return LipseysClient(_config(), session=session), session

	def test_get_sends_the_token_and_our_user_agent_and_deletes_its_file(self):
		made = []
		real = tempfile.mkstemp

		def spy(*a, **kw):
			fd, path = real(*a, **kw)
			made.append(path)
			return fd, path

		client, session = self.client({PRICING: pricing_feed([pricing_item(1)])})
		with patch.object(api_client.tempfile, "mkstemp", side_effect=spy):
			env = client.get_feed(PRICING, max_bytes=1 << 20, deadline_s=30)
		self.assertEqual(env["data"]["items"][0]["itemNumber"], sku(1))
		call = session.calls[-1]
		self.assertEqual((call["method"], call["path"]), ("GET", PRICING))
		self.assertEqual(str(call["headers"]["Token"]), syn.TOKEN)
		self.assertTrue(call["headers"]["User-Agent"].startswith("opengunstore-lipseys/"))
		self.assertEqual(len(made), 1)
		self.assertTrue(os.path.basename(made[0]).startswith(api_client.FEED_TMP_PREFIX))
		self.assertFalse(os.path.exists(made[0]))

	def test_an_expired_token_relogs_once_then_is_not_an_auth_failure(self):
		client, session = self.client({PRICING: [UNAUTHORIZED, pricing_feed([pricing_item(1)])]})
		client.get_feed(PRICING, max_bytes=1 << 20, deadline_s=30)
		self.assertEqual(session.paths(), [LOGIN, PRICING, LOGIN, PRICING])
		client, _ = self.client({PRICING: [UNAUTHORIZED, UNAUTHORIZED]})
		with self.assertRaises(LipseysError) as ctx:
			client.get_feed(PRICING, max_bytes=1 << 20, deadline_s=30)
		self.assertNotIsInstance(ctx.exception, LipseysAuthError)

	def test_bad_answers_are_refused(self):
		for answer in (Resp({"x": 1}, status=500), Resp("<html>blocked</html>", content_type="text/html"),
				Resp(b"{not json"), Resp([1, 2]), syn.TIMEOUT):
			client, _ = self.client({PRICING: answer})
			with self.assertRaises(LipseysError, msg=repr(answer)):
				client.get_feed(PRICING, max_bytes=1 << 20, deadline_s=30)
		client, _ = self.client({PRICING: pricing_feed([pricing_item(n) for n in range(1, 50)])})
		with self.assertRaises(LipseysError) as ctx:
			client.get_feed(PRICING, max_bytes=512, deadline_s=30)
		self.assertIn("over", str(ctx.exception))

	def test_fetch_maps_a_refused_login_to_an_auth_failure(self):
		for answer in (login_refused(), Resp({}, status=403), Resp({}, status=401)):
			session = FakeSession({LOGIN: answer})
			with self.assertRaises(FeedAuthError, msg=repr(answer)):
				lipseys.fetch(PRICING, expect=dict, max_bytes=1 << 20, deadline_s=30,
					client=LipseysClient(_config(), session=session))
			self.assertTrue(session.closed)
		session = FakeSession({LOGIN: Resp({}, status=503)})
		with self.assertRaises(FeedUnavailable) as ctx:
			lipseys.fetch(PRICING, expect=dict, max_bytes=1 << 20, deadline_s=30,
				client=LipseysClient(_config(), session=session))
		self.assertNotIsInstance(ctx.exception, FeedAuthError)
		session = FakeSession({LOGIN: login_ok(cus_no="900002")})
		with self.assertRaises(FeedAuthError):
			lipseys.fetch(PRICING, expect=dict, max_bytes=1 << 20, deadline_s=30,
				client=LipseysClient(_config(customer_number="900001"), session=session))

	def test_fetch_refuses_an_unsuccessful_or_empty_envelope(self):
		for answer in (envelope(None, success=False, errors=["boom from dealer@example.com"]),
				envelope([]), envelope({"items": []}, success=True)):
			session = FakeSession({LOGIN: login_ok(), CATALOG: answer})
			with self.assertRaises(FeedFormatError) as ctx:
				lipseys.fetch(CATALOG, expect=list, max_bytes=1 << 20, deadline_s=30,
					client=LipseysClient(_config(), session=session))
			self.assertNotIn("dealer@example.com", str(ctx.exception))


# ---- lanes (database)


def _purge() -> None:
	for table in ("dc_offer", "dc_product_move", "dc_restriction", "dc_meta"):
		frappe.db.sql(f"DELETE FROM {table} WHERE distributor = %s", (TCODE,))
	frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("99993%",))
	frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("099993%",))
	frappe.db.sql("DELETE FROM dc_sync_run WHERE source LIKE %s", (TCODE + "\\_%",))
	frappe.db.commit()


def _age_all(**kw) -> None:
	"""Move every run of the test house back in time; the attempt stamps (always
	older than their run's row) are dropped with them."""
	seconds = int(timedelta(**kw).total_seconds())
	frappe.db.sql("""UPDATE dc_sync_run SET started_at = started_at - INTERVAL %s SECOND,
		finished_at = finished_at - INTERVAL %s SECOND WHERE source LIKE %s""",
		(seconds, seconds, TCODE + "\\_%"))
	frappe.db.sql("UPDATE dc_meta SET lane_attempts = NULL WHERE distributor = %s", (TCODE,))
	frappe.db.commit()


def _runs() -> list:
	return [tuple(r) for r in frappe.db.sql("""SELECT source, status, error_class FROM dc_sync_run
		WHERE source LIKE %s ORDER BY id""", (TCODE + "\\_%",))]


def _offer(s: str):
	return frappe.db.sql("""SELECT cost, qty, discontinued, status_flag, ffl_required, product_class,
		department, department_name, image_url, change_seq FROM dc_offer
		WHERE distributor = %s AND sku = %s""", (TCODE, s), as_dict=True)[0]


def _count() -> int:
	return int(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor = %s", (TCODE,))[0][0])


N = 60  # 2% of 60 live rows is 1.2: one row may vanish, two may not


class _LipseysBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		schema.ensure()  # dc_meta.creds_saved_at on a site migrated before it

	def setUp(self):
		catalog_stub.uninstall()
		_purge()
		self.items = [catalog_item(n) for n in range(1, N + 1)]
		self.prices = [pricing_item(n, qty=n % 9, price=200.0 + n) for n in range(1, N + 1)]
		self.session = FakeSession({LOGIN: login_ok(),
			CATALOG: lambda _body: catalog_feed(self.items),
			PRICING: lambda _body: pricing_feed(self.prices)})
		self.driver = sync.register(lipseys.LipseysDriver(code=TCODE,
			client_factory=lambda: LipseysClient(_config(), session=self.session)))
		self.addCleanup(sync.unregister, TCODE)
		self.addCleanup(_purge)
		floor = patch.object(lipseys, "MIN_ROWS", 0)  # the production floor is far above 60
		floor.start()
		self.addCleanup(floor.stop)
		disk = patch.object(lipseys.rsr, "check_disk")  # a test box's free disk is not under test
		disk.start()
		self.addCleanup(disk.stop)

	def dials(self) -> list:
		return [c["path"] for c in self.session.calls if c["path"] != LOGIN]


class TestLipseysLanes(_LipseysBase):

	def test_first_run_loads_the_catalog_then_the_pricing(self):
		out = sync.run_house(TCODE)
		self.assertEqual(out["_failed"], [])
		self.assertEqual(self.dials(), [CATALOG, PRICING])
		self.assertEqual(_runs(), [(TCODE + "_catalog", "success", ""),
			(TCODE + "_quantity", "success", "")])
		self.assertEqual(out["catalog"]["created"], N)
		self.assertEqual(_count(), N)
		one = _offer(sku(1))
		self.assertEqual((one["qty"], float(one["cost"])), (1, 201.0))  # the pricing lane's values
		self.assertEqual(one["department_name"], "Semi-Auto Pistol")
		self.assertEqual(one["image_url"], "https://www.lipseyscloud.com/images/ZZL00001.jpg")
		self.assertEqual((one["ffl_required"], one["product_class"]), (1, "Handgun"))
		# Both lanes are fresh now: nothing dials.
		self.session.calls.clear()
		out = sync.run_house(TCODE)
		self.assertEqual(self.dials(), [])
		self.assertIsNone(out["catalog"])

	def test_the_catalog_never_overwrites_the_pricing_lanes_values(self):
		sync.run_house(TCODE)
		self.prices[1] = pricing_item(2, qty=9, allocated=True)
		_age_all(hours=1)
		sync.run_lane(TCODE, "quantity")
		self.assertEqual((_offer(sku(2))["qty"], _offer(sku(2))["status_flag"]), (0, "Allocated"))
		seq = _offer(sku(1))["change_seq"]
		self.items[0] = catalog_item(1, quantity=77, currentPrice=5.0, description1="ACME TEST RENAMED")
		self.items[1] = catalog_item(2, allocated=False, quantity=40)
		self.items.append(catalog_item(N + 1, quantity=3, currentPrice=99.0))
		_age_all(hours=4)
		out = sync.run_lane(TCODE, "catalog")
		self.assertEqual((out["created"], out["updated"]), (1, 1))
		one = _offer(sku(1))
		self.assertEqual((one["qty"], float(one["cost"])), (1, 201.0))
		self.assertGreater(one["change_seq"], seq)  # the new title moved it
		self.assertEqual((_offer(sku(2))["qty"], _offer(sku(2))["status_flag"]), (0, "Allocated"))
		new = _offer(sku(N + 1))  # a row born in this load is seeded from the catalog
		self.assertEqual((new["qty"], float(new["cost"])), (3, 99.0))

	def test_vanished_rows_are_retired_below_the_bar_and_wait_for_a_forced_run_above_it(self):
		sync.run_house(TCODE)
		del self.items[0]
		_age_all(hours=4)
		out = sync.run_lane(TCODE, "catalog")
		self.assertEqual(out["disappeared"], 1)
		gone = _offer(sku(1))
		self.assertEqual((gone["discontinued"], gone["status_flag"], gone["qty"]), (1, "Discontinued", 0))
		del self.items[0:2]  # 2 of 59 live rows: over 2%
		self.items[0] = catalog_item(4, description1="ACME TEST STILL LOADED")
		_age_all(hours=4)
		self.assertIsNone(sync.run_lane(TCODE, "catalog"))
		self.assertEqual(_runs()[-1], (TCODE + "_catalog", "failed", "FeedFormatError"))
		self.assertEqual(_offer(sku(2))["discontinued"], 0)  # not retired
		self.assertEqual(LocalCatalog().rows(TCODE, [sku(4)])[sku(4)]["short_description"],
			"ACME TEST STILL LOADED 9MM 4IN")  # but the file loaded
		# Sync now opens the schedule, never the guard.
		_age_all(hours=1)
		self.assertIsNone(sync.run_lane(TCODE, "catalog", skip_schedule=True))
		self.assertEqual(_offer(sku(2))["discontinued"], 0)
		_age_all(hours=1)
		out = sync.run_lane(TCODE, "catalog", force=True)
		self.assertEqual(out["disappeared"], 2)
		self.assertEqual(_offer(sku(2))["status_flag"], "Discontinued")

	def test_a_retired_row_that_comes_back_is_revived_with_the_files_stock(self):
		sync.run_house(TCODE)
		del self.items[0]
		_age_all(hours=4)
		sync.run_lane(TCODE, "catalog")
		self.assertEqual(_offer(sku(1))["status_flag"], "Discontinued")
		self.items.insert(0, catalog_item(1, quantity=6, currentPrice=150.0))
		_age_all(hours=4)
		out = sync.run_lane(TCODE, "catalog")
		self.assertEqual(out["revived"], 1)
		back = _offer(sku(1))
		self.assertEqual((back["discontinued"], back["status_flag"], back["qty"], float(back["cost"])),
			(0, "", 6, 150.0))
		self.assertEqual(LocalCatalog().rows(TCODE, [sku(1)])[sku(1)]["qty"], 6)

	def test_the_catalog_floor_and_the_reject_gate_refuse_before_any_row_changes(self):
		with patch.object(lipseys, "MIN_ROWS", N + 1):
			sync.run_house(TCODE)
		self.assertEqual(_runs(), [(TCODE + "_catalog", "failed", "ValueError")])
		self.assertEqual(_count(), 0)
		self.items[0] = catalog_item(1, fflRequired=None)
		_age_all(hours=25)
		with patch.object(lipseys_feed, "REJECT_RATIO_FLOOR", 10):
			sync.run_lane(TCODE, "catalog")
		self.assertEqual(_runs()[-1], (TCODE + "_catalog", "failed", "FeedFormatError"))
		self.assertEqual(_count(), 0)

	def test_quantity_waits_for_the_first_catalog(self):
		self.assertEqual(sync.run_lane(TCODE, "quantity"), {"skipped": "not_due"})
		self.assertEqual(self.dials(), [])

	def test_quantity_guards_and_absent_rows(self):
		sync.run_house(TCODE)
		# A file that matches the catalog for under 90% of its rows is the wrong file.
		real = self.prices
		self.prices = [pricing_item(n) for n in range(1000, 1000 + N)]
		_age_all(hours=1)
		self.assertIsNone(sync.run_lane(TCODE, "quantity"))
		self.assertEqual(_runs()[-1], (TCODE + "_quantity", "failed", "FeedFormatError"))
		# A live row the file leaves out has no stock (up to 10% of the live rows).
		self.prices = [p for p in real if p["itemNumber"] != sku(5)]
		_age_all(hours=1)
		out = sync.run_lane(TCODE, "quantity")
		self.assertEqual(out["absent"], 1)
		self.assertEqual(_offer(sku(5))["qty"], 0)
		self.assertEqual(float(_offer(sku(5))["cost"]), 205.0)  # no price in the file: kept
		self.prices = real[: N - 7]
		_age_all(hours=1)
		self.assertIsNone(sync.run_lane(TCODE, "quantity"))
		self.assertEqual(_runs()[-1], (TCODE + "_quantity", "failed", "FeedFormatError"))

	def test_a_refused_login_holds_both_lanes_until_the_settings_are_saved(self):
		sync.run_house(TCODE)
		self.session.routes[LOGIN] = login_refused()
		_age_all(hours=5)
		sync.run_house(TCODE)
		self.assertEqual(_runs()[-2:], [(TCODE + "_catalog", "failed", "FeedAuthError"),
			(TCODE + "_quantity", "failed", "FeedAuthError")])
		msg = frappe.db.sql("""SELECT error_msg FROM dc_sync_run WHERE source = %s
			ORDER BY id DESC LIMIT 1""", (TCODE + "_catalog",))[0][0]
		self.assertIn("Lipseys Settings is saved again", msg)
		self.assertNotIn("dealer@example.com", msg)
		self.session.calls.clear()
		_age_all(days=2)
		for kw in ({}, {"skip_schedule": True}, {"force": True}):
			out = sync.run_house(TCODE, **kw)
			self.assertEqual((out["catalog"], out["quantity"]), (None, None), kw)
		self.assertEqual(self.session.calls, [])  # no login at all while held
		lipseys.note_credentials_saved(TCODE)
		frappe.db.commit()
		self.session.routes[LOGIN] = login_ok()
		out = sync.run_house(TCODE)
		self.assertEqual(out["_failed"], [])
		self.assertEqual(self.dials(), [CATALOG, PRICING])

	def test_a_failing_lane_backs_off_and_sync_now_waits_out_the_floor(self):
		self.session.routes[CATALOG] = Resp({}, status=502)
		sync.run_house(TCODE)
		self.assertEqual(_runs(), [(TCODE + "_catalog", "failed", "FeedUnavailable")])
		self.session.calls.clear()
		self.assertEqual(sync.run_lane(TCODE, "catalog", skip_schedule=True), {"skipped": "not_due"})
		_age_all(minutes=31)
		self.assertIsNone(sync.run_lane(TCODE, "catalog", skip_schedule=True))  # dials, fails again
		self.assertEqual(self.dials(), [CATALOG])
		_age_all(hours=5)  # two failures in a row: 3 h 50 min x 2
		self.assertEqual(sync.run_lane(TCODE, "catalog"), {"skipped": "not_due"})
		_age_all(hours=3)
		self.session.routes[CATALOG] = lambda _body: catalog_feed(self.items)
		self.assertEqual(sync.run_lane(TCODE, "catalog")["created"], N)

	def test_a_run_that_died_counts_as_a_failed_attempt(self):
		sync.run_house(TCODE)
		_age_all(hours=5)
		sync.mark_attempt(TCODE, "catalog")  # stamped, and no run row followed
		self.assertEqual(lipseys.recent_attempts(TCODE, "catalog")[0][::2], ("failed", "Interrupted"))
		self.assertEqual(sync.run_lane(TCODE, "catalog"), {"skipped": "not_due"})

	def test_a_lock_skip_is_not_an_attempt(self):
		sync.run_house(TCODE)
		_age_all(hours=5)
		other = _other_connection(self)
		self.assertEqual(other.sql("SELECT GET_LOCK(%s, 0)", (store.lock_name(TCODE),))[0][0], 1)
		try:
			out = sync.run_lane(TCODE, "catalog")
		finally:
			other.sql("SELECT RELEASE_LOCK(%s)", (store.lock_name(TCODE),))
		self.assertTrue(out.get("lock_skipped"))
		self.assertNotIn("catalog", sync.lane_attempts().get(TCODE, {}))
		self.assertEqual(lipseys.recent_attempts(TCODE, "catalog")[0][0], "success")
		self.assertIsInstance(sync.run_lane(TCODE, "catalog"), dict)

	def test_the_disk_check_runs_before_the_catalog_dials(self):
		with patch.object(lipseys.rsr, "check_disk", side_effect=FeedUnavailable("no room")) as disk:
			sync.run_house(TCODE)
		disk.assert_called_once()
		self.assertEqual(disk.call_args.args[1], lipseys.CATALOG_MAX_BYTES)
		self.assertEqual(self.dials(), [])
		self.assertEqual(_runs(), [(TCODE + "_catalog", "failed", "FeedUnavailable")])

	def test_the_shipped_driver_registers_with_its_two_lanes(self):
		shipped = sync.load_drivers()["lipseys"]
		self.assertIsInstance(shipped, lipseys.LipseysDriver)
		self.assertEqual([ln.name for ln in shipped.lanes()], ["catalog", "quantity"])
		self.assertEqual([ln.heavy for ln in shipped.lanes()], [True, False])
		self.assertEqual(shipped.lane("catalog").timeout, sync.HEAVY_TIMEOUT_SECONDS)
		self.assertEqual((shipped.stale_after("quantity"), shipped.stale_after("catalog")),
			(10_800, 43_200))
		self.assertEqual(shipped.label, "Lipsey's")


def _other_connection(case):
	from frappe.database import get_db

	conf = frappe.local.conf
	other = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
		user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
	other.connect()
	case.addCleanup(other.close)
	return other


class TestLipseysLegalAndContract(_LipseysBase):

	def test_classes_and_legal_answers_from_the_local_catalog(self):
		from ffl_integrations.distributor.util import legal_firearm

		self.items[:6] = [
			catalog_item(1),
			catalog_item(2, type="NFA - Silencer", sotRequired=True, description1="ACME TEST CAN"),
			catalog_item(3, type="Accessory-Lowers", description1="ACME TEST STRIPPED LOWER"),
			catalog_item(4, type="Accessory-Holsters", fflRequired=False),
			catalog_item(5, type="Muzzleloader", fflRequired=False),
			catalog_item(6, type="Accessory-Barrels", fflRequired=False)]
		sync.run_house(TCODE)
		with patch(LOCAL, return_value=True):
			frappe.local._remote_catalog_memo = {}
			rows = LocalCatalog().rows(TCODE, [sku(n) for n in range(1, 7)])
			want = {1: ("Handgun", True), 2: ("NFA", True), 3: ("Other Firearm", True),
				4: ("Accessory", False), 5: ("Other Firearm", True), 6: ("Gun Parts", False)}
			for n, (klass, firearm) in want.items():
				row = rows[sku(n)]
				self.assertEqual((row["product_class"], row["ffl_required"]), (klass, firearm), n)
				self.assertIs(legal_firearm(row, "lipseys"), firearm, n)
			# No Lipsey's type is owner-ruled "never a firearm": no product carries a legal answer.
			for n in range(1, 7):
				self.assertIsNone(LocalCatalog().products_batch([upc(n)])["products"][0]["legal"], n)

	def test_contract_check_passes_against_a_lipseys_fed_local_catalog(self):
		sync.run_house(TCODE)
		results = check(StubTransport(LocalCatalog()), TCODE, sku(1))
		failed = [r for r in results if not r["ok"]]
		self.assertEqual(failed, [], "\n".join(f"{r['name']}: {r['detail']}" for r in failed))


# ---- Lipseys Settings, Test Connection and the Distributor hub


SETTINGS = lipseys.SETTINGS
HUB_ROW = "HUB-LOCAL-LIPSEYS"


class TestLipseysSettingsFeed(FrappeTestCase):

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		schema.ensure()

	def setUp(self):
		from frappe.utils.password import get_decrypted_password

		names = ("dealer_email", "customer_number", "base_url", "last_error")
		before = {f: frappe.db.get_single_value(SETTINGS, f) for f in names}
		pw = get_decrypted_password(SETTINGS, SETTINGS, "dealer_password", raise_exception=False)
		self.addCleanup(self._restore, before, pw)

	def _restore(self, before, pw):
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		for field, value in before.items():
			frappe.db.set_single_value(SETTINGS, field, value)
		if pw:
			set_encrypted_password(SETTINGS, SETTINGS, pw, "dealer_password")
		else:
			remove_encrypted_password(SETTINGS, SETTINGS, "dealer_password")
		frappe.db.sql("DELETE FROM dc_meta WHERE distributor = %s", (lipseys.CODE,))
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		frappe.db.commit()

	def _save(self, email, password, base_url="", customer=""):
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		frappe.db.set_single_value(SETTINGS, "dealer_email", email)
		frappe.db.set_single_value(SETTINGS, "customer_number", customer)
		frappe.db.set_single_value(SETTINGS, "base_url", base_url)
		if password:
			set_encrypted_password(SETTINGS, SETTINGS, password, "dealer_password")
		else:
			remove_encrypted_password(SETTINGS, SETTINGS, "dealer_password")
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_credentials_read_the_dealer_login(self):
		self._save("", "pw-test-not-real")
		self.assertIsNone(lipseys.credentials())
		self._save("dealer@example.com", "")
		self.assertIsNone(lipseys.credentials())
		self._save(" dealer@example.com ", "pw-test-not-real", customer=" 900001 ")
		c = lipseys.credentials()
		self.assertEqual((c.email, c.password, c.customer_number, c.base_url),
			("dealer@example.com", "pw-test-not-real", "900001", api_client.DEFAULT_BASE_URL))
		self.assertNotIn("pw-test-not-real", repr(c))

	def test_missing_or_insecure_settings_hold_the_lane(self):
		self._save("", "")
		with self.assertRaises(FeedAuthError):
			lipseys.fetch(PRICING, expect=dict, max_bytes=1 << 20, deadline_s=30)
		self._save("dealer@example.com", "pw-test-not-real", base_url="http://api.example.invalid/x")
		with self.assertRaises(FeedAuthError):
			lipseys.fetch(PRICING, expect=dict, max_bytes=1 << 20, deadline_s=30)

	def test_configured_needs_an_enabled_distributor_and_the_dealer_email(self):
		driver = lipseys.LipseysDriver()
		self._save("dealer@example.com", "")
		with patch.object(frappe.db, "exists", return_value=True):
			self.assertTrue(driver.configured())
			self._save("", "")
			self.assertFalse(driver.configured())
		self._save("dealer@example.com", "")
		with patch.object(frappe.db, "exists", return_value=False):
			self.assertFalse(driver.configured())

	def test_saving_the_settings_lifts_the_auth_hold(self):
		frappe.db.sql("DELETE FROM dc_meta WHERE distributor = %s", (lipseys.CODE,))
		before = store.utc_now()
		doc = frappe.get_single(SETTINGS)
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
		saved = lipseys.credentials_saved_at()
		self.assertIsNotNone(saved)
		self.assertGreaterEqual(saved, before)
		# The shipping poll's own writes are not a save.
		frappe.db.set_single_value(SETTINGS, "last_error", "synthetic")
		self.assertEqual(lipseys.credentials_saved_at(), saved)

	def test_connection_test_adds_the_feed_row_only_in_local_mode(self):
		from ffl_integrations.distributor.lipseys_provider import LipseysProvider

		label = "Catalog feed (dealer login)"

		def checks(local):
			with patch(LOCAL, return_value=local), \
					patch.object(LipseysProvider, "catalog_health_check",
						return_value={"label": "Catalog", "ok": True, "detail": "fine"}):
				return {c["label"]: c for c in LipseysProvider().connection_test()["checks"]}

		self.assertNotIn(label, checks(False))
		self._save("", "")
		self.assertFalse(checks(True)[label]["ok"])
		self._save("dealer@example.com", "pw-test-not-real")
		with patch.object(lipseys, "fetch", return_value={"items": [1, 2, 3]}):
			row = checks(True)[label]
		self.assertEqual((row["ok"], row["detail"]), (True, "Signed in; 3 items priced."))
		with patch.object(lipseys, "fetch", side_effect=FeedAuthError("Lipsey's refused the login")):
			row = checks(True)[label]
		self.assertFalse(row["ok"])
		self.assertNotIn("pw-test-not-real", str(row))


class TestLipseysHub(FrappeTestCase):

	def setUp(self):
		if frappe.db.exists("Distributor", HUB_ROW):
			frappe.delete_doc("Distributor", HUB_ROW, ignore_permissions=True, force=True)
		frappe.get_doc({"doctype": "Distributor", "code": HUB_ROW, "distributor_name": "Hub Lipsey's",
			"provider": "lipseys", "enabled": 1, "dropship_enabled": 0, "stocking_enabled": 0,
			"poll_enabled": 0}).insert(ignore_permissions=True)

		def cleanup():
			if frappe.db.exists("Distributor", HUB_ROW):
				frappe.delete_doc("Distributor", HUB_ROW, ignore_permissions=True, force=True)
			frappe.db.commit()

		self.addCleanup(cleanup)

	def test_sync_now_queues_both_lanes_and_the_card_shows_them(self):
		from ffl_integrations.distributor import hub

		queued = []
		with patch(LOCAL, return_value=True), \
				patch.object(lipseys, "dealer_email", return_value="dealer@example.com"), \
				patch.object(sync, "worker_running", return_value=True), \
				patch.object(frappe, "enqueue", side_effect=lambda m, **kw: queued.append(kw) or object()):
			out = hub.sync_now(HUB_ROW)
		self.assertEqual(out["enqueued"], ["lipseys_catalog", "lipseys_quantity"])
		self.assertEqual([(kw["code"], kw["lane"], kw["skip_schedule"]) for kw in queued],
			[("lipseys", "catalog", True), ("lipseys", "quantity", True)])
		for kw in queued:
			self.assertNotIn("force", kw)
			self.assertEqual(kw["queue"], "catalog")
		with patch(LOCAL, return_value=True), \
				patch.object(lipseys, "dealer_email", return_value=""), \
				patch.object(frappe, "enqueue") as enqueue, self.assertRaises(frappe.ValidationError):
			hub.sync_now(HUB_ROW)
		enqueue.assert_not_called()
		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True):
			overview = hub.overview()
		row = next(r for r in overview["distributors"] if r["name"] == HUB_ROW)
		self.assertEqual([ln["lane"] for ln in row["catalog_lanes"]["lanes"]], ["catalog", "quantity"])
