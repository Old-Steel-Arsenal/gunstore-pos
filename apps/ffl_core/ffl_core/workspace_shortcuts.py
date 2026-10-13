"""Shared mechanics for workspace shortcuts added by code (patches, extensions).

A custom app's Settings single never appears on Frappe's curated settings page,
so each one gets a shortcut on our Settings workspace — and an extension's
Desk pages get theirs on the Gun Shop workspace when the store switches the
extension on (hooks ``gunshop_extension_toggled``). Every such shortcut needs
the two-landing-site contract: a fresh install reads the shipped workspace
JSON; an existing site only gets it from code, because import_file skips a
workspace whose DB ``modified`` is at or past the file's, and bumping the stamp
would delete-and-reinsert the workspace, discarding shop customisations. These
two idempotent steps are that code.
"""
from __future__ import annotations

import json

import frappe

WORKSPACE = "Settings"


def ensure_shortcut(label: str, link_to: str, *, anchor: str, block_id: str,
		color: str = "Blue", shortcut_type: str = "DocType", workspace: str = WORKSPACE,
		report_ref_doctype: str | None = None) -> bool:
	"""Add a shortcut (row + layout block) right after ``anchor`` on ``workspace``.

	``shortcut_type`` is "DocType" (``link_to`` = the Single), "Page"
	(``link_to`` = the page name), "Report" (``link_to`` = the report name, plus
	its ``report_ref_doctype``) or "URL" (``link_to`` = the URL). Returns True
	when something changed."""
	if not frappe.db.exists("Workspace", workspace):
		return False
	ws = frappe.get_doc("Workspace", workspace)
	changed = False
	if not any(s.label == label for s in ws.shortcuts):
		row = {"label": label, "type": shortcut_type, "color": color, "doc_view": ""}
		if shortcut_type == "URL":
			row["url"] = link_to
		else:
			row["link_to"] = link_to
		if report_ref_doctype:
			row["report_ref_doctype"] = report_ref_doctype
		ws.append("shortcuts", row)
		pos = next((i for i, s in enumerate(ws.shortcuts) if s.label == anchor), None)
		if pos is not None:
			ws.shortcuts.insert(pos + 1, ws.shortcuts.pop())
			for i, s in enumerate(ws.shortcuts):
				s.idx = i + 1
		changed = True
	content = _content(ws)
	present = {b.get("data", {}).get("shortcut_name") for b in content if b.get("type") == "shortcut"}
	if label not in present:
		idx = next((i for i, b in enumerate(content)
			if b.get("type") == "shortcut"
			and b.get("data", {}).get("shortcut_name") == anchor), len(content) - 1)
		content.insert(idx + 1, {"id": block_id, "type": "shortcut",
			"data": {"shortcut_name": label, "col": 4}})
		ws.content = json.dumps(content, separators=(",", ":"))
		changed = True
	if changed:
		_save_workspace(ws)
	return changed


def ensure_card_link(label: str, link_to: str, *, card: str, link_type: str = "Report",
		workspace: str = WORKSPACE) -> bool:
	"""Add a link as the first entry of the ``card`` Card Break on ``workspace``.

	``link_type`` "Report" marks it a query report. No-op when a link to
	``link_to`` already exists, or the shop removed the card. Returns True when
	something changed."""
	if not frappe.db.exists("Workspace", workspace):
		return False
	ws = frappe.get_doc("Workspace", workspace)
	if any(l.type == "Link" and l.link_to == link_to for l in ws.links):
		return False
	pos = next((i for i, l in enumerate(ws.links)
		if l.type == "Card Break" and l.label == card), None)
	if pos is None:
		return False
	ws.append("links", {"label": label, "type": "Link", "link_type": link_type,
		"link_to": link_to, "is_query_report": 1 if link_type == "Report" else 0,
		"hidden": 0, "onboard": 0, "link_count": 0})
	ws.links.insert(pos + 1, ws.links.pop())
	for i, l in enumerate(ws.links):
		l.idx = i + 1
	_save_workspace(ws)
	return True


def remove_shortcut(label: str, *, workspace: str = WORKSPACE) -> bool:
	"""The reverse: drop the row and the layout block for ``label``. Returns
	True when something changed."""
	if not frappe.db.exists("Workspace", workspace):
		return False
	ws = frappe.get_doc("Workspace", workspace)
	changed = False
	kept = [s for s in ws.shortcuts if s.label != label]
	if len(kept) != len(ws.shortcuts):
		ws.set("shortcuts", kept)
		changed = True
	content = _content(ws)
	kept_blocks = [b for b in content if not (b.get("type") == "shortcut"
		and b.get("data", {}).get("shortcut_name") == label)]
	if len(kept_blocks) != len(content):
		ws.content = json.dumps(kept_blocks, separators=(",", ":"))
		changed = True
	if changed:
		_save_workspace(ws)
	return changed


def ensure_number_card(name: str, *, anchor: str, block_id: str, col: int = 4,
		workspace: str = WORKSPACE) -> bool:
	"""Put the Number Card ``name`` on ``workspace``: a row plus a layout block right after the
	``anchor`` card's block (or after the last card block when the anchor is gone). Returns True
	when something changed."""
	if not frappe.db.exists("Workspace", workspace):
		return False
	ws = frappe.get_doc("Workspace", workspace)
	changed = False
	if not any(c.number_card_name == name for c in ws.number_cards):
		ws.append("number_cards", {"number_card_name": name, "label": name})
		changed = True
	content = _content(ws)
	if not any(_card_block(b, name) for b in content):
		blocks = [i for i, b in enumerate(content) if b.get("type") == "number_card"]
		idx = next((i for i in blocks if _card_block(content[i], anchor)), blocks[-1] if blocks else len(content) - 1)
		content.insert(idx + 1, {"id": block_id, "type": "number_card",
			"data": {"number_card_name": name, "col": col}})
		ws.content = json.dumps(content, separators=(",", ":"))
		changed = True
	if changed:
		_save_workspace(ws)
	return changed


def remove_number_card(name: str, *, workspace: str = WORKSPACE) -> bool:
	"""The reverse of :func:`ensure_number_card`. Returns True when something changed."""
	if not frappe.db.exists("Workspace", workspace):
		return False
	ws = frappe.get_doc("Workspace", workspace)
	changed = False
	kept = [c for c in ws.number_cards if c.number_card_name != name]
	if len(kept) != len(ws.number_cards):
		ws.set("number_cards", kept)
		changed = True
	content = _content(ws)
	kept_blocks = [b for b in content if not _card_block(b, name)]
	if len(kept_blocks) != len(content):
		ws.content = json.dumps(kept_blocks, separators=(",", ":"))
		changed = True
	if changed:
		_save_workspace(ws)
	return changed


def _card_block(block: dict, name: str) -> bool:
	return block.get("type") == "number_card" and block.get("data", {}).get("number_card_name") == name


def _content(ws) -> list:
	try:
		return json.loads(ws.content or "[]")
	except (ValueError, TypeError):
		return []


def _save_workspace(ws) -> None:
	"""Save WITHOUT writing the workspace back over the app's shipped JSON.

	On a developer_mode bench ``Workspace.on_update`` calls ``export_to_files()``
	and regenerates ``workspace/<name>/<name>.json`` from the database; a
	scripted add/remove would then rewrite the shipped file and bump its
	``modified`` — which is exactly what makes import_doc delete-and-reinsert
	the workspace on every existing site. Frappe already exempts install, patch,
	migrate, fixtures and tests (``workspace.disable_saving_as_public``); this
	runs from a settings save or an extension switch too, so state what it is:
	a scripted edit, not somebody rearranging their workspace in the UI.
	"""
	restore = frappe.flags.in_patch
	frappe.flags.in_patch = True
	try:
		ws.flags.ignore_permissions = True
		# During an app install the workspace names Number Cards and pages that do not exist yet:
		# ``after_install`` runs before Frappe syncs an app's dashboards, and the To Receive card's
		# page belongs to ffl_integrations, installed after ffl_core. The next migrate fills them in,
		# so there (``frappe.flags.in_install``, set by installer.install_app) a link to something
		# not there yet must not stop the install -- it stopped every fresh one at ffl_core's
		# after_install. Any other save still checks its links.
		ws.flags.ignore_links = bool(frappe.flags.in_install)
		ws.save(ignore_permissions=True)
	finally:
		frappe.flags.in_patch = restore
