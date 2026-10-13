"""Add the "Sales Report" entry to the Gun Shop workspace — a top shortcut tile
(next to the other report tiles) plus a link under the Sales card.

Frappe does not re-sync an existing workspace from the app's JSON on migrate
(it preserves customizations), so the workspace file change alone never reaches
sites that already have the workspace. This one-time, idempotent patch adds the
shortcut, its content-layout tile, and the Sales-card link.
"""
import json

import frappe

_LABEL = "Sales Report"
_REF = "Sales Invoice"


def execute():
	if not frappe.db.exists("Workspace", "Gun Shop"):
		return
	ws = frappe.get_doc("Workspace", "Gun Shop")
	changed = False

	# 1) Top shortcut tile.
	if not any(s.label == _LABEL for s in ws.shortcuts):
		ws.append("shortcuts", {"label": _LABEL, "type": "Report", "link_to": _LABEL,
			"report_ref_doctype": _REF, "color": "Green", "doc_view": ""})
		changed = True

	# 2) Content-layout block — placed after "Stock On Hand" if present, else end.
	try:
		content = json.loads(ws.content or "[]")
	except (ValueError, TypeError):
		content = []
	present = {b.get("data", {}).get("shortcut_name") for b in content
		if b.get("type") == "shortcut"}
	if _LABEL not in present:
		block = {"id": "sc_sales_report", "type": "shortcut",
			"data": {"shortcut_name": _LABEL, "col": 4}}
		idx = next((i for i, b in enumerate(content) if b.get("type") == "shortcut"
			and b.get("data", {}).get("shortcut_name") == "Stock On Hand"), None)
		if idx is None:
			content.append(block)
		else:
			content[idx + 1:idx + 1] = [block]
		ws.content = json.dumps(content)
		changed = True

	# 3) Sales-card link — inserted right after the "Sales" Card Break so it
	# lands inside that card.
	if not any(l.label == _LABEL and l.type == "Link" for l in ws.links):
		ws.append("links", {"label": _LABEL, "type": "Link", "link_type": "Report",
			"link_to": _LABEL, "is_query_report": 1, "hidden": 0, "onboard": 0,
			"link_count": 0})
		sales_idx = next((i for i, l in enumerate(ws.links)
			if l.type == "Card Break" and l.label == "Sales"), None)
		if sales_idx is not None:
			moved = ws.links.pop()  # the one just appended
			ws.links.insert(sales_idx + 1, moved)
			for i, l in enumerate(ws.links):
				l.idx = i + 1
		changed = True

	if changed:
		ws.flags.ignore_permissions = True
		ws.save(ignore_permissions=True)
