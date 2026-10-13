"""Store product categories follow the catalog master's tree: terms
linked by id / store slug / name under the parent, renamed and re-parented in
place (slug untouched, never deleted), products re-categorised category-only,
the change feed and the m3_resync migration."""
from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations import catalog_master as cm
from ffl_integrations.tests import catalog_stub
from ffl_woo_sync.woocommerce import categories


class FakeWoo:
	"""A store's product categories and products, over the client calls the mirror
	uses. ``terms``: id -> {name, slug, parent, menu_order, count}."""

	def __init__(self, terms):
		self.terms = {tid: dict(t, id=tid, menu_order=t.get("menu_order", 0), count=t.get("count", 0))
			for tid, t in terms.items()}
		self.puts, self.posts, self.product_updates, self.deletes = [], [], [], []
		self.products = {}      # product id (str) -> {category ids}, what GET products returns
		self.refuse = set()     # product ids whose update the store refuses
		self.flaky = set()      # product ids whose next update gets a 503 once
		self.gets = []

	def list_categories(self):
		return [dict(t) for t in self.terms.values()]

	def put(self, path, json=None):
		tid = int(path.rsplit("/", 1)[1])
		self.puts.append((tid, dict(json)))
		self.terms[tid].update(json)

	def post(self, path, json=None):
		tid = max(self.terms) + 1
		self.terms[tid] = {"id": tid, "name": json["name"], "slug": json["name"].lower().replace(" ", "-"),
			"parent": json.get("parent", 0), "menu_order": json.get("menu_order", 0), "count": 0}
		self.posts.append(dict(json))
		return {"id": tid}

	def delete(self, *a, **k):
		self.deletes.append(a)

	def get(self, path, params=None):
		assert path == "products", path
		self.gets.append(dict(params))
		ids = params["include"].split(",")
		assert len(ids) <= 100 and params["per_page"] == 100
		assert all(i.isdigit() for i in ids), ids   # the real store 400s the whole GET
		return [{"id": int(i), "categories": [{"id": c} for c in sorted(self.products[i])]}
			for i in ids if i in self.products]

	def update_product(self, product_id, payload):
		if str(product_id) in self.refuse:
			raise RuntimeError(f"store refused {product_id}")
		if str(product_id) in self.flaky:
			from ffl_woo_sync.woocommerce.client import WooCommerceError
			self.flaky.discard(str(product_id))
			raise WooCommerceError("PUT products → 503", status_code=503)
		self.product_updates.append((product_id, payload))
		if str(product_id) in self.products and "categories" in payload:
			self.products[str(product_id)] = {c["id"] for c in payload["categories"]}
		return {}


# A live store's terms (2026-09-26), the part the tests touch.
STORE_TERMS = {
	836: {"name": "Firearms", "slug": "firearms", "parent": 0},
	78: {"name": "Pistol", "slug": "pistol", "parent": 836},
	77: {"name": "Rifle", "slug": "rifle", "parent": 836},
	557: {"name": "NFA", "slug": "nfa", "parent": 836},
	561: {"name": "Suppressor", "slug": "suppressor", "parent": 557},
	831: {"name": "Parts", "slug": "parts", "parent": 0},
	816: {"name": "Handguards & Rails", "slug": "handguards-rails", "parent": 831},
	814: {"name": "Stocks & Grips", "slug": "stocks-grips", "parent": 831},
	346: {"name": "Optic", "slug": "optic", "parent": 0},
	780: {"name": "Magazines", "slug": "magazines", "parent": 0},
	589: {"name": "Accessory", "slug": "accessory", "parent": 0},
	518: {"name": "Knives & Tools", "slug": "knife", "parent": 589},
	818: {"name": "Optic Mounts & Adapters", "slug": "optic-mounts-adapters", "parent": 589},
	777: {"name": "Other Accessories", "slug": "other-accessories", "parent": 589},
	15: {"name": "Uncategorized", "slug": "uncategorized", "parent": 0},
	999: {"name": "Clearance", "slug": "clearance", "parent": 0},
}


class _Case(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()

	def setUp(self):
		frappe.db.delete("Catalog Category")
		for group in ("Bipods & Rests", "Gas System Parts"):   # the mirror creates them
			if frappe.db.exists("Item Group", group):
				frappe.delete_doc("Item Group", group, ignore_permissions=True, force=True)
		frappe.db.set_single_value(cm.SETTINGS, {"category_mirror": cm.MODE_APPLY,
			"category_tree_cursor": "", "category_products_cursor": "", "category_products_baselined": 0,
			"category_mirror_report": "", "category_mirror_issues": 0})
		self.stub = catalog_stub.install()
		self.stub.seed_tree(catalog_stub.TREE_A)
		self.woo = FakeWoo(STORE_TERMS)
		p = mock.patch.object(categories, "configured_stores", side_effect=lambda: [("retail", self.woo)])
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self):
		catalog_stub.uninstall()

	def _term(self, slug):
		return frappe.db.get_value("Catalog Category", slug, "woo_term_retail")

	def _list(self, code, slug, product_id="9001"):
		"""An Item filed under ``slug`` with a product listed on the retail store
		(reconcile Pass 1's predicate: a product id and ``woo_listed``)."""
		if frappe.db.exists("Item", code):
			frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Misc Accessory", "stock_uom": "Nos", "is_stock_item": 0,
			"woo_product_id": product_id}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", code, {"catalog_category": slug, "woo_listed": 1})


class StoreMirror(_Case):
	def test_links_by_store_slug_and_takes_the_trees_names_and_parents(self):
		cm.run_mirror()
		self.assertEqual(self._term("firearms-pistols"), 78)
		self.assertEqual(self.woo.terms[78]["name"], "Pistols")
		self.assertEqual(self.woo.terms[78]["slug"], "pistol")               # URL untouched
		self.assertEqual(self._term("accessories"), 589)
		self.assertEqual(self.woo.terms[589]["name"], "Accessories")
		self.assertEqual(self._term("firearms-suppressors"), 561)
		self.assertEqual(self.woo.terms[561]["name"], "Suppressors")
		self.assertEqual(self.woo.terms[561]["parent"], 557)
		# Optic mounts: linked by slug, renamed, and already under Accessory.
		self.assertEqual(self._term("optics-mounts-rings"), 818)
		self.assertEqual(self.woo.terms[818]["name"], "Optic Mounts & Rings")
		self.assertEqual(self.woo.terms[818]["parent"], 589)
		self.assertEqual(self.woo.deletes, [])

	def test_an_unescaped_same_name_under_the_parent_links(self):
		cm.run_mirror()
		self.assertEqual(self._term("parts-handguards"), 816)
		self.assertNotIn(816, [tid for tid, ch in self.woo.puts if "name" in ch])

	def test_missing_active_nodes_are_created_under_their_parent(self):
		self._list("M3B-L-RB", "parts-barrels-rifle")   # a listed product; its ancestors come too
		cm.run_mirror()
		tid = self._term("parts-barrels")
		self.assertTrue(tid)
		self.assertEqual(self.woo.terms[tid]["parent"], 831)
		self.assertEqual(self.woo.terms[tid]["name"], "Barrels")
		child = self._term("parts-barrels-rifle")
		self.assertEqual(self.woo.terms[child]["parent"], tid)

	def test_a_node_with_no_listed_product_waits_then_is_created(self):
		# Owner 2026-09-26: no empty store categories.
		report = cm.run_mirror()
		store = report["stores"]["retail"]
		self.assertFalse(self._term("accessories-bipods-rests"))
		self.assertIn("accessories-bipods-rests", {w["category"] for w in store["waiting"]})
		self.assertNotIn(("accessories-bipods-rests", "create"),
			{(a["category"], a["action"]) for a in store["actions"]})
		self.assertFalse(any(p["name"] == "Bipods & Rests" for p in self.woo.posts))
		# An empty subtree waits node by node — none of it is an issue.
		self.assertEqual(store["issues"], [])
		self.assertIn("ammunition-handgun", {w["category"] for w in store["waiting"]})
		self._list("M3B-L-BIPOD", "accessories-bipods-rests")
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		tid = self._term("accessories-bipods-rests")
		self.assertTrue(tid)
		self.assertEqual(self.woo.terms[tid]["parent"], 589)

	def test_until_created_a_push_uses_the_nearest_linked_parent(self):
		from ffl_woo_sync.woocommerce.sync import _apply_category
		cm.run_mirror()
		self._list("M3B-L-GAS", "parts-gas-system")
		payload = {}
		_apply_category(self.woo, payload, "M3B-L-GAS", "retail")
		self.assertEqual(payload["categories"], [{"id": 831}])     # Parts
		self.assertFalse(any(p["name"] == "Gas System Parts" for p in self.woo.posts))

	def test_an_inactive_node_is_never_linked_or_created(self):
		self.woo.terms[586] = {"id": 586, "name": "Service", "slug": "service", "parent": 0,
			"menu_order": 0, "count": 1}
		self.stub.seed_tree([{"slug": "services", "name": "Services", "parent": None,
			"active": False, "store_hint": "service"}])
		self._list("M3B-L-SVC", "services")
		self._list("M3B-L-AMMO", "ammunition-handgun")
		report = cm.run_mirror()
		store = report["stores"]["retail"]
		self.assertFalse(self._term("services"))
		self.assertIn(586, {t["id"] for t in store["unlinked_terms"]})
		self.assertNotIn("services", {i["category"] for i in store["issues"]})
		self.assertEqual(self.woo.terms[586]["name"], "Service")
		# "Service" is nothing like "Ammunition": the top-level create is not held.
		self.assertTrue(self._term("ammunition"))
		self.assertTrue(self._term("ammunition-handgun"))
		from ffl_woo_sync.woocommerce.sync import _apply_category
		payload = {}
		_apply_category(self.woo, payload, "M3B-L-SVC", "retail")
		self.assertNotIn("categories", payload)                    # keeps its store category

	def test_a_node_whose_products_are_off_the_store_waits(self):
		# Reconcile's predicate, not a bare product id — a product a human
		# took off the shop keeps its id; it is no reason for a store category.
		from ffl_woo_sync.woocommerce import sync
		self._list("M3B-L-BIPOD", "accessories-bipods-rests")
		sync.mark_woo_delisted("Item", "M3B-L-BIPOD", site="retail")
		store = cm.run_mirror()["stores"]["retail"]
		self.assertFalse(self._term("accessories-bipods-rests"))
		self.assertIn("accessories-bipods-rests", {w["category"] for w in store["waiting"]})
		self.assertNotIn("accessories-bipods-rests", categories.listed_nodes("retail"))

	def test_a_gun_on_sale_lists_its_node_sold_consigned_hidden_do_not(self):
		# listed_nodes' Serial No branch (reconcile Pass 2's predicate).
		from ffl_woo_sync.woocommerce.sync import _f
		code, sn, node = "M3B-SN-GUN", "M3B-SN-0001", "accessories-bipods-rests"
		def drop():   # a listed gun left behind would list the node in every later test
			for doctype, name in (("Serial No", sn), ("Item", code)):
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		drop()
		self.addCleanup(drop)
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Misc Accessory", "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", code, {"catalog_category": node})
		frappe.get_doc({"doctype": "Serial No", "serial_no": sn, "item_code": code}).insert(
			ignore_permissions=True)
		on_sale = {"status": "Active", "consigned_out": 0, "is_transfer_gun": 0,
			_f("retail", "product_id"): "9101", _f("retail", "delisted"): 0}
		frappe.db.set_value("Serial No", sn, on_sale)
		self.assertIn(node, categories.listed_nodes("retail"))
		for off in ({"status": "Delivered"}, {"consigned_out": 1}, {_f("retail", "delisted"): 1}):
			frappe.db.set_value("Serial No", sn, dict(on_sale, **off))
			self.assertNotIn(node, categories.listed_nodes("retail"), off)
		frappe.db.set_value("Serial No", sn, on_sale)
		self.assertIn(node, categories.listed_nodes("retail"))

	def test_a_linked_inactive_node_keeps_its_term_as_it_is(self):
		# Retired after it was linked — never renamed, moved or reordered.
		self.woo.terms[586] = {"id": 586, "name": "Service", "slug": "service", "parent": 831,
			"menu_order": 7, "count": 1}
		self.stub.seed_tree([{"slug": "services", "name": "Services", "parent": None,
			"active": False, "store_hint": "service"}])
		cm.run_mirror()
		frappe.db.set_value("Catalog Category", "services", "woo_term_retail", 586)
		store = cm.run_mirror()["stores"]["retail"]
		self.assertEqual(self._term("services"), 586)
		self.assertNotIn(586, [tid for tid, _ch in self.woo.puts])
		self.assertEqual((self.woo.terms[586]["name"], self.woo.terms[586]["parent"]), ("Service", 831))
		self.assertNotIn("services", {a["category"] for a in store["actions"]})

	def test_the_children_of_a_retired_node_are_skipped_not_issues(self):
		# A retired parent is never on the store; its children are not issues.
		self.stub.seed_tree([
			{"slug": "services", "name": "Services", "parent": None, "active": False},
			{"slug": "services-gunsmithing", "name": "Gunsmithing", "parent": "services"}])
		self._list("M3B-L-SMITH", "services-gunsmithing")
		store = cm.run_mirror()["stores"]["retail"]
		self.assertFalse(self._term("services-gunsmithing"))
		self.assertFalse(any(p["name"] == "Gunsmithing" for p in self.woo.posts))
		self.assertNotIn("services-gunsmithing", {i["category"] for i in store["issues"]})

	def test_a_second_tick_changes_nothing(self):
		cm.run_mirror()
		puts, posts = len(self.woo.puts), len(self.woo.posts)
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		self.assertEqual((len(self.woo.puts), len(self.woo.posts)), (puts, posts))

	def test_a_workbench_rename_renames_the_same_term_next_tick(self):
		cm.run_mirror()
		self.stub.edit_node("firearms-pistols", name="Handguns")
		self.woo.__dict__.pop("_catalog_terms", None)
		posts = len(self.woo.posts)
		cm.run_mirror()
		self.assertEqual(self.woo.terms[78]["name"], "Handguns")
		self.assertEqual(self.woo.terms[78]["slug"], "pistol")
		self.assertEqual(len(self.woo.posts), posts)   # no new term

	def test_a_workbench_move_re_parents_the_same_term(self):
		cm.run_mirror()
		self.stub.edit_node("accessories-knives-tools", parent="parts")
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		self.assertEqual(self._term("accessories-knives-tools"), 518)
		self.assertEqual(self.woo.terms[518]["parent"], 831)

	def test_a_hand_edit_in_wp_admin_is_put_back(self):
		cm.run_mirror()
		self.woo.terms[78]["name"] = "Handguns!"
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		self.assertEqual(self.woo.terms[78]["name"], "Pistols")

	def test_report_only_writes_nothing_to_the_store(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		self._list("M3B-L-RB", "parts-barrels-rifle")
		report = cm.run_mirror()
		self.assertEqual((self.woo.puts, self.woo.posts), ([], []))
		self.assertFalse(self._term("firearms-pistols"))
		actions = {(a["category"], a["action"]) for a in report["stores"]["retail"]["actions"]}
		self.assertIn(("firearms-pistols", "link"), actions)
		self.assertIn(("firearms-pistols", "update"), actions)
		self.assertIn(("parts-barrels", "create"), actions)
		self.assertIn(("parts-barrels-rifle", "create"), actions)   # under a parent to be created
		unlinked = {t["slug"] for t in report["stores"]["retail"]["unlinked_terms"]}
		self.assertIn("clearance", unlinked)
		self.assertNotIn("uncategorized", unlinked)

	def test_two_terms_of_the_name_under_the_parent_are_reported(self):
		self.woo.terms[2001] = {"id": 2001, "name": "Barrels", "slug": "barrels", "parent": 831,
			"menu_order": 0, "count": 0}
		self.woo.terms[2002] = {"id": 2002, "name": "Barrels", "slug": "barrels-2", "parent": 831,
			"menu_order": 0, "count": 0}
		report = cm.run_mirror()
		self.assertFalse(self._term("parts-barrels"))
		self.assertIn("parts-barrels", {i["category"] for i in report["stores"]["retail"]["issues"]})
		self.assertFalse(any(p["name"] == "Barrels" for p in self.woo.posts))

	def test_a_same_name_under_another_parent_is_reported_not_twinned(self):
		self.woo.terms[2003] = {"id": 2003, "name": "Barrels", "slug": "barrels", "parent": 0,
			"menu_order": 0, "count": 4}
		report = cm.run_mirror()
		self.assertFalse(self._term("parts-barrels"))
		self.assertFalse(any(p["name"] == "Barrels" for p in self.woo.posts))
		self.assertTrue(frappe.db.get_single_value(cm.SETTINGS, "category_mirror_issues"))
		self.assertIn("parts-barrels", {i["category"] for i in report["stores"]["retail"]["issues"]})

	def test_a_term_held_by_another_node_is_a_conflict(self):
		self.stub.edit_node("accessories-other", store_hint="knife")
		report = cm.run_mirror()
		issues = {i["category"] for i in report["stores"]["retail"]["issues"]}
		self.assertEqual(self._term("accessories-knives-tools"), 518)
		self.assertIn("accessories-other", issues)

	def test_a_deleted_term_is_linked_again(self):
		self._list("M3B-L-PISTOL", "firearms-pistols")
		cm.run_mirror()
		del self.woo.terms[78]
		self.woo.__dict__.pop("_catalog_terms", None)
		report = cm.run_mirror()
		self.assertIn("firearms-pistols", {i["category"] for i in report["stores"]["retail"]["issues"]})
		new = self._term("firearms-pistols")
		self.assertTrue(new and new != 78)
		self.assertEqual(self.woo.terms[new]["parent"], 836)

	def test_a_refused_write_is_that_nodes_issue_the_rest_go_on(self):
		real_put = self.woo.put

		def put(path, json=None):
			if path.endswith("/78"):
				raise RuntimeError("term_exists: Pistols")
			return real_put(path, json)

		self.woo.put = put
		report = cm.run_mirror()
		issues = {i["category"] for i in report["stores"]["retail"]["issues"]}
		self.assertIn("firearms-pistols", issues)
		self.assertEqual(self._term("firearms-pistols"), 78)          # the link is kept
		self.assertEqual(self.woo.terms[589]["name"], "Accessories")   # the rest applied

	def test_no_twin_beside_an_unlinked_category_with_products(self):
		# The store calls it "Uppers"; the tree "Uppers & Conversion Kits" (no hint).
		self.woo.terms[3001] = {"id": 3001, "name": "Uppers", "slug": "uppers", "parent": 831,
			"menu_order": 0, "count": 5}
		self._list("M3B-L-UPPER", "parts-uppers")
		report = cm.run_mirror()
		self.assertFalse(self._term("parts-uppers"))
		self.assertFalse(any(p["name"] == "Uppers & Conversion Kits" for p in self.woo.posts))
		self.assertIn("parts-uppers", {i["category"] for i in report["stores"]["retail"]["issues"]})
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		self.woo.__dict__.pop("_catalog_terms", None)
		dry = cm.run_mirror()
		self.assertNotIn(("parts-uppers", "create"),
			{(a["category"], a["action"]) for a in dry["stores"]["retail"]["actions"]})

	def test_report_mode_push_follows_the_mirrors_conflict_rules(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		self.stub.edit_node("accessories-other", store_hint="knife")   # held by Knives & Tools
		cm.run_mirror()
		self.assertEqual(categories.term_for(self.woo, "retail", "accessories-knives-tools"), 518)
		self.assertEqual(categories.term_for(self.woo, "retail", "accessories-other"), 589)  # its parent

	def test_a_store_that_fails_is_reported(self):
		self.woo.list_categories = mock.Mock(side_effect=RuntimeError("502"))
		report = cm.run_mirror()
		self.assertIn("502", report["stores"]["retail"]["issues"][0]["issue"])


class ProductCategory(_Case):
	CODE = "M3B-WOO-BBL"

	def setUp(self):
		super().setUp()
		if frappe.db.exists("Item", self.CODE):
			frappe.delete_doc("Item", self.CODE, ignore_permissions=True, force=True)
		frappe.get_doc({"doctype": "Item", "item_code": self.CODE, "item_name": "FAXON BBL",
			"item_group": "Misc Accessory", "stock_uom": "Nos", "is_stock_item": 1,
			"upc": "700000000054", "woo_product_id": "5001"}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", self.CODE, "woo_listed", 1)

	def test_payload_takes_the_linked_term(self):
		from ffl_woo_sync.woocommerce.sync import _apply_category
		frappe.db.set_value("Item", self.CODE, "catalog_category", "parts-barrels")  # listed: created
		cm.run_mirror()
		payload = {}
		_apply_category(self.woo, payload, self.CODE, "retail")
		self.assertTrue(self._term("parts-barrels"))
		self.assertEqual(payload["categories"], [{"id": self._term("parts-barrels")}])

	def test_a_non_catalog_item_files_by_its_group(self):
		from ffl_woo_sync.woocommerce.sync import _apply_category
		cm.run_mirror()
		payload = {}
		_apply_category(self.woo, payload, self.CODE, "retail")
		self.assertEqual(payload["categories"], [{"id": 777}])   # Misc Accessory → Other Accessories

	def test_report_mode_reads_the_term_and_walks_up_never_creates(self):
		from ffl_woo_sync.woocommerce.sync import _apply_category
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		cm.run_mirror()
		frappe.db.set_value("Item", self.CODE, "catalog_category", "parts-barrels-rifle")
		payload = {}
		_apply_category(self.woo, payload, self.CODE, "retail")
		self.assertEqual(payload["categories"], [{"id": 831}])   # Parts: Barrels is not on the store yet
		self.assertEqual(self.woo.posts, [])

	def test_no_node_no_categories(self):
		from ffl_woo_sync.woocommerce.sync import _apply_category
		payload = {}
		_apply_category(self.woo, payload, self.CODE, "retail")   # nothing mirrored
		self.assertNotIn("categories", payload)

	def test_feed_moves_the_item_and_its_product_category_only(self):
		cm.run_mirror()
		catalog_stub.seed_catalog([{"rsr_stock_number": "WOO-BBL", "upc": "700000000054",
			"department_number": "32", "short_description": "FAXON BBL", "ffl_required": 1,
			"product_class": "Gun Parts"}], distributor="rsr")
		cm.follow_product_changes()
		self.assertEqual(self.woo.product_updates, [])   # no category in the master yet
		self.stub.set_master("700000000054", "parts-barrels")
		out = cm.follow_product_changes()
		self.assertEqual(out["changed"], 1)
		row = frappe.db.get_value("Item", self.CODE, ["item_group", "catalog_category"], as_dict=True)
		self.assertEqual((row.item_group, row.catalog_category), ("Barrels", "parts-barrels"))
		# Barrels had no listed product yet, so no store term: its parent until the
		# next tick creates it (the product is listed there now).
		self.assertEqual(self.woo.product_updates, [("5001", {"categories": [{"id": 831}]})])
		self.assertTrue(frappe.db.get_single_value(cm.SETTINGS, "category_products_baselined"))
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		self.assertTrue(self._term("parts-barrels"))

	def test_switching_to_apply_moves_nothing_until_resync(self):
		# M3c: the master already has data when the POS release lands (report only).
		catalog_stub.seed_catalog([{"rsr_stock_number": "WOO-BBL", "upc": "700000000054",
			"department_number": "32", "short_description": "FAXON BBL", "ffl_required": 1,
			"product_class": "Gun Parts"}], distributor="rsr")
		self.stub.set_master("700000000054", "parts-barrels")
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		cm.scheduled()
		self.assertTrue(frappe.db.get_single_value(cm.SETTINGS, "category_products_baselined"))
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Misc Accessory")
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_APPLY)
		cm.scheduled()
		cm.scheduled()
		# The old master data moves an Item only through m3_resync.
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Misc Accessory")
		self.assertEqual(self.woo.product_updates, [])
		# A change made after the baseline is followed.
		self.stub.set_master("700000000054", "parts-handguards")
		cm.scheduled()
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Handguards & Rails")

	def test_follow_without_a_baseline_takes_one_and_moves_nothing(self):
		catalog_stub.seed_catalog([{"rsr_stock_number": "WOO-BBL", "upc": "700000000054",
			"department_number": "32", "short_description": "FAXON BBL", "ffl_required": 1,
			"product_class": "Gun Parts"}], distributor="rsr")
		self.stub.set_master("700000000054", "parts-barrels")
		frappe.db.set_single_value(cm.SETTINGS, {"category_products_cursor": "",
			"category_products_baselined": 0})
		out = cm.follow_product_changes()
		self.assertTrue(out["baselined"])
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Misc Accessory")

	def test_the_feed_pages_until_done(self):
		cm.run_mirror()
		self.stub.changes_page_size = 1
		catalog_stub.seed_catalog([{"rsr_stock_number": f"WOO-PG{i}", "upc": "",
			"department_number": "13", "short_description": "X", "ffl_required": 0}
			for i in range(3)] + [{"rsr_stock_number": "WOO-BBL", "upc": "700000000054",
			"department_number": "32", "short_description": "FAXON BBL", "ffl_required": 1,
			"product_class": "Gun Parts"}], distributor="rsr")
		self.stub.set_master("700000000054", "parts-barrels")
		out = cm.follow_product_changes()
		self.assertEqual(out["changed"], 1)
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Barrels")

	def test_feed_side_outage_is_logged_not_raised(self):
		cm.run_mirror()
		with mock.patch("ffl_integrations.distributor.catalog.changes",
				side_effect=cm.DataServiceUnavailable("down")):
			cm.tick()   # no exception
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Misc Accessory")

	def test_a_store_failure_keeps_the_rest_going(self):
		cm.run_mirror()
		catalog_stub.seed_catalog([{"rsr_stock_number": "WOO-BBL", "upc": "700000000054",
			"department_number": "32", "short_description": "FAXON BBL", "ffl_required": 1,
			"product_class": "Gun Parts"}], distributor="rsr")
		self.stub.set_master("700000000054", "parts-barrels")
		self.woo.update_product = mock.Mock(side_effect=RuntimeError("store down"))
		with mock.patch.object(categories.frappe, "log_error") as log:
			out = cm.follow_product_changes()
		# The Item follows the master; the store's refusal is logged (the product
		# takes its category at its next push).
		self.assertEqual(out["changed"], 1)
		self.assertEqual(frappe.db.get_value("Item", self.CODE, "item_group"), "Barrels")
		self.assertIn("Store category not updated", log.call_args.kwargs["title"])


class Resync(_Case):
	CODES = ("M3B-RS-GRIP", "M3B-RS-GUN", "M3B-RS-PLAIN")

	def setUp(self):
		super().setUp()
		cm.run_mirror()
		for code in self.CODES:
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		base = {"doctype": "Item", "stock_uom": "Nos", "is_stock_item": 1}
		frappe.get_doc(dict(base, item_code="M3B-RS-GRIP", item_name="MAGPUL MOE GRIP",
			item_group="Misc Accessory", upc="700000000061", woo_product_id="6001")).insert(ignore_permissions=True)
		frappe.db.set_value("Item", "M3B-RS-GRIP", "woo_listed", 1)
		frappe.get_doc(dict(base, item_code="M3B-RS-GUN", item_name="GLOCK 19",
			item_group="Pistol", upc="700000000078")).insert(ignore_permissions=True)
		frappe.get_doc(dict(base, item_code="M3B-RS-PLAIN", item_name="SHOP RAG",
			item_group="Misc Accessory")).insert(ignore_permissions=True)
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "RS-GRIP", "upc": "700000000061", "department_number": "11",
			 "short_description": "MAGPUL MOE GRIP", "ffl_required": 0},
			{"rsr_stock_number": "RS-GUN", "upc": "700000000078", "department_number": "01",
			 "short_description": "GLOCK 19", "ffl_required": 1},
		], distributor="rsr")
		self.stub.set_master("700000000061", "parts-stocks-grips")
		self.stub.set_master("700000000078", "firearms-rifles", is_firearm=True)

	def _mine(self, out):
		return {c["item"]: c for c in out["items"] if c["item"] in self.CODES}

	def test_dry_run_lists_every_change_and_touches_nothing(self):
		out = cm.m3_resync(dry_run=1)
		mine = self._mine(out)
		self.assertEqual(set(mine), {"M3B-RS-GRIP"})    # the gun is serialized, the rag no catalog Item
		self.assertEqual((mine["M3B-RS-GRIP"]["old_group"], mine["M3B-RS-GRIP"]["new_group"]),
			("Misc Accessory", "Stocks & Grips"))
		self.assertGreaterEqual(out["moves"].get("Misc Accessory → Stocks & Grips", 0), 1)
		self.assertGreaterEqual(out["counts"]["serialized_skipped"], 1)
		self.assertIn("retail", out["stores"])
		self.assertEqual(frappe.db.get_value("Item", "M3B-RS-GRIP", "item_group"), "Misc Accessory")
		self.assertEqual(self.woo.product_updates, [])

	def test_apply_matches_the_dry_run(self):
		full = cm.m3_resync(dry_run=1)
		dry = self._mine(full)
		out = cm.m3_resync(dry_run=0, token=full["token"])
		self.assertTrue(out["queued"])
		self.assertTrue(frappe.db.get_single_value(cm.SETTINGS, "category_products_baselined"))
		for code, change in dry.items():
			row = frappe.db.get_value("Item", code, ["item_group", "catalog_category"], as_dict=True)
			self.assertEqual((row.item_group, row.catalog_category), (change["new_group"], change["new_category"]))
		self.assertEqual(frappe.db.get_value("Item", "M3B-RS-GUN", "item_group"), "Pistol")
		self.assertFalse(frappe.db.get_value("Item", "M3B-RS-GUN", "catalog_category"))
		self.assertIn(("6001", {"categories": [{"id": self._term("parts-stocks-grips")}]}),
			self.woo.product_updates)

	def test_apply_refuses_a_plan_that_changed_since_the_dry_run(self):
		full = cm.m3_resync(dry_run=1)
		with self.assertRaises(frappe.ValidationError):
			cm.m3_resync(dry_run=0)                       # no token
		self.stub.set_master("700000000061", "parts-handguards")
		with self.assertRaises(frappe.ValidationError):
			cm.m3_resync(dry_run=0, token=full["token"])  # the plan moved
		self.assertEqual(frappe.db.get_value("Item", "M3B-RS-GRIP", "item_group"), "Misc Accessory")

	def test_apply_resync_goes_on_after_a_failure(self):
		changes = [{"item": "M3B-RS-GRIP", "item_group": "Stocks & Grips"},
			{"item": "M3B-RS-PLAIN", "item_group": "Stocks & Grips"}]
		real = cm.apply_item

		def apply_item(change):
			if change["item"] == "M3B-RS-GRIP":
				raise RuntimeError("locked")
			real(change)

		with mock.patch.object(cm, "apply_item", side_effect=apply_item), \
				mock.patch.object(cm.frappe, "log_error"):
			out = cm.apply_resync(changes)
		self.assertEqual(out, {"done": 1, "failed": 1})
		self.assertEqual(frappe.db.get_value("Item", "M3B-RS-GRIP", "item_group"), "Misc Accessory")
		self.assertEqual(frappe.db.get_value("Item", "M3B-RS-PLAIN", "item_group"), "Stocks & Grips")

	def test_apply_needs_the_mirror_in_apply(self):
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		with self.assertRaises(frappe.ValidationError):
			cm.m3_resync(dry_run=0)


class NewTermRecategorize(_Case):
	"""prod 2026-09-27: products pushed to a parent's category (their node had no
	term yet) move into the node's term once the mirror creates or first links it,
	and ``recategorize_store`` catches up the whole store."""

	NODE = "accessories-bipods-rests"

	def setUp(self):
		super().setUp()
		p = mock.patch.object(categories, "PACE_SECONDS", 0)
		p.start()
		self.addCleanup(p.stop)

	def _listed(self, code, slug, product_id, now):
		self._list(code, slug, product_id)
		self.addCleanup(lambda: frappe.delete_doc("Item", code, ignore_permissions=True, force=True))
		self.woo.products[product_id] = set(now)

	def _gun(self, code, sn, slug, product_id, now):
		from ffl_woo_sync.woocommerce.sync import _f
		def drop():
			for doctype, name in (("Serial No", sn), ("Item", code)):
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		drop()
		self.addCleanup(drop)
		frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Misc Accessory", "stock_uom": "Nos", "is_stock_item": 1,
			"has_serial_no": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", code, "catalog_category", slug)
		frappe.get_doc({"doctype": "Serial No", "serial_no": sn, "item_code": code}).insert(
			ignore_permissions=True)
		frappe.db.set_value("Serial No", sn, {"status": "Active", "consigned_out": 0,
			"is_transfer_gun": 0, _f("retail", "product_id"): product_id, _f("retail", "delisted"): 0})
		self.woo.products[product_id] = set(now)

	def test_a_new_term_takes_the_products_filed_under_its_parent(self):
		self._listed("M3B-NT-BIPOD", self.NODE, "9201", {589})        # in Accessories
		self._gun("M3B-NT-GUN", "M3B-NT-0001", self.NODE, "9202", {589})  # a per-serial product
		self._listed("M3B-NT-CURATED", self.NODE, "9203", {589, 999})  # + Clearance (not in the tree)
		self._listed("M3B-NT-ONLY", self.NODE, "9204", {999})          # only outside the tree
		cm.run_mirror()
		tid = self._term(self.NODE)
		self.assertTrue(tid)
		self.assertEqual(self.woo.products["9201"], {tid})
		self.assertEqual(self.woo.products["9202"], {tid})
		# lead 2026-09-27: only tree terms are replaced; Clearance stays.
		self.assertEqual(self.woo.products["9203"], {tid, 999})
		self.assertEqual(self.woo.products["9204"], {tid, 999})
		# Idempotent: the next tick links nothing new and writes nothing.
		before = list(self.woo.product_updates)
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		self.assertEqual(self.woo.product_updates, before)
		self.assertEqual(categories.recategorize_site("retail", under=[self.NODE]),
			{"moved": 0, "failed": 0})

	def test_report_mode_moves_nothing(self):
		self._listed("M3B-NT-BIPOD", self.NODE, "9201", {589})
		frappe.db.set_single_value(cm.SETTINGS, "category_mirror", cm.MODE_REPORT)
		cm.run_mirror()
		self.assertEqual(self.woo.product_updates, [])

	def test_dry_run_reports_and_writes_nothing_then_applying_moves(self):
		cm.run_mirror()                                  # every node linked; bipods waits
		self._listed("M3B-NT-KNIFE", "accessories-knives-tools", "9211", {589})
		self._listed("M3B-NT-OK", "accessories-knives-tools", "9212", {518})
		self._listed("M3B-NT-GONE", "accessories-knives-tools", "9213", set())
		del self.woo.products["9213"]                    # no longer on the store
		frappe.set_user("Administrator")
		out = categories.recategorize_store(site="retail", dry_run=1)["retail"]
		self.assertEqual(out["to_move"], 1)
		self.assertEqual(out["sample"][0]["product_id"], "9211")
		self.assertEqual(out["sample"][0]["want"], 518)
		self.assertEqual(out["sample"][0]["now"], [589])
		self.assertEqual(out["sample"][0]["set"], [518])
		self.assertNotIn("queued", out)
		self.assertEqual(out["moves"], [{"doctype": "Item", "now": "#589 Accessories",
			"want": "#518 Knives & Tools", "count": 1, "sample": ["9211"]}])
		self.assertEqual(out["by_doctype"], {"Item": 1, "Serial No": 0})
		self.assertEqual(out["gone"], 1)
		self.assertEqual(self.woo.product_updates, [])
		out = categories.recategorize_store(site="retail", dry_run=0)["retail"]
		self.assertTrue(out["queued"])
		self.assertEqual(self.woo.product_updates, [("9211", {"categories": [{"id": 518}]})])
		self.assertEqual(categories.recategorize_store(site="retail")["retail"]["to_move"], 0)

	def test_the_catch_up_keeps_categories_outside_the_tree(self):
		cm.run_mirror()
		self._listed("M3B-NT-OPEN", "accessories-knives-tools", "9231", {589, 999, 15})
		self._listed("M3B-NT-KEPT", "accessories-knives-tools", "9232", {518, 999})
		frappe.set_user("Administrator")
		out = categories.recategorize_store(site="retail", dry_run=1)["retail"]
		self.assertEqual([r["product_id"] for r in out["sample"]], ["9231"])   # 9232 is right as it is
		categories.recategorize_store(site="retail", dry_run=0)
		self.assertEqual(self.woo.products["9231"], {518, 999})   # Accessory, Uncategorized replaced
		self.assertEqual(self.woo.products["9232"], {518, 999})

	def test_one_refused_product_is_logged_the_rest_move(self):
		cm.run_mirror()
		self._listed("M3B-NT-K1", "accessories-knives-tools", "9221", {589})
		self._listed("M3B-NT-K2", "accessories-knives-tools", "9222", {589})
		self.woo.refuse = {"9221"}
		frappe.db.delete("Error Log", {"method": ["like", "Store category not updated (retail): M3B-NT-K1%"]})
		frappe.db.delete("Error Log", {"method": "Store categories: 1 not updated (retail)"})
		out = categories.recategorize_site("retail")
		self.assertEqual(out, {"moved": 1, "failed": 1})
		self.assertEqual(self.woo.products["9222"], {518})
		self.assertTrue(frappe.db.exists("Error Log",
			{"method": ["like", "Store category not updated (retail): M3B-NT-K1%"]}))
		self.assertTrue(frappe.db.exists("Error Log",
			{"method": "Store categories: 1 not updated (retail)"}))

	def test_a_5xx_on_a_product_is_retried(self):
		cm.run_mirror()
		self._listed("M3B-NT-K1", "accessories-knives-tools", "9221", {589})
		self.woo.flaky = {"9221"}
		with mock.patch("ffl_woo_sync.woocommerce.sync.time.sleep") as slept:
			self.assertEqual(categories.recategorize_site("retail"), {"moved": 1, "failed": 0})
		self.assertTrue(slept.called)
		self.assertEqual(self.woo.products["9221"], {518})

	def test_a_first_link_moves_nothing_until_the_catch_up(self):
		# lead 2026-09-27: a store's first Apply links every node at once (by store
		# slug or name) — that must not rewrite the store; only a created term moves.
		self._listed("M3B-NT-KNIFE", "accessories-knives-tools", "9241", {589})
		self._gun("M3B-NT-KGUN", "M3B-NT-0002", "accessories-knives-tools", "9242", {589})
		self.assertFalse(self._term("accessories-knives-tools"))
		report = cm.run_mirror()
		self.assertEqual(self._term("accessories-knives-tools"), 518)   # linked by store slug
		self.assertNotIn("accessories-knives-tools", report["stores"]["retail"]["created"])
		self.assertEqual(self.woo.product_updates, [])
		frappe.set_user("Administrator")
		out = categories.recategorize_store(site="retail")["retail"]
		self.assertEqual(out["by_doctype"], {"Item": 1, "Serial No": 1})
		self.assertEqual({(m["doctype"], m["now"], m["want"], m["count"]) for m in out["moves"]},
			{("Item", "#589 Accessories", "#518 Knives & Tools", 1),
				("Serial No", "#589 Accessories", "#518 Knives & Tools", 1)})

	def test_a_new_term_moves_its_subtree_only(self):
		cm.run_mirror()
		self._listed("M3B-NT-RBBL", "parts-barrels-rifle", "9251", {831})     # under Parts for now
		self._listed("M3B-NT-SIB", "accessories-knives-tools", "9252", {589})  # drifting elsewhere
		report = cm.run_mirror()                         # creates Barrels and Rifle Barrels
		created = report["stores"]["retail"]["created"]
		self.assertEqual(created, ["parts-barrels", "parts-barrels-rifle"])
		self.assertEqual(self.woo.products["9251"], {self._term("parts-barrels-rifle")})
		self.assertEqual([u[0] for u in self.woo.product_updates], ["9251"])
		self.assertEqual(self.woo.products["9252"], {589})

	def test_under_scopes_the_catch_up_to_a_subtree(self):
		cm.run_mirror()                                  # Barrels / Rifle Barrels wait
		self._listed("M3B-NT-RBBL", "parts-barrels-rifle", "9261", {814})     # files up to Parts
		self._listed("M3B-NT-SIB", "accessories-knives-tools", "9262", {589})
		frappe.set_user("Administrator")
		for under in (["parts"], '["parts"]', "parts"):  # two levels up; list, JSON, text
			out = categories.recategorize_store(site="retail", under=under)["retail"]
			self.assertEqual([r["product_id"] for r in out["sample"]], ["9261"], under)
			self.assertEqual(out["sample"][0]["want"], 831)
		self.assertEqual(categories.recategorize_store(site="retail")["retail"]["to_move"], 2)
		with self.assertRaises(frappe.ValidationError):
			categories.recategorize_store(site="retail", under="no-such-node")
		categories.recategorize_store(site="retail", dry_run=0, under="parts")
		self.assertEqual([u[0] for u in self.woo.product_updates], ["9261"])
		self.assertEqual(self.woo.products["9262"], {589})

	def test_counts_what_it_cannot_move(self):
		cm.run_mirror()
		self._listed("M3B-NT-AMMO", "ammunition-handgun", "9271", {15})   # no term, nor its parent
		self._listed("M3B-NT-BADID", "accessories-knives-tools", "woo-9272", {589})
		frappe.set_user("Administrator")
		out = categories.recategorize_store()             # every store
		self.assertEqual(out["retail"]["no_term"], 1)
		self.assertEqual(out["retail"]["gone"], 1)       # not a number: never sent to the store
		self.assertEqual(out["retail"]["to_move"], 0)
		self.assertEqual(set(out), {"retail"})          # the only store

	def test_a_per_serial_firearm_item_is_not_an_item_product(self):
		# reconcile Pass 1 skips it (its guns are the products): so does the catch-up.
		self._listed("M3B-NT-PSF", "accessories-knives-tools", "9281", {589})
		frappe.db.set_value("Item", "M3B-NT-PSF", {"is_firearm": 1, "uniform_sell_price": 0})
		self.assertNotIn("9281", {r[3] for r in categories.on_store("retail")})
		frappe.db.set_value("Item", "M3B-NT-PSF", "uniform_sell_price", 1)
		self.assertIn("9281", {r[3] for r in categories.on_store("retail")})

	def test_reads_the_store_a_hundred_products_a_get(self):
		cm.run_mirror()
		self.woo.gets.clear()
		with mock.patch.object(categories, "on_store", return_value=[
				("Item", f"X{i}", "M3B-NT-KNIFE", str(10000 + i)) for i in range(250)]), \
				mock.patch.object(categories, "_item_nodes",
					return_value={"M3B-NT-KNIFE": "accessories-knives-tools"}):
			categories.store_drift("retail", self.woo)
		self.assertEqual(len(self.woo.gets), 3)

	def test_a_store_off_the_tree_is_left_alone(self):
		frappe.db.set_single_value("WooCommerce Settings", "follow_category_tree", 0)
		frappe.set_user("Administrator")
		# The real configured_stores leaves an off store out; this class stubs it in.
		with mock.patch.object(categories, "configured_stores", return_value=[]):
			self.assertEqual(categories.recategorize_store(site="retail", dry_run=0),
				{"retail": {"skipped": "off"}})
			self.assertEqual(categories.recategorize_site("retail")["skipped"], "off")
		self.assertEqual(self.woo.product_updates, [])


class StoreSwitch(FrappeTestCase):
	"""Owner 2026-09-26: the mirror touches only the stores that follow the tree —
	the retail store, by default."""

	def _clear(self):
		frappe.db.sql("DELETE FROM `tabSingles` WHERE field='follow_category_tree'")

	def test_defaults_retail_on(self):
		self._clear()
		self.assertTrue(categories.follows_tree("retail"))
		frappe.db.set_single_value("WooCommerce Settings", "follow_category_tree", 0)
		self.assertFalse(categories.follows_tree("retail"))

	def test_an_off_store_is_skipped_not_an_issue(self):
		self._clear()
		frappe.db.set_single_value("WooCommerce Settings", "follow_category_tree", 0)
		with mock.patch.object(categories, "configured_stores", return_value=[]):
			out = categories.mirror_stores(apply=False)
		self.assertEqual(out["retail"]["skipped"], "off")
		self.assertEqual(out["retail"]["issues"], [])
		# The same keys as a mirrored store's report.
		self.assertTrue({"actions", "issues", "waiting", "twin_risk", "linked",
			"unlinked_terms"} <= set(out["retail"]))

	def test_configured_stores_leaves_an_off_store_out(self):
		self._clear()
		with mock.patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				side_effect=lambda site: site) as build:
			self.assertEqual(categories.configured_stores(), [("retail", "retail")])
		build.assert_called_once_with("retail")

	def test_the_seed_patch_ticks_retail_once_and_never_overturns(self):
		# The form shows the retail default; a stated 0 stays 0.
		from ffl_core.utils import single_raw
		from ffl_woo_sync.patches import seed_follow_category_tree
		self._clear()
		seed_follow_category_tree.execute()
		self.assertEqual(str(single_raw("WooCommerce Settings", "follow_category_tree")), "1")
		frappe.db.set_single_value("WooCommerce Settings", "follow_category_tree", 0)
		seed_follow_category_tree.execute()
		self.assertFalse(categories.follows_tree("retail"))

	def test_like_names_fold_plurals(self):
		for store_name, node_name in (("Accessory", "Accessories"), ("Knife", "Knives & Tools"),
				("Batteries", "Battery"), ("Ammo", "Ammunition"), ("Uppers", "Uppers & Conversion Kits")):
			self.assertTrue(categories._like(store_name, node_name), (store_name, node_name))
		self.assertFalse(categories._like("Service", "Ammunition"))
		self.assertFalse(categories._like("Other Accessories", "Other Parts"))
		self.assertTrue(categories._like("Gloves", "Glove"))          # not "glof"
		self.assertTrue(categories._like("Sleeves", "Sleeve"))

	def test_a_push_to_an_off_store_leaves_its_categories(self):
		from ffl_woo_sync.woocommerce.sync import _apply_category
		self._clear()
		frappe.db.set_single_value("WooCommerce Settings", "follow_category_tree", 0)
		payload = {}
		with mock.patch.object(categories, "item_category") as node:
			_apply_category(object(), payload, "ANY", "retail")
		node.assert_not_called()
		self.assertNotIn("categories", payload)
