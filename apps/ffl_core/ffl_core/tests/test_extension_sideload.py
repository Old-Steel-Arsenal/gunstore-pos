"""Sideload pipeline: the staging job against a scratch
bench (temp sites/ + apps-ext/), a fake replay script, and the real registry.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.extensions import DOCTYPE, registry, sideload

EXT_HELLO = Path(__file__).resolve().parents[4] / "deploy" / "prod" / "tests" / "ext_hello"
APP = "ext_uptest"


def _renamed_zip(dest: Path, app: str = APP, title: str = "Ext Uptest", *, version: str = "0.1.0") -> Path:
	"""ext_hello with a new id at ``version`` (with its release note), as the zip a developer would hand over."""
	root = dest / app
	shutil.copytree(EXT_HELLO, root, ignore=shutil.ignore_patterns("__pycache__"))
	(root / "ext_hello").rename(root / app)
	for p in root.rglob("*"):
		if p.is_file() and p.suffix in (".py", ".toml", ".txt", ".md", ".js", ".json"):
			t = p.read_text()
			p.write_text(t.replace("ext_hello", app).replace("Ext Hello", title))
	for p in list(root.rglob("*ext_hello*")):
		p.rename(p.with_name(p.name.replace("ext_hello", app)))
	pkg = root / app
	for f, pattern, repl in ((pkg / "__init__.py", r'__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"'),
			(pkg / "hooks.py", r'"version":\s*"[^"]*"', f'"version": "{version}"')):
		text, n = re.subn(pattern, repl, f.read_text(), count=1)
		assert n == 1, f"no version to set in {f}"
		f.write_text(text)
	note = pkg / "change_log" / f"v{version.split('.')[0]}" / f"v{version.replace('.', '_')}.md"
	note.parent.mkdir(parents=True, exist_ok=True)
	note.write_text(f"### Version {version}\n\n- Release {version} of the test extension.\n")
	arc = dest / f"{app}.zip"
	with zipfile.ZipFile(arc, "w") as z:
		for p in sorted(root.rglob("*")):
			if p.is_file():
				z.write(p, str(p.relative_to(root)))
	return arc


class ScratchBench(FrappeTestCase):
	"""sites/ and apps-ext/ under a temp dir; the real DB and registry."""

	def setUp(self):
		super().setUp()
		self.tmp = Path(tempfile.mkdtemp(prefix="sideload-"))
		self.addCleanup(shutil.rmtree, self.tmp, True)
		(self.tmp / "sites").mkdir()
		(self.tmp / "apps-ext").mkdir()
		(self.tmp / "apps").mkdir()
		for name, value in (("sites_dir", self.tmp / "sites"), ("bench_root", self.tmp)):
			p = patch.object(registry, name, return_value=value)
			p.start(); self.addCleanup(p.stop)
		self.replay = self.tmp / "replay.sh"
		self.replay.write_text("#!/bin/bash\necho replayed > \"$BENCH_PATH/replay.log\"\n[ -z \"$REPLAY_FAIL\" ] || { echo boom; exit 1; }\n")
		p = patch.object(sideload, "replay_script", return_value=self.replay)
		p.start(); self.addCleanup(p.stop)
		self.addCleanup(lambda: frappe.delete_doc(DOCTYPE, APP, force=True, ignore_permissions=True) if frappe.db.exists(DOCTYPE, APP) else None)
		# uploads go under the real site's private dir; keep them out of the way
		up = patch.object(sideload, "upload_dir", return_value=self.tmp / "uploads")
		up.start(); self.addCleanup(up.stop)
		(self.tmp / "uploads").mkdir()

	def upload(self) -> str:
		arc = _renamed_zip(self.tmp / "src")
		return sideload.save_upload(arc.read_bytes())


class Inspect(ScratchBench):
	def test_inspect_reports_id_permissions_and_placement(self):
		sha = self.upload()
		out = sideload.inspect(sha)
		self.assertTrue(out["ok"], out["findings"])
		self.assertEqual((out["app_id"], out["version"]), (APP, "0.1.0"))
		self.assertIn("pos_ui", out["permissions"])
		self.assertEqual((out["on_bench"], out["installed_on_site"], out["baked"]), (False, False, False))

	def test_unsafe_zip_is_thrown_out_and_deleted(self):
		bad = self.tmp / "bad.zip"
		with zipfile.ZipFile(bad, "w") as z:
			z.writestr("../evil.py", "x")
		sha = sideload.save_upload(bad.read_bytes())
		with self.assertRaises(frappe.ValidationError):
			sideload.inspect(sha)
		self.assertFalse((self.tmp / "uploads" / f"{sha}.zip").exists())


class Stage(ScratchBench):
	def test_stage_moves_code_writes_manifest_replays_and_requests_restart(self):
		sha = self.upload()
		sideload.stage(sha, APP, "Administrator")
		self.assertTrue((self.tmp / "apps-ext" / APP / APP / "hooks.py").exists())
		manifest = json.loads((self.tmp / "sites" / "extensions.json").read_text())
		self.assertEqual(manifest["extensions"][APP]["source"], "upload")
		self.assertEqual(manifest["extensions"][APP]["sha256"], sha)
		self.assertEqual((self.tmp / "replay.log").read_text().strip(), "replayed")
		req = json.loads((self.tmp / "sites" / sideload.REQUEST_FILE).read_text())
		self.assertEqual((req["action"], req["app"], req["site"]), ("install", APP, frappe.local.site))
		row = frappe.db.get_value(DOCTYPE, APP, ["job_state", "installed_on_site", "title", "version"], as_dict=True)
		self.assertEqual((row.job_state, row.installed_on_site, row.title, row.version), ("Staged", 0, "Ext Uptest", "0.1.0"))
		self.assertFalse((self.tmp / "uploads" / f"{sha}.zip").exists(), "the upload is consumed")
		# the registry described it without importing it (AST fallback)
		self.assertEqual(registry.manifest_of(APP)["id"], APP)

	def test_replay_failure_rolls_everything_back(self):
		sha = self.upload()
		with patch.dict("os.environ", {"REPLAY_FAIL": "1"}):
			with self.assertRaises(RuntimeError) as cm:
				sideload.stage(sha, APP, "Administrator")
		self.assertIn("ext-replay failed", str(cm.exception), str(cm.exception))
		self.assertFalse((self.tmp / "apps-ext" / APP).exists())
		self.assertNotIn(APP, json.loads((self.tmp / "sites" / "extensions.json").read_text())["extensions"])
		self.assertFalse((self.tmp / "sites" / sideload.REQUEST_FILE).exists())
		self.assertFalse(frappe.db.exists(DOCTYPE, APP))
		self.assertEqual(sideload.failure_for(sha)["app"], APP)

	def test_a_half_copied_tree_is_removed_on_failure(self):
		sha = self.upload()
		with patch.object(sideload.shutil, "copytree", side_effect=OSError("disk full")):
			with self.assertRaises(OSError):
				sideload.stage(sha, APP, "Administrator")
		self.assertFalse((self.tmp / "apps-ext" / APP).exists())

	def test_a_re_upload_clears_the_old_failure_marker(self):
		# The same archive's bytes again: a second zip of the tree carries its files'
		# mtimes, which differ across a 2-second boundary (a different sha).
		data = _renamed_zip(self.tmp / "src").read_bytes()
		sha = sideload.save_upload(data)
		sideload._remember_failure(sha, APP, "old")
		self.assertIsNotNone(sideload.failure_for(sha))
		self.assertEqual(sideload.save_upload(data), sha)
		self.assertIsNone(sideload.failure_for(sha))

	def test_an_encrypted_or_corrupt_zip_is_a_user_error_not_a_500(self):
		bad = self.tmp / "corrupt.zip"
		good = _renamed_zip(self.tmp / "src2").read_bytes()
		bad.write_bytes(good[: len(good) // 2])                    # truncated central directory
		sha = sideload.save_upload(bad.read_bytes())
		with self.assertRaises(frappe.ValidationError):
			sideload.inspect(sha)

	def test_refuses_an_id_already_on_the_bench(self):
		sha = self.upload()
		(self.tmp / "apps-ext" / APP).mkdir()
		with self.assertRaises(RuntimeError) as cm:
			sideload.stage(sha, APP, "Administrator")
		self.assertIn("already on this server", str(cm.exception))


class InstallGuard(ScratchBench):
	def test_install_on_site_refuses_an_app_this_container_cannot_import(self):
		from ffl_core.api import extensions as api

		sha = self.upload()
		sideload.stage(sha, APP, "Administrator")                    # row: Staged, not importable here
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			api.install_on_site(APP)                                 # busy (Staged)
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Failed", update_modified=False)
		sideload.mark_done(APP, False, "the restart failed")         # the agent consumed the request
		with self.assertRaises(frappe.ValidationError) as cm:
			api.install_on_site(APP)
		self.assertIn("has not restarted", str(cm.exception))

	def test_manifest_fallback_only_while_not_installed(self):
		sha = self.upload()
		sideload.stage(sha, APP, "Administrator")
		self.assertIsNotNone(registry.manifest_of(APP))
		with patch.object(registry, "installed_apps", return_value={APP}):
			self.assertIsNone(registry.manifest_of(APP), "installed but not importable = the unreadable rule, not the fallback")


class References(ScratchBench):
	def test_a_caller_shaped_reference_is_refused_everywhere(self):
		for fn in (sideload.upload_path, sideload.inspected, sideload.failure_for):
			with self.assertRaises(frappe.ValidationError):
				fn("../../etc/passwd")

	def test_stale_sweep_only_consumes_its_own_request(self):
		from frappe.utils import add_to_date, now_datetime

		doc = frappe.get_doc({"doctype": DOCTYPE, "extension_id": "ext_stalea", "title": "Stale A", "job_state": "Staged",
			"enabled": 0, "compatible": 1, "job_started_at": add_to_date(now_datetime(), minutes=-45)}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc(DOCTYPE, doc.name, force=True, ignore_permissions=True))
		sideload.request_restart("install", "ext_newer_b")           # B staged after A got stuck
		with patch("ffl_core.extensions.jobs.commit"):
			registry.reset_stale_jobs()
		self.assertEqual(sideload.pending_request()["app"], "ext_newer_b", "B's request survives A's timeout")


class Finish(ScratchBench):
	def test_finish_install_marks_done_and_clears_the_request(self):
		sideload.request_restart("install", APP)
		with patch("ffl_core.extensions.jobs.install_on_site") as job:
			out = sideload.finish_install(APP)
		job.assert_called_once_with(APP)
		self.assertTrue(out["ok"])
		self.assertFalse((self.tmp / "sites" / sideload.REQUEST_FILE).exists())
		self.assertTrue(sideload.last_done()["ok"])

	def test_finish_install_failure_is_recorded(self):
		sideload.request_restart("install", APP)
		with patch("ffl_core.extensions.jobs.install_on_site", side_effect=RuntimeError("no such app")):
			out = sideload.finish_install(APP)
		self.assertFalse(out["ok"])
		self.assertIn("no such app", sideload.last_done()["message"])


class Switches(FrappeTestCase):
	def test_assert_switches_restores_a_flipped_switch(self):
		snap = registry.switch_snapshot()
		if not snap:
			self.skipTest("no extension rows on this site")
		app, enabled = next(iter(snap.items()))
		frappe.db.set_value(DOCTYPE, app, "enabled", 1 - enabled, update_modified=False)
		out = registry.assert_switches(json.dumps(snap))         # ffl_core.utils.commit is a no-op in tests
		self.assertEqual(out["restored"], [app])
		self.assertEqual(int(frappe.db.get_value(DOCTYPE, app, "enabled")), enabled)
		self.assertEqual(registry.assert_switches(snap)["restored"], [])

	def test_an_incompatible_row_is_restored_too(self):
		"""Nothing but the store writes ``enabled``, so a 1→0 flip across a
		migrate is never legitimate — incompatibility is what ``effective`` reads, and it lifts on its own."""
		doc = frappe.get_doc({"doctype": DOCTYPE, "extension_id": "ext_oldcore", "title": "Old", "enabled": 0, "compatible": 0,
			"incompatible_reason": "needs core >=9"}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc(DOCTYPE, doc.name, force=True, ignore_permissions=True))
		out = registry.assert_switches({doc.name: 1})              # the snapshot said on
		self.assertEqual(out["restored"], [doc.name])
		self.assertEqual(int(frappe.db.get_value(DOCTYPE, doc.name, "enabled")), 1)


class StaleStaged(ScratchBench):
	def test_a_staged_row_older_than_the_timeout_fails_with_the_agent_hint(self):
		from frappe.utils import add_to_date, now_datetime

		doc = frappe.get_doc({"doctype": DOCTYPE, "extension_id": "ext_stale", "title": "Stale", "job_state": "Staged",
			"enabled": 0, "compatible": 1, "job_started_at": add_to_date(now_datetime(), minutes=-45)}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc(DOCTYPE, doc.name, force=True, ignore_permissions=True))
		sideload.request_restart("install", doc.name)
		with patch("ffl_core.extensions.jobs.commit"):
			self.assertIn(doc.name, registry.reset_stale_jobs())
		msg = frappe.db.get_value(DOCTYPE, doc.name, "job_message")
		self.assertIn("ext-agent", msg)
		self.assertNotIn("press Install", msg)
		self.assertIsNone(sideload.pending_request(), "the dangling request is consumed so the page stops polling")
		self.assertFalse(sideload.last_done()["ok"])
