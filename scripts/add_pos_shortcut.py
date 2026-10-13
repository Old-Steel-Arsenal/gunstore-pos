"""Add a Point of Sale shortcut to the Selling workspace, including its
rendering block in the Workspace `content` JSON.

Frappe v15+ workspaces store both a `shortcuts` child table (data) and
a `content` JSON column (visual layout). Adding only to `shortcuts` makes
the entry exist but invisible. Both must be touched for the shortcut to
render at the top of the page.
"""
from __future__ import annotations

import json

import frappe


WORKSPACE = "Selling"
SHORTCUT_LABEL = "Point of Sale"
SHORTCUT_URL = "/app/point-of-sale"
SHORTCUT_BLOCK_ID = "ffl-pos-shortcut"


def run():
	ws = frappe.get_doc("Workspace", WORKSPACE)

	if not any(s.label == SHORTCUT_LABEL for s in ws.shortcuts):
		ws.append("shortcuts", {
			"label": SHORTCUT_LABEL,
			"type": "URL",
			"url": SHORTCUT_URL,
			"color": "Green",
		})
		print(f"shortcut row added")
	else:
		print(f"shortcut row already present")

	content = json.loads(ws.content or "[]")
	if not any(b.get("id") == SHORTCUT_BLOCK_ID for b in content):
		content.insert(0, {
			"id": SHORTCUT_BLOCK_ID,
			"type": "shortcut",
			"data": {"shortcut_name": SHORTCUT_LABEL, "col": 3},
		})
		ws.content = json.dumps(content)
		print(f"shortcut block prepended to content")
	else:
		print(f"shortcut block already in content")

	ws.save(ignore_permissions=True)
	frappe.db.commit()
	print(f"saved; shortcuts={len(ws.shortcuts)}, content blocks={len(json.loads(ws.content))}")
