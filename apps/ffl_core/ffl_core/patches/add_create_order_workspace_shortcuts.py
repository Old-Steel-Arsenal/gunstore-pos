"""Add the "Create Order" + "Pending Order" shortcuts to the Gun Shop
workspace, placed right after "Open POS".

Frappe does not re-sync an existing workspace from the app's JSON on migrate
(it preserves customizations), so the workspace file change alone never reaches
sites that already have the workspace. This one-time, idempotent patch adds the
two shortcuts to both the shortcuts list and the content layout.
"""
import json

import frappe

_SHORTCUTS = [
	("Create Order", "/app/create-order"),
	("Pending Order", "/app/pending-dispositions"),
]


def execute():
	if not frappe.db.exists("Workspace", "Gun Shop"):
		return
	ws = frappe.get_doc("Workspace", "Gun Shop")
	changed = False

	# Drop the stale "Build Shipped Order" shortcut — the page was renamed to
	# Create Order, so its /app/build-order tile now 404s.
	stale = {"Build Shipped Order"}
	kept = [s for s in ws.shortcuts if s.label not in stale]
	if len(kept) != len(ws.shortcuts):
		ws.set("shortcuts", kept)
		changed = True

	have = {s.label for s in ws.shortcuts}
	for label, url in _SHORTCUTS:
		if label not in have:
			ws.append("shortcuts", {"label": label, "type": "URL", "url": url,
				"color": "Blue", "doc_view": ""})
			changed = True

	try:
		content = json.loads(ws.content or "[]")
	except (ValueError, TypeError):
		content = []
	content_changed = False
	cleaned = [b for b in content if not (b.get("type") == "shortcut"
		and b.get("data", {}).get("shortcut_name") in stale)]
	if len(cleaned) != len(content):
		content = cleaned
		content_changed = True
	present = {b.get("data", {}).get("shortcut_name") for b in content
		if b.get("type") == "shortcut"}
	missing = [(label, "sc_" + label.lower().replace(" ", "_"))
		for label, _ in _SHORTCUTS if label not in present]
	if missing:
		# Insert just after Open POS so the order workflow leads the grid.
		idx = next((i for i, b in enumerate(content)
			if b.get("type") == "shortcut"
			and b.get("data", {}).get("shortcut_name") == "Open POS"), 0)
		blocks = [{"id": sid, "type": "shortcut",
			"data": {"shortcut_name": label, "col": 4}} for label, sid in missing]
		content[idx + 1:idx + 1] = blocks
		content_changed = True
	if content_changed:
		ws.content = json.dumps(content)
		changed = True

	if changed:
		ws.flags.ignore_permissions = True
		ws.save(ignore_permissions=True)
