"""Rename the seeded Sports South Distributor from code ``SS`` to
``Sports South`` (the code is the record name and the hub pill). Links follow
through rename_doc; the distributor-scoped brand-policy rows are renamed to
match their autoname (``<brand> @ Sports South``). Idempotent."""

import frappe


def execute():
	if frappe.db.exists("Distributor", "SS") and not frappe.db.exists("Distributor", "Sports South"):
		frappe.rename_doc("Distributor", "SS", "Sports South", force=True)
		frappe.db.set_value("Distributor", "Sports South", "code", "Sports South", update_modified=False)
	for row in frappe.get_all("Dropship Brand Policy",
			filters={"distributor": "Sports South", "name": ["like", "% @ SS"]},
			fields=["name", "brand"]):
		target = f"{row.brand} @ Sports South"
		if not frappe.db.exists("Dropship Brand Policy", target):
			frappe.rename_doc("Dropship Brand Policy", row.name, target, force=True)
