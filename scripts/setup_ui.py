"""Build a single, focused 'Gun Shop' workspace and hide every ERPNext
workspace a single-store FFL doesn't use.

First principles: an FFL gun shop's daily UI has four jobs — sell, receive,
look at stock, and handle compliance. Everything else is noise. This script
collapses ERPNext's default 13-workspace surface into one.

Run:
    bench --site dev.localhost console <<EOF
    import sys; sys.path.insert(0, "/workspace")
    from scripts.setup_ui import run; run()
    EOF

Idempotent — safe to re-run.
"""
from __future__ import annotations

import json
from typing import Any

import frappe


WORKSPACE_NAME = "Gun Shop"
WORKSPACE_TITLE = "Gun Shop"
WORKSPACE_LABEL = "Gun Shop"

# Workspaces we hide from the sidebar / switcher. They still exist, so any
# advanced user can navigate via awesomebar (⌘K), but they don't pollute the
# default UI surface.
WORKSPACES_TO_HIDE = (
	"Buying",
	"Manufacturing",
	"Projects",
	"Quality",
	"Subcontracting",
	"Assets",
	"Tools",
	"Framework",
	"ERPNext Integrations",
	"ERPNext Settings",
	"Organization",
)

# These we keep visible because the user might dig in occasionally.
# (kept here as docs only)
_KEEP_VISIBLE = ("Stock", "Selling", "Accounting", "Home")


def run() -> None:
	_upsert_workspace()
	_hide_unused_workspaces()
	_set_default_landing_page()
	frappe.db.commit()
	print(
		f"setup_ui complete; '{WORKSPACE_NAME}' workspace ready, "
		f"{len(WORKSPACES_TO_HIDE)} hidden, default landing set."
	)


# ---------------------------------------------------------------- workspace

def _upsert_workspace() -> None:
	shortcuts = _shortcuts()
	links = _links()
	content = _content_layout(shortcuts)

	if frappe.db.exists("Workspace", WORKSPACE_NAME):
		ws = frappe.get_doc("Workspace", WORKSPACE_NAME)
		ws.shortcuts = []
		ws.links = []
	else:
		ws = frappe.new_doc("Workspace")
		ws.name = WORKSPACE_NAME

	ws.title = WORKSPACE_TITLE
	ws.label = WORKSPACE_LABEL
	ws.module = "FFL Core"
	ws.public = 1
	ws.is_hidden = 0
	ws.icon = "retail"
	ws.indicator_color = "green"
	ws.content = json.dumps(content)

	for s in shortcuts:
		ws.append("shortcuts", s)
	for l in links:
		ws.append("links", l)

	ws.save(ignore_permissions=True)


def _shortcuts() -> list[dict[str, Any]]:
	# Daily-action shortcuts. "Receive Firearm" points to the dedicated Intake
	# page rather than the raw FFL Acquisition doctype — Intake is the
	# fast-path single-page workflow; FFL Acquisition is for back-office edits.
	return [
		{"label": "Open POS", "type": "URL", "url": "/app/point-of-sale", "color": "Green"},
		{"label": "Receive Firearm", "type": "URL", "url": "/app/firearm-intake", "color": "Blue"},
		{"label": "New Listing", "type": "DocType", "link_to": "Item", "doc_view": "New", "color": "Cyan"},
		{"label": "Add Customer", "type": "DocType", "link_to": "Customer", "doc_view": "New", "color": "Yellow"},
		{"label": "A&D Book", "type": "DocType", "link_to": "FFL Disposition", "color": "Pink"},
		{"label": "Stock On Hand", "type": "Report", "link_to": "Stock Balance", "color": "Grey"},
	]


def _links() -> list[dict[str, Any]]:
	"""Sidebar grouping. Each Card Break starts a new card; following Links are
	the items inside that card. We keep this curated and short."""
	return [
		# --- Sales card ---
		{"label": "Sales", "type": "Card Break", "icon": "selling"},
		{"type": "Link", "link_type": "DocType", "label": "POS Invoice", "link_to": "POS Invoice"},
		{"type": "Link", "link_type": "DocType", "label": "Sales Invoice", "link_to": "Sales Invoice"},
		{"type": "Link", "link_type": "DocType", "label": "Sales Order", "link_to": "Sales Order"},
		{"type": "Link", "link_type": "DocType", "label": "POS Opening Entry", "link_to": "POS Opening Entry"},
		{"type": "Link", "link_type": "DocType", "label": "POS Closing Entry", "link_to": "POS Closing Entry"},

		# --- Inventory card ---
		{"label": "Inventory", "type": "Card Break", "icon": "stock"},
		{"type": "Link", "link_type": "DocType", "label": "Item", "link_to": "Item"},
		{"type": "Link", "link_type": "DocType", "label": "Item Group", "link_to": "Item Group"},
		{"type": "Link", "link_type": "DocType", "label": "Stock Entry", "link_to": "Stock Entry"},
		{"type": "Link", "link_type": "DocType", "label": "Serial No", "link_to": "Serial No"},
		{"type": "Link", "link_type": "Report", "label": "Stock Balance", "link_to": "Stock Balance", "is_query_report": 1},
		{"type": "Link", "link_type": "Report", "label": "Stock Ledger", "link_to": "Stock Ledger", "is_query_report": 1},

		# --- Compliance (FFL A&D) card ---
		{"label": "Compliance (A&D)", "type": "Card Break", "icon": "non_profit"},
		{"type": "Link", "link_type": "DocType", "label": "FFL Acquisition", "link_to": "FFL Acquisition"},
		{"type": "Link", "link_type": "DocType", "label": "FFL Disposition", "link_to": "FFL Disposition"},

		# --- Customers / Dealers card ---
		{"label": "Customers & Dealers", "type": "Card Break", "icon": "users"},
		{"type": "Link", "link_type": "DocType", "label": "Customer", "link_to": "Customer"},
		{"type": "Link", "link_type": "DocType", "label": "Customer Group", "link_to": "Customer Group"},

		# --- Setup card (collapsible by user) ---
		{"label": "Setup", "type": "Card Break", "icon": "settings"},
		{"type": "Link", "link_type": "DocType", "label": "POS Profile", "link_to": "POS Profile"},
		{"type": "Link", "link_type": "DocType", "label": "Mode of Payment", "link_to": "Mode of Payment"},
		{"type": "Link", "link_type": "DocType", "label": "Caliber", "link_to": "Caliber"},
		{"type": "Link", "link_type": "DocType", "label": "Brand", "link_to": "Brand"},
		{"type": "Link", "link_type": "DocType", "label": "Warehouse", "link_to": "Warehouse"},
	]


def _content_layout(shortcuts: list[dict]) -> list[dict]:
	"""Visual layout. Shortcuts are 4-wide (3 per row, two rows) so labels fit
	without truncation; followed by a 'Lists & Reports' card row.
	"""
	col_per_shortcut = 4
	blocks: list[dict] = []
	for s in shortcuts:
		blocks.append({
			"id": f"sc_{s['label'].lower().replace(' ', '_').replace('&','and')}",
			"type": "shortcut",
			"data": {"shortcut_name": s["label"], "col": col_per_shortcut},
		})
	blocks.append({
		"id": "header_lists",
		"type": "header",
		"data": {"text": "<span class='h4'><b>Lists & Reports</b></span>", "col": 12},
	})
	# Cards (one per Card Break label in _links()).
	for card in ("Sales", "Inventory", "Compliance (A&D)", "Customers & Dealers", "Setup"):
		blocks.append({
			"id": f"card_{card.lower().replace(' ', '_').replace('&','and').replace('(','').replace(')','')}",
			"type": "card",
			"data": {"card_name": card, "col": 4},
		})
	return blocks


# ---------------------------------------------------------------- hiding

def _hide_unused_workspaces() -> None:
	for ws_name in WORKSPACES_TO_HIDE:
		if not frappe.db.exists("Workspace", ws_name):
			continue
		# is_hidden=1 keeps the data but removes from the switcher / sidebar.
		# public stays as-is so reverting is just `is_hidden=0`.
		frappe.db.set_value("Workspace", ws_name, "is_hidden", 1, update_modified=False)


def _set_default_landing_page() -> None:
	"""Make /app/gun-shop the home for every System User. Frappe routes
	`/app` → user.default_workspace when set, falling back to 'Home'.
	"""
	target = WORKSPACE_NAME
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User"},
		pluck="name",
	)
	for u in users:
		if u in ("Administrator", "Guest"):
			continue
		current = frappe.db.get_value("User", u, "default_workspace")
		if current != target:
			frappe.db.set_value("User", u, "default_workspace", target, update_modified=False)
