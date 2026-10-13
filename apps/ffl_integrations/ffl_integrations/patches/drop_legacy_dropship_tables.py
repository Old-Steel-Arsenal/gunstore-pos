"""Drop what the website drop-ship layer left behind on a store that will never
install the osa_dropship extension.

**DELIBERATELY NOT REGISTERED IN patches.txt.** Run by hand, dry run first::

    bench --site <site> execute ffl_integrations.patches.drop_legacy_dropship_tables.run
    bench --site <site> execute ffl_integrations.patches.drop_legacy_dropship_tables.run --kwargs '{"apply": 1}'

Core 1.5 moved the layer (listings, listing rules, rule runs, pricing settings)
to the extension. Frappe's migrate deleted the old DocType records but, as it
does, kept their tables and the pricing Single's values: on a store that
installs the extension, its install adopts them in place (osa_dropship
setup.adopt_legacy). On a store that never will they are dead weight.

Never automatic: on the store that DOES use drop-ship, running this before the
extension is installed would delete every listing. So it refuses whenever the
extension is installed or a listing, listing rule or rule run exists — a store
with those should install the extension, not lose them. Pricing rows (markup
rules, brand MAP modes) are seeded on every store and are dropped with the rest.

Returns {"tables": {table: rows}, "singles": rows, "dropped": bool}.
"""
from __future__ import annotations

import frappe

LEGACY_TABLES = (
	"Distributor Listing", "Distributor Listing Excluded Source", "Distributor Listing Rule",
	"Distributor Listing Cost Range", "Distributor Rule Run", "Dropship Pricing Rule",
	"Dropship Brand MAP Mode")
LEGACY_SINGLE = "Dropship Pricing Settings"
# Rows only a store that listed products has; pricing rows were seeded everywhere.
HELD = ("Distributor Listing", "Distributor Listing Rule", "Distributor Rule Run")


def run(apply: int = 0) -> dict:
	if "osa_dropship" in frappe.get_installed_apps():
		frappe.throw("osa_dropship is installed here: its install adopted these tables. Nothing to drop.")
	tables = {t: frappe.db.count(t) for t in LEGACY_TABLES if frappe.db.table_exists(t)}
	for t in tables:
		if frappe.db.exists("DocType", t):
			frappe.throw(f"{t} is still a DocType on this site (core older than 1.5?). Migrate first.")
	singles = frappe.db.count("Singles", {"doctype": LEGACY_SINGLE})
	out = {"tables": tables, "singles": singles, "dropped": False}
	held = {t: n for t, n in tables.items() if n and t in HELD}
	if held:
		frappe.throw(f"Legacy drop-ship tables still hold rows: {held}. Install osa_dropship instead of dropping them.")
	if not apply:
		return out
	for t in tables:
		frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{t}`")
	frappe.db.delete("Singles", {"doctype": LEGACY_SINGLE})
	frappe.db.commit()
	out["dropped"] = True
	return out
