"""Add an 'Import from FastBound' shortcut to the Gun Shop workspace.

Run via:
    bench --site dev.localhost console <<EOF
    import sys; sys.path.insert(0, "/workspace")
    from scripts.setup_fastbound_shortcut import run; run()
    EOF
"""
from __future__ import annotations

import json

import frappe


def run() -> None:
	if not frappe.db.exists("Workspace", "Gun Shop"):
		print("Gun Shop workspace not found — run scripts.setup_ui first.")
		return

	ws = frappe.get_doc("Workspace", "Gun Shop")

	if not any(s.label == "Import from FastBound" for s in ws.shortcuts):
		ws.append("shortcuts", {
			"label": "Import from FastBound",
			"type": "DocType",
			"link_to": "FastBound Settings",
			"color": "Pink",
		})

	# Add to content layout if not already present.
	content = json.loads(ws.content or "[]")
	if not any(b.get("id") == "sc_import_from_fastbound" for b in content):
		# Insert after the existing shortcut row (find the last 'shortcut' block).
		insert_at = 0
		for i, block in enumerate(content):
			if block.get("type") == "shortcut":
				insert_at = i + 1
		content.insert(insert_at, {
			"id": "sc_import_from_fastbound",
			"type": "shortcut",
			"data": {"shortcut_name": "Import from FastBound", "col": 4},
		})
		ws.content = json.dumps(content)

	# Also add a Setup-card link to FastBound Settings so admin discovery is easy.
	if not any(l.label == "FastBound Settings" for l in ws.links):
		# Find the Setup card section (last Card Break with label 'Setup').
		setup_idx = None
		for i, link in enumerate(ws.links):
			if link.type == "Card Break" and link.label == "Setup":
				setup_idx = i
				break
		new_link = {
			"type": "Link",
			"link_type": "DocType",
			"label": "FastBound Settings",
			"link_to": "FastBound Settings",
		}
		if setup_idx is not None:
			# append at end of links — Setup card is last, so any append goes there.
			ws.append("links", new_link)
		else:
			ws.append("links", new_link)

	ws.save(ignore_permissions=True)
	frappe.db.commit()
	print("setup_fastbound_shortcut: shortcut + setup link added")
