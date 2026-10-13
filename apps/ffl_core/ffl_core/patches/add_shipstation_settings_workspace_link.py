"""Add a "ShipStation Settings" link to the Gun Shop workspace, next to
"FFL Settings".

Frappe does not re-sync an existing workspace from the app's JSON on migrate
(it preserves customizations), so the workspace file change alone never
reaches sites that already have the workspace. Custom-app settings singles
also never appear on Frappe's curated /desk/settings page — the workspace
card is the discoverable entry point. One-time, idempotent.
"""
import frappe

_LABEL = "ShipStation Settings"


def execute():
	if not frappe.db.exists("Workspace", "Gun Shop"):
		return
	ws = frappe.get_doc("Workspace", "Gun Shop")
	if any(l.label == _LABEL and l.type == "Link" for l in ws.links):
		return
	ws.append("links", {
		"label": _LABEL, "type": "Link", "link_type": "DocType",
		"link_to": _LABEL, "hidden": 0, "onboard": 0,
		"is_query_report": 0, "link_count": 0,
	})
	# Land it right under the FFL Settings link so it sits in the same card.
	anchor = next((i for i, l in enumerate(ws.links)
		if l.type == "Link" and l.label == "FFL Settings"), None)
	if anchor is not None:
		moved = ws.links.pop()
		ws.links.insert(anchor + 1, moved)
		for i, l in enumerate(ws.links):
			l.idx = i + 1
	ws.flags.ignore_permissions = True
	ws.save(ignore_permissions=True)
