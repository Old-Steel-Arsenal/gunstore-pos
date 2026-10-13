"""The Settings-workspace shortcut that makes Data Service Settings reachable.
Same two-landing-site contract as test_gunbroker_settings_shortcut.py:
    workspace/settings/settings.json         fresh installs
    add_data_service_settings_shortcut       sites that already have the workspace
"""
from __future__ import annotations

import json
import os
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches.add_data_service_settings_shortcut import execute as add_shortcut

_WORKSPACE = "Settings"
_LABEL = "Data Service (FFL & Catalog)"
_DOCTYPE = "Data Service Settings"
_ANCHOR = "GunBroker (Marketplace)"


def _shipped_workspace_json() -> dict:
	path = os.path.join(
		frappe.get_app_path("ffl_core", "ffl_core", "workspace", "settings"), "settings.json")
	with open(path) as f:
		return json.load(f)


def _ws():
	return frappe.get_doc("Workspace", _WORKSPACE)


def _labels() -> list[str]:
	return [s.label for s in _ws().shortcuts if s.label == _LABEL]


def _blocks() -> list[str]:
	content = json.loads(_ws().content or "[]")
	return [b["data"]["shortcut_name"] for b in content
		if b.get("type") == "shortcut" and b.get("data", {}).get("shortcut_name") == _LABEL]


def _seed_without_shortcut() -> None:
	ws = _ws()
	ws.shortcuts = [s for s in ws.shortcuts if s.label != _LABEL]
	content = [b for b in json.loads(ws.content or "[]")
		if not (b.get("type") == "shortcut"
			and b.get("data", {}).get("shortcut_name") == _LABEL)]
	ws.content = json.dumps(content, separators=(",", ":"))
	ws.flags.ignore_permissions = True
	ws.save(ignore_permissions=True)


class TheShippedWorkspaceJson(FrappeTestCase):
	def test_the_shipped_json_carries_the_shortcut_and_its_block(self):
		d = _shipped_workspace_json()
		by_label = {s["label"]: s for s in d["shortcuts"]}
		self.assertEqual((by_label[_LABEL]["link_to"], by_label[_LABEL]["type"]), (_DOCTYPE, "DocType"))
		names = [b["data"]["shortcut_name"] for b in json.loads(d["content"]) if b.get("type") == "shortcut"]
		self.assertEqual(names.index(_LABEL), names.index(_ANCHOR) + 1)
		# never bump: import_doc would delete-and-reinsert the workspace on every site
		self.assertEqual(d["modified"], "2026-08-18 12:00:00")


class ThePatchOnAnExistingSite(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Workspace", _WORKSPACE):
			raise unittest.SkipTest(f"{_WORKSPACE} workspace not installed on this site")

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_the_patch_adds_row_and_block_once_next_to_the_anchor(self):
		_seed_without_shortcut()
		self.assertEqual(_labels(), [])
		add_shortcut()
		add_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_blocks(), [_LABEL])
		labels = [s.label for s in _ws().shortcuts]
		self.assertEqual(labels.index(_LABEL), labels.index(_ANCHOR) + 1)
		row = next(s for s in _ws().shortcuts if s.label == _LABEL)
		self.assertEqual((row.link_to, row.type), (_DOCTYPE, "DocType"))
