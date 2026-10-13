"""The Settings-workspace shortcut that makes the GunBroker channel reachable.

Same two-landing-site contract as the consignment shortcuts (see
test_consignment_out_workspace_shortcuts.py for the full mechanics, which this
file does not repeat):

    workspace/settings/settings.json      fresh installs
    add_gunbroker_settings_shortcut       sites that already have the workspace

Neither half covers both kinds of site, so both are asserted here. What makes
this shortcut load-bearing rather than cosmetic: a custom app's Settings single
never appears on Frappe's curated settings page, and every GunBroker button in
the Desk is gated on `GunBroker Settings.enabled` — which ships 0. Without the
shortcut the only way to switch the channel on is to know the URL.
"""
from __future__ import annotations

import json
import os
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches.add_gunbroker_settings_shortcut import execute as add_shortcut
from ffl_core.patches.rename_gunbroker_settings_shortcut import execute as rename_shortcut

_WORKSPACE = "Settings"
_LABEL = "GunBroker (Marketplace)"
_DOCTYPE = "GunBroker Settings"
_ANCHOR = "WooCommerce (Online Store)"
_OLD_LABEL = "GunBroker (Auction Channel)"


def _shipped_workspace_json() -> dict:
	"""The settings.json that `bench new-site --install-app ffl_core` imports."""
	path = os.path.join(
		frappe.get_app_path("ffl_core", "ffl_core", "workspace", "settings"), "settings.json"
	)
	with open(path) as f:
		return json.load(f)


def _ws() -> "frappe.Document":
	return frappe.get_doc("Workspace", _WORKSPACE)


def _labels() -> list[str]:
	"""Every shortcut row carrying the label — length doubles as a count."""
	return [s.label for s in _ws().shortcuts if s.label == _LABEL]


def _blocks(ws=None) -> list[str]:
	"""Every layout block addressing the label."""
	ws = ws or _ws()
	content = json.loads(ws.content or "[]")
	return [b["data"]["shortcut_name"] for b in content
		if b.get("type") == "shortcut" and b.get("data", {}).get("shortcut_name") == _LABEL]


def _seed_without_shortcut() -> None:
	"""An existing site from before the channel: no row, no layout block."""
	ws = _ws()
	ws.shortcuts = [s for s in ws.shortcuts if s.label != _LABEL]
	content = [b for b in json.loads(ws.content or "[]")
		if not (b.get("type") == "shortcut"
			and b.get("data", {}).get("shortcut_name") == _LABEL)]
	ws.content = json.dumps(content, separators=(",", ":"))
	ws.flags.ignore_permissions = True
	ws.save(ignore_permissions=True)


class TheShippedWorkspaceJson(FrappeTestCase):
	"""The fresh-install half. Reads the file, not the site."""

	def test_the_shipped_json_carries_the_gunbroker_shortcut(self):
		by_label = {s["label"]: s for s in _shipped_workspace_json()["shortcuts"]}
		self.assertIn(_LABEL, by_label, msg=(
			"settings.json ships no GunBroker shortcut. A fresh site never runs "
			"add_gunbroker_settings_shortcut (install_app marks patches complete "
			"without executing them), so this file is the only thing that can "
			"put it there."))
		self.assertEqual(by_label[_LABEL]["link_to"], _DOCTYPE)
		self.assertEqual(by_label[_LABEL]["type"], "DocType")

	def test_the_shipped_layout_references_the_shortcut(self):
		content = json.loads(_shipped_workspace_json()["content"])
		names = [b["data"]["shortcut_name"] for b in content if b.get("type") == "shortcut"]
		self.assertIn(_LABEL, names, msg=(
			"The shortcut row without a layout block renders nothing — a "
			"workspace draws only what `content` addresses."))
		# The three sales channels read as one group.
		self.assertEqual(names.index(_LABEL), names.index(_ANCHOR) + 1)

	def test_the_shipped_json_modified_stamp_is_not_bumped(self):
		"""Bumping it would make import_doc delete and re-insert the workspace on
		every existing site, discarding whatever the shop customised. The patch
		is what reaches those sites."""
		self.assertEqual(_shipped_workspace_json()["modified"], "2026-08-18 12:00:00")


class ThePatchOnAnExistingSite(FrappeTestCase):
	"""The other half: a site that has the workspace but not the shortcut."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Workspace", _WORKSPACE):
			raise unittest.SkipTest(f"{_WORKSPACE} workspace not installed on this site")

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def test_the_patch_adds_both_the_row_and_the_layout_block(self):
		_seed_without_shortcut()
		self.assertEqual(_labels(), [])
		add_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_blocks(), [_LABEL])
		row = next(s for s in _ws().shortcuts if s.label == _LABEL)
		self.assertEqual(row.link_to, _DOCTYPE)
		self.assertEqual(row.type, "DocType")

	def test_the_patch_lands_it_next_to_the_other_online_channel(self):
		_seed_without_shortcut()
		add_shortcut()
		labels = [s.label for s in _ws().shortcuts]
		self.assertEqual(labels.index(_LABEL), labels.index(_ANCHOR) + 1)

	def test_running_the_patch_twice_does_not_duplicate_it(self):
		_seed_without_shortcut()
		add_shortcut()
		add_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_blocks(), [_LABEL])

	def test_a_site_with_the_row_but_no_layout_block_gets_the_block(self):
		"""The two halves are independent: the row carries the link, the block
		is what draws it. A site can end up with one and not the other."""
		_seed_without_shortcut()
		ws = _ws()
		ws.append("shortcuts", {"label": _LABEL, "type": "DocType",
			"link_to": _DOCTYPE, "color": "Blue", "doc_view": ""})
		ws.flags.ignore_permissions = True
		ws.save(ignore_permissions=True)
		self.assertEqual(_blocks(), [])
		add_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_blocks(), [_LABEL])

	def test_the_patch_is_a_no_op_without_the_workspace(self):
		"""Sites that never installed the Settings workspace must not raise."""
		from unittest.mock import patch as mock_patch
		with mock_patch.object(frappe.db, "exists", return_value=False):
			add_shortcut()  # must not raise


class TheRenamePatch(FrappeTestCase):
	"""Sites that got the shortcut under its first name, "Auction Channel".

	Fixed-price listings are the default and auctions one option, so the old
	label described the channel wrongly. The shipped JSON carries the new name
	for fresh installs; existing sites only ever see it through this patch.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Workspace", _WORKSPACE):
			raise unittest.SkipTest(f"{_WORKSPACE} workspace not installed on this site")

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _seed_old_name(self):
		_seed_without_shortcut()
		ws = _ws()
		ws.append("shortcuts", {"label": _OLD_LABEL, "type": "DocType",
			"link_to": _DOCTYPE, "color": "Blue", "doc_view": ""})
		content = json.loads(ws.content or "[]")
		content.append({"id": "sc_gunbroker_settings", "type": "shortcut",
			"data": {"shortcut_name": _OLD_LABEL, "col": 4}})
		ws.content = json.dumps(content, separators=(",", ":"))
		ws.flags.ignore_permissions = True
		ws.save(ignore_permissions=True)

	def _old_rows_and_blocks(self):
		ws = _ws()
		rows = [s.label for s in ws.shortcuts if s.label == _OLD_LABEL]
		blocks = [b for b in json.loads(ws.content or "[]")
			if b.get("type") == "shortcut"
			and b.get("data", {}).get("shortcut_name") == _OLD_LABEL]
		return rows, blocks

	def test_the_row_and_the_layout_block_are_renamed_together(self):
		"""The layout addresses the row by label: renaming one without the
		other leaves a block pointing at nothing, which renders as nothing."""
		self._seed_old_name()
		rename_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_blocks(), [_LABEL])
		self.assertEqual(self._old_rows_and_blocks(), ([], []))
		row = next(s for s in _ws().shortcuts if s.label == _LABEL)
		self.assertEqual(row.link_to, _DOCTYPE)

	def test_running_it_twice_changes_nothing_more(self):
		self._seed_old_name()
		rename_shortcut()
		rename_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_blocks(), [_LABEL])

	def test_a_site_already_on_the_new_name_is_left_alone(self):
		_seed_without_shortcut()
		add_shortcut()
		before = _ws().modified
		rename_shortcut()
		self.assertEqual(_labels(), [_LABEL])
		self.assertEqual(_ws().modified, before)

	def test_the_patch_is_a_no_op_without_the_workspace(self):
		from unittest.mock import patch as mock_patch
		with mock_patch.object(frappe.db, "exists", return_value=False):
			rename_shortcut()  # must not raise
