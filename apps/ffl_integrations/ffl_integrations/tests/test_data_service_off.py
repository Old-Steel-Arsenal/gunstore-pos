"""Running without the Data Service (docs/data-service.md, the capability model).

- ``capabilities()``: each capability from the settings, cached for the
  transaction, dropped on a Data Service Settings save, carried by the boot;
- ``require`` / ``CapabilityOff``: HTTP 417 with one message, quiet for the selector;
- the endpoints over catalog ∈ {data_service, local, off}: no 500, no Error Log,
  the Data Service answers unchanged;
- the scheduled jobs with everything off: 0 Error Log rows over 3 ticks, nothing
  enqueued, the category mirror's stale report cleared exactly once;
- catalog_master split: the tree needs ``category_tree``, the product follower only
  ``catalog`` — a Local catalog files an FFL-flag change exactly as the Data Service does;
- nothing in ffl_integrations reaches ``data_service/client.py`` without
  ``require()`` or the catalog selector (an AST check over the package).
"""
from __future__ import annotations

import ast
import json
import pathlib
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations import catalog_master as cm
from ffl_integrations import data_service as ds
from ffl_integrations.catalog_local import store
from ffl_integrations.data_service import client
from ffl_integrations.distributor import api, catalog, hub, remote_catalog
from ffl_integrations.tests import catalog_stub, catalog_testhouse
from ffl_integrations.tests.catalog_stub import data_service_configured
from ffl_integrations.tests.catalog_testhouse import CODE, make_row

LOCAL = "ffl_integrations.catalog_local.is_local"
APP = pathlib.Path(__file__).resolve().parents[1]
# The real readers, before _Case patches them for the rest of each test.
REAL_SERVICE = ds._service_configured
REAL_DISTRIBUTOR = ds._distributor_service
REAL_FFL_PROVIDER = ds._ffl_provider_is_service
REAL_HOUSE = ds._house_service

_ai_answer = {"value": True}


def upc(body: str) -> str:
	"""A UPC-A with its check digit (Item barcodes are validated)."""
	odd = sum(int(d) for d in body[0::2])
	even = sum(int(d) for d in body[1::2])
	return body + str((10 - (odd * 3 + even) % 10) % 10)


def ai_provider():
	"""A fake ``gunshop_ai_connected`` provider."""
	return _ai_answer["value"]


def ai_provider_broken():
	raise RuntimeError("synthetic broken AI provider")


class _Case(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap

		test_bootstrap.run()
		item_groups.ensure()

	def setUp(self):
		# These tests are about the real capability answer (off / data_service /
		# local), not the "no stub installed" guard the other suites get.
		setattr(frappe.flags, remote_catalog.REAL_CAPABILITY_FLAG, True)
		self.addCleanup(frappe.flags.pop, remote_catalog.REAL_CAPABILITY_FLAG, None)
		catalog_stub.uninstall()
		self.addCleanup(catalog_stub.uninstall)
		ds.clear_capabilities()
		self.addCleanup(ds.clear_capabilities)
		# A blank site unless a test says otherwise: no connection, no Distributor
		# override, the FFL provider not the Data Service, no AI extension.
		for name, value in (("_service_configured", False), ("_distributor_service", False),
				("_ffl_provider_is_service", False)):
			p = mock.patch.object(ds, name, return_value=value)
			setattr(self, name, p.start())
			self.addCleanup(p.stop)
		# Every house reads the site's service (no Distributor names its own).
		p = mock.patch.object(ds, "_house_service", return_value=ds.SITE_SERVICE)
		self._house_service = p.start()
		self.addCleanup(p.stop)

	def mode(self, catalog_mode: str):
		"""Put the site's catalog in one of the three states."""
		ds.clear_capabilities()
		catalog_stub.uninstall()
		self._service_configured.return_value = False
		if catalog_mode == "data_service":
			catalog_stub.install()   # the stub stands in for a configured service
		elif catalog_mode == "local":
			p = mock.patch(LOCAL, return_value=True)
			p.start()
			self.addCleanup(p.stop)
		ds.clear_capabilities()

	def error_logs(self) -> int:
		return frappe.db.count("Error Log")


# ---------------------------------------------------------------- the model

class TestCapabilities(_Case):
	def test_a_blank_site_has_everything_off(self):
		self.assertEqual(ds.capabilities(), {"catalog": "off", "category_tree": "off",
			"ffl_national_search": "off", "extension_updates": "off", "ai": "off"})

	def test_a_configured_service_turns_its_features_on(self):
		self._service_configured.return_value = True
		self._ffl_provider_is_service.return_value = True
		self.assertEqual(ds.capabilities(), {"catalog": "data_service", "category_tree": "data_service",
			"ffl_national_search": "data_service", "extension_updates": "data_service", "ai": "off"})

	def test_a_connected_store_that_verifies_elsewhere_gets_no_notice(self):
		"""Connected, ATF Form Scraper as the provider: local FFL records by the store's
		choice (``not_chosen``), never "off" — the notice is for a missing connection."""
		self._service_configured.return_value = True
		self._ffl_provider_is_service.return_value = False
		self.assertEqual(ds.capability(ds.FFL_NATIONAL_SEARCH), "not_chosen")
		self.assertFalse(ds.is_off(ds.FFL_NATIONAL_SEARCH))
		from ffl_integrations.atf import search as atf_search

		with mock.patch("ffl_integrations.atf.data_service_provider.search_remote",
				side_effect=AssertionError("national search not chosen")):
			self.assertEqual(atf_search.search("NO SUCH DEALER D1", 5), [])
		self.assertIn("FFL Settings", ds.MESSAGES[ds.FFL_NATIONAL_SEARCH])
		self.assertIn("Data Service Settings", ds.MESSAGES[ds.FFL_NATIONAL_SEARCH])

	def test_unreadable_settings_are_unknown_not_off(self):
		with mock.patch.object(ds.frappe.db, "get_single_value", side_effect=RuntimeError("db hiccup")), \
				mock.patch.object(ds.frappe, "get_all", side_effect=RuntimeError("db hiccup")):
			self.assertIsNone(REAL_SERVICE())
			self.assertIsNone(REAL_DISTRIBUTOR())
			self.assertIsNone(REAL_FFL_PROVIDER())
		self._service_configured.return_value = None
		self._distributor_service.return_value = None
		caps = ds.capabilities()
		for name in ("catalog", "category_tree", "ffl_national_search", "extension_updates"):
			self.assertEqual(caps[name], "unknown", name)
			self.assertFalse(ds.is_off(name))
		self.assertEqual(ds.require(ds.CATALOG), "unknown")   # no refusal on an unread setting

	def test_the_connection_is_three_fields_without_decrypting_the_secret(self):
		from frappe.utils.password import set_encrypted_password

		self.addCleanup(self._forget_credentials, ds.SETTINGS, ds.SETTINGS, "secret")
		self.addCleanup(frappe.db.set_single_value, ds.SETTINGS, {"endpoint": "", "client_id": ""})
		frappe.db.set_single_value(ds.SETTINGS, {"endpoint": "https://api.example", "client_id": "store"})
		self.assertFalse(REAL_SERVICE())                     # no secret yet
		set_encrypted_password(ds.SETTINGS, ds.SETTINGS, "s3cret", "secret")
		with mock.patch("frappe.utils.password.get_decrypted_password",
				side_effect=AssertionError("decrypted")):
			self.assertTrue(REAL_SERVICE())
		frappe.db.set_single_value(ds.SETTINGS, "endpoint", "")
		self.assertFalse(REAL_SERVICE())

	def test_only_an_enabled_distributor_with_a_full_credential_counts(self):
		from frappe.utils.password import set_encrypted_password

		before = frappe.db.get_value("Distributor", "RSR", ["enabled", "catalog_endpoint", "catalog_client_id"],
			as_dict=True)
		self.addCleanup(self._forget_credentials, "Distributor", "RSR", "catalog_secret")
		self.addCleanup(frappe.db.set_value, "Distributor", "RSR", dict(before))
		frappe.db.set_value("Distributor", "RSR", {"enabled": 0, "catalog_endpoint": "https://own.example",
			"catalog_client_id": "own"})
		set_encrypted_password("Distributor", "RSR", "own-secret", "catalog_secret")
		p = mock.patch.object(ds, "_house_service", side_effect=REAL_HOUSE)
		p.start()
		self.addCleanup(p.stop)
		self.assertFalse(REAL_DISTRIBUTOR())                 # a stale endpoint on a disabled row
		frappe.db.set_value("Distributor", "RSR", "enabled", 1)
		self.assertTrue(REAL_DISTRIBUTOR())
		frappe.db.set_value("Distributor", "RSR", "catalog_client_id", "")
		self.assertFalse(REAL_DISTRIBUTOR())                 # incomplete credential

	@staticmethod
	def _forget_credentials(doctype, name, field):
		frappe.db.sql("DELETE FROM `__Auth` WHERE doctype = %s AND name = %s AND fieldname = %s",
			(doctype, name, field))

	def test_a_save_drops_the_boot_only_after_the_commit(self):
		frappe.db.after_commit.reset()
		self.addCleanup(frappe.db.after_commit.reset)
		with mock.patch.object(ds.frappe.cache, "delete_key") as delete:
			ds.on_settings_change()
			delete.assert_not_called()
			frappe.db.after_commit.run()
		delete.assert_called_with("bootinfo")

	def test_distributors_and_extension_switches_drop_the_answer_too(self):
		events = frappe.get_hooks("doc_events")
		handler = "ffl_integrations.data_service.on_settings_change"
		for doctype, event in (("Data Service Settings", "on_update"), ("FFL Settings", "on_update"),
				("Distributor", "on_update"), ("Distributor", "on_trash")):
			self.assertIn(handler, events.get(doctype, {}).get(event, []), (doctype, event))
		self.assertIn("ffl_integrations.data_service.on_extension_toggled",
			frappe.get_hooks("gunshop_extension_toggled"))
		from ffl_core.extensions.lint import GUNSHOP_HOOKS

		self.assertIn(ds.AI_HOOK, GUNSHOP_HOOKS)   # an AI extension may declare it

	def test_a_local_catalog_has_no_tree(self):
		self._service_configured.return_value = True
		with mock.patch(LOCAL, return_value=True):
			caps = ds.capabilities()
		self.assertEqual((caps["catalog"], caps["category_tree"], caps["extension_updates"]),
			("local", "off", "data_service"))

	def test_a_distributor_reading_its_own_service_has_a_catalog_but_no_tree(self):
		self._distributor_service.return_value = True
		caps = ds.capabilities()
		self.assertEqual((caps["catalog"], caps["category_tree"]), ("data_service", "off"))

	def test_ai_comes_from_the_extension_hook_and_a_broken_one_is_off_logged_once(self):
		real = frappe.get_hooks
		hooks = {ds.AI_HOOK: [f"{__name__}.ai_provider"]}

		def get_hooks(name=None, *a, **k):
			return hooks[name] if name in hooks else real(name, *a, **k)

		frappe.cache().delete_value(ds._AI_WARNING_KEY)
		self.addCleanup(frappe.cache().delete_value, ds._AI_WARNING_KEY)
		with mock.patch.object(ds.frappe, "get_hooks", side_effect=get_hooks):
			self.assertEqual(ds.capabilities()["ai"], "connected")
			_ai_answer["value"] = False
			self.addCleanup(_ai_answer.update, value=True)
			ds.clear_capabilities()
			self.assertEqual(ds.capabilities()["ai"], "off")
			hooks[ds.AI_HOOK] = [f"{__name__}.ai_provider_broken"]
			with mock.patch("ffl_integrations.notifications.safe_log_error") as log:
				for _ in range(3):
					ds.clear_capabilities()
					self.assertEqual(ds.capabilities()["ai"], "off")
		self.assertEqual(log.call_count, 1)

	def test_cached_for_the_transaction_and_dropped_when_the_settings_are_saved(self):
		reads = self._service_configured
		reads.reset_mock()
		ds.capabilities()
		ds.capabilities()
		self.assertEqual(reads.call_count, 1)
		frappe.get_single("Data Service Settings").save(ignore_permissions=True)
		ds.capabilities()
		self.assertEqual(reads.call_count, 2)
		frappe.db.value_cache.clear()     # what a commit / rollback does
		ds.capabilities()
		self.assertEqual(reads.call_count, 3)

	def test_the_catalog_selector_reads_only_what_the_catalog_needs(self):
		with mock.patch.object(ds, "_ai") as ai:
			self.assertEqual(ds.capability(ds.CATALOG), "off")
			self.assertTrue(ds.is_off(ds.CATALOG))
		ai.assert_not_called()
		self._ffl_provider_is_service.assert_not_called()

	def test_boot_and_the_endpoint_carry_the_same_answer(self):
		from ffl_integrations.boot import extend_bootinfo

		bootinfo = frappe._dict()
		extend_bootinfo(bootinfo)
		self.assertEqual(bootinfo.ffl_capabilities, ds.capabilities())
		self.assertEqual(ds.get_capabilities(), ds.capabilities())


class TestGuard(_Case):
	def test_capability_off_is_a_417_and_a_service_outage(self):
		self.assertTrue(issubclass(ds.CapabilityOff, frappe.ValidationError))
		self.assertTrue(issubclass(ds.CapabilityOff, client.DataServiceUnavailable))
		self.assertEqual(ds.CapabilityOff.http_status_code, 417)

	def test_require_tells_the_user_and_names_the_capability(self):
		frappe.clear_messages()
		with self.assertRaises(ds.CapabilityOff) as ctx:
			ds.require(ds.CATALOG)
		self.assertEqual(ctx.exception.capability, "catalog")
		self.assertIn("Distributor catalog is not set up", frappe.message_log[-1]["message"])
		self.assertEqual(frappe.local.response.get("capability_off"), "catalog")
		frappe.local.response.pop("capability_off", None)

	def test_the_quiet_form_raises_without_a_message(self):
		frappe.clear_messages()
		with self.assertRaises(ds.CapabilityOff):
			ds.require(ds.CATALOG, notify=False)
		self.assertEqual(frappe.message_log, [])

	def test_a_test_without_a_stub_gets_the_stub_guard_not_capability_off(self):
		"""The contract extensions' tests rely on: no stub, not Local -> CatalogStubMissing,
		even on a blank site (where the real answer is CapabilityOff)."""
		self.assertIs(ds.capability(ds.CATALOG), ds.OFF)
		with self.assertRaises(ds.CapabilityOff):
			remote_catalog._backend()                      # the opt-in the class sets
		frappe.flags.pop(remote_catalog.REAL_CAPABILITY_FLAG, None)
		with self.assertRaises(remote_catalog.CatalogStubMissing):
			remote_catalog._backend()
		# the endpoint guards (quote, ensure_dropship_item, promote) do not refuse either
		self.assertEqual(ds.require(ds.CATALOG), ds.UNKNOWN)
		self.mode("local")
		from ffl_integrations.catalog_local.backend import LocalCatalog

		self.assertIsInstance(remote_catalog._backend(), LocalCatalog)

	def test_an_on_capability_returns_its_source(self):
		self._service_configured.return_value = True
		ds.clear_capabilities()
		self.assertEqual(ds.require(ds.CATALOG), "data_service")


# ---------------------------------------------------------------- the endpoints

class TestEndpointMatrix(_Case):
	"""Every catalog / Data Service entry point in each catalog state: an answer or
	CapabilityOff (HTTP 417) — never another exception — and no Error Log."""

	SKU, UPC = "D1-OFF-1", "700000000909"

	def setUp(self):
		super().setUp()
		# One enabled drop-ship house, so the counter search and the watchdog have work.
		frappe.db.set_value("Distributor", "RSR", {"enabled": 1, "dropship_enabled": 1})
		frappe.clear_messages()

	def _seed(self):
		catalog_stub.seed_catalog([{"rsr_stock_number": self.SKU, "upc": self.UPC,
			"short_description": "D1 TEST WIDGET", "dealer_price": 10.0, "map_price": 15.0,
			"quantity": 4, "department_number": "13"}])

	def _calls(self):
		from ffl_integrations.atf import search as atf_search
		from ffl_integrations.rsr import promote
		from ffl_integrations.rsr import search as rsr_search

		return {
			"search": lambda: api.search("D1 TEST", 5),
			"search_named": lambda: api.search("D1 TEST", 5, distributor="RSR"),
			"catalog_by_upc": lambda: api.catalog_by_upc(self.UPC),
			"quote": lambda: api.quote(sku=self.SKU, distributor="RSR"),
			"rsr_search": lambda: rsr_search.search("D1 TEST", 5),
			"item_form_lookup": lambda: promote.lookup_for_item_form(self.SKU),
			"hub": lambda: hub.overview(),
			"ffl_search": lambda: atf_search.search("NO SUCH DEALER D1", 5),
			"capabilities": lambda: ds.get_capabilities(),
		}

	def _run_all(self):
		out = {}
		for name, call in self._calls().items():
			try:
				out[name] = call()
			except ds.CapabilityOff as exc:
				out[name] = exc
		return out

	def test_off_answers_or_417_and_logs_nothing(self):
		self.mode("off")
		before = self.error_logs()
		with mock.patch.object(client, "_request", side_effect=AssertionError("reached the client")), \
				mock.patch("ffl_integrations.atf.data_service_provider.search_remote",
					side_effect=AssertionError("national search with the capability off")):
			out = self._run_all()
		self.assertEqual(out["search"], [])
		self.assertEqual(out["search_named"], [])
		self.assertEqual(out["catalog_by_upc"], [])
		self.assertEqual(out["rsr_search"], [])
		self.assertIsInstance(out["quote"], ds.CapabilityOff)
		self.assertEqual(out["quote"].http_status_code, 417)
		self.assertIsInstance(out["item_form_lookup"], ds.CapabilityOff)
		rsr = next(r for r in out["hub"]["distributors"] if r.name == "RSR")
		self.assertEqual(rsr["health"], {"catalog_off": True})
		self.assertEqual(out["hub"]["capabilities"]["catalog"], "off")
		self.assertEqual(out["ffl_search"], [])
		self.assertEqual(out["capabilities"]["catalog"], "off")
		self.assertEqual(self.error_logs(), before)

	def test_data_service_answers_as_before(self):
		self.mode("data_service")
		self._seed()
		before = self.error_logs()
		out = self._run_all()
		self.assertEqual([r["sku"] for r in out["search"]], [self.SKU])
		self.assertEqual([r["sku"] for r in out["search_named"]], [self.SKU])
		self.assertEqual([r["sku"] for r in out["catalog_by_upc"]], [self.SKU])
		self.assertTrue(out["quote"]["found"])
		self.assertEqual((out["quote"]["sku"], out["quote"]["cost"]), (self.SKU, 10.0))
		self.assertEqual([r["rsr_stock_number"] for r in out["rsr_search"]], [self.SKU])
		self.assertTrue(out["item_form_lookup"]["found"])
		rsr = next(r for r in out["hub"]["distributors"] if r.name == "RSR")
		self.assertNotIn("catalog_off", rsr["health"])
		self.assertEqual(out["capabilities"]["catalog"], "data_service")
		self.assertEqual(self.error_logs(), before)

	def test_local_answers_from_its_own_tables(self):
		self.mode("local")
		before = self.error_logs()
		with mock.patch.object(client, "_request", side_effect=AssertionError("reached the client")):
			out = self._run_all()
		for name, value in out.items():
			self.assertNotIsInstance(value, ds.CapabilityOff, name)
		self.assertEqual(out["quote"]["found"], False)   # an empty local catalog
		self.assertEqual(out["capabilities"]["catalog"], "local")
		self.assertEqual(self.error_logs(), before)

	def test_the_counter_sale_takes_no_quote_and_logs_nothing_when_off(self):
		"""manual_order asks quote() for a drop-ship line; off is "no quote", silently."""
		from ffl_core.api import manual_order

		self.mode("off")
		before = self.error_logs()
		frappe.clear_messages()
		self.assertIsNone(manual_order._dropship_quote("ANY-ITEM", "RSR", self.SKU))
		self.assertEqual(frappe.message_log, [])
		self.assertNotIn("capability_off", frappe.local.response)
		self.assertEqual(self.error_logs(), before)

	def test_endpoints_that_need_the_catalog_refuse_with_417(self):
		from ffl_integrations.rsr import promote

		self.mode("off")
		for call in (lambda: api.ensure_dropship_item(self.SKU, "RSR"),
				lambda: promote.promote_to_item({"rsr_stock_number": self.SKU}),
				lambda: promote.backfill_from_rsr("NOPE"),
				lambda: cm.run_category_mirror(),
				lambda: cm.m3_resync(dry_run=1)):
			with self.assertRaises(ds.CapabilityOff):
				call()


# ---------------------------------------------------------------- the jobs

class TestJobsWithEverythingOff(_Case):
	def setUp(self):
		super().setUp()
		# No enabled drop-ship house: nothing publishes catalog stock, so "off" is silent.
		frappe.db.set_value("Distributor", "RSR", {"enabled": 1, "dropship_enabled": 0})
		# A site that had mirror trouble before the upgrade.
		frappe.db.set_single_value(cm.SETTINGS, {"category_mirror": cm.MODE_REPORT,
			"category_mirror_issues": 3,
			"category_mirror_report": json.dumps({"error": "catalog service: not configured"})})
		self.mode("off")

	def _tick(self):
		from ffl_core.extensions import updates
		from ffl_integrations.catalog_local import sync
		from ffl_integrations.distributor import tasks

		cm.scheduled()
		watch = tasks.check_feed_staleness()
		local = sync.tick()
		ext = updates.check()
		return watch, local, ext

	def test_three_ticks_log_nothing_enqueue_nothing_and_clean_up_once(self):
		from ffl_integrations.distributor import tasks

		before = self.error_logs()
		writes = []
		real_set = frappe.db.set_single_value

		def spy(doctype, *a, **k):
			if doctype == cm.SETTINGS:
				writes.append(a or k)
			return real_set(doctype, *a, **k)

		with mock.patch.object(cm.frappe, "enqueue") as enqueue, \
				mock.patch.object(tasks, "notify_admins") as notify, \
				mock.patch.object(cm.frappe.db, "set_single_value", side_effect=spy):
			results = [self._tick() for _ in range(3)]
		self.assertEqual(self.error_logs(), before)
		enqueue.assert_not_called()
		notify.assert_not_called()
		self.assertEqual(len(writes), 1)   # the cleanup, on the first tick only
		self.assertEqual(frappe.db.get_single_value(cm.SETTINGS, "category_mirror_issues"), 0)
		self.assertFalse(frappe.db.get_single_value(cm.SETTINGS, "category_mirror_report"))
		watch, local, ext = results[-1]
		self.assertTrue(watch["off"])
		self.assertEqual(local, {"skipped": "not_local"})
		self.assertEqual(ext, {"checked": False, "offered": {}})

	def test_is_feed_stale_has_nothing_to_judge_without_a_drop_ship_house(self):
		from ffl_integrations.distributor import tasks

		frappe.local._dist_feed_stale_cache = {}
		with mock.patch.object(tasks, "notify_admins") as notify:
			self.assertFalse(tasks.is_feed_stale("RSR"))
		notify.assert_not_called()

	def test_a_drop_ship_house_without_a_catalog_is_stale_and_alarms(self):
		from ffl_integrations.distributor import dropship_hooks, tasks

		frappe.db.set_value("Distributor", "RSR", "dropship_enabled", 1)
		frappe.local._dist_feed_stale_cache = {}
		self.assertTrue(tasks.is_feed_stale("RSR"))
		with mock.patch.object(tasks, "notify_admins") as notify, \
				mock.patch.object(dropship_hooks, "feed_stale", return_value=True) as zero:
			watch = tasks.check_feed_staleness()
		self.assertEqual((watch["stale"], watch["off"]), (["RSR"], True))
		zero.assert_called_once_with("RSR", aged=True)    # the house's own zero-stock policy
		notify.assert_called_once()
		self.assertIn("no distributor catalog", notify.call_args[0][1])
		self.assertIn("ON", notify.call_args[0][1])
		self.assertEqual(notify.call_args[1]["dedup_key"], "dist_feed_stale_RSR")

	def test_an_unreadable_setting_skips_the_tick_and_cleans_nothing(self):
		self._service_configured.return_value = None   # a database hiccup, not "not configured"
		ds.clear_capabilities()
		with mock.patch.object(cm.frappe, "enqueue") as enqueue:
			cm.scheduled()
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.get_single_value(cm.SETTINGS, "category_mirror_issues"), 3)
		self.assertIn("not configured", frappe.db.get_single_value(cm.SETTINGS, "category_mirror_report"))


class TestCatalogMasterSplit(_Case):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_APPLY)

	def _tick(self):
		with mock.patch.object(cm, "run_mirror") as mirror, \
				mock.patch.object(cm, "follow_product_changes") as follow:
			cm.scheduled()     # enqueues the tick (run now in tests) when there is work
		return mirror.call_count, follow.call_count

	def test_the_data_service_runs_both_halves(self):
		self.mode("data_service")
		self.assertEqual(self._tick(), (1, 1))

	def test_local_follows_the_products_without_a_tree(self):
		self.mode("local")
		self.assertEqual(self._tick(), (0, 1))

	def test_off_runs_neither(self):
		self.mode("off")
		self.assertEqual(self._tick(), (0, 0))

	def test_report_only_never_follows(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		self.mode("local")
		self.assertEqual(self._tick(), (0, 0))


class _LocalFollowFixtures(_Case):
	"""A local test house, its Items and the follower's baseline (no tests here)."""

	def setUp(self):
		super().setUp()
		catalog_testhouse.purge()
		catalog_testhouse.install(CODE)
		self.addCleanup(catalog_testhouse.uninstall)
		self.addCleanup(catalog_testhouse.purge)      # commits: runs after the rollback below
		self.addCleanup(frappe.db.rollback)
		frappe.cache().delete_value(cm.FeedLegalMarks.KEY)
		self.addCleanup(frappe.cache().delete_value, cm.FeedLegalMarks.KEY)
		self.group = "Misc Accessory" if frappe.db.exists("Item Group", "Misc Accessory") else "All Item Groups"

	def _item(self, code, upc):
		if frappe.db.exists("Item", code):
			frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "upc": upc,
			"item_group": self.group, "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 0, "is_firearm": 0}).insert(ignore_permissions=True)

	def _baseline(self, cursor):
		"""After the store's last commit: these writes roll back with the test."""
		frappe.db.delete("Error Log", {"method": ("like", "Catalog category: D1-%")})
		frappe.db.set_single_value(cm.SETTINGS, {"category_mirror": cm.MODE_APPLY,
			"category_products_cursor": cursor, "category_products_baselined": 1})

	def _review_log(self, item):
		rows = frappe.get_all("Error Log", filters={"method": f"Catalog category: {item} needs review"},
			fields=["error"], order_by="creation desc")
		self.assertEqual(len(rows), 1, f"one review entry for {item}")
		return rows[0].error


class TestLocalFollowsAnFflFlagChangeLikeTheDataService(_LocalFollowFixtures):
	"""A feed change of an FFL flag reaches the Item the same way in both
	modes — the owner is told, nothing about the firearm is changed by itself."""

	def test_both_modes_report_the_same_firearm_difference(self):
		upc_local = upc("99990000007")
		# The local catalog: the house unflags, then flags the product (both loads commit,
		# so they come first; nothing after them commits).
		store.load_catalog(iter([make_row(7, upc=upc_local, ffl_required=False)]), distributor=CODE)
		with mock.patch(LOCAL, return_value=True):
			ds.clear_capabilities()
			head = cm.feed_head()
			store.load_catalog(iter([make_row(7, upc=upc_local, ffl_required=True)]), distributor=CODE)
			self._baseline(head)
			self._item("D1-LOCAL-FLAG", upc_local)
			with mock.patch.object(catalog, "houses", return_value=[{"name": "Test House", "provider": CODE}]), \
					mock.patch.object(client, "_request", side_effect=AssertionError("reached the client")):
				local = cm.follow_product_changes()
		local_log = self._review_log("D1-LOCAL-FLAG")

		# The Data Service (stub): the master's legal answer flips the same way.
		self.mode("data_service")
		cm.upsert_nodes([{"slug": "accessories-other", "name": "Other Accessories", "level": 1}])  # mirrored
		upc_ds = upc("70000000091")
		stub = catalog_stub.seed_catalog([{"rsr_stock_number": "D1-DS-FLAG", "upc": upc_ds,
			"short_description": "D1 FLAG WIDGET", "ffl_required": 0, "department_number": "13"}])
		stub.set_master(upc_ds, "accessories-other", is_firearm=False)
		self._baseline(stub.products_changes("")["cursor"])
		self._item("D1-DS-FLAG", upc_ds)
		frappe.db.set_value("Item", "D1-DS-FLAG", "catalog_category", "accessories-other")
		stub.set_master(upc_ds, "accessories-other", is_firearm=True)
		service = cm.follow_product_changes()
		service_log = self._review_log("D1-DS-FLAG")

		self.assertEqual((local["issues"], service["issues"]), (1, 1))
		self.assertEqual(local_log, service_log)
		self.assertIn("firearm=True", local_log)
		for code in ("D1-LOCAL-FLAG", "D1-DS-FLAG"):   # never applied
			self.assertEqual(frappe.db.get_value("Item", code, ["is_firearm", "item_group"]), (0, self.group))

	def test_a_difference_is_reported_once_per_conclusion(self):
		"""The local feed names a product again on every price / quantity change: the
		same difference is not reported again; an agreement resets it."""
		item = frappe._dict(name="D1-DEDUPE", item_name="D1-DEDUPE", item_group=self.group,
			catalog_category=None, is_firearm=0, has_serial_no=0)

		def product(ffl, qty=1, cost=10.0):
			return {"category": None, "legal": None, "offers": [{"house": CODE, "legal": None,
				"ffl_required": ffl, "product_class": "", "qty": qty, "cost": cost}]}

		def page(prod, row=item):
			"""One follower page: plan, then the marks recorded after its commit."""
			marks = cm.FeedLegalMarks()
			change = cm._feed_legal_change(row, prod, marks)
			marks.flush()
			return change

		self.assertTrue(page(product(True)))
		self.assertIsNone(page(product(True, qty=9, cost=12.5)))   # price / qty
		self.assertIsNone(page(product(False)))                    # agrees again
		self.assertTrue(page(product(True)))                       # a new difference
		self.assertIsNone(page(product(True), frappe._dict(item, has_serial_no=1)))
		# filed by the master: its answer stands, unless a difference is on record
		filed = frappe._dict(item, name="D1-FILED", catalog_category="accessories-other")
		self.assertIsNone(page(product(True), filed))
		marks = cm.FeedLegalMarks()
		marks.mark("D1-FILED", "old")
		marks.flush()
		self.assertTrue(page(product(True), filed))                # re-checked while on record
		self.assertIsNone(page(product(False), filed))             # agreement clears it
		self.assertNotIn("D1-FILED", cm.FeedLegalMarks().stored)

	def test_an_unchanged_flag_reports_nothing(self):
		code = upc("99990000008")
		store.load_catalog(iter([make_row(8, upc=code, ffl_required=False)]), distributor=CODE)
		with mock.patch(LOCAL, return_value=True):
			ds.clear_capabilities()
			self._baseline("")
			before = self.error_logs()
			self._item("D1-LOCAL-SAME", code)
			with mock.patch.object(catalog, "houses", return_value=[{"name": "Test House", "provider": CODE}]):
				out = cm.follow_product_changes()
		self.assertEqual(out["issues"], 0)
		self.assertEqual(self.error_logs(), before)


# ---------------------------------------------------------------- the grep check

CLIENT = "ffl_integrations.data_service.client"
CLIENT_CALLS = {"get", "post", "get_bytes", "_request", "get_connection"}
CLIENT_TARGETS = {f"{CLIENT}.{fn}" for fn in CLIENT_CALLS}
# Only calls that resolve to the real guards count — not any function named so.
GUARD_TARGETS = {"ffl_integrations.distributor.remote_catalog._backend",
	"ffl_integrations.data_service.require"}
APPS_DIR = APP.parents[1]
FIRST_PARTY = ("ffl_core", "ffl_integrations", "ffl_woo_sync")
# Reached only through a guard elsewhere, or outside the capability model — each says why.
ALLOWED = {
	# The Settings page's own probe of the connection: it is how a store sets the
	# service up, so it must run while every capability is still off.
	"ffl_integrations/ffl_integrations/doctype/data_service_settings/data_service_settings.py::test_connection",
	# The switch from the local catalog probes the connection it is about to rely on:
	# the catalog capability is still Local (off for the service) at that moment.
	"ffl_integrations/ffl_integrations/doctype/data_service_settings/data_service_settings.py::_probe_service",
	# EZ Check through the provider chosen in FFL Settings — on its own setting; the
	# national search that also uses it is gated by ffl_national_search in atf/search.py.
	"ffl_integrations/atf/data_service_provider.py::_post",
	# The update source exists only when extension_updates is on (get_source).
	"ffl_integrations/data_service/extension_updates.py::OsaApiSource.latest",
	"ffl_integrations/data_service/extension_updates.py::OsaApiSource.fetch",
}


def _sources(extra=None):
	"""(path from the app dir, dotted module, source) for every first-party module:
	tests and the client itself excluded."""
	for app in FIRST_PARTY:
		root = APPS_DIR / app
		for path in sorted((root / app).rglob("*.py")):
			rel = path.relative_to(root).as_posix()
			if "/tests/" in f"/{rel}" or rel == "ffl_integrations/data_service/client.py":
				continue
			yield rel, rel[:-3].replace("/", "."), path.read_text()
	for rel, source in (extra or {}).items():
		yield rel, rel[:-3].replace("/", "."), source


def _absolute(module: str, node: ast.ImportFrom) -> str:
	"""An ImportFrom's module, relative imports resolved against ``module``."""
	if not node.level:
		return node.module or ""
	parts = module.split(".")
	package = parts if module.endswith(".__init__") else parts[:-1]
	if package and package[-1] == "__init__":
		package = package[:-1]
	base = package[:len(package) - (node.level - 1)]
	return ".".join(base + ([node.module] if node.module else []))


def _module_name(module: str) -> str:
	return module[:-len(".__init__")] if module.endswith(".__init__") else module


def _bindings(tree, module: str) -> dict:
	"""``{local name: the dotted path it stands for}`` over the module (imports at
	any depth, and the module's own top-level functions): ``import a.b`` binds
	``a``; ``import a.b as x`` binds ``x`` to ``a.b``; ``from m import n as x``
	binds ``x`` to ``m.n`` (relative imports resolved)."""
	out = {node.name: f"{_module_name(module)}.{node.name}" for node in tree.body
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom):
			source = _absolute(module, node)
			for a in node.names:
				out[a.asname or a.name] = f"{source}.{a.name}" if source else a.name
		elif isinstance(node, ast.Import):
			for a in node.names:
				if a.asname:
					out[a.asname] = a.name
				else:
					head = a.name.split(".", 1)[0]
					out[head] = head
	return out


def _resolve(node, bindings) -> str:
	"""The dotted path a call's function stands for (``x.y.z`` with ``x`` replaced
	by what it is bound to), or "" when it is not a plain name / attribute chain."""
	dotted = _dotted(node)
	if not dotted:
		return ""
	head, _, rest = dotted.partition(".")
	if head not in bindings:
		return ""
	return bindings[head] + (f".{rest}" if rest else "")


def _dotted(node) -> str:
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if not isinstance(node, ast.Name):
		return ""
	parts.append(node.id)
	return ".".join(reversed(parts))


def unguarded_client_calls(extra=None) -> tuple[list, set]:
	"""(functions calling the client without a guard CALL before it, functions
	calling it at all), over the three first-party apps plus ``extra``
	``{path from the app dir: source}``. A call reaches the client however the
	client is named: imported module or function, any alias, the package's
	attribute (``data_service.client.get``) or the full dotted path."""
	bad, seen = [], set()
	for rel, module, source in _sources(extra):
		tree = ast.parse(source)
		bindings = _bindings(tree, module)
		if not any(v == CLIENT or v.startswith(f"{CLIENT}.") or CLIENT.startswith(f"{v}.")
				for v in bindings.values()):
			continue

		def visit(node, prefix=""):
			for child in ast.iter_child_nodes(node):
				if isinstance(child, ast.ClassDef):
					visit(child, f"{prefix}{child.name}.")
				elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
					calls = [n for n in ast.walk(child) if isinstance(n, ast.Call)]
					hits = [(c.lineno, c.col_offset) for c in calls
						if _resolve(c.func, bindings) in CLIENT_TARGETS]
					if not hits:
						visit(child, f"{prefix}{child.name}.")
						continue
					key = f"{rel}::{prefix}{child.name}"
					seen.add(key)
					guards = [(c.lineno, c.col_offset) for c in calls
						if _resolve(c.func, bindings) in GUARD_TARGETS]
					if key not in ALLOWED and not (guards and min(guards) < min(hits)):
						bad.append(key)
		visit(tree)
	return bad, seen


class TestNothingReachesTheClientUnguarded(FrappeTestCase):
	def test_every_client_call_sits_behind_require_or_the_selector(self):
		bad, _seen = unguarded_client_calls()
		self.assertEqual(bad, [], "call data_service.require() or remote_catalog._backend() first, "
			"or add the function to ALLOWED with the reason")

	def test_the_allow_list_has_no_stale_entries(self):
		_bad, seen = unguarded_client_calls()
		self.assertEqual(sorted(ALLOWED - seen), [])

	def test_the_check_sees_the_known_callers(self):
		_bad, seen = unguarded_client_calls()
		for key in ("ffl_integrations/distributor/remote_catalog.py::rows",
				"ffl_integrations/distributor/catalog.py::batch",
				"ffl_integrations/catalog_master.py::fetch_tree",
				"ffl_integrations/distributor/remote_catalog.py::_ops_get"):
			self.assertIn(key, seen)

	def test_a_new_unguarded_caller_is_caught_however_it_imports_the_client(self):
		bad, seen = unguarded_client_calls({
			"ffl_woo_sync/probe.py": (
				"from ffl_integrations.data_service import client as dsc\n"
				"from ffl_integrations.data_service.client import get_connection as conn\n"
				"import ffl_integrations.data_service.client\n"
				"import ffl_integrations.data_service.client as full\n"
				"from ffl_integrations.data_service import require\n\n"
				"def aliased():\n\treturn dsc.get('/v1/catalog/health')\n\n"
				"def function():\n\treturn conn()\n\n"
				"def plain_import():\n\treturn ffl_integrations.data_service.client.post('/x', {})\n\n"
				"def module_alias():\n\treturn full.get_bytes('/x', max_bytes=1)\n\n"
				"def mentions_require_in_a_string():\n\t'require(catalog) _backend()'\n"
				"\treturn dsc.get('/x')\n\n"
				"def guard_after_the_call():\n\tdsc.get('/x')\n\trequire('catalog')\n\n"
				"def fine():\n\trequire('catalog')\n\treturn dsc.post('/x', {})\n"),
			# Package attributes, the full dotted path, and look-alike guards.
			"ffl_woo_sync/probe3.py": (
				"import ffl_integrations\n"
				"import ffl_integrations.data_service as pkg\n"
				"from ffl_integrations import data_service\n"
				"from ffl_integrations.data_service import require\n"
				"from ffl_integrations.distributor import remote_catalog\n\n"
				"def package_attribute():\n\treturn data_service.client.get('/x')\n\n"
				"def package_alias():\n\treturn pkg.client.post('/x', {})\n\n"
				"def full_dotted():\n\treturn ffl_integrations.data_service.client.get('/x')\n\n"
				"def lookalike_guard():\n\tsomething.require('catalog')\n\t_backend()\n"
				"\treturn data_service.client.get('/x')\n\n"
				"def guarded_by_package():\n\tdata_service.require('catalog')\n"
				"\treturn data_service.client.get('/x')\n\n"
				"def guarded_by_selector():\n\tremote_catalog._backend('RSR')\n"
				"\treturn ffl_integrations.data_service.client.get('/x')\n\n"
				"def guarded_by_name():\n\trequire('catalog')\n\treturn pkg.client.get('/x')\n"),
			"ffl_integrations/distributor/probe4.py": (
				"from .. import data_service\n\n"
				"def relative_package():\n\treturn data_service.client.get('/x')\n"),
			"ffl_integrations/data_service/probe.py": (
				"from . import client\nfrom .client import get as fetch\n\n"
				"def relative():\n\treturn client.get('/x')\n\n"
				"def relative_function():\n\treturn fetch('/x')\n"),
			"ffl_integrations/distributor/probe2.py": (
				"from ..data_service import client\n\n"
				"def up_one():\n\treturn client.get('/x')\n"),
		})
		for name in ("aliased", "function", "plain_import", "module_alias",
				"mentions_require_in_a_string", "guard_after_the_call"):
			self.assertIn(f"ffl_woo_sync/probe.py::{name}", bad)
		self.assertNotIn("ffl_woo_sync/probe.py::fine", bad)
		self.assertIn("ffl_woo_sync/probe.py::fine", seen)
		self.assertIn("ffl_integrations/data_service/probe.py::relative", bad)
		self.assertIn("ffl_integrations/data_service/probe.py::relative_function", bad)
		self.assertIn("ffl_integrations/distributor/probe2.py::up_one", bad)
		for name in ("package_attribute", "package_alias", "full_dotted", "lookalike_guard"):
			self.assertIn(f"ffl_woo_sync/probe3.py::{name}", bad)
		for name in ("guarded_by_package", "guarded_by_selector", "guarded_by_name"):
			self.assertIn(f"ffl_woo_sync/probe3.py::{name}", seen)
			self.assertNotIn(f"ffl_woo_sync/probe3.py::{name}", bad)
		self.assertIn("ffl_integrations/distributor/probe4.py::relative_package", bad)


# ---------------------------------------------------------------- follow-up

class TestFollowUpCapabilityEdges(_Case):
	def test_an_unreadable_catalog_source_is_unknown_never_off(self):
		"""Item 2: a hiccup reading catalog_source is not a confirmed "not Local"."""
		from ffl_integrations import catalog_local

		real = frappe.db.get_single_value

		def flaky(doctype, field, *a, **k):
			if field == "catalog_source":
				raise RuntimeError("db hiccup")
			return real(doctype, field, *a, **k)

		with mock.patch.object(frappe.db, "get_single_value", side_effect=flaky):
			self.assertIsNone(catalog_local.is_local(strict=True))
			self.assertFalse(catalog_local.is_local())          # the other callers' contract
			for name in (ds.CATALOG, ds.CATEGORY_TREE):
				ds.clear_capabilities()
				self.assertEqual(ds.capability(name), ds.UNKNOWN, name)
			self.assertEqual(ds.capability(ds.CATALOG, house="rsr"), ds.UNKNOWN)
			self.assertEqual(ds.require(ds.CATALOG), ds.UNKNOWN)   # no refusal
			with mock.patch.object(cm.frappe, "enqueue") as enqueue:
				cm.scheduled()
			enqueue.assert_not_called()

	def test_an_unknown_capability_name_is_a_programming_error(self):
		"""Item 5: never "off" for a typo (the JS never treats an unknown key as off either)."""
		for call in (lambda: ds.capability("catalogue"), lambda: ds.require("catalogue"),
				lambda: ds.is_off("catalogue"), lambda: ds.require("catalogue", notify=False)):
			with self.assertRaises(ValueError) as ctx:
				call()
			self.assertIn("unknown capability", str(ctx.exception))
		self.assertEqual(set(ds.capabilities()), set(ds.NAMES))


class TestCapabilityAgreesWithTheClient(_Case):
	"""Item 3: the capability answers data_service exactly when the client's
	connection for that read resolves — same row selection (first row per provider
	by priority, disabled rows included), cross-house reads on Data Service Settings
	only."""

	CODE = "d1f-house"

	def setUp(self):
		super().setUp()
		from ffl_integrations.tests.site_fixtures import snapshot_single_values

		self.addCleanup(snapshot_single_values(ds.SETTINGS, ("endpoint", "client_id"),
			passwords=("secret",)))
		# the real readers: this is about what the settings say
		for name, real in (("_service_configured", REAL_SERVICE), ("_distributor_service", REAL_DISTRIBUTOR),
				("_house_service", REAL_HOUSE)):
			p = mock.patch.object(ds, name, side_effect=real)
			p.start()
			self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		for name, priority, enabled in (("D1F-A", 1, 0), ("D1F-B", 2, 1)):
			if not frappe.db.exists("Distributor", name):
				frappe.get_doc({"doctype": "Distributor", "code": name, "distributor_name": name,
					"provider": self.CODE, "priority": priority, "enabled": enabled}).insert(
					ignore_permissions=True)

	def _site(self, on: bool):
		from frappe.utils.password import set_encrypted_password

		frappe.db.set_single_value(ds.SETTINGS, {"endpoint": "https://site.example" if on else "",
			"client_id": "site" if on else ""})
		set_encrypted_password(ds.SETTINGS, ds.SETTINGS, "site-secret", "secret")

	def _house(self, name, endpoint="", client_id="", secret=None):
		from frappe.utils.password import set_encrypted_password

		frappe.db.set_value("Distributor", name, {"catalog_endpoint": endpoint,
			"catalog_client_id": client_id}, update_modified=False)
		frappe.db.sql("DELETE FROM `__Auth` WHERE doctype='Distributor' AND name=%s "
			"AND fieldname='catalog_secret'", (name,))
		if secret:
			set_encrypted_password("Distributor", name, secret, "catalog_secret")
		frappe.clear_document_cache("Distributor", name)

	def _agree(self, house, label):
		ds.clear_capabilities()
		try:
			client.get_connection(house)
			reachable = True
		except client.DataServiceUnavailable:
			reachable = False
		answer = ds.capability(ds.CATALOG, house=house)
		self.assertEqual(answer, ds.DATA_SERVICE if reachable else ds.OFF, label)
		# the selector refuses exactly the reads the client would refuse
		try:
			remote_catalog._backend(house)
			selected = True
		except ds.CapabilityOff:
			selected = False
		self.assertEqual(selected, reachable, label)
		return reachable

	def test_every_row_layout_answers_as_the_client_connects(self):
		cases = [
			# (label, site on, A (disabled, first) settings, B (enabled, second) settings, house read ok, cross ok)
			("no own service, site on", True, {}, {}, True, True),
			("no own service, site blank", False, {}, {}, False, False),
			("disabled first row complete", False,
				{"endpoint": "https://a.example", "client_id": "a", "secret": "s"}, {}, True, False),
			("disabled first row incomplete wins over a complete enabled one", True,
				{"endpoint": "https://a.example"},
				{"endpoint": "https://b.example", "client_id": "b", "secret": "s"}, False, True),
			("only the enabled second row names one", False, {},
				{"endpoint": "https://b.example", "client_id": "b", "secret": "s"}, True, False),
		]
		for label, site, a, b, house_ok, cross_ok in cases:
			self._site(site)
			self._house("D1F-A", **a)
			self._house("D1F-B", **b)
			self.assertEqual(self._agree(self.CODE, label), house_ok, label)
			self.assertEqual(self._agree(None, f"{label} (cross-house)"), cross_ok, label)
			# the site as a whole: on when some read can succeed
			ds.clear_capabilities()
			self.assertEqual(ds.capability(ds.CATALOG) == ds.DATA_SERVICE, house_ok or cross_ok, label)

	def test_the_selection_is_one_helper(self):
		self._house("D1F-A", endpoint="https://a.example")
		self.assertEqual(client.own_service_row(self.CODE), "D1F-A")
		with mock.patch.object(client, "own_service_row", return_value=None) as pick:
			ds.clear_capabilities()
			ds.capability(ds.CATALOG, house=self.CODE)
			client._distributor_connection(self.CODE)
		self.assertEqual(pick.call_count, 2)

	def test_the_unnamed_counter_search_needs_the_site_service(self):
		"""A house's own service does not serve a cross-house search: no request, no log."""
		self._site(False)
		self._house("D1F-B", endpoint="https://b.example", client_id="b", secret="s")
		ds.clear_capabilities()
		self.assertEqual(ds.capability(ds.CATALOG), ds.DATA_SERVICE)
		before = self.error_logs()
		with mock.patch.object(client, "_request", side_effect=AssertionError("reached the client")):
			self.assertEqual(api.product_search("D1 TEST", 5), [])
		self.assertEqual(self.error_logs(), before)


class TestWatchdogOnlyForHousesWithAFeed(_Case):
	"""Item 1: with the catalog off, only a drop-ship house that has a feed is alarmed
	and zeroed; a phone-order house stays skipped, as before the capability model."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Distributor", "RSR", {"enabled": 1, "dropship_enabled": 1})
		self.mode("off")
		frappe.local._dist_feed_stale_cache = {}
		self.addCleanup(setattr, frappe.local, "_dist_feed_stale_cache", {})

	def test_a_house_without_a_feed_stays_skipped(self):
		from ffl_integrations.distributor import dropship_hooks, tasks

		phone_order = object()   # no feed_heartbeat
		with mock.patch.object(tasks, "_provider_by_code", return_value=phone_order), \
				mock.patch.object(tasks, "notify_admins") as notify, \
				mock.patch.object(dropship_hooks, "feed_stale") as zero:
			watch = tasks.check_feed_staleness()
			self.assertFalse(tasks.is_feed_stale("RSR"))
		self.assertEqual((watch["stale"], watch["skipped"]), ([], ["RSR"]))
		notify.assert_not_called()
		zero.assert_not_called()

	def test_a_house_with_a_feed_is_alarmed_and_zeroed(self):
		from ffl_integrations.distributor import dropship_hooks, tasks

		class Fed:
			def feed_heartbeat(self):
				raise AssertionError("asked a feed with no catalog")

		with mock.patch.object(tasks, "_provider_by_code", return_value=Fed()), \
				mock.patch.object(tasks, "notify_admins") as notify, \
				mock.patch.object(dropship_hooks, "feed_stale", return_value=False) as zero:
			watch = tasks.check_feed_staleness()
			self.assertTrue(tasks.is_feed_stale("RSR"))
		self.assertEqual((watch["stale"], watch["skipped"], watch.get("off")), (["RSR"], [], True))
		zero.assert_called_once_with("RSR", aged=True)
		notify.assert_called_once()

	def test_only_the_house_whose_reads_are_off_is_judged_off(self):
		"""A house with an incomplete own service is off; with the site on, the
		watchdog probes every other house as before."""
		from ffl_integrations.distributor import tasks

		self._service_configured.return_value = True
		self._house_service.return_value = False   # RSR's own row is half filled in
		ds.clear_capabilities()
		self.assertEqual(ds.capability(ds.CATALOG), ds.DATA_SERVICE)
		self.assertEqual(ds.capability(ds.CATALOG, house="rsr"), ds.OFF)
		with mock.patch.object(tasks, "notify_admins"), \
				mock.patch("ffl_integrations.distributor.dropship_hooks.feed_stale", return_value=False):
			watch = tasks.check_feed_staleness()
		self.assertIn("RSR", watch["stale"])

	def test_an_unreadable_distributor_keeps_the_last_quantity(self):
		"""Item 8: the DB read sits inside the non-raising contract."""
		from ffl_integrations.distributor import tasks

		with mock.patch.object(tasks.frappe.db, "get_value", side_effect=RuntimeError("db hiccup")), \
				mock.patch.object(tasks, "notify_admins") as notify:
			self.assertFalse(tasks.is_feed_stale("RSR"))
		notify.assert_called_once()
		self.assertIn("Feed staleness check failed", notify.call_args[0][0])


class TestLocalFollowerCostAndDedupe(_LocalFollowFixtures):
	"""Items 4 and 7: the local follower reads offers in one batch a page for the
	Items it checks, keeps its digests in one cache value (one read a tick, one write
	a page, no delete when nothing is stored), and records a report only after its
	Error Log row and the page commit went through."""

	def _local_page(self, rows, items):
		"""Load ``rows`` after a baseline and create ``items`` [(code, upc, extra)]."""
		store.load_catalog(iter([make_row(n, **dict(r, ffl_required=False)) for n, r in rows]),
			distributor=CODE)
		head = cm.feed_head()
		store.load_catalog(iter([make_row(n, **r) for n, r in rows]), distributor=CODE)
		self._baseline(head)
		for code, code_upc, extra in items:
			self._item(code, code_upc)
			if extra:
				frappe.db.set_value("Item", code, extra)
		return head

	def _run(self):
		with mock.patch.object(catalog, "houses", return_value=[{"name": "Test House", "provider": CODE}]), \
				mock.patch.object(client, "_request", side_effect=AssertionError("reached the client")):
			return cm.follow_product_changes()

	def test_one_batch_and_one_cache_value_a_page(self):
		u = [upc(f"9999000002{i}") for i in range(4)]
		with mock.patch(LOCAL, return_value=True):
			ds.clear_capabilities()
			head = self._local_page([(20 + i, {"upc": u[i], "ffl_required": True}) for i in range(4)], [
				("D1-COST-A", u[0], None), ("D1-COST-B", u[1], None),
				("D1-COST-SER", u[2], {"has_serial_no": 1}),
				("D1-COST-FILED", u[3], None)])
			frappe.db.sql("UPDATE `tabItem` SET catalog_category='accessories-other' WHERE name='D1-COST-FILED'")
			cache = frappe.cache()
			batch = mock.Mock(wraps=catalog.batch)
			with mock.patch.object(catalog, "batch", batch), \
					mock.patch.object(cache, "get_value", wraps=cache.get_value) as get, \
					mock.patch.object(cache, "set_value", wraps=cache.set_value) as put, \
					mock.patch.object(cache, "delete_value", wraps=cache.delete_value) as delete:
				out = self._run()
			self.assertEqual(out["issues"], 2)                          # A and B only
			self.assertEqual(batch.call_count, 1)                       # one page, one read
			self.assertEqual(sorted(batch.call_args[0][0]), sorted([u[0], u[1]]))
			mark_key = cm.FeedLegalMarks.KEY
			self.assertEqual([c for c in get.call_args_list if c.args[:1] == (mark_key,)], [mock.call(mark_key)])
			self.assertEqual(len([c for c in put.call_args_list if c.args[:1] == (mark_key,)]), 1)
			self.assertFalse([c for c in delete.call_args_list if mark_key in str(c)])
			self.assertEqual(sorted(cm.FeedLegalMarks().stored), ["D1-COST-A", "D1-COST-B"])

			# The same products again (a price change): nothing reported, nothing written.
			self._baseline(head)
			with mock.patch.object(cache, "set_value", wraps=cache.set_value) as put, \
					mock.patch.object(cache, "delete_value", wraps=cache.delete_value) as delete:
				self.assertEqual(self._run()["issues"], 0)
			self.assertFalse([c for c in put.call_args_list if c.args[:1] == (mark_key,)])
			self.assertFalse([c for c in delete.call_args_list if mark_key in str(c)])

	def test_a_report_is_recorded_only_after_it_is_logged_and_committed(self):
		code_upc = upc("99990000031")
		with mock.patch(LOCAL, return_value=True):
			ds.clear_capabilities()
			head = self._local_page([(31, {"upc": code_upc, "ffl_required": True})],
				[("D1-ORDER", code_upc, None)])
			with mock.patch.object(cm.frappe, "log_error", side_effect=RuntimeError("log down")), \
					self.assertRaises(RuntimeError):
				self._run()
			self.assertNotIn("D1-ORDER", cm.FeedLegalMarks().stored)
			self._baseline(head)
			with mock.patch.object(cm, "commit", side_effect=RuntimeError("commit failed")), \
					self.assertRaises(RuntimeError):
				self._run()
			self.assertNotIn("D1-ORDER", cm.FeedLegalMarks().stored)
			self._baseline(head)
			self.assertEqual(self._run()["issues"], 1)                 # still reported
			self.assertIn("D1-ORDER", cm.FeedLegalMarks().stored)
