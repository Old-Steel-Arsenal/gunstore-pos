"""Product version + release notes: the three apps agree, the notes parse,
the Help-menu row is synced once, and bump_version refuses a note-less bump."""
from __future__ import annotations

import importlib.util
import re
import shutil
import tempfile
from pathlib import Path

import frappe
from frappe.core.doctype.navbar_settings.navbar_settings import sync_standard_items
from frappe.tests.utils import FrappeTestCase
from semantic_version import Version

from ffl_core.api import changelog

SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
HELP_LABEL = "What's New in POS"


class TestVersion(FrappeTestCase):
	def test_three_apps_share_one_semver(self):
		import ffl_core
		import ffl_integrations
		import ffl_woo_sync

		self.assertRegex(ffl_core.__version__, SEMVER)
		self.assertEqual(ffl_core.__version__, ffl_integrations.__version__)
		self.assertEqual(ffl_core.__version__, ffl_woo_sync.__version__)

	def test_current_version_has_a_release_note(self):
		import ffl_core

		versions = [v for v, _md in changelog.read_release_notes()]
		self.assertTrue(versions)
		# Exact match on purpose: frappe cannot strip a pre-release tail (see
		# scripts/bump_version.py note_path), so a beta needs its own note file.
		self.assertEqual(versions[0], ffl_core.__version__, "newest note must be the current version")

	def test_app_title_is_what_the_popup_filter_expects(self):
		# the changelog bundle keeps only the entry titled like this; a rename would let
		# ERPNext's own release notes back onto the counter.
		import re
		from pathlib import Path

		bundle = Path(frappe.get_app_path("ffl_core", "public", "js", "ffl_core.bundle.js")).read_text()
		m = re.search(r"""\bOUR_APP_TITLE\s*=\s*['"]([^'"]+)['"]""", bundle)
		self.assertIsNotNone(m, "OUR_APP_TITLE not found in ffl_core.bundle.js")
		js_title = m.group(1)
		self.assertEqual(frappe.get_hooks("app_title", app_name="ffl_core")[0], js_title)
		self.assertEqual(js_title, "OpenGunstore")


class TestChangelogApi(FrappeTestCase):
	def test_get_changelog_shape_and_order(self):
		out = changelog.get_changelog()
		self.assertRegex(out["version"], SEMVER)
		self.assertTrue(out["entries"])
		versions = [Version(e[0]) for e in out["entries"]]
		self.assertEqual(versions, sorted(versions, reverse=True))
		self.assertTrue(out["entries"][0][1].lstrip().startswith("### Version"))

	def test_release_note_is_plain_language(self):
		# No PR numbers, no code-ish identifiers, so the note reads for the counter.
		for _v, md in changelog.get_changelog()["entries"]:
			self.assertNotRegex(md, r"#\d{2,}", "PR/issue numbers do not belong in a release note")
			self.assertNotRegex(md, r"\b[a-z]+_[a-z_]+\b", "snake_case identifiers do not belong in a release note")

	def test_requires_login(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.is_whitelisted(changelog.get_changelog)  # the /api/method guest gate
		finally:
			frappe.set_user("Administrator")


class TestHelpMenuHook(FrappeTestCase):
	def test_standard_navbar_item_syncs_once_at_the_top_level(self):
		sync_standard_items()
		sync_standard_items()
		nav = frappe.get_doc("Navbar Settings")
		rows = [r for r in nav.settings_dropdown if r.item_label == HELP_LABEL]
		self.assertEqual(len(rows), 1)
		self.assertEqual([r for r in nav.help_dropdown if r.item_label == HELP_LABEL], [], "moved out of Help")
		self.assertEqual(rows[0].item_type, "Action")
		self.assertEqual(rows[0].action, "gunstore.showWhatsNew()")
		self.assertTrue(rows[0].is_standard)


class TestBumpScript(FrappeTestCase):
	"""Runs scripts/bump_version.py against a throwaway copy of the tree."""

	def _load(self, root: Path):
		# The app may be symlinked into the bench (CI does that): resolve first.
		src = Path(frappe.get_app_path("ffl_core")).resolve().parents[2] / "scripts" / "bump_version.py"
		if not src.is_file():
			self.skipTest(f"repo scripts/ not reachable from this bench: {src}")
		spec = importlib.util.spec_from_file_location("bump_version", src)
		mod = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(mod)
		mod.ROOT = root
		return mod

	def setUp(self):
		self.root = Path(tempfile.mkdtemp())
		for app in ("ffl_core", "ffl_integrations", "ffl_woo_sync"):
			d = self.root / "apps" / app / app
			d.mkdir(parents=True)
			(d / "__init__.py").write_text('__version__ = "1.0.0"\nX = 1\n')
		(self.root / "apps/ffl_core/ffl_core/change_log/v1").mkdir(parents=True)

	def tearDown(self):
		shutil.rmtree(self.root, ignore_errors=True)

	def _versions(self):
		return [(self.root / "apps" / a / a / "__init__.py").read_text() for a in ("ffl_core", "ffl_integrations", "ffl_woo_sync")]

	def test_refuses_without_release_note(self):
		mod = self._load(self.root)
		with self.assertRaises(SystemExit):
			mod.bump("1.0.1", regenerate_docs=False)
		self.assertEqual(self._versions(), ['__version__ = "1.0.0"\nX = 1\n'] * 3)

	def test_rejects_non_semver(self):
		mod = self._load(self.root)
		with self.assertRaises(SystemExit):
			mod.bump("1.0", regenerate_docs=False)

	def test_writes_nothing_when_one_file_lacks_the_line(self):
		mod = self._load(self.root)
		(self.root / "apps/ffl_core/ffl_core/change_log/v1/v1_0_1.md").write_text("### Version 1.0.1\n- x\n")
		(self.root / "apps/ffl_woo_sync/ffl_woo_sync/__init__.py").write_text("X = 1\n")
		with self.assertRaises(SystemExit):
			mod.bump("1.0.1", regenerate_docs=False)
		self.assertIn('"1.0.0"', self._versions()[0], "first file must not be half-bumped")

	def test_bumps_all_three_and_keeps_the_rest(self):
		mod = self._load(self.root)
		(self.root / "apps/ffl_core/ffl_core/change_log/v1/v1_0_1-beta_2.md").write_text("### Version 1.0.1 Beta 2\n- x\n")
		changed = mod.bump("1.0.1-beta.2", regenerate_docs=False)
		self.assertEqual(len(changed), 3)
		self.assertEqual(self._versions(), ['__version__ = "1.0.1-beta.2"\nX = 1\n'] * 3)
