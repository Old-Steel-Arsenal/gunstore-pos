"""POS Extension rows ⇄ what is actually on this bench and this site.

One row per extension app present on the bench (``sites/apps.txt`` minus the
five core apps, only apps whose hooks declare ``gunshop_extension``). The row
carries the manifest snapshot, whether the app is installed on *this* site,
whether it is compatible with this core, and the store's on/off switch.

``sync()`` runs after every migrate and behind the Extensions page's Refresh:
it never deletes data, never installs anything — it only makes the registry
describe reality (inside a migrate it also records an upgrade that migrate
completed, sideload.note_migrate_succeeded). An extension that vanished from
the bench (not in apps.txt any more) loses its row; one whose ``requires_core``
no longer matches — or whose hooks cannot be read at all — is marked
incompatible with a reason and kept. The store's
switch (``enabled``) is never written here: it is the store's intent, and
``effective`` (enabled ∧ installed ∧ compatible) is what acts
 A job that has been "running" longer than its timeout is
marked Failed so the page never stays stuck.
"""
from __future__ import annotations

import json
from pathlib import Path

import frappe
import frappe.utils
from frappe.utils import now_datetime

from ffl_core.extensions import DOCTYPE, EXTENSIONS_DOC, clear_enabled_cache
from ffl_core.extensions.jobs import BUSY_STATES

CORE_APPS = frozenset({"frappe", "erpnext", "ffl_core", "ffl_integrations", "ffl_woo_sync"})
MANIFEST_HOOK = "gunshop_extension"
# What sites/extensions.json may tell us about an id; anything
# else about the extension comes from its own hooks.
_MANIFEST_FILE_KEYS = ("source", "installed_at", "installed_by")


def core_version() -> str:
	from ffl_core import __version__
	return __version__


def sites_dir() -> Path:
	"""The sites/ directory (one seam: tests point it at a scratch bench)."""
	return Path(frappe.local.sites_path).resolve()


def bench_root() -> Path:
	return sites_dir().parent


def ext_dir(app: str | None = None) -> Path:
	"""``apps-ext`` on the bench (the volume in production), or one app in it."""
	d = bench_root() / "apps-ext"
	return d / app if app else d


def manifest_of(app: str) -> dict | None:
	"""The app's ``gunshop_extension`` dict, or None when it is not an extension.

	Read straight off the hooks module: ``frappe.get_hooks`` wraps every leaf
	of a dict-valued hook in a list ("Value for Title cannot be a list").
	When the module cannot be imported *in this container* but the id is in
	sites/extensions.json, read the literal off the file on the volume instead:
	a freshly staged upload is wired only in the worker that staged it until
	the agent restarts everything, and the page must still
	describe it correctly meanwhile.
	"""
	try:
		m = getattr(frappe.get_module(f"{app}.hooks"), MANIFEST_HOOK, None)
	except Exception:
		# Only for the staging window: declared, not yet installed here. An
		# *installed* app that no longer imports keeps the "cannot be read" rule.
		m = _manifest_from_file(app) if app in manifest_file() and app not in installed_apps() else None
	return dict(m) if isinstance(m, dict) and m else None


def _hooks_literal(app: str, name: str):
	"""A top-level literal assignment out of apps-ext/<app>/<app>/hooks.py, without importing it (lint's parser)."""
	import ast

	from ffl_core.extensions.lint import _hooks_assignments, _literal

	path = ext_dir(app) / app / "hooks.py"
	try:
		node = _hooks_assignments(ast.parse(path.read_text(encoding="utf-8"))).get(name)
	except (OSError, SyntaxError):
		return None
	return _literal(node) if node is not None else None


def _manifest_from_file(app: str) -> dict | None:
	m = _hooks_literal(app, MANIFEST_HOOK)
	return m if isinstance(m, dict) else None


def installed_apps() -> set[str]:
	"""Apps installed on this site (one seam for tests; frappe's own is not patchable)."""
	return set(frappe.get_installed_apps())


def bench_apps() -> list[str]:
	"""Non-core apps on this bench, in apps.txt order — the only thing that
	decides whether a registry row may be deleted."""
	return [a for a in frappe.get_all_apps() if a not in CORE_APPS]


def candidates() -> list[str]:
	"""Extension apps on this bench: non-core apps whose hooks declare a manifest."""
	return [a for a in bench_apps() if manifest_of(a) is not None]


def version_of(app: str, manifest: dict) -> str:
	try:
		return str(getattr(frappe.get_module(app), "__version__", "") or manifest.get("version") or "")
	except Exception:
		return str(manifest.get("version") or "")


def check_compat(requires_core: str | None, core: str | None = None) -> tuple[bool, str]:
	"""(compatible, reason). No range declared counts as compatible — lint's job, not ours."""
	if not requires_core:
		return True, ""
	from packaging.specifiers import InvalidSpecifier, SpecifierSet
	from packaging.version import InvalidVersion, Version

	core = core or core_version()
	try:
		spec = SpecifierSet(str(requires_core))
		# A store on 1.1.0-beta.1 has the 1.1 SDK: compare the base release,
		# otherwise every ">=1.1.0" extension is locked out for the whole beta
		# (PEP 440 orders 1.1.0b1 below 1.1.0).
		ok = spec.contains(Version(Version(core).base_version), prereleases=True)
	except (InvalidSpecifier, InvalidVersion) as e:
		return False, f"requires_core is not a valid version range ({e})"
	if ok:
		return True, ""
	return False, f"needs core {requires_core}, this store runs {core}"


def manifest_file() -> dict[str, dict]:
	"""``sites/extensions.json`` → {id: entry}; missing or malformed → {}."""
	path = sites_dir() / "extensions.json"
	try:
		data = json.loads(path.read_text())
	except (OSError, ValueError):
		return {}
	if not isinstance(data, dict) or data.get("schema") != 1:
		return {}
	exts = data.get("extensions") or {}
	return exts if isinstance(exts, dict) else {}


def _as_datetime(value):
	"""extensions.json writes ISO 8601 (with a T and a zone); the row stores a
	naive local datetime. Parse once so the change check compares like with like."""
	if not value:
		return None
	try:
		dt = frappe.utils.get_datetime(str(value))
	except Exception:
		return None
	if dt is not None and dt.tzinfo is not None:
		from zoneinfo import ZoneInfo
		dt = dt.astimezone(ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)
	return dt


def describe(app: str, manifest: dict, *, installed: set[str], meta: dict | None) -> dict:
	"""The registry row an extension should have right now (nothing about the switch)."""
	compatible, reason = check_compat(manifest.get("requires_core"))
	meta = meta or {}
	try:
		description = getattr(frappe.get_module(f"{app}.hooks"), "app_description", "")
	except Exception:
		description = _hooks_literal(app, "app_description") or ""
	return {
		"title": manifest.get("title") or app,
		"publisher": manifest.get("publisher") or "",
		"publisher_id": manifest.get("publisher_id") or "",
		"version": version_of(app, manifest),
		"requires_core": manifest.get("requires_core") or "",
		"description": str(description or ""),
		"permissions": json.dumps(list(manifest.get("permissions") or [])),
		"manifest": json.dumps(manifest, default=str),
		"homepage": manifest.get("homepage") or "",
		"support": manifest.get("support") or "",
		"license_required": 1 if manifest.get("license_required") else 0,
		# sites/extensions.json is the only source of truth about where an
		# extension came from; no entry = we do not know (the page paints it
		# "Sideloaded"). "baked" is written by the image build, never assumed.
		"source": str(meta.get("source") or "unknown"),
		"installed_at": _as_datetime(meta.get("installed_at")),
		"installed_by": meta.get("installed_by") or "",
		"installed_on_site": 1 if app in installed else 0,
		"compatible": 1 if compatible else 0,
		"incompatible_reason": reason,
	}


def sync_app(app: str, *, installed: set[str] | None = None, meta: dict | None = None) -> str:
	"""Create or update one row; returns "created" / "updated" / "unchanged" / "skipped"."""
	manifest = manifest_of(app)
	if manifest is None:
		return "skipped"
	if installed is None:
		installed = installed_apps()
	if meta is None:
		meta = manifest_file().get(app)
	want = describe(app, manifest, installed=installed, meta=meta)

	if frappe.db.exists(DOCTYPE, app):
		doc = frappe.get_doc(DOCTYPE, app)
		was_installed = bool(doc.installed_on_site)
		changed = False
		for k, v in want.items():
			have = doc.get(k)
			if k == "installed_at":              # both datetimes now; compare to the second
				have = str(have)[:19] if have else None
				v = str(v)[:19] if v else None
			if (have or None) != (v or None):
				doc.set(k, v)
				changed = True
		if want["installed_on_site"] and not was_installed and want["compatible"]:
			doc.enabled = 1                      # freshly installed → on
			changed = True
		if not changed:
			return "unchanged"
		doc.last_synced = now_datetime()
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		return "updated"

	doc = frappe.get_doc({
		"doctype": DOCTYPE, "extension_id": app,
		"enabled": 1 if want["compatible"] else 0,
		"last_synced": now_datetime(), **want,
	})
	doc.insert(ignore_permissions=True)
	return "created"


JOB_TIMEOUT_MINUTES = 30      # the long-queue jobs run with timeout=1800


def unregister_unreadable(app: str, *, installed: set[str]) -> str:
	"""An app on the bench whose hooks cannot be read: keep the row (if any),
	mark it incompatible and say why. Never a deletion — the code is still there."""
	if not frappe.db.exists(DOCTYPE, app):
		return "skipped"
	doc = frappe.get_doc(DOCTYPE, app)
	reason = "its hooks.py could not be read or has no gunshop_extension manifest"
	if doc.compatible or doc.incompatible_reason != reason:
		doc.compatible, doc.incompatible_reason = 0, reason
		doc.installed_on_site = 1 if app in installed else 0
		doc.last_synced = now_datetime()
		doc.save(ignore_permissions=True)
		return "updated"
	return "unchanged"


def reset_stale_jobs() -> list[str]:
	"""A row busy (jobs.BUSY_STATES) longer than the job timeout is a dead worker
	or a store that never restarted, not a job: mark it Failed so the page and
	the endpoints unlock, and consume its own restart request if one is left.
	Then no upgrade entry stays ``staged`` under a row that is no longer busy (an
	interrupted upgrade's intent file excepted: that one is the self-hosting guide's)."""
	from frappe.utils import add_to_date

	from ffl_core.extensions.jobs import set_state
	from ffl_core.extensions.sideload import intent_path, mark_done, migrate_note, pending_request, rollback_hint, set_upgrade, upgrade_ask

	cutoff = add_to_date(now_datetime(), minutes=-JOB_TIMEOUT_MINUTES)
	stuck = frappe.get_all(DOCTYPE, filters={
		"job_state": ["in", BUSY_STATES], "job_started_at": ["<", cutoff],
	}, fields=["name", "job_state"])
	for row in stuck:
		record = (manifest_file().get(row.name) or {}).get("upgrade")
		staged = isinstance(record, dict) and record.get("state") == "staged"                   # the entry stage_upgrade wrote
		interrupted = intent_path(row.name).exists()                                             # stage_upgrade died moving trees
		if row.job_state == "Upgrading":
			message = f"The upgrade did not finish within 30 minutes. {migrate_note(record)} {upgrade_ask(record)} {rollback_hint(row.name)}"
		elif row.job_state == "Staged" and interrupted:
			message = ("The upgrade was interrupted while the new code was being put in place, so it never finished. "
				"Ask your developer to put the previous version back. " + rollback_hint(row.name))
		elif row.job_state == "Staged" and staged:
			message = (f"The store did not restart within 30 minutes, so the upgrade never finished. {migrate_note(record)} "
				f"{upgrade_ask(record)} {rollback_hint(row.name)}")
		elif row.job_state == "Staged":
			message = ("The store did not restart within 30 minutes, so the install never finished. The code is on the server; "
				f"ask your developer to check the ext-agent timer ({EXTENSIONS_DOC}). Install becomes possible once the store has restarted.")
		else:
			message = "The job did not finish within 30 minutes (the background worker may have restarted). Press Refresh and try again."
		req = pending_request()
		if req and req.get("app") == row.name:                     # only this row's request, never a newer one
			mark_done(row.name, False, message)
		set_state(row.name, "Failed", message)
	# An upgrade entry still staged while its row is not busy is one this sweep or finish_upgrade could not
	# record (extensions.json not writable then): record it failed — the unfinished upgrade it is, which
	# docs/self-hosting.md#extensions rolls back before the id may be uploaded again.
	busy = set(frappe.get_all(DOCTYPE, filters={"job_state": ["in", BUSY_STATES]}, pluck="name"))
	for app_id, entry in manifest_file().items():
		upgrade = entry.get("upgrade") if isinstance(entry, dict) else None
		if isinstance(upgrade, dict) and upgrade.get("state") == "staged" and app_id not in busy and not intent_path(app_id).exists():
			try:
				set_upgrade(app_id, state="failed")
			except Exception:
				frappe.log_error(title=f"extension {app_id}: recording the failed upgrade in extensions.json failed")
	return [r.name for r in stuck]


def switch_snapshot() -> dict[str, int]:
	"""{id: enabled} for every registry row — taken before a migrate by the
	deploy scripts (``bench execute ffl_core.extensions.registry.switch_snapshot``)."""
	return {r.name: int(r.enabled) for r in frappe.get_all(DOCTYPE, fields=["name", "enabled"])}


def assert_switches(snapshot) -> dict:
	"""After a migrate: every switch the snapshot knew must still read the same.
	A flipped one is put back and reported (a 1→0 flip nobody
	could explain; the belt for it). Incompatible rows too: nothing but the store
	writes ``enabled`` — incompatibility is what ``effective``
	reads. Returns {"restored": [...], "missing": [...]}."""
	if isinstance(snapshot, str):
		snapshot = json.loads(snapshot or "{}")
	from ffl_core.utils import commit

	now = switch_snapshot()
	restored, missing = [], []
	for app, enabled in (snapshot or {}).items():
		if app not in now:
			missing.append(app)
		elif now[app] != int(enabled):
			frappe.db.set_value(DOCTYPE, app, "enabled", int(enabled), update_modified=False)
			restored.append(app)
	if restored:
		commit()
		clear_enabled_cache()
		frappe.log_error(title="extension switches flipped by migrate, restored: " + ", ".join(restored))
	return {"restored": restored, "missing": missing}


def on_app_change(app_name: str | None = None) -> None:
	"""hooks after_app_install / after_app_uninstall (frappe passes the app
	name): keep the registry in step with the site, so a CLI `bench
	install-app` shows up installed without a manual sync. Best effort — the
	install itself must not fail on a registry hiccup; the Extensions section's explicit
	sync stays as the belt to these braces."""
	if not frappe.db.exists("DocType", DOCTYPE):
		return
	try:
		sync()
	except Exception:
		frappe.log_error(title=f"extension registry: sync after app change ({app_name}) failed")


def sync() -> dict:
	"""Make the registry match the bench and the site. Returns a summary."""
	installed = installed_apps()
	meta = manifest_file()
	on_bench = bench_apps()
	result = {"created": [], "updated": [], "unchanged": [], "removed": [], "reset": []}
	for app in on_bench:
		if manifest_of(app) is None:
			outcome = unregister_unreadable(app, installed=installed)
			if outcome != "skipped":
				result[outcome].append(app)
			continue
		result[sync_app(app, installed=installed, meta=meta.get(app))].append(app)
	for stale in frappe.get_all(DOCTYPE, pluck="name"):
		# Gone from the bench AND not installed on this site. An installed app
		# cannot vanish without an uninstall; if apps.txt momentarily disagrees
		# (a bench being rewritten mid-migrate) the row must survive, or the
		# store's switch would be rebuilt from defaults.
		if stale not in on_bench and stale not in installed:
			frappe.delete_doc(DOCTYPE, stale, ignore_permissions=True, force=True)
			result["removed"].append(stale)
	if frappe.flags.in_migrate:                   # after_migrate: the sweep below reads what this migrate did
		from ffl_core.extensions.sideload import note_migrate_succeeded
		note_migrate_succeeded()
	result["reset"] = reset_stale_jobs()
	clear_enabled_cache()
	return result
