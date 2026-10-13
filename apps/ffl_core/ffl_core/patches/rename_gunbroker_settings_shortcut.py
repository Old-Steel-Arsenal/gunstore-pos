"""Rename the Settings-workspace shortcut "GunBroker (Auction Channel)" to
"GunBroker (Marketplace)".

Fixed-price listings are the default and auctions one option, so the first
label described the channel wrongly. The shipped workspace JSON carries the new
name for fresh installs; this patch reaches sites that already have the
workspace (add_gunbroker_settings_shortcut.py explains why the JSON cannot).
The layout addresses a shortcut by label, so the row and the block are renamed
together — one without the other renders nothing. One-time, idempotent.
"""
import json

import frappe

_WORKSPACE = "Settings"
_OLD = "GunBroker (Auction Channel)"
_NEW = "GunBroker (Marketplace)"


def execute():
	if not frappe.db.exists("Workspace", _WORKSPACE):
		return
	ws = frappe.get_doc("Workspace", _WORKSPACE)
	changed = False

	for row in ws.shortcuts:
		if row.label == _OLD:
			row.label = _NEW
			changed = True

	try:
		content = json.loads(ws.content or "[]")
	except (ValueError, TypeError):
		content = []
	for block in content:
		if block.get("type") == "shortcut" and block.get("data", {}).get("shortcut_name") == _OLD:
			block["data"]["shortcut_name"] = _NEW
			changed = True

	if changed:
		ws.content = json.dumps(content, separators=(",", ":"))
		ws.flags.ignore_permissions = True
		ws.save(ignore_permissions=True)
