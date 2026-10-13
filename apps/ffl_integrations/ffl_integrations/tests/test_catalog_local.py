"""Local catalog (L1): the backend seam, the dc_* store, the sync framework and
the catalog queue — fed by the synthetic ``testhouse`` driver.

The store commits (it is a loader), so every test here starts and ends by
purging the test houses' dc_* rows, and none of them creates DocType records.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations import catalog_local, catalog_master
from ffl_integrations.catalog_contract import check
from ffl_integrations.catalog_contract.transports import StubTransport
from ffl_integrations.catalog_local import backend as local_backend
from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.backend import LocalCatalog
from ffl_integrations.data_service import client
from ffl_integrations.distributor import catalog, remote_catalog
from ffl_integrations.tests import catalog_stub, catalog_testhouse
from ffl_integrations.tests.catalog_testhouse import CODE, CODE2, make_row

LOCAL = "ffl_integrations.catalog_local.is_local"


def _seq(code=CODE) -> int:
	row = frappe.db.sql("SELECT change_seq FROM dc_meta WHERE distributor = %s", (code,))
	return int(row[0][0]) if row else 0


def _offer_seqs(code=CODE) -> dict:
	return dict(frappe.db.sql("SELECT sku, change_seq FROM dc_offer WHERE distributor = %s", (code,)))


def _runs(code=CODE) -> list:
	return frappe.db.sql("""SELECT source, status, error_class FROM dc_sync_run
		WHERE source LIKE %s ORDER BY id""", (code + "\\_%",))


class _Base(FrappeTestCase):
	def setUp(self):
		catalog_stub.uninstall()
		catalog_testhouse.purge()
		self.driver = catalog_testhouse.install(CODE)
		self.addCleanup(catalog_testhouse.uninstall)
		self.addCleanup(catalog_testhouse.purge)

	def load(self, rows, code=CODE, **kw):
		return store.load_catalog(iter(rows), distributor=code, **kw)

	def _other_connection(self):
		from frappe.database import get_db

		conf = frappe.local.conf
		other = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
			user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
		other.connect()
		self.addCleanup(other.close)
		return other

	def assert_seq_covers(self, code=CODE):
		"""No number handed out to an offer or a UPC move is above the house
		counter (the next write would hand it out again)."""
		top = frappe.db.sql("""SELECT GREATEST(
			COALESCE((SELECT MAX(change_seq) FROM dc_offer WHERE distributor = %(c)s), 0),
			COALESCE((SELECT MAX(change_seq) FROM dc_product_move WHERE distributor = %(c)s), 0))""",
			{"c": code})[0][0]
		self.assertLessEqual(int(top), _seq(code))


# ---------------------------------------------------------------- AC 1 / AC 2

class TestBackendSeam(_Base):
	def test_setting_selects_the_local_backend(self):
		"""Data Service Settings.catalog_source is the switch; a test stub still wins."""
		before = frappe.db.get_single_value("Data Service Settings", "catalog_source")
		try:
			frappe.db.set_single_value("Data Service Settings", "catalog_source", "Local")
			frappe.db.value_cache.clear()
			self.assertTrue(catalog_local.is_local())
			self.assertIsInstance(remote_catalog._backend(), LocalCatalog)
			stub = catalog_stub.install()
			self.assertIs(remote_catalog._backend(), stub)
			catalog_stub.uninstall()
		finally:
			frappe.db.set_single_value("Data Service Settings", "catalog_source",
				before or "Data Service")
			frappe.db.value_cache.clear()
		self.assertFalse(catalog_local.is_local())
		with catalog_stub.data_service_configured(), \
				self.assertRaises(remote_catalog.CatalogStubMissing):
			remote_catalog._backend()

	def test_data_service_mode_uses_the_client_and_reads_no_dc_table(self):
		"""AC 1: with the default source every catalog read goes over the client,
		exactly as before, and no query names a dc_* table."""
		calls = []

		def fake_get(path, params=None, **kw):
			calls.append(("GET", path))
			if path.endswith("/health"):
				return {"distributor": "rsr", "offers_active": 1}
			if path.endswith("/changes"):
				return {"keys": [], "cursor": "", "more": False}
			return {}

		def fake_post(path, payload, **kw):
			calls.append(("POST", path))
			return {}

		real_sql = frappe.db.sql
		seen_sql = []

		def spy_sql(query, *a, **kw):
			seen_sql.append(str(query))
			return real_sql(query, *a, **kw)

		cache_keys = ("rc_health:rsr", "rc_ops_status:rsr:None", "rc_ops_history:rsr:20",
			"rc_ops_flags:rsr", "rc_row:rsr:X-1")

		def forget():
			for key in cache_keys:
				frappe.cache().delete_value(key)

		forget()
		self.addCleanup(forget)
		with catalog_stub.data_service_configured(), \
				patch.dict(frappe.flags, {"in_test": False}), \
				patch.object(client, "get", side_effect=fake_get), \
				patch.object(client, "post", side_effect=fake_post), \
				patch.object(frappe.db, "sql", side_effect=spy_sql):
			remote_catalog.rows("rsr", ["X-1"])
			remote_catalog.cached_row("rsr", "X-1")
			remote_catalog.search(None, "acme", 5)
			remote_catalog.health("rsr")
			remote_catalog.item_by_upc("999900000001")
			remote_catalog.restrictions_entries("rsr", "X-1")
			remote_catalog.ops_status("rsr")
			remote_catalog.ops_history("rsr")
			remote_catalog.ops_flags("rsr")
			catalog.products_page({}, in_houses=[])
			catalog.batch(["999900000001"], in_houses=[])
			catalog.legal_of(["999900000001"])
			catalog.changes("")
			catalog.facets()
			catalog_master.fetch_tree()
		paths = {p for _m, p in calls}
		for wire in ("/v1/catalog/rows", "/v1/catalog/search", "/v1/catalog/health",
				"/v1/catalog/item/999900000001", "/v1/catalog/restrictions/rsr/X-1",
				"/v1/catalog/ops/status", "/v1/catalog/ops/history", "/v1/catalog/ops/flags",
				"/v1/catalog/products", "/v1/catalog/products/batch", "/v1/catalog/products/changes",
				"/v1/catalog/products/facets", "/v1/catalog/categories"):
			self.assertIn(wire, paths)
		self.assertEqual([q for q in seen_sql if "dc_" in q], [])

	def test_local_mode_never_reaches_the_client(self):
		"""AC 2: the client patched to raise; every catalog operation still answers."""
		self.load([make_row(1), make_row(2, upc="")])
		self.addCleanup(catalog_stub.uninstall)  # clears the per-request row memo
		boom = AssertionError("catalog call reached data_service.client")
		houses = [{"name": "Test House", "provider": CODE}]
		with patch(LOCAL, return_value=True), \
				patch.object(client, "get", side_effect=boom), \
				patch.object(client, "post", side_effect=boom), \
				patch.object(client, "_request", side_effect=boom):
			self.assertEqual(set(remote_catalog.rows(CODE, ["TH-00001", "NOPE"])), {"TH-00001"})
			remote_catalog.prefetch(CODE, ["TH-00001"])
			self.assertEqual(remote_catalog.cached_row(CODE, "TH-00002")["sku"], "TH-00002")
			self.assertTrue(remote_catalog.search(CODE, "widget", 5))
			self.assertTrue(remote_catalog.search(None, "TH-00001", 5))
			self.assertEqual(remote_catalog.health(CODE)["offers_total"], 2)
			self.assertEqual(len(remote_catalog.item_by_upc("999900000001")["offers"]), 1)
			self.assertEqual(remote_catalog.restrictions_entries(CODE, "TH-00001"), [])
			remote_catalog.classify([{"ffl_required": True, "sku": "TH-00001", "distributor": CODE}])
			self.assertTrue(remote_catalog.ops_status(CODE)["known"])
			remote_catalog.ops_history(CODE)
			remote_catalog.ops_flags(CODE)
			page = catalog.products_page({}, in_houses=houses, count=True)
			self.assertEqual(page["total"], 2)
			self.assertEqual(page["products"][0]["offers"][0]["distributor"], "Test House")
			self.assertEqual(len(list(catalog.iter_products({}, in_houses=houses))), 2)
			self.assertEqual(set(catalog.batch(["999900000001", f"{CODE}:TH-00002"],
				in_houses=houses)), {"999900000001", f"{CODE}:TH-00002"})
			self.assertIn("999900000001", catalog.legal_of(["999900000001"]))
			self.assertIsNotNone(catalog.product("999900000001", in_houses=houses))
			self.assertIn("999900000001", catalog.changes("")["keys"])
			self.assertTrue(catalog.facets(CODE)["brands"])
			self.assertEqual(catalog.count({}, in_houses=[]), 2)
			self.assertEqual(catalog.keys_for(CODE, ["TH-00001"]), {"TH-00001": "999900000001"})
			self.assertEqual(catalog_master.fetch_tree(), ([], "0"))

	def test_search_ranks_by_relevance_not_by_upc(self):
		"""Shapes from the real feeds: a holster whose number holds "19" sorts before the
		pistol by UPC; Sports South spells the Holosun 507 "HS507COMP"."""
		self.load([
			make_row(1, manufacturer="Bianchi", model="Black Widow", qty=5,
				short_description="BIANCHI 15190 OWB HOLSTER FITS GLOCK 23"),
			make_row(2, manufacturer="Glock", model="19", qty=0, short_description="GLOCK 19 GEN5 9MM"),
			make_row(3, manufacturer="Holosun", model="", qty=0, short_description="HOLOSUN HS507COMP REFLEX"),
			make_row(4, manufacturer="Glock", model="Mag", qty=0, short_description="GLOCK MAG 17RD"),
			make_row(5, manufacturer="Glock", model="Mag", qty=3, short_description="GLOCK MAG 17RD"),
			make_row(6, manufacturer="Magpul", model="PMAG", qty=9, short_description="MAGPUL PMAG 17RD"),
			make_row(7, manufacturer="Acme Mounts", model="507C Mount", qty=40,
				short_description="MOUNT FOR HOLOSUN 507C"),
		])

		def upcs(q):
			return [h["upc"] for h in LocalCatalog().search(CODE, q, 10)]

		glock_19 = upcs("glock 19")
		self.assertEqual(glock_19[0], make_row(2)["upc"])     # brand + model + title beat UPC order
		self.assertIn(make_row(1)["upc"], glock_19)            # the substring match is still found
		# the Holosun product leads a better-stocked third-party mount that names it
		self.assertEqual(upcs("holosun 507"), [make_row(3)["upc"], make_row(7)["upc"]])
		mags = upcs("glock mag")
		self.assertLess(mags.index(make_row(5)["upc"]), mags.index(make_row(4)["upc"]))  # in stock first
		# "mag" is a word of the Glock title, only the start of the brand Magpul
		self.assertEqual(upcs("mag 17rd")[0], make_row(5)["upc"])
		self.assertEqual(upcs("AC-1")[0], make_row(1)["upc"])  # an exact MFG# still leads


# ---------------------------------------------------------------- AC 4 (store)

class TestChangeSequence(_Base):
	def test_reloading_unchanged_data_bumps_nothing(self):
		rows = [make_row(n) for n in range(1, 6)]
		first = self.load(rows)
		self.assertEqual((first["created"], first["updated"], first["total"]), (5, 0, 5))
		seqs, meta = _offer_seqs(), _seq()
		cursor = LocalCatalog().products_changes("")["cursor"]
		again = self.load(rows)
		self.assertEqual((again["created"], again["updated"], again["unchanged"]), (0, 0, 5))
		self.assertEqual(_offer_seqs(), seqs)
		self.assertEqual(_seq(), meta)
		self.assertEqual(LocalCatalog().products_changes(cursor)["keys"], [])

	def test_one_changed_price_bumps_exactly_that_row(self):
		rows = [make_row(n) for n in range(1, 6)]
		self.load(rows)
		seqs = _offer_seqs()
		cursor = LocalCatalog().products_changes("")["cursor"]
		rows[2] = make_row(3, cost=777.77)
		out = self.load(rows)
		self.assertEqual((out["created"], out["updated"]), (0, 1))
		after = _offer_seqs()
		self.assertEqual({k for k in after if after[k] != seqs[k]}, {"TH-00003"})
		self.assertGreater(after["TH-00003"], max(seqs.values()))
		self.assertEqual(LocalCatalog().products_changes(cursor)["keys"], [make_row(3)["upc"]])
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00003"])["TH-00003"]["cost"], 777.77)

	def test_changes_page_in_order_across_houses(self):
		self.load([make_row(n) for n in range(1, 6)])
		self.load([make_row(n, sku=f"T2-{n}", upc=f"99991{n:07d}") for n in range(1, 4)], code=CODE2)
		local = LocalCatalog()
		local.changes_page_size = 2
		# Start every other house already on file (a dev site that tried Local) at
		# its end, so only the two test houses page.
		others = [f"{h}:{s}" for h, s in frappe.db.sql("SELECT distributor, change_seq FROM dc_meta")
			if h not in (CODE, CODE2)]
		keys, cursor, pages = [], ";".join(others), 0
		while True:
			page = local.products_changes(cursor)
			keys += page["keys"]
			cursor, pages = page["cursor"], pages + 1
			if not page["more"]:
				break
		mine = [k for k in keys if k.startswith("99990")]
		theirs = [k for k in keys if k.startswith("99991")]
		self.assertEqual(mine, [make_row(n)["upc"] for n in range(1, 6)])
		self.assertEqual(theirs, [f"99991{n:07d}" for n in range(1, 4)])
		self.assertEqual(pages, 3)
		self.assertIn(f"{CODE}:{_seq(CODE)};{CODE2}:{_seq(CODE2)}", cursor)
		self.assertEqual(local.products_changes(cursor)["keys"], [])
		with self.assertRaises(ValueError):
			local.products_changes("testhouse:abc")

	def test_a_upc_move_reports_the_old_and_the_new_product(self):
		self.load([make_row(1)])
		cursor = LocalCatalog().products_changes("")["cursor"]
		self.load([make_row(1, upc="999909999999")], mode="delta")
		self.assertEqual(set(LocalCatalog().products_changes(cursor)["keys"]),
			{make_row(1)["upc"], "999909999999"})

	def test_quantity_lane_bumps_only_real_changes(self):
		self.load([make_row(n) for n in range(1, 4)])
		seqs = _offer_seqs()
		self.assertEqual(store.apply_quantity([("TH-00001", 1), ("TH-00002", 9), ("NOPE", 4)],
			distributor=CODE), {"updated": 1, "total": 3})
		after = _offer_seqs()
		self.assertEqual({k for k in after if after[k] != seqs[k]}, {"TH-00002"})
		self.assertEqual(store.apply_quantity([("TH-00002", 9)], distributor=CODE)["updated"], 0)
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00002"])["TH-00002"]["qty"], 9)
		# a discontinued row keeps its terminal flag
		self.load([make_row(1, discontinued=True, status_flag="Discontinued")], mode="delta")
		store.apply_quantity([("TH-00001", 5, None, "Allocated")], distributor=CODE)
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00001"])["TH-00001"]["status_flag"],
			"Discontinued")

	def test_snapshot_guards_refuse_before_touching_live_rows(self):
		self.load([make_row(1)])
		with self.assertRaises(ValueError):
			self.load([make_row(1, cost=1.0)], min_rows=5)
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00001"])["TH-00001"]["cost"], 101.0)

	def test_no_ffl_flag_means_firearm(self):
		"""Spec rule: only the distributor's explicit flag says "no FFL"."""
		row = make_row(1)
		del row["ffl_required"]
		self.load([row, make_row(2, ffl_required=None), make_row(3, ffl_required=False)])
		got = LocalCatalog().rows(CODE, ["TH-00001", "TH-00002", "TH-00003"])
		self.assertEqual([got[s]["ffl_required"] for s in ("TH-00001", "TH-00002", "TH-00003")],
			[True, True, False])

	def test_product_layer_merges_houses(self):
		"""One UPC at two houses: the in-stock cheaper offer leads the content;
		FFL is an OR over both."""
		upc = make_row(1)["upc"]
		self.load([make_row(1, qty=0, cost=50.0, short_description="FIRST HOUSE",
			ffl_required=False)])
		self.load([make_row(1, sku="T2-1", qty=4, cost=90.0, short_description="SECOND HOUSE",
			ffl_required=True)], code=CODE2)
		item = LocalCatalog().item_by_upc(upc)
		self.assertEqual(item["product"]["description"], "SECOND HOUSE")
		self.assertTrue(item["product"]["ffl_required"])
		self.assertEqual(item["best_offer"]["distributor"], CODE2)
		page = LocalCatalog().products_batch([upc])["products"][0]
		self.assertEqual(page["total_qty"], 4)
		self.assertEqual({o["distributor"] for o in page["offers"]}, {CODE, CODE2})

	def test_malformed_upc_is_an_orphan_and_counted(self):
		self.load([make_row(1, upc="12-AB")])
		row = LocalCatalog().rows(CODE, ["TH-00001"])["TH-00001"]
		self.assertIsNone(row["upc"])
		self.assertIn(f"{CODE}:TH-00001", LocalCatalog().products_changes("")["keys"])
		self.assertEqual(frappe.db.sql("SELECT upc_reject_count FROM dc_meta WHERE distributor = %s",
			(CODE,))[0][0], 1)

	def test_restrictions_swap_and_hash_skip(self):
		self.load([make_row(1)])
		entries = [["", "ZZ", "state", "synthetic"]]
		self.assertEqual(store.load_restrictions({"TH-00001": entries}, "h1", distributor=CODE)
			["rows_written"], 1)
		self.assertTrue(store.load_restrictions({}, "h1", distributor=CODE)["skipped_unchanged"])
		self.assertEqual(LocalCatalog().restrictions_entries(CODE, "TH-00001"), entries)
		store.load_restrictions({}, "h2", distributor=CODE)
		self.assertEqual(LocalCatalog().restrictions_entries(CODE, "TH-00001"), [])


# ---------------------------------------------------------------- AC 5

class TestWriteLock(_Base):
	def test_a_second_writer_records_a_lock_skip_not_a_failure(self):
		self.driver.snapshot_rows = [make_row(1)]
		other = self._other_connection()
		self.assertEqual(other.sql("SELECT GET_LOCK(%s, 0)", (store.lock_name(CODE),))[0][0], 1)
		try:
			self.assertEqual(self.load([make_row(1)]), store.LOCK_SKIPPED)
			self.assertEqual(store.apply_quantity([("TH-00001", 1)], distributor=CODE),
				store.LOCK_SKIPPED)
			result = sync.run_house(CODE)
		finally:
			other.sql("SELECT RELEASE_LOCK(%s)", (store.lock_name(CODE),))
		self.assertEqual(result["snapshot"], store.LOCK_SKIPPED)
		self.assertEqual(result["_failed"], [])
		self.assertEqual(_runs(), ())  # a skip writes no run row
		self.assertEqual(frappe.db.sql("SELECT lock_skip_count FROM dc_meta WHERE distributor = %s",
			(CODE,))[0][0], 4)
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor = %s",
			(CODE,))[0][0], 0)
		self.assertEqual(self.load([make_row(1)])["created"], 1)  # lock released

	def test_houses_do_not_block_each_other(self):
		other = self._other_connection()
		other.sql("SELECT GET_LOCK(%s, 0)", (store.lock_name(CODE2),))
		try:
			self.assertEqual(self.load([make_row(1)])["created"], 1)
		finally:
			other.sql("SELECT RELEASE_LOCK(%s)", (store.lock_name(CODE2),))


# ---------------------------------------------------------------- AC 5 / 11 (framework)

class TestSyncFramework(_Base):
	def test_a_failing_lane_records_failed_and_the_next_lane_runs(self):
		self.driver.snapshot_rows = [make_row(1)]
		self.driver.fail = {"snapshot"}
		result = sync.run_house(CODE)
		self.assertEqual(result["_failed"], [f"{CODE}_snapshot"])
		self.assertEqual(self.driver.calls, ["snapshot", "quantity"])
		self.assertEqual(_runs(), ((f"{CODE}_snapshot", "failed", "FeedUnavailable"),
			(f"{CODE}_quantity", "success", "")))
		history = LocalCatalog().ops_history(CODE)["runs"]
		self.assertEqual([r["source"] for r in history], [f"{CODE}_quantity", f"{CODE}_snapshot"])
		self.assertNotIn("synthetic outage", str(history))  # class only, never the message
		status = LocalCatalog().ops_status(CODE)
		self.assertEqual(status["last_failure"]["error_class"], "FeedUnavailable")
		self.assertEqual({f["feed"]: f["reason"] for f in status["feeds"]},
			{"snapshot": "never_synced", "quantity": "fresh"})

	def test_gates_skip_a_fresh_lane(self):
		self.driver.snapshot_rows = [make_row(1)]
		sync.run_house(CODE)
		sync.run_house(CODE)
		self.assertEqual(self.driver.calls, ["snapshot", "quantity"])
		sync.run_house(CODE, force=True)
		self.assertEqual(len(_runs()), 4)

	def test_skip_schedule_runs_lanes_that_are_not_due(self):
		self.driver.snapshot_rows = [make_row(1)]
		sync.run_house(CODE)
		sync.run_house(CODE)
		self.assertEqual(self.driver.calls, ["snapshot", "quantity"])
		sync.run_house(CODE, skip_schedule=True)
		self.assertEqual(self.driver.calls, ["snapshot", "quantity", "snapshot", "quantity"])
		self.assertEqual(len(_runs()), 4)

	def test_sync_now_queues_every_lane_with_only_the_schedule_skipped(self):
		queued = []
		with patch.object(sync, "worker_running", return_value=True), \
				patch.object(frappe, "enqueue", side_effect=lambda m, **kw: queued.append((m, kw)) or object()):
			out = sync.sync_now(CODE)
		self.assertEqual(out["enqueued"], [f"{CODE}_snapshot", f"{CODE}_quantity"])
		self.assertEqual(out["deduplicated"], [])
		self.assertTrue(out["worker_running"])
		self.assertEqual([kw["lane"] for _m, kw in queued], ["snapshot", "quantity"])
		for method, kw in queued:
			self.assertEqual(method, "ffl_integrations.catalog_local.sync.run_lane")
			self.assertEqual(kw["queue"], "catalog")
			self.assertIs(kw["skip_schedule"], True)
			self.assertTrue(kw["deduplicate"])
			self.assertEqual(kw["job_id"], sync.job_id(CODE, kw["lane"]))
			self.assertEqual(kw["timeout"], self.driver.lane(kw["lane"]).timeout)
			self.assertNotIn("force", kw)
		with patch.object(sync, "worker_running", return_value=True), \
				patch.object(frappe, "enqueue", return_value=None):
			out = sync.sync_now(CODE)
		self.assertEqual(out["enqueued"], [])
		self.assertEqual(out["deduplicated"], [f"{CODE}_snapshot", f"{CODE}_quantity"])

	def test_sync_now_refuses_a_house_with_no_driver_or_no_setup(self):
		with patch.object(frappe, "enqueue") as enqueue:
			with self.assertRaises(frappe.ValidationError):
				sync.sync_now("nosuchhouse")
			with patch.object(self.driver, "configured", return_value=False):
				with self.assertRaises(frappe.ValidationError):
					sync.sync_now(CODE)
		enqueue.assert_not_called()

	def test_lane_health_reports_last_success_rows_and_failures(self):
		self.assertIsNone(sync.lane_health("nosuchhouse"))
		self.driver.snapshot_rows = [make_row(1)]
		sync.run_house(CODE)
		health = sync.lane_health(CODE)
		self.assertEqual([ln["lane"] for ln in health["lanes"]], ["snapshot", "quantity"])
		snapshot = health["lanes"][0]
		self.assertEqual((snapshot["rows"], snapshot["last_status"]), (1, "success"))
		self.assertEqual(health["offers_total"], 1)
		self.assertEqual(health["offers_active"], 1)
		success_at = snapshot["last_success_at"]
		self.assertTrue(success_at)
		self.driver.fail = {"snapshot"}
		sync.run_house(CODE, skip_schedule=True)
		snapshot = sync.lane_health(CODE)["lanes"][0]
		self.assertEqual((snapshot["last_status"], snapshot["error_class"]), ("failed", "FeedUnavailable"))
		self.assertEqual(snapshot["last_success_at"], success_at)

	def test_health_derives_from_sync_runs(self):
		self.driver.snapshot_rows = [make_row(1), make_row(2, discontinued=True)]
		self.assertIsNone(LocalCatalog().health(CODE)["last_quantity_sync_at"])
		sync.run_house(CODE)
		with patch(LOCAL, return_value=True):
			h = remote_catalog.health(CODE)
		self.assertEqual((h["offers_total"], h["offers_active"], h["contract_version"]), (2, 1, "1.1"))
		self.assertEqual(h["catalog_version"], h["last_catalog_sync_at"])
		heartbeat = remote_catalog.parse_service_ts(h["last_quantity_sync_at"])
		self.assertLess(abs((frappe.utils.now_datetime() - heartbeat).total_seconds()), 120)

	def test_tick_queues_lanes_on_the_catalog_queue_one_heavy_per_tick(self):
		second = catalog_testhouse.install(CODE2)
		self.driver.snapshot_rows = [make_row(1)]
		second.snapshot_rows = [make_row(2, sku="T2-2")]
		queued = []
		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True), \
				patch.object(frappe, "enqueue", side_effect=lambda m, **kw: queued.append((m, kw)) or object()):
			out = sync.tick()
		self.assertEqual(out["enqueued"], [f"{CODE}_snapshot", f"{CODE}_quantity", f"{CODE2}_quantity"])
		self.assertEqual(out["yielded"], [f"{CODE2}_snapshot"])
		for method, kw in queued:
			self.assertEqual(method, "ffl_integrations.catalog_local.sync.run_lane")
			self.assertEqual(kw["queue"], "catalog")
			self.assertTrue(kw["deduplicate"])
			self.assertEqual(kw["job_id"], sync.job_id(kw["code"], kw["lane"]))
			self.assertEqual(kw["timeout"], 1500 if kw["lane"] == "snapshot" else 300)
		for _m, kw in queued:  # what the catalog worker would do
			sync.run_lane(kw["code"], kw["lane"])
		self.assertEqual(len(_runs(CODE)), 2)
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00001"])["TH-00001"]["sku"], "TH-00001")
		# next tick: CODE's lanes are fresh, CODE2's snapshot gets its turn
		queued.clear()
		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True), \
				patch.object(frappe, "enqueue", side_effect=lambda m, **kw: queued.append((m, kw)) or object()):
			out = sync.tick()
		self.assertEqual(out["enqueued"], [f"{CODE2}_snapshot"])

	def test_tick_is_a_no_op_unless_local(self):
		with patch.object(frappe, "enqueue") as enqueue:
			self.assertEqual(sync.tick(), {"skipped": "not_local"})
		enqueue.assert_not_called()

	def test_worker_missing_is_said_out_loud(self):
		"""AC 11: Local with nobody on the catalog queue alerts and the hub says so."""
		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=False), \
				patch("ffl_integrations.notifications.notify_admins") as notify, \
				patch.object(frappe, "enqueue"):
			sync.tick()
			status = sync.status()
		self.assertEqual(notify.call_args.kwargs["dedup_key"], sync.WORKER_ALERT_KEY)
		self.assertIn("Catalog worker not running", notify.call_args.args[0])
		self.assertFalse(status["worker_running"])
		self.assertIn("Catalog worker not running", status["warning"])
		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True):
			self.assertEqual(sync.status()["warning"], "")
		self.assertEqual(sync.status(), {"local": False, "worker_running": None, "warning": ""})

	def test_worker_running_reads_the_catalog_queue(self):
		import frappe.utils.background_jobs as bj

		with patch.object(bj, "get_queues_timeout", return_value={"default": 300}):
			self.assertFalse(sync.worker_running())
		with patch.object(bj, "get_queues_timeout", return_value={"catalog": 1800}), \
				patch.object(bj, "get_queue", return_value=MagicMock()) as get_queue, \
				patch.object(bj, "get_workers", return_value=[object()]):
			self.assertTrue(sync.worker_running())
		get_queue.assert_called_with("catalog")
		with patch.object(bj, "get_queues_timeout", return_value={"catalog": 1800}), \
				patch.object(bj, "get_queue", return_value=MagicMock()), \
				patch.object(bj, "get_workers", return_value=[]):
			self.assertFalse(sync.worker_running())

	def test_hub_overview_carries_the_worker_status(self):
		from ffl_integrations.distributor import hub

		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=False):
			self.assertIn("Catalog worker not running", hub.overview()["catalog_local"]["warning"])

	def test_queue_is_declared_by_migrate(self):
		from frappe.utils.background_jobs import get_queues_timeout

		self.assertIn("catalog", get_queues_timeout())


class TestReviewR1(_Base):
	"""Review round 1: tick fairness, exact compare, lock scope, derived
	columns, SKU spelling, stage robustness, the source switch, schema drift."""

	def _tick(self, enqueue_result=lambda kw: object()):
		queued = []

		def fake(method, **kw):
			queued.append(kw)
			return enqueue_result(kw)

		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True), \
				patch.object(frappe, "enqueue", side_effect=fake):
			return sync.tick(), queued

	def test_a_failing_heavy_lane_does_not_starve_another_house(self):
		second = catalog_testhouse.install(CODE2)
		self.driver.fail = {"snapshot"}
		self.driver.snapshot_rows = [make_row(1)]
		second.snapshot_rows = [make_row(2, sku="T2-2")]
		out, queued = self._tick()
		self.assertIn(f"{CODE}_snapshot", out["enqueued"])
		self.assertEqual(out["yielded"], [f"{CODE2}_snapshot"])
		for kw in queued:
			sync.run_lane(kw["code"], kw["lane"])
		self.assertIn((f"{CODE}_snapshot", "failed", "FeedUnavailable"), _runs(CODE))
		out, _queued = self._tick()   # CODE's snapshot is still due, but it went last time
		self.assertIn(f"{CODE2}_snapshot", out["enqueued"])
		self.assertEqual(out["yielded"], [f"{CODE}_snapshot"])


	def test_a_case_only_change_bumps_change_seq(self):
		self.load([make_row(1)])
		for change in ({"image_url": "https://img.example.com/TH/1.JPG"}, {"manufacturer": "ACME ARMS"}):
			before = _offer_seqs()["TH-00001"]
			out = self.load([make_row(1, **change)], mode="delta")
			self.assertEqual(out["updated"], 1, change)
			self.assertGreater(_offer_seqs()["TH-00001"], before, change)
		store.apply_quantity([("TH-00001", 1, None, "Allocated")], distributor=CODE)
		self.assertEqual(store.apply_quantity([("TH-00001", 1, None, "ALLOCATED")], distributor=CODE)
			["updated"], 1)

	def test_lock_names_carry_the_database(self):
		self.assertNotEqual(store.lock_name(CODE, "site_a_db"), store.lock_name(CODE, "site_b_db"))
		self.assertIn(frappe.conf.db_name, store.lock_name(CODE))
		self.assertLessEqual(len(store.lock_name("h" * 32, "d" * 64)), 64)

	def _offer(self, *cols):
		return frappe.db.sql(f"SELECT {', '.join(cols)} FROM dc_offer WHERE distributor = %s "
			"AND sku = 'TH-00001'", (CODE,), as_dict=True)[0]

	def test_one_sku_spelling_for_every_writer(self):
		self.load([make_row(1, sku=" TH-00001 ")])
		self.assertEqual(set(LocalCatalog().rows(CODE, ["TH-00001"])), {"TH-00001"})
		self.assertEqual(store.apply_quantity([("TH-00001  ", 9)], distributor=CODE)["updated"], 1)
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00001"])["TH-00001"]["qty"], 9)
		store.load_restrictions({" TH-00001 ": [["", "ZZ", "state", "x"]]}, "h", distributor=CODE)
		self.assertEqual(len(LocalCatalog().restrictions_entries(CODE, "TH-00001")), 1)

	def test_a_failed_run_record_never_raises_out_of_a_stage(self):
		import pymysql

		with patch.object(sync, "record_run", side_effect=pymysql.OperationalError(2006, "gone")):
			self.assertIsNone(sync.stage(f"{CODE}_x", lambda: 1 / 0))
			self.assertEqual(sync.stage(f"{CODE}_y", lambda: {"total": 1}), {"total": 1})

	def test_queue_declaration_merges(self):
		import json
		import os
		import tempfile

		from ffl_integrations.catalog_local import declare_queue

		config, changed = declare_queue.merged({"workers": {"long": {"timeout": 9}}, "db_host": "x"})
		self.assertTrue(changed)
		self.assertEqual(config["workers"], {"long": {"timeout": 9}, "catalog": {"timeout": 1500}})
		self.assertEqual(config["db_host"], "x")
		self.assertFalse(declare_queue.merged({"workers": {"catalog": {"timeout": 60}}})[1])
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "common_site_config.json")
			with open(path, "w") as fh:
				json.dump({"redis_queue": "redis://q"}, fh)
			self.assertTrue(declare_queue.declare(path))
			self.assertFalse(declare_queue.declare(path))
			with open(path) as fh:
				self.assertEqual(json.load(fh), {"redis_queue": "redis://q",
					"workers": {"catalog": {"timeout": 1500}}})


class TestReviewR2(_Base):
	"""Review round 2: derived columns from the final row, the heavy slot,
	isolation after a reconnect, UPC-move numbering, guarded gates and lock
	release, reader SKU spelling, the restrictions count."""

	def _tick(self, enqueue_result=lambda kw: object(), in_flight=()):
		queued = []

		def fake(method, **kw):
			queued.append(kw)
			return enqueue_result(kw)

		with patch("ffl_integrations.catalog_local.sync.is_local", return_value=True), \
				patch.object(sync, "worker_running", return_value=True), \
				patch("frappe.utils.background_jobs.is_job_enqueued",
					side_effect=lambda jid: jid in in_flight), \
				patch.object(frappe, "enqueue", side_effect=fake):
			return sync.tick(), queued

	def _offer(self, sku="TH-00001"):
		return frappe.db.sql("""SELECT sku, upc, mfg_number, manufacturer, model, short_description,
			product_key, brand_key, search_text FROM dc_offer WHERE distributor = %s AND sku = %s""",
			(CODE, sku), as_dict=True)[0]

	def assert_derived_consistent(self, row):
		self.assertEqual(row.product_key, store.product_key(CODE, row.sku, row.upc))
		self.assertEqual(row.brand_key, store.brand_key(row.manufacturer))
		self.assertEqual(row.search_text, store.search_text(row.sku, row))

	def _hit_skus(self, q):
		return {o["sku"] for h in LocalCatalog().search(CODE, q, 10) for o in h["offers"]}

	# ---- 1. derived columns
	def test_a_partial_lane_rebuilds_derived_columns_from_the_final_row(self):
		self.load([make_row(1)])
		# upc moves, title changes, manufacturer/model are another lane's
		row = make_row(1, model="Z9", upc="999909999999", short_description="BRANDNEW TITLE")
		del row["manufacturer"]
		self.load([row], mode="delta", exclude_cols=("manufacturer", "model"))
		got = self._offer()
		self.assertEqual((got.manufacturer, got.model, got.upc), ("Acme Arms", "Model 1", "999909999999"))
		self.assert_derived_consistent(got)
		self.assertEqual(self._hit_skus("brandnew"), {"TH-00001"})
		self.assertEqual(self._hit_skus("acme model 1"), {"TH-00001"})   # kept columns still searchable
		# upc is another lane's: the title alone changes
		self.load([make_row(1, upc="999908888888", short_description="CHANGED TITLE")], mode="delta",
			exclude_cols=("upc",))
		after = self._offer()
		self.assertEqual((after.upc, after.product_key), ("999909999999", "999909999999"))
		self.assert_derived_consistent(after)
		self.assertEqual(self._hit_skus("changed title"), {"TH-00001"})
		self.assertEqual(self._hit_skus("brandnew"), set())
		# an insert-only title is not written, so search keeps the stored one
		self.load([make_row(1, upc="999909999999", short_description="IGNORED TITLE",
			manufacturer="Other Co")], mode="delta", insert_only_cols=("short_description",))
		last = self._offer()
		self.assertEqual((last.short_description, last.brand_key), ("CHANGED TITLE", "other co"))
		self.assert_derived_consistent(last)
		self.assertEqual(self._hit_skus("ignored"), set())

	# ---- 2. heavy slot
	def test_a_heavy_job_in_flight_holds_the_slot(self):
		catalog_testhouse.install(CODE2)
		sync.mark_attempt(CODE, "snapshot")   # A's snapshot started: B is first in fair order
		# (the shipped drivers, unconfigured here, may sit anywhere in the order)
		self.assertEqual([c for c in sync._fair_order() if c in (CODE, CODE2)], [CODE2, CODE])
		snap = sync.job_id(CODE, "snapshot")
		out, queued = self._tick(lambda kw: None if kw["job_id"] == snap else object(),
			in_flight={snap})
		self.assertNotIn(f"{CODE2}_snapshot", out["enqueued"])
		self.assertEqual(out["yielded"], [f"{CODE2}_snapshot"])
		self.assertEqual(out["deduplicated"], [f"{CODE}_snapshot"])
		self.assertEqual(sorted(out["enqueued"]), [f"{CODE2}_quantity", f"{CODE}_quantity"])
		self.assertNotIn(snap, [kw["job_id"] for kw in queued])
		# A's job finished: the slot is free and fair order hands it to B
		out, _queued = self._tick()
		self.assertIn(f"{CODE2}_snapshot", out["enqueued"])
		self.assertEqual(out["yielded"], [f"{CODE}_snapshot"])

	def test_a_job_deduplicated_at_enqueue_still_takes_the_slot(self):
		"""It went in between the in-flight read and the enqueue."""
		catalog_testhouse.install(CODE2)
		sync.mark_attempt(CODE2, "snapshot")   # A first in fair order
		snap = sync.job_id(CODE, "snapshot")
		out, _queued = self._tick(lambda kw: None if kw["job_id"] == snap else object())
		self.assertEqual(out["deduplicated"], [f"{CODE}_snapshot"])
		self.assertEqual(out["yielded"], [f"{CODE2}_snapshot"])

	# ---- 3. reconnect keeps READ COMMITTED
	def test_a_reconnected_session_runs_the_next_lane_at_read_committed(self):
		other = self._other_connection()
		seen = {}
		conn_ids = []

		def snapshot(run):
			conn_ids.append(frappe.db.sql("SELECT CONNECTION_ID()")[0][0])
			other.sql(f"KILL CONNECTION {int(conn_ids[0])}")
			raise sync.FeedUnavailable("connection killed under the lane")

		def quantity(run):
			conn_ids.append(frappe.db.sql("SELECT CONNECTION_ID()")[0][0])
			seen["isolation"] = store._isolation()
			return store.apply_quantity([], distributor=CODE)

		self.driver._snapshot, self.driver._quantity = snapshot, quantity
		before = store._isolation()
		result = sync.run_house(CODE)
		self.assertNotEqual(conn_ids[0], conn_ids[1], "the connection was not replaced")
		self.assertEqual(seen["isolation"], "READ COMMITTED")
		self.assertEqual(result["_failed"], [f"{CODE}_snapshot"])
		self.assertEqual(_runs(), ((f"{CODE}_snapshot", "failed", "FeedUnavailable"),
			(f"{CODE}_quantity", "success", "")))
		self.assertEqual(store._isolation(), before)   # restored on the new session

	def test_reapply_isolation_only_inside_read_committed(self):
		store.reapply_isolation()
		self.assertNotEqual(store._isolation(), "READ COMMITTED")
		with store.read_committed():
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			frappe.db.commit()
			store.reapply_isolation()
			self.assertEqual(store._isolation(), "READ COMMITTED")
		self.assertNotEqual(store._isolation(), "READ COMMITTED")

	# ---- 4. UPC-move numbering
	def test_an_insert_only_upc_moves_nothing(self):
		self.load([make_row(1), make_row(2)])
		cursor = LocalCatalog().products_changes("")["cursor"]
		meta = _seq()
		moves = frappe.db.sql("SELECT COUNT(*) FROM dc_product_move WHERE distributor = %s", (CODE,))[0][0]
		out = self.load([make_row(1, upc="999907777777"), make_row(2)], mode="delta",
			insert_only_cols=("upc",))
		self.assertEqual(out["updated"], 0)
		self.assertEqual(_seq(), meta)
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_product_move WHERE distributor = %s",
			(CODE,))[0][0], moves)
		self.assertEqual(LocalCatalog().products_changes(cursor)["keys"], [])
		self.assertEqual(self._offer().upc, make_row(1)["upc"])
		# with another column changing, the offer bumps and still no move is written
		out = self.load([make_row(1, upc="999907777777", cost=1.23)], mode="delta",
			insert_only_cols=("upc",))
		self.assertEqual(out["updated"], 1)
		self.assertEqual(LocalCatalog().products_changes(cursor)["keys"], [make_row(1)["upc"]])
		self.assert_seq_covers()
		# a new SKU still gets the insert-only upc
		self.load([make_row(3, upc="999906666666")], mode="delta", insert_only_cols=("upc",))
		self.assertEqual(self._offer("TH-00003").upc, "999906666666")
		self.assert_seq_covers()

	def test_numbers_are_never_handed_out_twice(self):
		self.load([make_row(n) for n in range(1, 4)])
		self.assert_seq_covers()
		self.load([make_row(1, upc="999905555555"), make_row(2), make_row(3)], mode="delta")
		self.assert_seq_covers()
		store.apply_quantity([("TH-00002", 7)], distributor=CODE)
		self.assert_seq_covers()
		seqs = [r[0] for r in frappe.db.sql("""SELECT change_seq FROM dc_offer WHERE distributor = %(c)s
			UNION ALL SELECT change_seq FROM dc_product_move WHERE distributor = %(c)s""", {"c": CODE})]
		self.assertEqual(len(seqs), len(set(seqs)))

	# ---- 6. guarded gates
	def test_a_raising_gate_is_a_failed_run_and_the_house_goes_on(self):
		self.driver.snapshot_rows = [make_row(1)]
		self.driver.gate_fail = {"snapshot"}
		result = sync.run_house(CODE)
		self.assertEqual(result["_failed"], [f"{CODE}_snapshot"])
		self.assertIsNone(result["snapshot"])
		self.assertEqual(self.driver.calls, ["quantity"])
		self.assertEqual(_runs(), ((f"{CODE}_snapshot", "failed", "RuntimeError"),
			(f"{CODE}_quantity", "success", "")))
		self.assertIsNone(sync.run_lane(CODE, "snapshot"))   # the queue job too
		self.assertEqual(_runs()[-1], (f"{CODE}_snapshot", "failed", "RuntimeError"))

	def test_a_failed_attempt_stamp_does_not_stop_the_lane(self):
		import pymysql

		self.driver.snapshot_rows = [make_row(1)]
		with patch.object(sync, "mark_attempt", side_effect=pymysql.OperationalError(1205, "lock wait")):
			result = sync.run_house(CODE)
		self.assertEqual(result["_failed"], [])
		self.assertEqual(result["snapshot"]["created"], 1)

	# ---- 7. guarded rollback and lock release
	def test_a_lost_release_after_the_commit_is_not_a_failed_write(self):
		import pymysql

		real = frappe.db.sql

		def sql(query, *a, **kw):
			out = real(query, *a, **kw)
			if "RELEASE_LOCK" in str(query):
				raise pymysql.OperationalError(2013, "Lost connection to server during query")
			return out

		with patch.object(frappe.db, "sql", side_effect=sql):
			out = self.load([make_row(1)])
		self.assertEqual(out["created"], 1)
		self.driver.snapshot_rows = [make_row(1, cost=5.0)]
		with patch.object(frappe.db, "sql", side_effect=sql):
			result = sync.run_house(CODE)
		self.assertEqual(result["_failed"], [])
		self.assertEqual(_runs()[0], (f"{CODE}_snapshot", "success", ""))
		self.assertEqual(LocalCatalog().rows(CODE, ["TH-00001"])["TH-00001"]["cost"], 5.0)
		other = self._other_connection()   # and the lock really is free
		self.assertEqual(other.sql("SELECT IS_FREE_LOCK(%s)", (store.lock_name(CODE),))[0][0], 1)

	def test_a_failing_rollback_keeps_the_original_error(self):
		import pymysql

		real = frappe.db.rollback
		calls = {"n": 0}

		def flaky(*a, **kw):
			calls["n"] += 1
			if calls["n"] == 1:
				raise pymysql.OperationalError(2006, "server has gone away")
			return real(*a, **kw)

		self.load([make_row(1)])
		with patch.object(frappe.db, "rollback", side_effect=flaky), \
				self.assertRaises(ValueError):
			self.load([make_row(1)], min_rows=5)
		calls["n"] = 0
		self.driver._snapshot = lambda run: store.load_catalog(iter([make_row(1)]),
			distributor=CODE, min_rows=5)
		with patch.object(frappe.db, "rollback", side_effect=flaky):
			result = sync.run_house(CODE)
		self.assertEqual(result["_failed"], [f"{CODE}_snapshot"])
		self.assertEqual(_runs()[0], (f"{CODE}_snapshot", "failed", "ValueError"))

	# ---- 9. reader SKU spelling
	def test_readers_match_the_stored_sku_spelling(self):
		long_sku = "L" * 70
		self.load([make_row(1), make_row(2, sku=long_sku, upc="999904444444")])
		local = LocalCatalog()
		got = local.rows(CODE, ["TH-00001 ", long_sku, "TH-00001"])
		self.assertEqual(set(got), {"TH-00001 ", long_sku, "TH-00001"})
		self.assertEqual(got[long_sku]["sku"], long_sku[:64])
		self.assertEqual(got["TH-00001 "]["sku"], "TH-00001")
		with patch(LOCAL, return_value=True):
			self.assertEqual(remote_catalog.cached_row(CODE, " TH-00001  ")["sku"], "TH-00001")
			self.assertEqual(remote_catalog.cached_row(CODE, long_sku + "X")["sku"], long_sku[:64])
			self.assertEqual(catalog.keys_for(CODE, ["TH-00001 "]), {"TH-00001 ": make_row(1)["upc"]})
		entries = [["", "ZZ", "state", "x"]]
		store.load_restrictions({long_sku: entries}, "h-long", distributor=CODE)
		self.assertEqual(local.restrictions_entries(CODE, long_sku + " "), entries)
		self.assertEqual(local.restrictions_entries(CODE, long_sku[:64]), entries)
		self.assertEqual(self._hit_skus(long_sku + "MORE"), {long_sku[:64]})
		self.assertEqual(self._hit_skus("TH-00001 "), {"TH-00001"})
		self.assertEqual([p["key"] for p in local.products_page({"q": long_sku})["products"]],
			["999904444444"])

	# ---- 10. restrictions count
	def test_restrictions_report_rows_after_dedup(self):
		e1, e2 = [["", "ZZ", "state", "a"]], [["", "YY", "state", "b"]]
		out = store.load_restrictions({"TH-00001": e1, " TH-00001 ": e2, "TH-00002": e1}, "h-dup",
			distributor=CODE)
		self.assertEqual(out["rows_written"], 2)
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_restriction WHERE distributor = %s",
			(CODE,))[0][0], 2)
		self.assertEqual(LocalCatalog().restrictions_entries(CODE, "TH-00001"), e2)


class TestSourceSwitch(FrappeTestCase):
	"""Data Service → Local is for new installs only (refused once a change cursor
	exists); Local → Data Service is allowed and starts every cursor over."""

	def setUp(self):
		self.before = {f: frappe.db.get_single_value("Data Service Settings", f, cache=False)
			for f in ("catalog_source", "category_products_cursor", "category_products_baselined",
				"endpoint", "client_id")}
		self.auth = frappe.db.sql("""SELECT * FROM `__Auth` WHERE doctype = 'Data Service Settings'
			AND fieldname = 'secret'""", as_dict=True)

	def tearDown(self):
		for field, value in self.before.items():
			frappe.db.set_single_value("Data Service Settings", field, value)
		frappe.db.sql("DELETE FROM `__Auth` WHERE doctype = 'Data Service Settings' AND fieldname = 'secret'")
		for row in self.auth:
			frappe.db.sql(f"INSERT INTO `__Auth` ({', '.join(f'`{k}`' for k in row)}) VALUES ({', '.join(['%s'] * len(row))})",
				list(row.values()))
		frappe.db.value_cache.clear()

	def _local_site(self, **fields):
		frappe.db.set_single_value("Data Service Settings", {"catalog_source": "Local",
			"category_products_cursor": "rsr:5", "category_products_baselined": 1,
			"endpoint": "", "client_id": "", **fields})
		frappe.db.sql("DELETE FROM `__Auth` WHERE doctype = 'Data Service Settings' AND fieldname = 'secret'")
		return frappe.get_single("Data Service Settings")

	def test_local_to_data_service_needs_the_connection(self):
		doc = self._local_site()
		doc.catalog_source = "Data Service"
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save()
		self.assertIn("Fill in the endpoint, client ID and secret", str(ctx.exception))
		self.assertEqual(frappe.db.get_single_value("Data Service Settings", "catalog_source",
			cache=False), "Local")

	def test_local_to_data_service_starts_every_cursor_over(self):
		frappe.db.sql("""INSERT INTO `tabSingles` (doctype, field, value)
			VALUES ('Zz Cursor Test', 'sync_cursor', 'rsr:5;ss:9')""")
		self.addCleanup(frappe.db.sql, "DELETE FROM `tabSingles` WHERE doctype = 'Zz Cursor Test'")
		doc = self._local_site(category_tree_cursor="0")
		doc.update({"catalog_source": "Data Service", "endpoint": "https://catalog.example.com",
			"client_id": "store-1", "secret": "s3cret"})
		with self._hooks(["Zz Cursor Test.sync_cursor"]), \
				patch("ffl_integrations.data_service.client.get", return_value={}) as probe:
			doc.save()
		probe.assert_called_once()
		self.assertFalse(doc.flags.get("switching_to_service"))   # a second save of this doc resets nothing
		get = lambda dt, f: frappe.db.get_single_value(dt, f, cache=False)  # noqa: E731
		self.assertEqual(get("Data Service Settings", "catalog_source"), "Data Service")
		for field in ("category_products_cursor", "category_tree_cursor"):
			self.assertFalse(get("Data Service Settings", field))
			self.assertFalse(doc.get(field))   # the form gets the cleared doc back, not the old one
		self.assertEqual(int(get("Data Service Settings", "category_products_baselined") or 0), 0)
		self.assertEqual(int(doc.category_products_baselined or 0), 0)
		self.assertEqual(frappe.db.sql("""SELECT value FROM `tabSingles`
			WHERE doctype = 'Zz Cursor Test' AND field = 'sync_cursor'""")[0][0], "")

	def test_a_service_that_does_not_answer_keeps_the_local_catalog(self):
		from ffl_integrations.data_service import client

		doc = self._local_site()
		doc.update({"catalog_source": "Data Service", "endpoint": "https://catalog.example.com",
			"client_id": "store-1", "secret": "wrong"})
		frappe.db.savepoint("switch")   # a request rolls back on the error; so does this test
		with patch.object(client, "get", side_effect=client.DataServiceUnavailable("401 bad signature")):
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.save()
		frappe.db.rollback(save_point="switch")
		self.assertIn("check failed", str(ctx.exception))
		self.assertEqual(frappe.db.get_single_value("Data Service Settings", "catalog_source",
			cache=False), "Local")
		self.assertEqual(frappe.db.get_single_value("Data Service Settings",
			"category_products_cursor", cache=False), "rsr:5")

	def test_saving_without_a_switch_keeps_the_cursors(self):
		doc = self._local_site()
		doc.save()
		self.assertEqual(frappe.db.get_single_value("Data Service Settings",
			"category_products_cursor", cache=False), "rsr:5")

	def test_a_stored_cursor_refuses_the_switch(self):
		from ffl_integrations.ffl_integrations.doctype.data_service_settings import (
			data_service_settings as dss,
		)

		frappe.db.set_single_value("Data Service Settings", "catalog_source", "Data Service")
		frappe.db.set_single_value("Data Service Settings", "category_products_cursor", "rsr:5")
		self.assertIn("Data Service Settings.category_products_cursor", dss.stored_change_cursors())
		doc = frappe.get_single("Data Service Settings")
		doc.catalog_source = "Local"
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save()
		self.assertIn("not supported yet", str(ctx.exception))
		self.assertIn("Data Service Settings.category_products_cursor", str(ctx.exception))

	def _hooks(self, extra):
		"""frappe.get_hooks with ``extra`` registered under the cursor hook."""
		from ffl_integrations.ffl_integrations.doctype.data_service_settings import (
			data_service_settings as dss,
		)

		real = frappe.get_hooks

		def get_hooks(hook=None, *a, **kw):
			out = real(hook, *a, **kw)
			return [*out, *extra] if hook == dss.CHANGE_CURSOR_HOOK else out

		return patch.object(frappe, "get_hooks", side_effect=get_hooks)

	def test_only_registered_cursor_fields_count(self):
		"""A field merely named like a cursor on some other Single does not block;
		one an extension registers does, and the refusal names it."""
		from ffl_integrations.ffl_integrations.doctype.data_service_settings import (
			data_service_settings as dss,
		)

		frappe.db.set_single_value("Data Service Settings", "catalog_source", "Data Service")
		frappe.db.set_single_value("Data Service Settings", "category_products_cursor", "")
		frappe.db.sql("""INSERT INTO `tabSingles` (doctype, field, value)
			VALUES ('Zz Cursor Test', 'sync_cursor', 'rsr:5')""")
		self.addCleanup(frappe.db.sql, "DELETE FROM `tabSingles` WHERE doctype = 'Zz Cursor Test'")
		self.assertEqual(dss.stored_change_cursors(), [])
		self.assertIn(("Data Service Settings", "category_products_cursor"), dss.change_cursor_fields())
		with self._hooks(["Zz Cursor Test.sync_cursor", "malformed", "Zz Cursor Test.empty_cursor"]):
			self.assertEqual(dss.stored_change_cursors(), ["Zz Cursor Test.sync_cursor"])
			doc = frappe.get_single("Data Service Settings")
			doc.catalog_source = "Local"
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.save()
		self.assertIn("Zz Cursor Test.sync_cursor", str(ctx.exception))

	def test_a_fresh_site_may_choose_local(self):
		from ffl_integrations.ffl_integrations.doctype.data_service_settings import (
			data_service_settings as dss,
		)

		frappe.db.set_single_value("Data Service Settings", "catalog_source", "Data Service")
		with patch.object(dss, "stored_change_cursors", return_value=[]):
			doc = frappe.get_single("Data Service Settings")
			doc.catalog_source = "Local"
			doc.save()
		self.assertEqual(frappe.db.get_single_value("Data Service Settings", "catalog_source",
			cache=False), "Local")


# ---------------------------------------------------------------- AC 3 (contract + parity)

def _rows():
	"""One synthetic catalog for both sides: testhouse rows (one orphan, one
	Deleted, one discontinued) and a testhouse2 offer on a shared UPC."""
	rows = [make_row(n) for n in range(1, 7)]
	rows[3] = make_row(4, upc="")
	rows[4] = make_row(5, status_flag="Deleted")
	rows[5] = make_row(6, discontinued=True)
	return rows, [make_row(1, sku="T2-1", qty=7, cost=80.0)]


def _shape(v):
	if isinstance(v, dict):
		return {k: _shape(x) for k, x in v.items()}
	if isinstance(v, list):
		return [_shape(v[0])] if v else []
	return None if v is None else type(v).__name__


class TestContractAndParity(_Base):
	def setUp(self):
		super().setUp()
		self.second = catalog_testhouse.install(CODE2)
		self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM dc_offer WHERE distributor NOT IN %(c)s",
			{"c": (CODE, CODE2)})[0][0], 0, "the parity checks read the whole local catalog: this "
			"site holds other houses' dc_offer rows (a Local trial?) — run them on a clean test site")
		mine, theirs = _rows()
		self.driver.snapshot_rows, self.second.snapshot_rows = mine, theirs
		sync.run_house(CODE)
		sync.run_house(CODE2)
		store.load_restrictions({"TH-00002": [["", "ZZ", "state", "synthetic"]]}, "h",
			distributor=CODE)
		# the reference, seeded with the same rows
		self.stub = catalog_stub.CatalogStub()
		names = {}
		for code, rows in ((CODE, mine), (CODE2, theirs)):
			for row in rows:
				row = dict(row)
				names[(code, row["department"])] = row.pop("department_name")
				self.stub.upsert(code, catalog_stub._normalize(row))
		self.stub.department_names = names
		self.stub.seed_restrictions("TH-00002", [["", "ZZ", "state", "synthetic"]], distributor=CODE)
		self.stub.seed_sync_runs([{"status": "success", "source": f"{CODE}_quantity"}], distributor=CODE)
		self.stub.known_distributors.add(CODE)
		self.local = LocalCatalog()

	def assertCovers(self, ref, got, path="$"):
		"""Every key the reference returns is there, with the same type (None on
		either side matches anything)."""
		if isinstance(ref, dict):
			self.assertIsInstance(got, dict, path)
			for k, v in ref.items():
				self.assertIn(k, got, f"{path}.{k} missing")
				self.assertCovers(v, got[k], f"{path}.{k}")
		elif isinstance(ref, list):
			self.assertIsInstance(got, list, path)
			if ref and got:
				self.assertCovers(ref[0], got[0], f"{path}[0]")
		elif ref is not None and got is not None:
			self.assertEqual(ref, got, path)

	def parity(self, name, *args, **kwargs):
		ref = getattr(self.stub, name)(*args, **kwargs)
		got = getattr(self.local, name)(*args, **kwargs)
		self.assertCovers(_shape(ref), _shape(got), name)
		return ref, got

	def test_contract_check_passes_against_the_local_catalog(self):
		results = check(StubTransport(self.local), CODE, "TH-00002")
		failed = [r for r in results if not r["ok"]]
		self.assertEqual(failed, [], "\n".join(f"{r['name']}: {r['detail']}" for r in failed))
		self.assertGreaterEqual(len(results), 14)

	def test_all_thirteen_methods_answer_in_the_stub_shapes(self):
		names = []

		def p(name, *args, **kwargs):
			names.append(name)
			return self.parity(name, *args, **kwargs)

		ref, got = p("rows", CODE, ["TH-00001", "TH-00004", "NOPE"])
		self.assertEqual(set(ref), set(got))
		for sku in ref:
			self.assertEqual({k: ref[sku][k] for k in ref[sku]}, {k: got[sku][k] for k in ref[sku]})
		ref, got = p("search", None, "TH-00001", 10)
		self.assertEqual({h["upc"] for h in ref}, {h["upc"] for h in got})
		self.assertEqual({(o["distributor"], o["sku"]) for h in got for o in h["offers"]},
			{(CODE, "TH-00001"), (CODE2, "T2-1")})
		ref, got = p("search", CODE, "widget", 50)
		self.assertEqual(sorted(h["upc"] or "" for h in ref), sorted(h["upc"] or "" for h in got))
		ref, got = p("health", CODE)
		self.assertEqual((ref["offers_total"], ref["offers_active"]),
			(got["offers_total"], got["offers_active"]))
		ref, got = p("item_by_upc", make_row(1)["upc"])
		self.assertEqual(len(ref["offers"]), len(got["offers"]))
		self.assertIsNone(self.local.item_by_upc("000000000000"))
		ref, got = p("restrictions_entries", CODE, "TH-00002")
		self.assertEqual(ref, got)
		params = {"limit": 3, "count": True}
		ref, got = p("products_page", dict(params))
		self.assertEqual(([x["key"] for x in ref["products"]], ref["cursor"], ref["total"]),
			([x["key"] for x in got["products"]], got["cursor"], got["total"]))
		for flt in ({"in_stock": True}, {"ffl_required": False}, {"brand": ["ACME ARMS"]},
				{"distributor": [CODE2]}, {"department": [f"{CODE}:01"]}, {"max_cost": 103},
				{"product_class": ["Accessory"]}, {"q": "AC-3"}):
			ref, got = self.parity("products_page", dict(flt, limit=50))
			self.assertEqual([x["key"] for x in ref["products"]], [x["key"] for x in got["products"]],
				flt)
		for sort in ("cost", "qty", "brand"):
			ref, got = self.parity("products_page", {"limit": 50, "sort": sort})
			self.assertEqual({x["key"] for x in ref["products"]}, {x["key"] for x in got["products"]})
		ref, got = p("products_batch", [make_row(1)["upc"], "0" + make_row(2)["upc"], "nope"])
		self.assertEqual(got["keys"], {"0" + make_row(2)["upc"]: make_row(2)["upc"]})
		self.assertEqual(len(got["products"]), 2)
		ref, got = p("products_changes", "")
		self.assertEqual(set(ref["keys"]), set(got["keys"]))
		ref, got = p("products_facets")
		self.assertEqual(ref, {k: got[k] for k in ref})
		ref, got = p("products_facets", CODE)
		self.assertEqual(ref, {k: got[k] for k in ref})
		ref, got = p("categories")
		self.assertEqual(ref, got)
		ref, got = p("ops_status", CODE, 0)
		self.assertEqual((ref["offers_total"], ref["offers_active"], ref["products_total"]),
			(got["offers_total"], got["offers_active"], got["products_total"]))
		p("ops_history", CODE, 5)
		ref, got = p("ops_flags", CODE)
		for dept in ref["departments"] + got["departments"]:
			dept.pop("name")  # the stub has no department names (its own comment)
		self.assertEqual(ref, got)
		self.assertEqual(sorted(set(names)), sorted({"rows", "search", "health", "item_by_upc",
			"restrictions_entries", "products_page", "products_batch", "products_changes",
			"products_facets", "categories", "ops_status", "ops_history", "ops_flags"}))
		self.assertEqual(len(set(names)), 13)


class TestSchema(FrappeTestCase):
	def test_tables_exist_utf8mb4(self):
		for table in ("dc_offer", "dc_product", "dc_product_move", "dc_restriction", "dc_meta",
				"dc_sync_run"):
			collation = frappe.db.sql("""SELECT table_collation FROM information_schema.tables
				WHERE table_schema = DATABASE() AND table_name = %s""", (table,))
			self.assertTrue(collation, table)
			self.assertTrue(collation[0][0].startswith("utf8mb4"), table)

	def test_ensure_restores_a_missing_column(self):
		from ffl_integrations.catalog_local import schema

		frappe.db.sql_ddl("ALTER TABLE dc_offer DROP COLUMN department_name")
		try:
			schema.ensure()
		finally:
			has = frappe.db.sql("""SELECT COUNT(*) FROM information_schema.columns WHERE
				table_schema = DATABASE() AND table_name = 'dc_offer' AND column_name = 'department_name'""")[0][0]
			if not has:
				frappe.db.sql_ddl("ALTER TABLE dc_offer ADD COLUMN department_name VARCHAR(140) NOT NULL DEFAULT ''")
		self.assertEqual(has, 1)
		self.assertIn("lane_attempts", {c for c, _d in schema.declared_columns("dc_meta")})

	def test_ensure_is_idempotent(self):
		from ffl_integrations.catalog_local import schema

		schema.ensure()
		schema.ensure()

	def test_local_backend_module_has_every_stub_method(self):
		for name in ("rows", "search", "health", "item_by_upc", "restrictions_entries",
				"products_page", "products_batch", "products_changes", "products_facets",
				"categories", "ops_status", "ops_history", "ops_flags"):
			self.assertTrue(callable(getattr(local_backend.LocalCatalog, name)), name)
			self.assertTrue(callable(getattr(catalog_stub.CatalogStub, name)), name)

	def test_gate_reads_last_success(self):
		self.assertTrue(sync.is_due("nobody_quantity", timedelta(minutes=1), store.utc_now()))
