"""patches/remove_dealer_store and what the 1.11.0 removal of the dealer (wholesale) WooCommerce
store must leave behind.

The patch: safe to run twice and on a site that never had any of it; removes the 17 custom
fields, what the Dealer WooCommerce Settings doctype left elsewhere (saved values, passwords,
custom permissions, shares, desk links) and the Dealer Portal switch's value — the DocType row
itself goes in migrate's orphan sweep right after; moves the two fields that stay (Serial No's GunBroker section,
Item's Listing Status) off the anchors that go; never touches the fields that only share the
word "dealer" (Item.dealer_price — the Cost field —, Serial No.dealer_sell_price, Purchase
Receipt Item.ffl_dealer_price) or their data; and lists any ``site='dealer'`` Woo Online Order in
one Error Log without changing it (neither shop ever took one).

Also pinned here, because this module is the one place the removed names may still be spelled:
the switch is gone from settings, gate and boot, the Item / Serial No forms carry no dealer
buttons, and ``refuse_removed_field_reads`` — the guard other suites (serial correction, the
FastBound importer) run their real paths under to prove they read none of the removed fields.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches import remove_dealer_store as patch_module

# The data fields the removal took off the doctypes (the section / column breaks hold no data).
REMOVED_FIELDNAMES = frozenset({
	"dealer_woo_product_id", "dealer_woo_listed", "dealer_woo_synced_at", "dealer_woo_sync_error",
	"dealer_woo_delisted", "dealer_woo_sold_archived", "publish_to_portal", "dealer_credit_limit",
})


@contextmanager
def refuse_removed_field_reads():
	"""Fail any ``frappe.db.get_value`` that asks for (or filters on) a removed field.

	The DB columns outlive the removal (nothing drops them until a manual
	``bench trim-tables``), so a stray read would still "work" on an upgraded site and fail
	only on a fresh one — this makes it fail everywhere, at the line that does it."""
	real = frappe.db.get_value

	def guarded(doctype, filters=None, fieldname="name", *args, **kwargs):
		names = fieldname if isinstance(fieldname, (list, tuple)) else [fieldname]
		keys = list(filters) if isinstance(filters, dict) else []
		# Only plain field names count: frappe also takes aggregate dicts / query terms.
		bad = sorted({f for f in [*names, *keys] if isinstance(f, str) and f in REMOVED_FIELDNAMES})
		if bad:
			raise AssertionError(f"read of a removed dealer-store field on {doctype}: {bad}")
		return real(doctype, filters, fieldname, *args, **kwargs)

	with patch.object(frappe.db, "get_value", side_effect=guarded):
		yield


def _app_file(app: str, *parts: str) -> str:
	with open(os.path.join(frappe.get_app_path(app), *parts)) as f:
		return f.read()


class RemoveDealerStorePatch(FrappeTestCase):
	"""The patch, run twice over a site seeded the way a pre-1.11.0 site looks."""

	GROUP = "QA Dealer Removal"
	ITEM = "QA-DEALER-REMOVAL"
	SERIAL = "QA-DEALER-REMOVAL-SN"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		self._seeded_fields: list[str] = []
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		# Remove what was seeded even when an assertion failed half-way, then drop the cached
		# meta: a meta built with a seeded field whose column does not exist (a fresh site)
		# would break every later Item / Serial No save in this process.
		for name in self._seeded_fields:
			frappe.db.delete("Custom Field", {"name": name})
		frappe.db.delete("DocType", {"name": patch_module.SETTINGS_DOCTYPE})
		frappe.db.delete("DocField", {"parent": patch_module.SETTINGS_DOCTYPE})
		frappe.db.delete("Singles", {"doctype": patch_module.SETTINGS_DOCTYPE})
		frappe.db.delete("__Auth", {"doctype": patch_module.SETTINGS_DOCTYPE})
		for doctype, filters in patch_module.LEFTOVER_ROWS:
			if frappe.db.table_exists(doctype):
				frappe.db.delete(doctype, filters)
		for doctype in ("Item", "Serial No", "Customer", "FFL Settings"):
			frappe.clear_cache(doctype=doctype)

	# ---------------------------------------------------------------- seeding

	def _seed_kept_data(self) -> dict:
		"""The kept fields, carrying values, BEFORE any removed field is seeded (inserting a doc
		while the meta lists a field the table lacks would fail on a fresh site)."""
		if not frappe.db.exists("Item Group", self.GROUP):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": self.GROUP,
				"parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", self.ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": self.ITEM, "item_name": self.ITEM,
				"item_group": self.GROUP, "stock_uom": "Nos", "is_stock_item": 1,
				"has_serial_no": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", self.ITEM, {"dealer_price": 123.45, "listing_status": "Listed"},
			update_modified=False)
		if not frappe.db.exists("Serial No", self.SERIAL):
			frappe.get_doc({"doctype": "Serial No", "serial_no": self.SERIAL,
				"item_code": self.ITEM}).insert(ignore_permissions=True)
		frappe.db.set_value("Serial No", self.SERIAL, "dealer_sell_price", 456.78, update_modified=False)
		pr_item = frappe.get_doc({"doctype": "Purchase Receipt Item", "parenttype": "Purchase Receipt",
			"parentfield": "items", "parent": "QA-DEALER-REMOVAL-PR", "item_code": self.ITEM,
			"item_name": self.ITEM, "qty": 1, "rate": 100, "uom": "Nos", "stock_uom": "Nos",
			"conversion_factor": 1, "ffl_dealer_price": 99.5})
		pr_item.name = frappe.generate_hash(length=10)
		pr_item.db_insert()
		return {"pr_item": pr_item.name}

	def _seed_old_site(self):
		"""What a pre-1.11.0 site holds. Rows only (db_insert): creating them for real would ALTER
		TABLE — an implicit commit inside the test transaction (docs/testing.md)."""
		frappe.db.set_value("Custom Field", "Serial No-sn_section_gunbroker", "insert_after",
			"dealer_woo_sold_archived", update_modified=False)
		frappe.db.set_value("Custom Field", "Item-listing_status", "insert_after", "column_listing_a",
			update_modified=False)
		for name in patch_module.CUSTOM_FIELDS:
			if frappe.db.exists("Custom Field", name):
				continue
			dt, fieldname = name.split("-", 1)
			cf = frappe.get_doc({"doctype": "Custom Field", "dt": dt, "fieldname": fieldname,
				"fieldtype": "Section Break" if fieldname.startswith("section") or "_section_" in fieldname
				else "Data", "label": fieldname})
			cf.name = name
			cf.db_insert()
			self._seeded_fields.append(name)
		if not frappe.db.exists("DocType", patch_module.SETTINGS_DOCTYPE):
			dt = frappe.get_doc({"doctype": "DocType", "name": patch_module.SETTINGS_DOCTYPE,
				"module": "FFL Woo Sync", "issingle": 1, "custom": 0,
				"fields": [{"fieldname": "store_url", "fieldtype": "Data", "label": "Store URL"},
					{"fieldname": "consumer_secret", "fieldtype": "Password", "label": "Secret"}]})
			dt.db_insert()
			for child in dt.get_all_children():
				child.db_insert()
		frappe.db.delete("Singles", {"doctype": patch_module.SETTINGS_DOCTYPE})
		frappe.db.sql("insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
			(patch_module.SETTINGS_DOCTYPE, "store_url", "https://dealer.example.test"))
		from frappe.utils.password import set_encrypted_password
		set_encrypted_password(patch_module.SETTINGS_DOCTYPE, patch_module.SETTINGS_DOCTYPE,
			"not-a-real-secret", "consumer_secret")
		frappe.db.delete("Singles", {"doctype": "FFL Settings", "field": "dealer_portal_enabled"})
		frappe.db.sql("insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
			("FFL Settings", "dealer_portal_enabled", "1"))
		self._seed_leftovers()

	def _seed_leftovers(self):
		"""Rows elsewhere that name the doctype: a custom permission, a share, and a desk link,
		shortcut, quick list and sidebar row (each a child row of some page)."""
		dt = patch_module.SETTINGS_DOCTYPE
		rows = [
			{"doctype": "Custom DocPerm", "parent": dt, "parenttype": "DocType",
				"parentfield": "permissions", "role": "System Manager", "read": 1},
			{"doctype": "DocShare", "share_doctype": dt, "share_name": dt, "user": "Administrator",
				"read": 1},
			{"doctype": "Workspace Link", "parent": "QA Dealer Ws", "parenttype": "Workspace",
				"parentfield": "links", "type": "Link", "link_type": "DocType", "link_to": dt,
				"label": "Dealer Store"},
			{"doctype": "Workspace Shortcut", "parent": "QA Dealer Ws", "parenttype": "Workspace",
				"parentfield": "shortcuts", "type": "DocType", "link_to": dt, "label": "Dealer Store"},
			{"doctype": "Workspace Quick List", "parent": "QA Dealer Ws", "parenttype": "Workspace",
				"parentfield": "quick_lists", "document_type": dt, "label": "Dealer Store"},
			{"doctype": "Workspace Sidebar Item", "parent": "QA Dealer Sb",
				"parenttype": "Workspace Sidebar", "parentfield": "items", "type": "Link",
				"link_type": "DocType", "link_to": dt, "label": "Dealer Store"},
			{"doctype": "Sidebar Item", "parent": "QA Dealer Sb", "parenttype": "Sidebar",
				"parentfield": "items", "type": "Link", "link_type": "DocType", "link_to": dt,
				"label": "Dealer Store"},
		]
		self._leftovers = []
		for row in rows:
			if not frappe.db.table_exists(row["doctype"]):
				continue
			doc = frappe.get_doc(row)
			doc.name = frappe.generate_hash(length=10)
			doc.db_insert()
			self._leftovers.append(row["doctype"])
		# Custom DocPerm, DocShare and the workspace rows exist on every supported Frappe.
		self.assertGreaterEqual(len(self._leftovers), 5, self._leftovers)

	def _leftover_count(self) -> int:
		return sum(frappe.db.count(doctype, filters)
			for doctype, filters in patch_module.LEFTOVER_ROWS if frappe.db.table_exists(doctype))

	def _run_patch(self):
		# As migrate runs it: in_patch on, and never in developer mode (deleting a standard
		# DocType in developer mode would also remove its controller folder from the app).
		prior = frappe.flags.in_patch
		frappe.flags.in_patch = True
		try:
			with patch.dict(frappe.conf, {"developer_mode": 0}):
				patch_module.execute()
		finally:
			frappe.flags.in_patch = prior

	# ---------------------------------------------------------------- the patch

	def test_removes_everything_twice_and_keeps_the_shared_word_fields(self):
		kept = self._seed_kept_data()
		self._seed_old_site()
		self.assertTrue(frappe.db.exists("DocType", patch_module.SETTINGS_DOCTYPE))
		self.assertEqual(self._leftover_count(), len(self._leftovers))

		for _run in range(2):           # the second run finds nothing and changes nothing
			self._run_patch()
			for name in patch_module.CUSTOM_FIELDS:
				self.assertFalse(frappe.db.exists("Custom Field", name), name)
			self.assertEqual(len(patch_module.CUSTOM_FIELDS), 17)
			self.assertEqual(self._leftover_count(), 0)
			self.assertFalse(frappe.db.count("Singles", {"doctype": patch_module.SETTINGS_DOCTYPE}))
			self.assertFalse(frappe.db.sql(
				"select count(*) from `__Auth` where doctype=%s", patch_module.SETTINGS_DOCTYPE)[0][0])
			self.assertFalse(frappe.db.count("Singles",
				{"doctype": "FFL Settings", "field": "dealer_portal_enabled"}))
			# re-anchored, so the forms keep their layout
			self.assertEqual(frappe.db.get_value("Custom Field", "Serial No-sn_section_gunbroker",
				"insert_after"), "woo_sold_archived")
			self.assertEqual(frappe.db.get_value("Custom Field", "Item-listing_status",
				"insert_after"), "channels_tab")
			# kept: definitions and data
			for name in ("Item-dealer_price", "Serial No-dealer_sell_price",
					"Purchase Receipt Item-ffl_dealer_price", "Item-listing_status"):
				self.assertTrue(frappe.db.exists("Custom Field", name), name)
			self.assertEqual(float(frappe.db.get_value("Item", self.ITEM, "dealer_price")), 123.45)
			self.assertEqual(frappe.db.get_value("Item", self.ITEM, "listing_status"), "Listed")
			self.assertEqual(float(frappe.db.get_value("Serial No", self.SERIAL, "dealer_sell_price")),
				456.78)
			self.assertEqual(float(frappe.db.get_value("Purchase Receipt Item", kept["pr_item"],
				"ffl_dealer_price")), 99.5)
		self._seeded_fields.clear()   # the patch deleted them

		# The DocType row is migrate's orphan sweep's to delete (it runs after the post_model_sync
		# patches): this release ships no controller for it, so the sweep finds it.
		self.assertTrue(frappe.db.exists("DocType", patch_module.SETTINGS_DOCTYPE))
		self._run_orphan_sweep()
		self.assertFalse(frappe.db.exists("DocType", patch_module.SETTINGS_DOCTYPE))
		self.assertFalse(frappe.db.count("DocField", {"parent": patch_module.SETTINGS_DOCTYPE}))

	def _run_orphan_sweep(self):
		"""``frappe.model.sync.remove_orphan_doctypes`` as migrate runs it (in_migrate, never
		developer mode), minus its commit — the test transaction must still roll back."""
		from frappe.model.sync import remove_orphan_doctypes

		prior = frappe.flags.in_migrate
		frappe.flags.in_migrate = True
		try:
			with patch.dict(frappe.conf, {"developer_mode": 0}), patch.object(frappe.db, "commit"):
				remove_orphan_doctypes()
		finally:
			frappe.flags.in_migrate = prior

	def test_migrate_runs_the_orphan_sweep_after_the_patches(self):
		"""The patch leaves the DocType row to remove_orphan_doctypes; pin that migrate still
		runs it, and after the post_model_sync patches."""
		import inspect

		from frappe.migrate import SiteMigration

		source = inspect.getsource(SiteMigration)
		self.assertIn("remove_orphan_doctypes()", source)
		self.assertLess(source.index("PatchType.post_model_sync"),
			source.index("remove_orphan_doctypes()"))

	def test_a_dealer_store_order_is_listed_once_and_left_alone(self):
		"""Safety net: an order still saying site='dealer' (none expected) gets one Error Log
		naming it per run, and is not changed."""
		frappe.db.delete("Error Log", {"method": patch_module.DEALER_ORDERS_TITLE})
		order = frappe.get_doc({"doctype": "Woo Online Order", "woo_order_id": "QA-DLR-1",
			"site": "dealer", "order_status": "Transferred", "customer_name": "QA Dealer Order"})
		order.name = f"QA-DLR-{frappe.generate_hash(length=6)}"
		order.db_insert()   # the select no longer offers "dealer": a pre-1.11.0 row, as stored
		before = frappe.db.get_value("Woo Online Order", order.name,
			["site", "order_status", "modified"], as_dict=True)

		self._run_patch()

		logs = frappe.get_all("Error Log", filters={"method": patch_module.DEALER_ORDERS_TITLE},
			fields=["error"])
		self.assertEqual(len(logs), 1)
		self.assertIn(order.name, logs[0].error)
		self.assertEqual(frappe.db.get_value("Woo Online Order", order.name,
			["site", "order_status", "modified"], as_dict=True), before)

	def test_no_dealer_store_order_means_no_log(self):
		frappe.db.delete("Error Log", {"method": patch_module.DEALER_ORDERS_TITLE})
		frappe.db.delete("Woo Online Order", {"site": "dealer"})
		self._run_patch()
		self.assertFalse(frappe.db.count("Error Log", {"method": patch_module.DEALER_ORDERS_TITLE}))

	def test_a_site_that_never_had_any_of_it_is_a_no_op(self):
		frappe.db.delete("Singles", {"doctype": "FFL Settings", "field": "dealer_portal_enabled"})
		self._run_patch()
		self.assertFalse(frappe.db.exists("DocType", patch_module.SETTINGS_DOCTYPE))
		self.assertTrue(frappe.db.exists("Custom Field", "Item-dealer_price"))

	def test_registered_after_model_sync_and_the_spent_switch_patch_is_gone(self):
		text = _app_file("ffl_core", "patches.txt")
		post = text.split("[post_model_sync]", 1)[1]
		self.assertIn("ffl_core.patches.remove_dealer_store", post)
		self.assertNotIn("set_feature_switch_defaults", text)

	# ---------------------------------------------------------------- fixtures / layout

	def test_the_fixtures_say_what_the_patch_does(self):
		"""The fixture sync runs after the patch: it must write the same anchors and must not
		bring any removed field back."""
		fixtures = {}
		for app in ("ffl_core", "ffl_integrations", "ffl_woo_sync"):
			for row in json.loads(_app_file(app, "fixtures", "custom_field.json")):
				fixtures[row["name"]] = row
		for name in patch_module.CUSTOM_FIELDS:
			self.assertNotIn(name, fixtures)
		for name, anchor in patch_module.REANCHOR.items():
			self.assertEqual(fixtures[name]["insert_after"], anchor, name)

	def test_the_forms_keep_their_layout(self):
		"""Serial No: the GunBroker section still follows the retail WooCommerce section.
		Item: Listing Status still shows, first on the Channels tab."""
		frappe.clear_cache(doctype="Serial No")
		frappe.clear_cache(doctype="Item")
		serial = [f.fieldname for f in frappe.get_meta("Serial No").fields]
		if "sn_section_gunbroker" in serial:   # ffl_integrations installed
			self.assertEqual(serial.index("sn_section_gunbroker"), serial.index("woo_sold_archived") + 1)
		item = [f.fieldname for f in frappe.get_meta("Item").fields]
		self.assertIn("listing_status", item)
		self.assertEqual(item.index("listing_status"), item.index("channels_tab") + 1)
		self.assertFalse(REMOVED_FIELDNAMES & (set(serial) | set(item)))


class DealerStoreGoneEverywhere(FrappeTestCase):
	"""The switch: nothing left to see or call."""

	def test_the_settings_form_has_no_dealer_portal_switch(self):
		settings = json.loads(_app_file("ffl_core", "ffl_core", "doctype", "ffl_settings",
			"ffl_settings.json"))
		names = {f["fieldname"] for f in settings["fields"]}
		self.assertFalse(names & set(patch_module.FFL_SETTINGS_FIELDS))
		self.assertIn("store_customer_id_numbers", names)

	def test_the_gate_and_boot_no_longer_offer_it(self):
		from ffl_core import feature_gate
		from ffl_integrations.boot import extend_bootinfo

		self.assertFalse(hasattr(feature_gate, "dealer_portal_enabled"))
		self.assertFalse(hasattr(feature_gate, "assert_dealer_portal_enabled"))
		boot = frappe._dict()
		extend_bootinfo(boot)
		self.assertNotIn("ffl_dealer_portal_enabled", boot)
		self.assertNotIn("dealerPortalEnabled", _app_file("ffl_core", "public", "js", "ffl_core.bundle.js"))

	def test_item_and_serial_forms_have_no_dealer_buttons_or_sections(self):
		for script in ("item_woo.js", "serial_no_woo.js"):
			src = _app_file("ffl_woo_sync", "public", "js", script)
			with self.subTest(script=script):
				for gone in ("Dealer Portal", "site: 'dealer'", "dealer_woo", "publish_to_portal"):
					self.assertNotIn(gone, src)
		for dt in ("Item", "Serial No"):
			labels = {f.label for f in frappe.get_meta(dt).fields}
			self.assertNotIn("Dealer WooCommerce", labels)
			self.assertNotIn("Dealer Portal", labels)

	def test_the_settings_doctype_and_its_form_script_are_gone(self):
		from ffl_woo_sync import hooks
		from ffl_woo_sync.woocommerce.client import SITES, STORES

		self.assertNotIn(patch_module.SETTINGS_DOCTYPE, hooks.doctype_js)
		self.assertEqual(SITES, ("retail",))
		self.assertEqual({s["settings"] for s in STORES.values()}, {"WooCommerce Settings"})
		self.assertFalse(os.path.exists(os.path.join(frappe.get_app_path("ffl_woo_sync"),
			"ffl_woo_sync", "doctype", "dealer_woocommerce_settings")))

	def test_catalog_category_has_no_dealer_term(self):
		self.assertFalse(frappe.get_meta("Catalog Category").has_field("woo_term_dealer"))

	def test_the_order_site_offers_only_the_retail_store(self):
		"""The field and its (woo_order_id, site) key stay; the dealer option is gone (no order
		ever used it)."""
		site = frappe.get_meta("Woo Online Order").get_field("site")
		self.assertEqual((site.options or "").split("\n"), ["retail"])
		self.assertEqual(site.default, "retail")
