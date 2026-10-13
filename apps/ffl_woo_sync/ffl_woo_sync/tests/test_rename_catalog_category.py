"""ffl_core.patches.rename_catalog_category on a site in the old shape (DocType ``OSA Category``,
field ``Item.osa_category``): the rows, their parents, Item Groups and store term ids, the Items'
links, the field's Custom Field and Property Setters come through; each stored place the patch
lists (``PLACES`` and user settings) that names an old name or the old route is left as it was
and named in one Error Log; saved views of the category list follow it, cached ones included; a
second run changes nothing; and the category mirror and recategorize jobs then run on the new
names.

Lives in ffl_woo_sync because the jobs it runs after the patch span all three apps.

Its DDL (RENAME TABLE, ALTER TABLE) commits, and so everything pending is committed with it:
these commits are load-bearing (docs/testing.md rule 2). The test rewrites the whole category
table, so it runs only on a site that has no Catalog Category rows (a fresh CI site): on any
other site every test here is skipped — and under CI (env ``CI`` set) fails instead, so the
suite cannot go green without it. It takes a copy of the settings it changes and the Custom
Field values it edits; it records the exact name of every record it makes (the Property Setters
and the Error Logs the patch writes included) and deletes exactly those, and never deletes an
Item Group it did not create (the mirror may create two). The cleanup runs while ``in_patch`` is
still set: it runs the patch again (idempotent — it puts the new shape back if a test stopped
half way), empties the category table, restores the copies, deletes what the test made, and
commits.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches import rename_catalog_category as patch
from ffl_integrations import catalog_master as cm
from ffl_integrations.tests import catalog_stub
from ffl_woo_sync.tests.test_category_mirror import STORE_TERMS, FakeWoo, _Case
from ffl_woo_sync.woocommerce import categories

OLD_DT, NEW_DT, OLD_FIELD, NEW_FIELD = patch.OLD_DOCTYPE, patch.NEW_DOCTYPE, patch.OLD_FIELD, patch.NEW_FIELD
SETTINGS_FIELDS = ("category_mirror", "category_tree_cursor", "category_products_cursor",
	"category_products_baselined", "category_mirror_report", "category_mirror_issues")
ROW_FIELDS = ["slug", "node_name", "path", "parent_slug", "level", "position", "active", "item_group",
	"woo_term_retail"]
CF_NAME, CF_FIELDS = f"Item-{NEW_FIELD}", ("depends_on", "mandatory_depends_on", "read_only_depends_on", "fetch_from")
MIRROR_GROUPS = ("Bipods & Rests", "Gas System Parts")
ITEMS = {"_T-B1-RENAME-1": ("firearms-pistols", "9101"), "_T-B1-RENAME-2": ("accessories", "9102")}
T = "_T-B1"                                   # every record the test makes is named with this
LOG_TITLE = f"rename_catalog_category: still names {OLD_FIELD} or {OLD_DT}"


class RenameCatalogCategory(_Case):
	def setUp(self):
		if frappe.db.count(NEW_DT):
			why = (f"this site has {NEW_DT} rows: the rename test rewrites that table and its "
				"DDL commits, so it runs only on a site without category rows (a fresh CI site)")
			if os.environ.get("CI"):
				self.fail(f"{why}. Under CI that is a failure, not a skip: the suite must not go green without it")
			self.skipTest(why)
		# _Case.setUp would delete every Item Group the mirror makes, and the DDL below commits
		# whatever is pending: take the parts of it this test needs, minus deletes.
		FrappeTestCase.setUp(self)
		self._made: list[tuple[str, str]] = []                # (doctype, exact name), as made
		self._user_settings: list[tuple[str, str]] = []       # (user, doctype), as made
		self._settings = {f: frappe.db.get_single_value(cm.SETTINGS, f) for f in SETTINGS_FIELDS}
		self._groups = {g for g in MIRROR_GROUPS if frappe.db.exists("Item Group", g)}
		self._cf = frappe.db.get_value("Custom Field", CF_NAME, CF_FIELDS, as_dict=True)
		in_patch = frappe.flags.in_patch
		frappe.flags.in_patch = True          # what the patch runner sets: keeps rename_doc off the app files
		self.addCleanup(setattr, frappe.flags, "in_patch", in_patch)   # registered first, so it runs last
		log_error = frappe.log_error

		def record(*args, **kwargs):          # every Error Log made meanwhile, by name: _put_back deletes those
			doc = log_error(*args, **kwargs)
			self._made.append(("Error Log", doc.name))
			return doc

		log = mock.patch.object(frappe, "log_error", side_effect=record)
		log.start()
		self.addCleanup(log.stop)                                      # after _put_back: its run logs too
		self.addCleanup(self._put_back)                                # runs while in_patch is still set

		frappe.db.set_single_value(cm.SETTINGS, {"category_mirror": cm.MODE_APPLY,
			"category_tree_cursor": "", "category_products_cursor": "", "category_products_baselined": 0,
			"category_mirror_report": "", "category_mirror_issues": 0})
		self.stub = catalog_stub.install()
		self.stub.seed_tree(catalog_stub.TREE_A)
		self.woo = FakeWoo(STORE_TERMS)
		p = mock.patch.object(categories, "configured_stores", side_effect=lambda: [("retail", self.woo)])
		p.start()
		self.addCleanup(p.stop)

	def _make(self, doc: dict) -> None:
		"""A raw row with the given name (no controller: these are seeded data, not features)."""
		d = frappe.get_doc(doc)
		d.name = doc["name"]
		d.db_insert()
		self._made.append((doc["doctype"], doc["name"]))

	def _made_logs(self, title: str) -> list[str]:
		return frappe.get_all("Error Log", filters={"method": title,
			"name": ("in", [n for d, n in self._made if d == "Error Log"] or [""])}, pluck="error")

	def _user_setting(self, user: str, doctype: str, data: str) -> None:
		frappe.db.sql("insert into `__UserSettings` (user, doctype, data) values (%s, %s, %s)", (user, doctype, data))
		self._user_settings.append((user, doctype))

	def _put_back(self):
		frappe.flags.in_patch = True
		patch.execute()
		if frappe.db.has_column("Item", OLD_FIELD):           # a both-columns test stopped half way
			frappe.db.sql_ddl(f"alter table `tabItem` drop column `{OLD_FIELD}`")
		for code in ITEMS:
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, ignore_permissions=True, force=True)
		frappe.db.delete(NEW_DT)                              # the site had none (setUp's guard)
		for group in set(MIRROR_GROUPS) - self._groups:
			if frappe.db.exists("Item Group", group):
				frappe.delete_doc("Item Group", group, ignore_permissions=True, force=True)
		if self._cf:
			frappe.db.set_value("Custom Field", CF_NAME, dict(self._cf), update_modified=False)
		for doctype, name in self._made:
			frappe.db.delete(doctype, {"name": name})
		for user, doctype in self._user_settings:
			frappe.db.sql("delete from `__UserSettings` where user = %s and doctype = %s", (user, doctype))
		frappe.db.set_single_value(cm.SETTINGS, self._settings)
		frappe.db.commit()                    # load-bearing: the test's DDL already committed what it made
		frappe.clear_cache(doctype="Item")

	def _to_old_shape(self):
		"""What a site that has not run the patch looks like."""
		frappe.rename_doc("DocType", NEW_DT, OLD_DT, force=True)
		frappe.db.rename_column("Item", NEW_FIELD, OLD_FIELD)
		frappe.db.sql("update `tabCustom Field` set name = %s, fieldname = %s where name = %s",
			(f"Item-{OLD_FIELD}", OLD_FIELD, CF_NAME))
		frappe.clear_cache(doctype="Item")
		frappe.make_property_setter({"doctype": "Item", "fieldname": OLD_FIELD, "property": "bold",
			"value": "1", "property_type": "Check"}, validate_fields_for_doctype=False)
		self._made += [("Property Setter", f"Item-{OLD_FIELD}-bold"),
			("Property Setter", f"Item-{NEW_FIELD}-bold")]                # the name the patch gives it

	def _seed_references(self) -> list[tuple[str, str, str, str, str]]:
		"""Old-shape data that names an old name, one per place the patch lists (``PLACES`` and
		user settings). Returns (doctype, name, field, value, what the Error Log says)."""
		flt = json.dumps([["Item", OLD_FIELD, "=", "accessories"]])
		on_old_dt = json.dumps([[OLD_DT, "active", "=", 1]])
		search = f"item_name,{OLD_FIELD},item_group"
		report = json.dumps({"columns": [["name", "Item"], [OLD_FIELD, "Item"]], "filters": [],
			"order_by": f"`tabItem`.`{OLD_FIELD}` desc"})
		client = f"frappe.ui.form.on('Item', {{refresh(frm) {{ frm.toggle_display('{OLD_FIELD}', 1); }} }});"
		server = f"rows = frappe.get_all('{OLD_DT}', pluck='name')"
		views = json.dumps({"Report": {"fields": [[OLD_FIELD, "Item"]]}})
		fields = json.dumps([{"fieldname": OLD_FIELD, "label": "Category"}])
		route = json.dumps({"link": "/app/osa-category?active=1"})        # the old route, nothing else
		jinja, cond = f"{{{{ doc.{OLD_FIELD} }}}}", f"doc.{OLD_FIELD} == 'x'"
		user = f"{T}-views@example.com"

		self._make({"doctype": "Property Setter", "name": f"{T}-search_fields", "doctype_or_field": "DocType",
			"doc_type": "Item", "property": "search_fields", "property_type": "Data", "value": search})
		cf = {"depends_on": f"eval:doc.{OLD_FIELD}", "mandatory_depends_on": f"eval:!doc.{OLD_FIELD}",
			"read_only_depends_on": f"eval:doc.{OLD_FIELD}=='x'", "fetch_from": f"variant_of.{OLD_FIELD}"}
		frappe.db.set_value("Custom Field", f"Item-{OLD_FIELD}", cf, update_modified=False)
		self._make({"doctype": "List Filter", "name": f"{T} filter", "filter_name": f"{T} filter",
			"reference_doctype": "Item", "filters": flt})
		self._make({"doctype": "Number Card", "name": f"{T} card", "label": f"{T} card", "type": "Document Type",
			"document_type": "Item", "function": "Count", "filters_json": flt})
		self._make({"doctype": "Number Card", "name": f"{T} card old doctype", "label": f"{T} card old doctype",
			"type": "Document Type", "document_type": OLD_DT, "function": "Count", "filters_json": on_old_dt})
		self._make({"doctype": "Dashboard Chart", "name": f"{T} chart", "chart_name": f"{T} chart",
			"chart_type": "Group By", "document_type": "Item", "group_by_based_on": OLD_FIELD,
			"group_by_type": "Count", "filters_json": flt, "type": "Bar", "timeseries": 0})
		self._make({"doctype": "Report", "name": f"{T} report", "report_name": f"{T} report", "ref_doctype": "Item",
			"report_type": "Report Builder", "is_standard": "No", "module": "FFL Core", "json": report})
		self._make({"doctype": "Client Script", "name": f"{T} script", "dt": "Item", "view": "Form", "enabled": 0,
			"script": client})
		self._make({"doctype": "Server Script", "name": f"{T} server script", "script_type": "API",
			"api_method": "t_b1_unused", "disabled": 1, "script": server})
		kanban = {"field_name": OLD_FIELD, "filters": flt, "fields": json.dumps([OLD_FIELD]), "footer_date_field": OLD_FIELD}
		self._make({"doctype": "Kanban Board", "name": f"{T} kanban", "kanban_board_name": f"{T} kanban",
			"reference_doctype": "Item", **kanban})
		self._make({"doctype": "Workspace Shortcut", "name": f"{T}-shortcut", "parent": f"{T} workspace",
			"parenttype": "Workspace", "parentfield": "shortcuts", "type": "DocType", "link_to": "Item",
			"label": f"{T} shortcut", "stats_filter": flt})
		self._make({"doctype": "Workspace Quick List", "name": f"{T}-quick-list", "parent": f"{T} workspace",
			"parenttype": "Workspace", "parentfield": "quick_lists", "document_type": "Item", "label": f"{T} list",
			"quick_list_filter": flt})
		self._make({"doctype": "List View Settings", "name": f"{T} list", "fields": fields})
		self._make({"doctype": "Number Card", "name": f"{T} report card", "label": f"{T} report card", "type": "Report",
			"report_name": f"{T} report", "report_field": OLD_FIELD, "function": "Sum"})
		self._make({"doctype": "Dashboard Chart", "name": f"{T} report chart", "chart_name": f"{T} report chart",
			"chart_type": "Report", "report_name": f"{T} report", "x_field": OLD_FIELD, "custom_options": route,
			"type": "Bar", "timeseries": 0})
		self._make({"doctype": "Report Column", "name": f"{T}-column", "parent": f"{T} report", "parenttype": "Report",
			"parentfield": "columns", "fieldname": OLD_FIELD, "label": "Category", "fieldtype": "Link", "options": OLD_DT})
		self._make({"doctype": "Report Filter", "name": f"{T}-filter", "parent": f"{T} report", "parenttype": "Report",
			"parentfield": "filters", "fieldname": OLD_FIELD, "label": "Category", "fieldtype": "Link",
			"options": OLD_DT})
		self._make({"doctype": "Auto Email Report", "name": f"{T} email", "report": f"{T} report", "user": "Administrator",
			"enabled": 0, "format": "HTML", "frequency": "Daily", "filters": json.dumps({OLD_FIELD: "accessories"})})
		self._make({"doctype": "Print Format", "name": f"{T} print", "doc_type": "Item", "module": "FFL Core",
			"custom_format": 1, "disabled": 1, "html": jinja, "format_data": fields})
		self._make({"doctype": "Notification", "name": f"{T} notification", "subject": f"{T}", "document_type": "Item",
			"event": "Save", "enabled": 0, "condition": cond, "message": jinja})
		self._make({"doctype": "Webhook", "name": f"{T} webhook", "webhook_doctype": "Item", "webhook_docevent": "on_update",
			"request_url": "https://example.invalid/", "enabled": 0, "condition": cond, "webhook_json": jinja})
		self._make({"doctype": "Webhook Data", "name": f"{T}-webhook-data", "parent": f"{T} webhook",
			"parenttype": "Webhook", "parentfield": "webhook_data", "fieldname": OLD_FIELD, "key": "category"})
		# A field of a custom DocType is stored data; a standard DocType's is its app's JSON, synced
		# after the patch. (Select, not Link: rename_doc moves a Link's options itself.)
		for custom in (1, 0):
			self._make({"doctype": "DocType", "name": f"{T} doctype {custom}", "module": "FFL Core", "custom": custom})
			self._make({"doctype": "DocField", "name": f"{T}-docfield-{custom}", "parent": f"{T} doctype {custom}",
				"parenttype": "DocType", "parentfield": "fields", "fieldname": "t_b1", "fieldtype": "Select",
				"depends_on": f"eval:doc.{OLD_FIELD}", "options": f"{OLD_DT}\nOther"})
		self._user_setting(user, "Item", views)
		return [
			("Property Setter", f"{T}-search_fields", "value", search, f"Property Setter {T}-search_fields: value"),
			*[("Custom Field", CF_NAME, f, v, f"Custom Field {CF_NAME}: {', '.join(CF_FIELDS)}") for f, v in cf.items()],
			("List Filter", f"{T} filter", "filters", flt, f"List Filter {T} filter: filters"),
			("Number Card", f"{T} card", "filters_json", flt, f"Number Card {T} card: filters_json"),
			("Number Card", f"{T} card old doctype", "filters_json", on_old_dt,
				f"Number Card {T} card old doctype: filters_json"),
			("Dashboard Chart", f"{T} chart", "group_by_based_on", OLD_FIELD,
				f"Dashboard Chart {T} chart: filters_json, group_by_based_on"),
			("Report", f"{T} report", "json", report, f"Report {T} report: json"),
			("Client Script", f"{T} script", "script", client, f"Client Script {T} script: script"),
			("Server Script", f"{T} server script", "script", server, f"Server Script {T} server script: script"),
			*[("Kanban Board", f"{T} kanban", f, v, f"Kanban Board {T} kanban: {', '.join(kanban)}") for f, v in kanban.items()],
			("Workspace Shortcut", f"{T}-shortcut", "stats_filter", flt,
				f"Workspace Shortcut {T}-shortcut (in {T} workspace): stats_filter"),
			("Workspace Quick List", f"{T}-quick-list", "quick_list_filter", flt,
				f"Workspace Quick List {T}-quick-list (in {T} workspace): quick_list_filter"),
			("List View Settings", f"{T} list", "fields", fields, f"List View Settings {T} list: fields"),
			("Number Card", f"{T} report card", "report_field", OLD_FIELD, f"Number Card {T} report card: report_field"),
			("Dashboard Chart", f"{T} report chart", "custom_options", route,
				f"Dashboard Chart {T} report chart: x_field, custom_options"),
			("Report Column", f"{T}-column", "options", OLD_DT, f"Report Column {T}-column (in {T} report): fieldname, options"),
			("Report Filter", f"{T}-filter", "options", OLD_DT,
				f"Report Filter {T}-filter (in {T} report): fieldname, options"),
			("Auto Email Report", f"{T} email", "filters", json.dumps({OLD_FIELD: "accessories"}),
				f"Auto Email Report {T} email: filters"),
			("Print Format", f"{T} print", "html", jinja, f"Print Format {T} print: html, format_data"),
			("Notification", f"{T} notification", "condition", cond, f"Notification {T} notification: condition, message"),
			("Webhook", f"{T} webhook", "webhook_json", jinja, f"Webhook {T} webhook: condition, webhook_json"),
			("Webhook Data", f"{T}-webhook-data", "fieldname", OLD_FIELD,
				f"Webhook Data {T}-webhook-data (in {T} webhook): fieldname"),
			("DocField", f"{T}-docfield-1", "options", f"{OLD_DT}\nOther",
				f"DocField {T}-docfield-1 (in {T} doctype 1): depends_on, options"),
			("__UserSettings", user, "data", views, f"__UserSettings {user} / Item: data"),
		]

	def _state(self):
		return (frappe.get_all(NEW_DT, fields=ROW_FIELDS, order_by="slug", as_list=True),
			{code: frappe.db.get_value("Item", code, NEW_FIELD) for code in ITEMS})

	def test_rows_links_and_store_terms_survive_references_are_listed_and_the_jobs_run(self):
		for code, (slug, product_id) in ITEMS.items():
			self._list(code, slug, product_id)
		cm.run_mirror()
		before = self._state()
		self.assertTrue(before[0] and all(before[1].values()))
		self.assertEqual(self._term("firearms-pistols"), 78)

		self._to_old_shape()
		seeded = self._seed_references()
		self.assertTrue(frappe.db.table_exists(OLD_DT) and not frappe.db.table_exists(NEW_DT))
		self.assertEqual(frappe.db.get_value("Item", "_T-B1-RENAME-1", OLD_FIELD), "firearms-pistols")

		patch.execute()
		patch.execute()                       # idempotent
		self.assertFalse(frappe.db.exists("DocType", OLD_DT))
		self.assertFalse(frappe.db.table_exists(OLD_DT))
		self.assertFalse(frappe.db.has_column("Item", OLD_FIELD))
		self.assertEqual(self._state(), before)
		self.assertEqual(frappe.db.get_value("DocField", {"parent": NEW_DT, "fieldname": "parent_slug"}, "options"), NEW_DT)
		self.assertFalse(frappe.db.exists("Custom Field", f"Item-{OLD_FIELD}"))
		self.assertEqual(frappe.db.get_value("Custom Field", CF_NAME, ["fieldname", "options", "insert_after"]),
			(NEW_FIELD, NEW_DT, "actual_qty"))
		self.assertEqual(frappe.get_all("Property Setter", filters={"doc_type": "Item", "property": "bold",
			"field_name": ("in", [OLD_FIELD, NEW_FIELD])}, pluck="name"), [f"Item-{NEW_FIELD}-bold"])
		self.assertEqual(frappe.get_meta("Item").get_field(NEW_FIELD).options, NEW_DT)

		# Every other stored place is left as it was and named in one Error Log (one: the second
		# run renamed nothing, so it listed nothing).
		logs = self._made_logs(LOG_TITLE)
		self.assertEqual(len(logs), 1, logs)
		for doctype, name, field, value, line in seeded:
			if doctype == "__UserSettings":
				stored = frappe.db.sql("select data from `__UserSettings` where user = %s and doctype = 'Item'", name)[0][0]
			else:
				stored = frappe.db.get_value(doctype, name, field)
			self.assertEqual(stored, value, f"{doctype} {name}.{field} was edited")
			self.assertIn(line, logs[0], f"{doctype} {name} is not listed")
		self.assertNotIn(f"{T}-docfield-0", logs[0], "a standard DocType's field is its app's, not stored data")
		self.assertEqual(frappe.db.get_value("Number Card", f"{T} card old doctype", "document_type"), NEW_DT,
			"a Link to the DocType is rename_doc's to move")
		# What a person does with the list (the fixture sync would also reset the Custom Field).
		frappe.db.set_value("Custom Field", CF_NAME, dict(self._cf), update_modified=False)
		frappe.db.delete("Property Setter", {"name": f"{T}-search_fields"})
		frappe.clear_cache(doctype="Item")

		# The mirror finds every node linked (nothing posted or renamed on the store) ...
		self.woo.puts.clear()
		self.woo.posts.clear()
		self.woo.__dict__.pop("_catalog_terms", None)
		cm.run_mirror()
		self.assertEqual((self.woo.puts, self.woo.posts), ([], []))
		self.assertEqual(self._state(), before)
		# ... and recategorize points the Item's product at its node's term.
		self.woo.products["9101"] = {999}
		self.assertEqual(categories.recategorize("_T-B1-RENAME-1"), ["retail"])
		self.assertEqual(self.woo.product_updates[-1], ("9101", {"categories": [{"id": 78}]}))

	def test_a_site_with_category_rows_is_skipped_and_untouched_and_under_ci_fails(self):
		self._make({"doctype": NEW_DT, "name": f"{T.lower()}-real", "slug": f"{T.lower()}-real", "node_name": "Real",
			"woo_term_retail": 4242, "level": 1, "active": 1})
		result = unittest.TestResult()
		with mock.patch.dict(os.environ):
			os.environ.pop("CI", None)
			_half_way().run(result)
		self.assertEqual((len(result.skipped), result.failures, result.errors), (1, [], []), result.skipped)
		self.assertIn("runs only on a site without category rows", result.skipped[0][1])
		result = unittest.TestResult()
		with mock.patch.dict(os.environ, {"CI": "true"}):
			_half_way().run(result)
		self.assertEqual((result.skipped, len(result.failures), result.errors), ([], 1, []), result.failures)
		self.assertIn("Under CI that is a failure, not a skip", result.failures[0][1])
		self.assertTrue(frappe.db.exists("DocType", NEW_DT) and not frappe.db.exists("DocType", OLD_DT))
		self.assertEqual(frappe.db.get_value(NEW_DT, f"{T.lower()}-real", ["node_name", "woo_term_retail"]),
			("Real", 4242))

	def test_a_test_that_stops_half_way_still_leaves_the_new_shape(self):
		result = unittest.TestResult()
		_half_way().run(result)
		self.assertEqual(len(result.failures), 1, result.failures)
		self.assertTrue(frappe.db.exists("DocType", NEW_DT) and not frappe.db.exists("DocType", OLD_DT))
		self.assertTrue(frappe.db.table_exists(NEW_DT) and not frappe.db.table_exists(OLD_DT))
		self.assertTrue(frappe.db.has_column("Item", NEW_FIELD) and not frappe.db.has_column("Item", OLD_FIELD))
		self.assertTrue(frappe.db.exists("Custom Field", CF_NAME))
		self.assertTrue(frappe.flags.in_patch, "the outer test's flag is untouched")

	def _both_columns(self, old_values: dict):
		"""Both columns on Item, as a site where someone added the new field by hand would have."""
		for code, (slug, product_id) in ITEMS.items():
			self._list(code, slug, product_id)
		frappe.db.sql_ddl(f"alter table `tabItem` add column `{OLD_FIELD}` varchar(140)")
		for code, value in old_values.items():
			frappe.db.sql(f"update `tabItem` set `{OLD_FIELD}` = %s where name = %s", (value, code))
		frappe.make_property_setter({"doctype": "Item", "fieldname": NEW_FIELD, "property": "bold",
			"value": "1", "property_type": "Check"}, validate_fields_for_doctype=False)
		self._made.append(("Property Setter", f"Item-{NEW_FIELD}-bold"))
		self._make({"doctype": "Property Setter", "name": f"Item-{OLD_FIELD}-bold", "doctype_or_field": "DocField",
			"doc_type": "Item", "field_name": OLD_FIELD, "property": "bold", "property_type": "Check", "value": "1"})

	def test_both_columns_with_the_same_values_drop_the_old_one_and_twins(self):
		self._both_columns({"_T-B1-RENAME-1": "firearms-pistols", "_T-B1-RENAME-2": None})
		patch.rename_item_field()
		self.assertFalse(frappe.db.has_column("Item", OLD_FIELD))
		self.assertEqual(frappe.get_all("Property Setter", filters={"doc_type": "Item", "property": "bold",
			"field_name": ("in", [OLD_FIELD, NEW_FIELD])}, pluck="name"), [f"Item-{NEW_FIELD}-bold"])

	def test_both_columns_that_differ_keep_the_old_one_and_say_how_many(self):
		self._both_columns({"_T-B1-RENAME-1": "accessories", "_T-B1-RENAME-2": "accessories"})
		frappe.db.set_value("Item", "_T-B1-RENAME-2", NEW_FIELD, None)        # blank: filled from the old
		patch.rename_item_field()
		self.assertTrue(frappe.db.has_column("Item", OLD_FIELD))
		self.assertEqual(frappe.db.get_value("Item", "_T-B1-RENAME-2", NEW_FIELD), "accessories")
		self.assertEqual(frappe.db.get_value("Item", "_T-B1-RENAME-1", NEW_FIELD), "firearms-pistols")
		logs = self._made_logs(f"rename_catalog_category: Item.{OLD_FIELD} kept")
		self.assertTrue(len(logs) == 1 and logs[0].startswith("1 Item row(s)"), logs)

	def test_a_site_with_both_doctypes_stops_for_a_person(self):
		frappe.db.sql("insert into `tabDocType` (name, module, custom) values (%s, 'FFL Core', 1)", (OLD_DT,))
		try:
			with self.assertRaises(frappe.ValidationError):
				patch.rename_doctype()
		finally:
			frappe.db.delete("DocType", {"name": OLD_DT})

	def test_a_fresh_site_is_left_alone(self):
		before = frappe.db.get_value("Custom Field", CF_NAME, ["name", "fieldname", "options"])
		patch.execute()
		self.assertEqual(frappe.db.get_value("Custom Field", CF_NAME, ["name", "fieldname", "options"]), before)
		self.assertTrue(frappe.db.has_column("Item", NEW_FIELD))

	def test_saved_list_views_of_the_category_list_follow_it(self):
		moved, kept = f"{T}-moved@example.com", f"{T}-kept@example.com"
		users = ((moved, kept),)
		cached = [f"{dt}::{u}" for dt in (OLD_DT, NEW_DT) for u in (moved, kept)]
		self.addCleanup(frappe.db.sql, "delete from `__UserSettings` where user in %s", users)
		self.addCleanup(frappe.cache.hdel, "_user_settings", cached)
		self._to_old_shape()
		for user, doctype, data in ((moved, OLD_DT, '{"List": 1}'), (kept, OLD_DT, '{"old": 1}'),
				(kept, NEW_DT, '{"new": 1}')):
			frappe.db.sql("insert into `__UserSettings` (user, doctype, data) values (%s, %s, %s)",
				(user, doctype, data))
		rename_doc = frappe.rename_doc

		def rename_then_a_save(*args, **kwargs):
			# rename_doc syncs the cache and then clears it; a page still open on the old list saves
			# its view after that, into the cache only (what user_settings.save does).
			rename_doc(*args, **kwargs)
			frappe.cache.hset("_user_settings", f"{OLD_DT}::{moved}", '{"List": 2}')

		with mock.patch.object(frappe, "rename_doc", side_effect=rename_then_a_save):
			patch.execute()                   # its report_mentions syncs the cache, as the hourly job does
		got = {(u, d): data for u, d, data in frappe.db.sql(
			"select user, doctype, data from `__UserSettings` where user in %s", users)}
		# A user who already has a view on the new name keeps it; the old one is left, not merged.
		# The cached view moved too, and its old-name key is gone: no sync writes it back.
		self.assertEqual(got, {(moved, NEW_DT): '{"List": 2}', (kept, NEW_DT): '{"new": 1}',
			(kept, OLD_DT): '{"old": 1}'})
		self.assertIsNone(frappe.cache.hget("_user_settings", f"{OLD_DT}::{moved}"))


def _half_way() -> unittest.TestCase:
	"""A test that stops in the old shape, so only its cleanup can put the new one back. Made in a
	function so the test loader never collects it (nor runs the inherited tests again)."""

	class HalfWay(RenameCatalogCategory):
		def runTest(self):
			self._to_old_shape()
			self.fail("stopped half way, in the old shape")

	return HalfWay("runTest")


class LegacyJobIds(FrappeTestCase):
	"""For one release a job queued under its pre-rename id (osa_category_*) is the same job."""

	def _queued(self, legacy_id):
		return mock.patch("frappe.utils.background_jobs.is_job_enqueued", side_effect=lambda jid: jid == legacy_id)

	def test_the_tick_is_not_queued_beside_a_legacy_one(self):
		with mock.patch.object(cm, "capability", return_value="x"), \
				mock.patch.object(cm, "_halves", return_value=(True, False)), \
				mock.patch.object(cm.frappe, "enqueue") as enqueue:
			with self._queued("osa_category_tick"):
				cm.scheduled()
			enqueue.assert_not_called()
			with self._queued("nothing"):
				cm.scheduled()
			self.assertEqual(enqueue.call_args.kwargs["job_id"], "catalog_category_tick")

	def test_the_settings_button_does_not_queue_a_second_mirror(self):
		with mock.patch.object(cm, "require"), mock.patch.object(cm.frappe, "enqueue") as enqueue:
			with self._queued("osa_category_mirror"):
				self.assertEqual(cm.run_category_mirror(), {"queued": True})
			enqueue.assert_not_called()
			with self._queued("nothing"):
				cm.run_category_mirror()
			self.assertEqual(enqueue.call_args.kwargs["job_id"], "catalog_category_mirror")

	def test_recategorize_is_not_queued_beside_a_legacy_one(self):
		with mock.patch.object(categories.frappe, "enqueue") as enqueue:
			with self._queued("osa_category_recategorize:retail:all"):
				categories._enqueue_recategorize("retail")
			enqueue.assert_not_called()
			with self._queued("nothing"):
				categories._enqueue_recategorize("retail")
			self.assertEqual(enqueue.call_args.kwargs["job_id"], "catalog_category_recategorize:retail:all")

	def test_no_queue_to_ask_means_enqueue_as_before(self):
		from ffl_core.catalog_tree import legacy_job_queued

		with mock.patch("frappe.utils.background_jobs.is_job_enqueued", side_effect=ConnectionError):
			self.assertFalse(legacy_job_queued("catalog_category_tick"))
		self.assertFalse(legacy_job_queued("something_else"))
