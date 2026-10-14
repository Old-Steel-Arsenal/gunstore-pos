"""The Gun Shop sidebars show the product mark instead of Frappe's generic "retail" icon.

The header of a sidebar draws its ``header_icon`` from the desk's icon sprites; ``gunstore`` is the
Gun Shop mark (``public/icons/gunstore.svg``, registered in hooks ``app_include_icons``). The
shipped Gun Shop sidebar and the extension mirrors (``mirror_gun_shop_sidebar``) were written with
"retail" and are changed here; any other sidebar, and one a store gave its own icon, is left alone.
``update_modified=False`` so the shipped JSON is not re-imported over the store's copy for it.
Cosmetic only. Idempotent.
"""
import frappe

OLD, NEW = "retail", "gunstore"


def execute():
	from ffl_core.setup.gunshop_sidebar import mirror_shells
	from ffl_core.setup.sidebar import SIDEBAR_NAME, gun_shop_items

	base = gun_shop_items()
	modules = set(mirror_shells(base, "FFL Core")) if base else set()
	names = [SIDEBAR_NAME] + frappe.get_all("Sidebar", filters={"module": ["in", list(modules) or [""]]}, pluck="name")
	for name in names:
		if frappe.db.get_value("Sidebar", name, "header_icon") == OLD:
			frappe.db.set_value("Sidebar", name, "header_icon", NEW, update_modified=False)
	frappe.clear_cache(doctype="Sidebar")
