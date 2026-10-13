"""Extension updates from the store's update service (extensions/updates.py,
api.extensions.fetch_update, hook gunshop_extension_updates): what is offered,
how the download is checked, and that it upgrades through the upload pipeline
with no typed id — on test_extension_sideload's scratch bench, a fake source."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import frappe

from ffl_core.api import extensions as api
from ffl_core.extensions import DOCTYPE, registry, sideload, updates
from ffl_core.tests.test_extension_sideload import APP, _renamed_zip
from ffl_core.tests.test_extension_upgrade import Installed


class FakeSource:
	def __init__(self, packages: dict[str, bytes], requires_core: str = "", upper: bool = False):
		self.packages, self.requires_core, self.fetched, self.upper = packages, requires_core, [], upper

	def latest(self):
		v = max(self.packages, key=lambda x: tuple(int(n) for n in x.split(".")))
		sha = hashlib.sha256(self.packages[v]).hexdigest()
		return {APP: {"version": v, "sha256": f" {sha.upper()} " if self.upper else sha, "size": len(self.packages[v]),
			"requires_core": self.requires_core}}

	def fetch(self, ext_id, version):
		self.fetched.append((ext_id, version))
		return self.packages[version]


class Updates(Installed):
	def package(self, version: str) -> bytes:
		arc = self.tmp / f"pkg-{version}" / f"{APP}.zip"
		return (arc if arc.exists() else _renamed_zip(arc.parent, version=version)).read_bytes()

	def offer(self, source) -> dict:
		with patch.object(updates, "source", return_value=source):
			return updates.check()

	def card(self) -> dict:
		return next(r for r in api.list_extensions()["extensions"] if r["name"] == APP)

	def test_a_newer_version_is_offered_and_one_not_newer_is_cleared(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		self.assertEqual(self.offer(src), {"checked": True, "offered": {APP: "0.2.0"}})
		self.assertEqual(self.card()["update"], {"version": "0.2.0", "compatible": True, "reason": "", "requires_core": ""})
		self.assertEqual(self.offer(FakeSource({"0.1.0": self.package("0.1.0")}))["offered"], {})
		self.assertIsNone(self.card()["update"])

	def test_no_source_or_a_failing_one_changes_nothing(self):
		self.offer(FakeSource({"0.2.0": self.package("0.2.0")}))
		self.assertEqual(self.offer(None), {"checked": False, "offered": {}})

		class Broken:
			def latest(self):
				raise RuntimeError("service down")

		with patch.object(frappe, "log_error") as log:
			self.assertEqual(self.offer(Broken()), {"checked": False, "offered": {}, "error": True})
		log.assert_called_once()
		self.assertEqual(self.card()["update"]["version"], "0.2.0", "the last good answer stays")

	def test_a_version_the_core_cannot_run_is_shown_but_not_fetched(self):
		self.offer(FakeSource({"0.2.0": self.package("0.2.0")}, requires_core=">=99,<100"))
		update = self.card()["update"]
		self.assertEqual((update["version"], update["compatible"]), ("0.2.0", False))
		with self.assertRaises(frappe.ValidationError) as cm:
			api.fetch_update(APP)
		self.assertIn("needs a newer POS", str(cm.exception))

	def test_an_extension_the_update_service_may_not_replace_is_not_offered(self):
		sideload.add_manifest_entry(APP, {**registry.manifest_file()[APP], "source": "test"})
		# Set the row as registry.sync would: sync() drops this scratch app (not on the bench's app
		# list), which made the {} below hold for that reason instead of the source.
		frappe.db.set_value(DOCTYPE, APP, "source", "test", update_modified=False)
		self.assertEqual(self.offer(FakeSource({"0.2.0": self.package("0.2.0")}))["offered"], {})

	def test_update_downloads_checks_and_upgrades(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		self.offer(src)
		with patch.object(updates, "source", return_value=src):
			report = api.fetch_update(APP)
		self.assertEqual(src.fetched, [(APP, "0.2.0")])
		self.assertEqual((report["source"], report["upgrade"]["allowed"], report["filename"]),
			({"type": "update-service", "version": "0.2.0"}, True, f"{APP}-0.2.0.zip"))
		with patch.object(frappe, "enqueue") as enqueue:
			out = api.install_upload(report["sha256"], APP)            # the page sends the id; the manager confirms
		self.assertEqual((out["mode"], enqueue.call_args.args[0]), ("upgrade", "ffl_core.extensions.sideload.stage_upgrade"))
		# upgraded through the pipeline, it is recorded as from the update service — and may be updated again
		self.upgrade_to_sha(report["sha256"], "0.2.0")
		# Stored under the new name (1.12.0 and later read it too).
		self.assertEqual(self.entry()["source"], "update-service")
		self.assertEqual(self.card()["source"], "update-service")
		self.assertIn("update-service", sideload.UPGRADE_SOURCES)
		self.assertEqual(self.offer(FakeSource({"0.3.0": self.package("0.3.0")}))["offered"], {APP: "0.3.0"})

	def test_a_row_recorded_under_the_old_source_name_is_still_offered_its_update(self):
		"""extensions.json rows written before the rename say "osa-api": still the update service."""
		sideload.add_manifest_entry(APP, {**registry.manifest_file()[APP], "source": "osa-api"})
		frappe.db.set_value(DOCTYPE, APP, "source", "osa-api", update_modified=False)   # what registry.sync copies from it
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		self.assertEqual(self.offer(src)["offered"], {APP: "0.2.0"})
		self.assertEqual(self.card()["update"], {"version": "0.2.0", "compatible": True, "reason": "", "requires_core": ""})
		self.assertEqual(self.entry()["source"], "osa-api", "reading a row does not rewrite it")
		self.assertEqual(self.card()["source"], "update-service", "the page gets the normalized name")
		with patch.object(updates, "source", return_value=src):
			self.assertEqual(api.fetch_update(APP)["version"], "0.2.0", "the Update button reads it the same way")
		self.assertEqual(updates.normalize_source("osa-api"), "update-service")
		self.assertEqual([updates.normalize_source(s) for s in ("upload", "git", "baked", None)], ["upload", "git", "baked", None])

	def test_the_new_name_is_what_is_stored(self):
		"""Whichever name a saved report carries, extensions.json gets the new one
		(updates.STORED_SOURCE_TYPE); rows written before keep the legacy name and read as the new."""
		sha = "ab" * 32
		self.assertEqual(updates.STORED_SOURCE_TYPE, "update-service")
		for saved in ("osa-api", "update-service"):
			with patch.object(sideload, "_stored_report", return_value={"source": {"type": saved, "version": "0.2.0"}}):
				self.assertEqual(sideload._source_entry(sha), {"source": "update-service", "version": "0.2.0", "sha256": sha})

	def test_a_legacy_source_may_be_upgraded_from_an_upload(self):
		"""sideload's upgrade check normalizes too: an update-service row is replaceable in place."""
		sideload.add_manifest_entry(APP, {**registry.manifest_file()[APP], "source": "osa-api"})
		up = sideload.inspect(self.upload_version("0.2.0"))["upgrade"]
		self.assertTrue(up["allowed"], up)

	def upgrade_to_sha(self, sha: str, version: str) -> None:
		sideload.stage_upgrade(sha, APP, "Administrator")
		sideload.begin_upgrade(APP)
		self.migrate(version)
		self.assertTrue(sideload.finish_upgrade(APP, 1, "")["ok"])

	def test_a_download_that_is_not_what_was_announced_is_refused(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		self.offer(src)
		src.packages["0.2.0"] = self.package("0.2.0") + b"tampered"
		with patch.object(updates, "source", return_value=src), self.assertRaises(frappe.ValidationError) as cm:
			api.fetch_update(APP)
		self.assertIn("not the package the update service announced", str(cm.exception))

	def test_a_package_from_another_publisher_is_not_an_update(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		self.offer(src)
		frappe.db.set_value(DOCTYPE, APP, "publisher_id", "acme", update_modified=False)   # a sideloaded fork with the id
		with patch.object(updates, "source", return_value=src), self.assertRaises(frappe.ValidationError) as cm:
			api.fetch_update(APP)
		self.assertIn("not an update of it", str(cm.exception))

	def test_the_offer_and_the_package_agree_as_versions_not_spellings(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		self.offer(src)
		frappe.db.set_value(DOCTYPE, APP, "latest_version", "0.2.0.0", update_modified=False)
		with patch.object(updates, "source", return_value=src), patch.object(src, "fetch", return_value=src.packages["0.2.0"]):
			self.assertEqual(api.fetch_update(APP)["version"], "0.2.0")

	def test_the_packages_own_requires_core_decides(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})            # announced as fine for this core
		self.offer(src)
		with patch.object(updates, "source", return_value=src), \
				patch.object(api, "check_compat", side_effect=[(True, ""), (False, "needs core >=99")]), \
				self.assertRaises(frappe.ValidationError) as cm:
			api.fetch_update(APP)
		self.assertIn("needs a newer POS", str(cm.exception))

	def test_a_source_for_one_publisher_offers_only_its_extensions(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")})
		src.publisher_id = "osa"
		frappe.db.set_value(DOCTYPE, APP, "publisher_id", "acme", update_modified=False)
		self.assertEqual(self.offer(src)["offered"], {})
		frappe.db.set_value(DOCTYPE, APP, "publisher_id", "osa", update_modified=False)
		self.assertEqual(self.offer(src)["offered"], {APP: "0.2.0"})

	def test_an_extension_that_can_no_longer_be_replaced_shows_no_offer(self):
		self.offer(FakeSource({"0.2.0": self.package("0.2.0")}))
		frappe.db.set_value(DOCTYPE, APP, "source", "baked", update_modified=False)
		self.assertIsNone(self.card()["update"])

	def test_the_announced_digest_is_read_however_it_is_written(self):
		src = FakeSource({"0.2.0": self.package("0.2.0")}, upper=True)
		self.offer(src)
		with patch.object(updates, "source", return_value=src):
			self.assertEqual(api.fetch_update(APP)["version"], "0.2.0")

	def test_refresh_says_when_the_check_failed(self):
		class Broken:
			def latest(self):
				raise RuntimeError("service down")

		with patch.object(updates, "source", return_value=Broken()), patch.object(frappe, "log_error"):
			self.assertTrue(api.refresh()["updates"]["error"])

	def test_nothing_newer_or_a_busy_store_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			api.fetch_update(APP)                                   # nothing offered
		self.offer(FakeSource({"0.2.0": self.package("0.2.0")}))
		frappe.db.set_value(DOCTYPE, APP, "job_state", "Upgrading", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			api.fetch_update(APP)

	def test_an_upload_still_needs_its_typed_id(self):
		sha = self.upload_version("0.2.0")
		sideload.inspect(sha)
		with patch.object(frappe, "enqueue") as enqueue, self.assertRaises(frappe.ValidationError):
			api.install_upload(sha, "")
		enqueue.assert_not_called()


class Source(Installed):
	def test_the_first_provider_with_a_source_wins_and_a_broken_one_is_skipped(self):
		marker = object()

		def boom():
			raise RuntimeError("provider bug")

		hooks = ["a.none", "a.boom", "a.good"]
		attrs = {"a.none": lambda: None, "a.boom": boom, "a.good": lambda: marker}
		with patch.object(frappe, "get_hooks", return_value=hooks), \
				patch.object(frappe, "get_attr", side_effect=attrs.__getitem__), \
				patch.object(frappe, "log_error") as log:
			self.assertIs(updates.source(), marker)
		log.assert_called_once()

	def test_newer_reads_versions_and_refuses_what_it_cannot_read(self):
		self.assertTrue(updates.newer("1.10.0", "1.9.2"))
		self.assertFalse(updates.newer("1.3.1", "1.3.1"))
		self.assertFalse(updates.newer("not a version", "1.0"))
		self.assertFalse(updates.newer("", "1.0"))
