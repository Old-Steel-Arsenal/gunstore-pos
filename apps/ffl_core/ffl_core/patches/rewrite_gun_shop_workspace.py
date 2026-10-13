"""Bring an existing site's Gun Shop workspace to the "Needs attention" home page.

The shipped ``workspace/gun_shop/gun_shop.json`` is what a fresh install reads. A site that
already has the workspace never re-reads it (``import_file`` skips a workspace whose DB
``modified`` is at or past the file's, and bumping the stamp would delete-and-reinsert the
workspace -- see workspace_shortcuts.py), so this patch writes the same layout onto the row:
shortcuts, group cards, number cards and the block layout all come from the shipped file.

Shortcuts the core never shipped (an extension's tiles, e.g. consignment's) are kept and
appended after the new layout, each with the block it already had; so is every chart, quick list
and custom block (the core ships none) and every number card the shipped file does not name.
The card links are the one thing not carried over: nothing outside the core adds any.
Idempotent: a second run finds the layout in place and writes nothing.
"""
import json

import frappe

from ffl_core.workspace_shortcuts import _content, _save_workspace

WORKSPACE = "Gun Shop"
# Every shortcut label the core ever put on this page: replaced by the shipped layout. Anything
# else on the page was put there by an extension or the shop and is carried over.
CORE_LABELS = {
	"Open POS", "Create Order", "Pending Order", "Receive Goods", "Distributor Orders", "New Listing",
	"Add Customer", "Firearms In Stock", "Pending 4473 Orders", "4473 Records", "Open Special Orders",
	"Pending Transfer Pickups", "Stock On Hand", "Inventory Count", "Storage Locations", "Cash Drawer",
	"Sales Report", "Import from FastBound",
}


# Widgets the shop or an extension may have added by hand: block type -> child table. The block's
# data names the child row by its label (frappe.desk.desktop.clean_up matches them the same way).
_WIDGET_TABLES = {"chart": "charts", "quick_list": "quick_lists", "custom_block": "custom_blocks",
	"number_card": "number_cards"}


def _shipped() -> dict:
	path = frappe.get_app_path("ffl_core", "ffl_core", "workspace", "gun_shop", "gun_shop.json")
	with open(path) as f:
		return json.load(f)


def execute():
	if not frappe.db.exists("Workspace", WORKSPACE):
		return
	ws = frappe.get_doc("Workspace", WORKSPACE)
	shipped = _shipped()

	own = {s["label"] for s in shipped["shortcuts"]}
	kept = [s for s in ws.shortcuts if s.label not in CORE_LABELS | own]
	kept_labels = {s.label for s in kept}
	old = _content(ws)
	old_blocks = {b["data"]["shortcut_name"]: b for b in old
		if b.get("type") == "shortcut" and b.get("data", {}).get("shortcut_name") in kept_labels}
	tail = [old_blocks.get(s.label) or {"id": "sc_" + frappe.scrub(s.label), "type": "shortcut",
		"data": {"shortcut_name": s.label, "col": 3}} for s in kept]

	# other widgets: rows the shipped file does not name, and the blocks that show them
	kept_widgets = {}
	for kind, table in _WIDGET_TABLES.items():
		named = {r.get("label") for r in shipped.get(table, [])}
		kept_widgets[table] = [r for r in ws.get(table) if r.label not in named]
		shown = {r.label for r in kept_widgets[table]}
		tail += [b for b in old if b.get("type") == kind and b.get("data", {}).get(kind + "_name") in shown]

	blocks = json.loads(shipped["content"]) + tail
	content = json.dumps(blocks, separators=(",", ":"))

	new_shortcuts = [dict(s) for s in shipped["shortcuts"]]
	new_widgets = {t: [dict(r) for r in shipped.get(t, [])] for t in kept_widgets}
	if ([s.label for s in ws.shortcuts] == [s["label"] for s in new_shortcuts] + [s.label for s in kept]
			and [(b.get("id"), b.get("type")) for b in _content(ws)] == [(b["id"], b["type"]) for b in blocks]
			and [(l.type, l.label, l.link_to) for l in ws.links]
				== [(l["type"], l["label"], l.get("link_to")) for l in shipped["links"]]
			and all([r.label for r in ws.get(t)] == [r.get("label") for r in new_widgets[t]] + [r.label for r in kept_widgets[t]]
				for t in kept_widgets)):
		return

	ws.set("shortcuts", new_shortcuts + kept)
	ws.set("links", [dict(l) for l in shipped["links"]])
	for table, rows in new_widgets.items():
		ws.set(table, rows + kept_widgets[table])
	for table in ("shortcuts", *new_widgets):
		for i, row in enumerate(ws.get(table)):
			row.idx = i + 1          # a kept row brings its old position; the order is the one built here
	ws.content = content
	_save_workspace(ws)
