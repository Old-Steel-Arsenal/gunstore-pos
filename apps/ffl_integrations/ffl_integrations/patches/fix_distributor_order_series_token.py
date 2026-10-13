"""Rewrite the broken ``.YYMM.`` token in Distributor Order's naming series to
``.YY..MM.`` (``.YYMM.`` is not a Frappe token, so orders came out DO-YYMM-####).
Fixing the series in the doctype JSON (2026-09-18) left it alive in two places:

* Property Setters on naming_series. A setter outranks the JSON; one production site had an
  options + default pair holding the broken series from 2026-09-16. Document
  Naming Settings may also write several series newline-joined, or a leading
  blank with an empty default ("user must always select"). Only the token is
  rewritten, so the rest of such a customization survives.
* The series stored on each existing order. Duplicate copies it to the new
  order, and set_name_by_naming_series uses a non-empty one as it is.

Idempotent."""
import frappe

BROKEN, FIXED = ".YYMM.", ".YY..MM."


def execute():
	for name in frappe.get_all("Property Setter", pluck="name", filters={
			"doc_type": "Distributor Order", "field_name": "naming_series",
			"value": ("like", f"%{BROKEN}%")}):
		ps = frappe.get_doc("Property Setter", name)
		old, ps.value = ps.value, ps.value.replace(BROKEN, FIXED)
		ps.save(ignore_permissions=True)
		print(f"{name}: {old!r} -> {ps.value!r}")

	stale = frappe.db.count("Distributor Order", {"naming_series": ("like", f"%{BROKEN}%")})
	if stale:
		frappe.db.sql("""UPDATE `tabDistributor Order`
			SET naming_series = REPLACE(naming_series, %(broken)s, %(fixed)s)
			WHERE naming_series LIKE %(like)s""",
			{"broken": BROKEN, "fixed": FIXED, "like": f"%{BROKEN}%"})
		print(f"Distributor Order: {stale} order(s) moved off {BROKEN}")
	frappe.clear_cache(doctype="Distributor Order")
