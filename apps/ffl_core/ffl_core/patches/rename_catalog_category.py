"""Rename the category mirror to product names: DocType ``OSA Category`` → ``Catalog Category``
and the Item field ``osa_category`` → ``catalog_category``. Rows, links and store term ids are
kept as they are; only names change.

A **pre_model_sync** patch, on purpose. ``bench migrate`` syncs the DocType JSON between the two
patch passes (frappe/migrate.py ``run_schema_updates``): run after it, the sync would already have
created an empty ``Catalog Category`` table beside the old one, and ``rename_doc`` would refuse.
The fixtures (``Item-catalog_category``) sync later still, in ``post_schema_updates``. So before
both:

1. ``frappe.rename_doc("DocType", …)``: renames the table and updates everything that names the
   DocType — Link / Table options in DocField, Custom Field and Property Setter, Custom Field
   ``dt`` and Property Setter ``doc_type``, Custom DocPerm, every Link-to-DocType value (ToDo,
   Comment, Version, Error Log, …) and dynamic links. ``frappe.flags.in_patch`` (set by the
   patch runner) keeps it off the app's files. The category list's own saved list / report
   views (``__UserSettings`` rows of the old DocType) move to the new name, except for a user
   who already has one there. Cached views are written to the table first and their old-name keys
   dropped after, or the next sync (hourly, or step 4's) would write them back as rows of the
   old DocType.
2. The Item column is renamed in place (``ALTER TABLE … CHANGE COLUMN``), not copied, so the
   values never pass through Python. Its Custom Field record takes the new name and fieldname
   (position and ``insert_after`` unchanged). Should the site already have both columns, blanks
   in the new one are filled from the old, and the old column is dropped only when every value
   in it equals the new column's; otherwise it stays and an Error Log gives the count.
3. Property Setters of the field take the new name (``Item-catalog_category-<property>``); one
   whose new-named twin already exists is deleted, not re-pointed (no duplicates). frappe's
   ``update_property_setters`` then moves Item Custom Fields whose ``insert_after`` is the old
   field to the new one.
4. Nothing else is edited. The common stored places that may still name an old name or the
   old route ``osa-category`` (saved filters, reports, scripts, dashboards, print formats,
   notifications, user settings — see ``PLACES``) are **listed for a person** in one Error Log,
   with a printed count. On the live sites there were none (prod-copy rehearsal, both stores),
   and a fresh install has no old data.

Idempotent: a site that has no ``OSA Category`` (fresh install, or already renamed) and no
``osa_category`` column is left alone. A site with BOTH DocTypes stops with an error for a person
to look at, rather than guessing which table holds the data.
"""
from __future__ import annotations

import re

import frappe
from frappe.model.utils.rename_field import update_property_setters
from frappe.model.utils.user_settings import clear_user_settings_cache, sync_user_settings

OLD_DOCTYPE, NEW_DOCTYPE = "OSA Category", "Catalog Category"
OLD_FIELD, NEW_FIELD = "osa_category", "catalog_category"
OLD_ROUTE = "osa-category"                    # the old DocType's desk route (/app/osa-category)
FIELD_DOCTYPE = "Item"
MENTION = re.compile(rf"\b{OLD_FIELD}\b|{OLD_DOCTYPE}|\b{OLD_ROUTE}\b", re.IGNORECASE)   # also "tabOSA Category"

# The common stored places that may still name an old name after steps 1-3: (doctype, fields,
# parent field to show for a child row). Listed, never edited. A doctype or field the site does
# not have is skipped. __UserSettings (not a doctype) is read on its own.
DEPENDS_ON = ("depends_on", "mandatory_depends_on", "read_only_depends_on", "fetch_from")
PLACES = (
	("Property Setter", ("value",), None),
	("Custom Field", DEPENDS_ON, None),
	("DocField", (*DEPENDS_ON, "options"), "parent"),         # custom DocTypes only (ONLY_WHERE)
	("List Filter", ("filters", "columns"), None),
	("List View Settings", ("fields",), None),
	("Number Card", ("filters_json", "dynamic_filters_json", "aggregate_function_based_on", "report_field"), None),
	("Dashboard Chart", ("filters_json", "dynamic_filters_json", "based_on", "value_based_on",
		"group_by_based_on", "aggregate_function_based_on", "x_field", "custom_options"), None),
	("Report", ("json", "query", "report_script", "javascript"), None),
	("Report Column", ("fieldname", "options"), "parent"),
	("Report Filter", ("fieldname", "options"), "parent"),
	("Auto Email Report", ("filters",), None),
	("Print Format", ("html", "format_data"), None),
	("Notification", ("condition", "message"), None),
	("Webhook", ("condition", "webhook_json"), None),
	("Webhook Data", ("fieldname",), "parent"),
	("Client Script", ("script",), None),
	("Server Script", ("script",), None),
	("Kanban Board", ("field_name", "filters", "fields", "footer_date_field"), None),
	("Workspace Shortcut", ("stats_filter",), "parent"),
	("Workspace Quick List", ("quick_list_filter",), "parent"),
)
# A standard DocType's fields come from its app's JSON, synced again right after this
# pre_model_sync patch: only a custom DocType's are stored data.
ONLY_WHERE = {"DocField": "parent in (select name from `tabDocType` where custom = 1)"}


def execute():
	renamed = rename_doctype()
	renamed = rename_item_field() or renamed
	if renamed:
		report_mentions()
	frappe.clear_cache()


def rename_doctype() -> bool:
	if not frappe.db.exists("DocType", OLD_DOCTYPE):
		return False
	if frappe.db.exists("DocType", NEW_DOCTYPE):
		frappe.throw(f"Both {OLD_DOCTYPE!r} and {NEW_DOCTYPE!r} exist on this site; nothing was renamed. "
			f"Compare the two tables, keep the one with the category rows, then run the migrate again.")
	frappe.rename_doc("DocType", OLD_DOCTYPE, NEW_DOCTYPE, force=True)
	# rename_doc moves other doctypes' list settings, not the renamed DocType's own: a user's saved
	# list / report view of the category list follows it, unless the user already has one there.
	# What is only cached goes to the table first (rename_doc synced, then cleared the cache, but a
	# page still open may have saved since); its old-name keys go after, or the next sync
	# (report_mentions', or the hourly one) would write the moved rows back under the old DocType.
	sync_user_settings()
	frappe.db.sql("""update `__UserSettings` s set s.doctype = %(new)s where s.doctype = %(old)s
		and s.user not in (select user from (select user from `__UserSettings` where doctype = %(new)s) t)""",
		{"old": OLD_DOCTYPE, "new": NEW_DOCTYPE})
	clear_user_settings_cache(OLD_DOCTYPE)
	return True


def rename_item_field() -> bool:
	old_cf, new_cf = f"{FIELD_DOCTYPE}-{OLD_FIELD}", f"{FIELD_DOCTYPE}-{NEW_FIELD}"
	had_old = frappe.db.has_column(FIELD_DOCTYPE, OLD_FIELD)
	if had_old:
		if frappe.db.has_column(FIELD_DOCTYPE, NEW_FIELD):
			_merge_columns()
		else:
			frappe.db.rename_column(FIELD_DOCTYPE, OLD_FIELD, NEW_FIELD)

	if frappe.db.exists("Custom Field", old_cf):
		if frappe.db.exists("Custom Field", new_cf):
			frappe.db.delete("Custom Field", {"name": old_cf})
		else:
			frappe.db.sql("update `tabCustom Field` set name = %s, fieldname = %s where name = %s",
				(new_cf, NEW_FIELD, old_cf))
	elif not had_old:
		return False                          # nothing of the old field on this site

	_rename_field_property_setters()
	update_property_setters(FIELD_DOCTYPE, OLD_FIELD, NEW_FIELD)   # what is left: Custom Field insert_after
	frappe.clear_cache(doctype=FIELD_DOCTYPE)
	return True


def _merge_columns() -> None:
	"""Both columns (only if someone added the new field by hand): fill the new one where it is
	blank; drop the old one only when it holds nothing the new one does not."""
	table = f"tab{FIELD_DOCTYPE}"
	frappe.db.sql(f"""update `{table}` set `{NEW_FIELD}` = `{OLD_FIELD}`
		where ifnull(`{NEW_FIELD}`, '') = '' and ifnull(`{OLD_FIELD}`, '') != ''""")
	differ = frappe.db.sql(f"""select count(*) from `{table}`
		where ifnull(`{OLD_FIELD}`, '') != '' and `{OLD_FIELD}` != ifnull(`{NEW_FIELD}`, '')""")[0][0]
	if differ:
		frappe.log_error(title=f"rename_catalog_category: {FIELD_DOCTYPE}.{OLD_FIELD} kept",
			message=f"{differ} {FIELD_DOCTYPE} row(s) have a value in {OLD_FIELD} that differs from "
				f"{NEW_FIELD}. Both columns stay; compare them, then drop {OLD_FIELD} by hand.")
		print(f"rename_catalog_category: kept {OLD_FIELD}, {differ} row(s) differ from {NEW_FIELD}")
	else:
		frappe.db.sql_ddl(f"alter table `{table}` drop column `{OLD_FIELD}`")


def _rename_field_property_setters() -> None:
	for ps in frappe.get_all("Property Setter", filters={"doc_type": FIELD_DOCTYPE, "field_name": OLD_FIELD},
			fields=["name", "property"]):
		twin = frappe.db.get_value("Property Setter",
			{"doc_type": FIELD_DOCTYPE, "field_name": NEW_FIELD, "property": ps.property})
		if twin:
			frappe.db.delete("Property Setter", {"name": ps.name})
			continue
		new_name = f"{FIELD_DOCTYPE}-{NEW_FIELD}-{ps.property}"
		if ps.name != f"{FIELD_DOCTYPE}-{OLD_FIELD}-{ps.property}" or frappe.db.exists("Property Setter", new_name):
			new_name = ps.name                # a name frappe did not give it: keep it
		frappe.db.sql("update `tabProperty Setter` set name = %s, field_name = %s where name = %s",
			(new_name, NEW_FIELD, ps.name))


def report_mentions() -> list[str]:
	"""The common stored places (``PLACES``, user settings) that still name ``osa_category``
	(whole word), ``OSA Category`` or its route ``osa-category``, in one Error Log for a person: a
	migration does not guess at saved views, reports or code."""
	found = []
	like_args = {"field": f"%{OLD_FIELD}%", "doctype": f"%{OLD_DOCTYPE}%", "route": f"%{OLD_ROUTE}%"}
	for doctype, fields, parent in PLACES:
		if not frappe.db.table_exists(doctype):
			continue
		fields = [f for f in fields if frappe.db.has_column(doctype, f)]
		if not fields:
			continue
		shown = ["name", parent] if parent else ["name"]
		like = " or ".join(f"`{f}` like %({k})s" for f in fields for k in like_args)
		only = f" and {ONLY_WHERE[doctype]}" if doctype in ONLY_WHERE else ""
		rows = frappe.db.sql(f"select {', '.join(f'`{c}`' for c in shown + fields)} from `tab{doctype}` "
			f"where ({like}){only}", like_args, as_dict=True)
		for row in rows:
			hits = [f for f in fields if row[f] and MENTION.search(row[f])]
			if hits:
				where = f" (in {row[parent]})" if parent else ""
				found.append(f"{doctype} {row.name}{where}: {', '.join(hits)}")

	if "__UserSettings" in frappe.db.get_tables(cached=False):
		sync_user_settings()                  # settings still only in the cache count too
		for user, doctype, data in frappe.db.sql("""select user, doctype, data from `__UserSettings`
				where doctype = %(old)s or data like %(field)s or data like %(doctype)s or data like %(route)s""",
				{"old": OLD_DOCTYPE, **like_args}):
			hits = [k for k, v in (("doctype", doctype), ("data", data)) if v and MENTION.search(v)]
			if hits:
				found.append(f"__UserSettings {user} / {doctype}: {', '.join(hits)}")

	print(f"rename_catalog_category: {len(found)} stored place(s) still name {OLD_FIELD} or {OLD_DOCTYPE}"
		+ (" (listed in the Error Log)" if found else ""))
	if found:
		frappe.log_error(title=f"rename_catalog_category: still names {OLD_FIELD} or {OLD_DOCTYPE}",
			message=f"{OLD_DOCTYPE} is now {NEW_DOCTYPE} and {FIELD_DOCTYPE}.{OLD_FIELD} is now "
				f"{FIELD_DOCTYPE}.{NEW_FIELD}. Nothing below was edited; check each by hand:\n" + "\n".join(found))
	return found
