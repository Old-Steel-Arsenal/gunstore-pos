"""Desk navigation for the Gun Shop shell (Frappe >= 16.50: the `Sidebar` doctype).

Frappe 16.50 draws a module's sidebar from a `Sidebar` document (app content, shipped as
`<app>/<module>/sidebar/<name>/<name>.json` and imported on install and migrate) or, when a
module has none, computes one from what the module holds -- every doctype, report and page,
which is not navigation anyone at the counter wants. The old `Workspace Sidebar` store is an
archive nothing renders; `frappe.patches.v16_0.convert_sidebars` carried a migrated site's
rows over once.

What the Gun Shop shell needs:

* FFL Core (the Gun Shop and Settings workspaces) ships `ffl_core/ffl_core/sidebar/gun_shop`:
  Gun Shop and Settings, so the Settings page leads back to Gun Shop.
* FFL Integrations (the pages behind the Settings cards: distributor-hub, payment-hub)
  ships `ffl_integrations/ffl_integrations/sidebar/settings` with the same two links.
* An extension's module (osa_consignment) gets the Gun Shop links plus its own pages through
  `mirror_gun_shop_sidebar`, which every extension version calls from its install, migrate
  and switch hooks.
"""
from __future__ import annotations

import os

import frappe
from ffl_core.utils import commit

SIDEBAR_NAME = "Gun Shop"
SETTINGS_LINK = "Settings"
# Kept for callers that pass it (extensions); a Sidebar written here carries no app, like the
# ones frappe's conversion writes, so it never stands in for the POS app's own shell.
POS_APP = "ffl_core"

# The sidebars this repo ships: name -> (app, module).
_SHIPPED = {SIDEBAR_NAME: ("ffl_core", "FFL Core"), SETTINGS_LINK: ("ffl_integrations", "FFL Integrations")}


def ensure_settings_sidebar_link() -> None:
	"""after_install / after_migrate: the Gun Shop sidebar in force is the one ffl_core ships."""
	_ensure_shipped_sidebar(SIDEBAR_NAME)


def ensure_settings_sidebar_backlink() -> None:
	"""after_install / after_migrate: so is the Settings one (pages behind the Settings cards)."""
	_ensure_shipped_sidebar(SETTINGS_LINK)


def _ensure_shipped_sidebar(name: str) -> None:
	"""Re-import a shipped sidebar when the row in force is not it.

	`import_file` keeps a row whose `modified` is later than the file's, and the conversion of
	a migrated site dates its rows by when the archive last changed -- so a store whose archive
	was touched after the file was written would keep the converted copy. Forcing the import
	makes the outcome independent of those dates."""
	from frappe.modules.import_file import import_file_by_path

	app, module = _SHIPPED[name]
	if app not in frappe.get_installed_apps() or frappe.db.get_value("Sidebar", name, "standard"):
		return
	scrubbed = frappe.scrub(name)
	path = os.path.join(frappe.get_module_path(module), "sidebar", scrubbed, f"{scrubbed}.json")
	if os.path.exists(path):
		import_file_by_path(path, force=True)
		commit()


def item_dicts(sidebar) -> list[dict]:
	"""A `Sidebar` document's links, as `Sidebar Item` field dicts."""
	from frappe.desk.doctype.sidebar.sidebar import SIDEBAR_ITEM_FIELDS

	return [{f: row.get(f) for f in SIDEBAR_ITEM_FIELDS if row.get(f) is not None} for row in sidebar.items]


def gun_shop_items() -> list[dict]:
	"""The Gun Shop sidebar's links, as `Sidebar Item` field dicts."""
	if not frappe.db.exists("Sidebar", SIDEBAR_NAME):
		return []
	return item_dicts(frappe.get_doc("Sidebar", SIDEBAR_NAME))


def mirror_gun_shop_sidebar(name: str, *, module: str | None = None, app: str | None = POS_APP,
		header_icon: str = "retail", extra_items: list[dict] | None = None) -> bool:
	"""Make module ``module``'s sidebar (titled ``name``) the Gun Shop links + ``extra_items``.

	An extension's pages live in its own module, and without a `Sidebar` that module gets the
	computed one: its doctype lists and no way back to Gun Shop. This writes the module's base,
	in the shape `convert_sidebars` writes a migrated store's (not standard, no app), so a fresh
	install and an upgraded one end the same. ``app`` is accepted for older callers and unused.

	A module whose own app ships a standard sidebar is left alone. A `Sidebar` is app content
	and frappe refuses to write one outside install, migrate, a patch or developer mode; called
	from anywhere else (an extension's switch hook) with something to change, this writes
	nothing and the next migrate does it. Idempotent; True when it wrote.
	"""
	from frappe.desk.doctype.sidebar.sidebar import SYSTEM_WRITE_FLAGS

	module = module or name
	links = gun_shop_items()
	if not links or not frappe.db.exists("Module Def", module):
		return False
	desired = links + [dict(item) for item in (extra_items or [])]

	existing = frappe.db.get_value("Sidebar", {"module": module, "title": name}, ["name", "standard"], as_dict=True)
	if existing and existing.standard:
		return False
	doc = frappe.get_doc("Sidebar", existing.name) if existing else None

	def key(rows):
		return [((r.get("type") or ""), (r.get("label") or ""), (r.get("link_to") or "")) for r in rows]

	if doc and key([r.as_dict() for r in doc.items]) == key(desired):
		return False
	if not frappe.conf.developer_mode and not any(frappe.flags.get(f) for f in SYSTEM_WRITE_FLAGS):
		return False

	doc = doc or frappe.new_doc("Sidebar")
	doc.update({"module": module, "title": name, "standard": 0})
	doc.header_icon = doc.header_icon or header_icon
	doc.set("items", [])
	for item in desired:
		doc.append("items", item)
	doc.flags.ignore_links = True  # a link to a page the store has since removed must not stop a migrate
	doc.save(ignore_permissions=True)
	# The store's switches and extension sections are a site layer over this base
	# (gunshop_sidebar.py): give the module the same layer as the Gun Shop shell has.
	from ffl_core.setup.gunshop_sidebar import _guarded, sync_site_layer

	_guarded(f"site layer for {module}", sync_site_layer)
	commit()
	return True
