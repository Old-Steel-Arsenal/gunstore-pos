"""patches/v0_1_0_reset_last_known_versions: the POS apps' stored What's New versions above the
running one go back to 0.0.1, so the 0.1.0 note pops once after the 1.15.1 → 0.1.0 restart.
Other apps, versions at or below the running one and unreadable values are left alone; a second
run changes nothing."""
from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.change_log import get_change_log_for_app

from ffl_core.patches import v0_1_0_reset_last_known_versions as patch_module

ARCHIVE = "1.15.1"      # the private line's last release, what both stores stored before the restart
CURRENT = "0.1.0"       # the release that runs the patch (pinned: later releases keep its note on disk)


def _user(email: str, known) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "Version", "send_welcome_email": 0,
			"user_type": "System User"}).insert(ignore_permissions=True)
	value = known if isinstance(known, str) or known is None else json.dumps(known)
	frappe.db.set_value("User", email, "last_known_versions", value, update_modified=False)
	return email


def _raw(email: str):
	return frappe.db.get_value("User", email, "last_known_versions")


def _known(email: str) -> dict:
	return json.loads(_raw(email))


def _entry(version: str, title: str = "Gun Shop") -> dict:
	return {"title": title, "description": "", "branch": "", "version": version}


class ResetLastKnownVersions(FrappeTestCase):
	def setUp(self):
		import ffl_core

		self.current = CURRENT
		pinned = patch.object(ffl_core, "__version__", CURRENT)
		pinned.start()
		self.addCleanup(pinned.stop)
		self.archived = _user("v010-archived@example.com", {
			"frappe": _entry("16.17.3", "Frappe Framework"), "erpnext": _entry("16.9.0", "ERPNext"),
			"ffl_core": _entry(ARCHIVE), "ffl_integrations": _entry(ARCHIVE, "FFL Integrations"),
			"ffl_woo_sync": _entry("1.0.0-beta.1", "FFL Woo Sync"), "osa_dropship": _entry("1.6.0", "Drop-ship"),
		})
		self.current_user = _user("v010-current@example.com", {
			"ffl_core": _entry(self.current), "ffl_integrations": _entry("0.0.1"),
			"ffl_woo_sync": _entry("0.1.0-beta.1"), "frappe": _entry("16.17.3")})
		self.unreadable = _user("v010-unreadable@example.com", {"ffl_core": _entry("not a version"),
			"ffl_integrations": {"title": "no version"}, "ffl_woo_sync": "1.15.1"})
		self.not_json = _user("v010-not-json@example.com", "{broken")
		self.empty = _user("v010-empty@example.com", None)

	def test_resets_only_the_pos_apps_above_the_running_version(self):
		before = {u: _raw(u) for u in (self.current_user, self.unreadable, self.not_json)}
		patch_module.execute()

		known = _known(self.archived)
		for app in patch_module.APPS:
			self.assertEqual(known[app]["version"], "0.0.1", app)
			self.assertIn("title", known[app], "only the version changes")
		self.assertEqual(known["frappe"]["version"], "16.17.3")
		self.assertEqual(known["erpnext"]["version"], "16.9.0")
		self.assertEqual(known["osa_dropship"]["version"], "1.6.0", "an extension keeps its own line")

		for user, value in before.items():                 # at or below the running version, or unreadable
			self.assertEqual(_raw(user), value, user)
		self.assertFalse(_raw(self.empty), "a user frappe has not recorded yet stays unrecorded")

	def test_a_second_run_changes_nothing(self):
		patch_module.execute()
		after_first = frappe.db.get_value("User", self.archived, ["last_known_versions", "modified"])
		patch_module.execute()
		self.assertEqual(frappe.db.get_value("User", self.archived, ["last_known_versions", "modified"]), after_first)

	def test_the_running_versions_note_is_what_pops_next(self):
		# frappe's own diff from the reset value reaches the current note; from the archived one it found nothing.
		self.assertEqual(get_change_log_for_app("ffl_core", ARCHIVE, self.current) or [], [])
		patch_module.execute()
		notes = get_change_log_for_app("ffl_core", _known(self.archived)["ffl_core"]["version"], self.current)
		self.assertEqual([v for v, _md in notes][:1], [self.current])
