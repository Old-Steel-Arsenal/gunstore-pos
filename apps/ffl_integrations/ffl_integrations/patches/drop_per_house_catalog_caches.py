"""The per-house catalog caches are gone from the code. Their Redis
keys were written with no expiry (the browse rows, ~60k slim rows a house and
catalog version) or survive a flush of nothing that reads them; delete them."""
import frappe

PREFIXES = ("dist_browse::", "dist_rules_rows:", "dist_catalog_version_last:",
	"dist_catalog_count_last:")


def execute():
	for prefix in PREFIXES:
		frappe.cache.delete_keys(prefix)
