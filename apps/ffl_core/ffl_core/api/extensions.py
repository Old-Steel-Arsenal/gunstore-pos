"""Extensions page (/app/extensions) — System Manager only.

Reads the POS Extension registry, flips the store switch, and starts the
site-level install / uninstall jobs (ffl_core.extensions.jobs). Progress is
polled through list_extensions (job_state on the row). Nothing here touches
the bench: putting code on the apps-ext volume is docs/self-hosting.md#extensions / the phase-2
agent.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint

from ffl_core.extensions import DOCTYPE, EXTENSIONS_DOC, clear_enabled_cache, effective
from ffl_core.extensions.jobs import BUSY_STATES
from ffl_core.extensions import updates
from ffl_core.extensions.registry import check_compat, core_version, sync

_ROW_FIELDS = (
	"name", "title", "version", "enabled", "installed_on_site", "compatible", "incompatible_reason",
	"publisher", "publisher_id", "license_required", "source", "installed_at", "installed_by",
	"description", "requires_core", "homepage", "support", "permissions", "manifest",
	"job_state", "job_message", "last_synced", "modified", "latest_version", "latest_requires_core",
)
HIGH_RISK_PREFIXES = ("network:", "compliance_read", "public_endpoint", "first_party")


def _rows() -> list[dict]:
	rows = frappe.get_all(DOCTYPE, fields=list(_ROW_FIELDS), order_by="title asc")
	for r in rows:
		r["source"] = updates.normalize_source(r.get("source"))
		try:
			r["permissions"] = json.loads(r.get("permissions") or "[]")
		except ValueError:
			r["permissions"] = []
		try:
			r["manifest"] = json.loads(r.get("manifest") or "{}")
		except ValueError:
			r["manifest"] = {}
		r["high_risk"] = _high_risk(r["permissions"])
		r["effective"] = effective(r)
		r["update"] = _update_of(r)
	return rows


def _update_of(row: dict) -> dict | None:
	"""The newer version the update service offers (extensions/updates.check),
	and whether this POS can take it — None when there is nothing newer. ``row.source`` is
	already normalized (updates.normalize_source)."""
	from ffl_core.extensions.sideload import UPGRADE_SOURCES

	if row.get("source") not in UPGRADE_SOURCES or not updates.newer(row.get("latest_version"), row.get("version")):
		return None
	ok, reason = check_compat(row.get("latest_requires_core") or None)
	return {"version": row["latest_version"], "compatible": ok, "reason": reason,
		"requires_core": row.get("latest_requires_core") or ""}


def _high_risk(permissions: list[str]) -> list[str]:
	return [p for p in permissions if p.startswith(HIGH_RISK_PREFIXES)]


def _payload() -> dict:
	from ffl_core.extensions.sideload import last_done, pending_request

	rows = _rows()
	return {
		"core_version": core_version(),
		"extensions": rows,
		"incompatible": sum(1 for r in rows if r["installed_on_site"] and not r["compatible"]),
		"busy": sum(1 for r in rows if r["job_state"] in BUSY_STATES),
		"busy_states": list(BUSY_STATES),
		"restart": {"pending": pending_request(), "done": last_done()},
		# Whether an update source is set up here (extensions/updates.source): the page's
		# footer says "Update service not configured" when not.
		"update_service": updates.source() is not None,
	}


@frappe.whitelist(methods=["GET"])
def list_extensions():
	"""The registry as the Extensions page shows it.

	Returns:
	    dict: {"core_version": str, "extensions": [row...], "incompatible": int, "busy": int,
	    "busy_states": [str], "restart": {"pending": dict | None, "done": dict | None},
	    "update_service": bool}
	    — each row is a POS Extension with ``permissions`` / ``manifest``
	    decoded, ``high_risk`` (the permissions the page paints red) and
	    ``effective`` (bool: switched on AND installed here AND compatible —
	    what the extension actually does; ``enabled`` is only the store's
	    switch) and ``update`` (None, or {"version", "compatible", "reason",
	    "requires_core"}: the newer version the update service offers).
	    ``busy_states``: the job states that lock a row. ``update_service``: an
	    update source is set up on this site (else no update is ever offered).
	"""
	frappe.only_for("System Manager")
	return _payload()


@frappe.whitelist(methods=["POST"])
def refresh():
	"""Re-read the bench and the site (ffl_core.extensions.registry.sync), ask the
	update service what is newer (ffl_core.extensions.updates.check), then list.

	Returns:
	    dict: same shape as list_extensions, plus ``sync`` — the sync summary
	    ({"created": [...], "updated": [...], "unchanged": [...], "removed": [...],
	    "reset": [...]} — ``reset`` lists rows whose stuck job was marked Failed) —
	    and ``updates``: {"checked": bool, "offered": {id: version}, "error"?: True}.
	"""
	frappe.only_for("System Manager")
	summary = sync()
	checked = updates.check()             # never raises: a source that fails is logged, the rows stay as they were
	payload = _payload()
	payload["sync"] = summary
	payload["updates"] = checked
	return payload


@frappe.whitelist(methods=["GET"])
def disable_notice(extension: str):
	"""What turning this extension off will interrupt, in the extension's own words.

	Args:
	    extension: POS Extension id.

	Returns:
	    str: one sentence from the manifest's ``disable_notice`` callable
	    (dotted path), or "" when the extension declares none — the page then
	    flips the switch without a dialog.
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(DOCTYPE, extension)
	try:
		manifest = json.loads(doc.manifest or "{}")
	except ValueError:
		manifest = {}
	path = manifest.get("disable_notice")
	if not path or not doc.enabled:
		return ""
	# The manifest is data a package author wrote: only run code from that package.
	if not str(path).startswith(f"{doc.name}."):
		frappe.log_error(title=f"disable_notice outside the extension: {extension}")
		return ""
	try:
		return str(frappe.get_attr(path)() or "")
	except Exception:
		frappe.log_error(title=f"disable_notice failed: {extension}")
		return ""


@frappe.whitelist(methods=["POST"])
def set_enabled(extension: str, enabled: int | str):
	"""Turn an installed extension on or off for this store.

	Args:
	    extension: POS Extension id.
	    enabled: 1 to turn on, 0 to turn off.

	Returns:
	    dict: the updated row (same shape as a list_extensions row).
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(DOCTYPE, extension)
	if not doc.installed_on_site:
		frappe.throw(_("{0} is not installed on this site.").format(doc.title), title=_("Not installed"))
	if doc.job_state in BUSY_STATES:
		frappe.throw(_("{0} is busy ({1}). Try again in a minute.").format(doc.title, doc.job_state))
	doc.enabled = cint(enabled)
	doc.save()
	return next(r for r in _rows() if r["name"] == extension)


def _importable_here(app: str) -> bool:
	"""Can this container import the app's hooks? (One seam: tests with a fake row patch it.)"""
	try:
		frappe.get_module(f"{app}.hooks")
		return True
	except Exception:
		return False


def _refuse_while_busy() -> None:
	"""One thing at a time on the extension pipeline (sideload.pipeline_busy)."""
	from ffl_core.extensions.sideload import pipeline_busy

	if busy := pipeline_busy():
		frappe.throw(busy, title=_("Busy"))


def _enqueue(doc, job: str, state: str) -> str:
	from ffl_core.extensions.jobs import set_state

	set_state(doc.name, state)
	try:
		frappe.enqueue(job, queue="long", timeout=1800, job_id=f"ext-{state.lower()}:{doc.name}",
			deduplicate=True, app=doc.name)
	except Exception as e:
		# Nothing was queued: undo the busy state or the row is stuck for 30 min.
		set_state(doc.name, "Failed", _("Could not start the job: {0}").format(str(e)[:200] or type(e).__name__))
		raise
	return doc.name


@frappe.whitelist(methods=["POST"])
def install_on_site(extension: str):
	"""Install an extension that is on the bench but not on this site (background job).

	Args:
	    extension: POS Extension id.

	Returns:
	    dict: {"extension": id, "job_state": "Installing"} — poll list_extensions.
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(DOCTYPE, extension)
	if doc.installed_on_site:
		frappe.throw(_("{0} is already installed on this site.").format(doc.title))
	if not doc.compatible:
		frappe.throw(_("{0} cannot be installed: {1}.").format(doc.title, doc.incompatible_reason),
			title=_("Not compatible"))
	_refuse_while_busy()
	# Installing an app this web container cannot import would make every request
	# 500 (frappe imports every installed app's hooks): only after the restart.
	if not _importable_here(doc.name):
		frappe.throw(_("{0} is on the server but the store has not restarted to load it yet. "
			"Ask your developer to run the extension agent ({1}), then try again.").format(doc.title, EXTENSIONS_DOC),
			title=_("Restart needed"))
	_enqueue(doc, "ffl_core.extensions.jobs.install_on_site", "Installing")
	return {"extension": doc.name, "job_state": "Installing"}


@frappe.whitelist(methods=["POST"])
def uninstall_from_site(extension: str, confirm_id: str):
	"""Back up the database, then remove the extension from this site (background job).

	The code stays on the bench (removing it is docs/self-hosting.md#extensions / the agent);
	the extension's own before_uninstall hook cleans what it added to core.

	Args:
	    extension: POS Extension id.
	    confirm_id: must equal ``extension`` — the typed confirmation.

	Returns:
	    dict: {"extension": id, "job_state": "Uninstalling"} — poll list_extensions.
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(DOCTYPE, extension)
	if (confirm_id or "").strip() != doc.name:
		frappe.throw(_("Type the extension id exactly ({0}) to confirm.").format(doc.name), title=_("Not confirmed"))
	if not doc.installed_on_site:
		frappe.throw(_("{0} is not installed on this site.").format(doc.title))
	_refuse_while_busy()
	held = sum(t["rows"] for t in records_held(doc.name) if not t["child"])
	if held:
		frappe.throw(
			_("{0} holds {1} records on this site. Uninstalling would delete them; turn it off instead.")
			.format(doc.title, held), title=_("Has records"))
	_enqueue(doc, "ffl_core.extensions.jobs.uninstall_from_site", "Uninstalling")
	clear_enabled_cache()
	return {"extension": doc.name, "job_state": "Uninstalling"}


@frappe.whitelist(methods=["GET"])
def release_notes(extension: str):
	"""The extension's own release notes (its change_log folder), newest first.

	Args:
	    extension: POS Extension id.

	Returns:
	    list: [[version, markdown], ...] — the same reader (and the same
	    pre-release file-name rule) as the core's What's New; [] when the
	    extension ships none.
	"""
	from ffl_core.api.changelog import read_release_notes

	frappe.only_for("System Manager")
	doc = frappe.get_doc(DOCTYPE, extension)
	try:
		return read_release_notes(app=doc.name, version=doc.version or "0.0.0")
	except Exception:
		frappe.log_error(title=f"release notes unreadable: {extension}")
		return []


@frappe.whitelist(methods=["GET"])
def uninstall_preview(extension: str):
	"""What uninstalling removes from this site, so the confirmation can say so.

	Args:
	    extension: POS Extension id.

	Returns:
	    dict: {"doctypes": [{"name", "rows", "child"}], "custom_fields": int,
	    "roles": [str], "modules": [str], "records": int, "blocked": bool} —
	    every DocType of the app's modules (what remove_app drops) with row
	    counts, the custom fields it added to core DocTypes, the roles it
	    created, and ``blocked`` when any of its tables still hold records
	    (then the only way out is turning it off — uninstall_from_site refuses).
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(DOCTYPE, extension)
	modules = _modules_of(doc.name)
	doctypes = records_held(doc.name, modules=modules)
	custom_fields = frappe.db.count("Custom Field", {"module": ["in", modules]}) if modules else 0
	roles = frappe.get_all("Role", filters={"module": ["in", modules]}, pluck="name") if modules \
		and frappe.get_meta("Role").has_field("module") else []
	records = sum(t["rows"] for t in doctypes if not t["child"])   # what a manager counts: documents, not rows
	return {"doctypes": doctypes, "custom_fields": custom_fields, "roles": roles, "modules": modules,
		"records": records, "blocked": records > 0}


def _modules_of(app: str) -> list[str]:
	return frappe.get_all("Module Def", filters={"app_name": app}, pluck="name")


def records_held(app: str, *, modules: list[str] | None = None) -> list[dict]:
	"""[{name, rows, child}] for every DocType remove_app would drop for ``app``
	(all DocTypes of its modules, custom and child tables included) — the
	uninstall guard: a table with rows means history a backup file does not
	bring back by itself, so the page and the endpoint refuse and say "turn it
	off instead"."""
	modules = _modules_of(app) if modules is None else modules
	out = []
	if not modules:
		return out
	for row in frappe.get_all("DocType", filters={"module": ["in", modules]}, fields=["name", "istable"]):
		try:
			rows = frappe.db.count(row.name)
		except Exception:
			rows = 0
		out.append({"name": row.name, "rows": rows, "child": bool(row.istable)})
	return out


# --------------------------------------------------------------------------- sideload
@frappe.whitelist(methods=["POST"])
def inspect_upload():
	"""Take an uploaded extension zip (multipart field ``file``), lint it, and describe it.

	Nothing is installed: the zip is kept under the site's private files by
	its sha256 until ``install_upload`` is called or it is replaced.

	Returns:
	    dict: the lint report (``ok``, ``findings``, ``permissions``, ``app_id``,
	    ``title``, ``version``, ``publisher``…) plus ``sha256``, ``high_risk``,
	    ``on_bench`` / ``installed_on_site`` / ``baked`` for that id, and
	    ``upgrade``: None when the id is not on this server, else {"from", "to",
	    "allowed", "reason", "permissions_added", "permissions_removed",
	    "release_notes"} (the package's notes for versions after ``from`` up to
	    ``to``, newest first, as one markdown text). An id whose last upgrade did
	    not finish is not allowed until it is rolled back; ``reason`` says so.
	"""
	frappe.only_for("System Manager")
	from ffl_core.extensions.lint import MAX_ZIP_BYTES
	from ffl_core.extensions.sideload import inspect, save_upload

	f = (frappe.request.files or {}).get("file") if getattr(frappe, "request", None) else None
	if f is None:
		frappe.throw(_("Choose a .zip file first."))
	data = f.stream.read(MAX_ZIP_BYTES + 1)
	if len(data) > MAX_ZIP_BYTES:
		frappe.throw(_("The file is larger than {0} MB.").format(MAX_ZIP_BYTES // (1024 * 1024)), title=_("Too large"))
	if not data:
		frappe.throw(_("The file is empty."))
	out = inspect(save_upload(data))
	out["high_risk"] = _high_risk(out.get("permissions", []))
	out["filename"] = f.filename
	return out


@frappe.whitelist(methods=["POST"])
def inspect_git(url: str, ref: str):
	"""Clone a public git repository at a branch or tag, lint it, and describe it like an upload.

	The clone is shallow, over https only, with no credentials, and gives up
	after 60 seconds; a private repository fails at once (download its zip
	and upload that instead). The tree is packed into an upload by its sha256,
	exactly as ``inspect_upload`` keeps a zip — ``install_upload`` installs or
	upgrades it.

	Args:
	    url: https://<host>/<owner>/<repo>[.git] of a public repository.
	    ref: branch or tag name.

	Returns:
	    dict: the ``inspect_upload`` shape (``filename`` is ``<url>@<ref>``) plus
	    ``source``: {"type": "git", "url", "ref", "commit"}.
	"""
	frappe.only_for("System Manager")
	from ffl_core.extensions.sideload import inspect_git as clone_and_inspect

	out = clone_and_inspect(url, ref)
	out["high_risk"] = _high_risk(out.get("permissions", []))
	out["filename"] = f"{out['source']['url']}@{out['source']['ref']}"
	return out


@frappe.whitelist(methods=["POST"])
def fetch_update(extension: str):
	"""Download the newer version the update service offers for an extension
	(the page's Update button), check it, and describe it like an upload.

	The package must be what the service announced (``latest_sha256`` from
	extensions/updates.check), for this extension and version, and this POS
	must be able to run it, and it must come from the publisher of the one
	installed (an id clash with a sideloaded fork is not an update);
	``install_upload(sha256, id)`` then upgrades.

	Args:
	    extension: POS Extension id.

	Returns:
	    dict: the ``inspect_upload`` shape (``filename`` is ``<id>-<version>.zip``)
	    plus ``source``: {"type": "update-service", "version"}.
	"""
	frappe.only_for("System Manager")
	import hashlib

	from ffl_core.extensions.lint import MAX_ZIP_BYTES
	from ffl_core.extensions.sideload import inspect, save_upload

	row = frappe.db.get_value(DOCTYPE, extension, ["version", "source", "publisher_id", "latest_version", "latest_sha256",
		"latest_requires_core"], as_dict=True)
	if row:
		row.source = updates.normalize_source(row.source)
	offer = _update_of(row) if row else None
	if not offer or not row.latest_sha256:
		frappe.throw(_("There is no newer version of {0} to update to. Refresh the page to check again.").format(extension),
			title=_("No update"))
	if not offer["compatible"]:
		frappe.throw(_("{0} {1} needs a newer POS ({2}). It can be installed once the POS itself is updated.").format(
			extension, row.latest_version, offer["reason"]), title=_("Needs a newer POS"))
	_refuse_while_busy()
	src = updates.source()
	if src is None:
		frappe.throw(_("No update service is set up on this site."), title=_("No update service"))
	try:
		data = src.fetch(extension, row.latest_version) or b""
	except Exception:
		frappe.log_error(title=f"Extension update download failed: {extension} {row.latest_version}")
		frappe.throw(_("Could not download {0} {1}. Try again in a few minutes.").format(extension, row.latest_version),
			title=_("Download failed"))
	if not data or len(data) > MAX_ZIP_BYTES or hashlib.sha256(data).hexdigest() != row.latest_sha256:
		frappe.throw(_("The download of {0} {1} is not the package the update service announced; nothing was changed. "
			"Refresh the page and try again.").format(extension, row.latest_version), title=_("Download did not match"))
	out = inspect(save_upload(data), source={"type": updates.STORED_SOURCE_TYPE, "version": row.latest_version})
	if out.get("app_id") != extension or not updates.same_version(out.get("version"), row.latest_version):
		frappe.throw(_("The update service sent {0} {1} for {2} {3}; nothing was changed.").format(
			out.get("app_id") or "?", out.get("version") or "?", extension, row.latest_version), title=_("Wrong package"))
	ok, reason = check_compat(out.get("requires_core") or None)
	if not ok:
		frappe.throw(_("{0} {1} needs a newer POS ({2}); nothing was changed.").format(extension, row.latest_version, reason),
			title=_("Needs a newer POS"))
	if (out.get("publisher_id") or "") != (row.publisher_id or ""):
		frappe.throw(_("{0} {1} on the update service is published by {2}, but the {0} installed here is {3}'s; it is "
			"not an update of it. Nothing was changed.").format(extension, row.latest_version, out.get("publisher_id") or "?",
			row.publisher_id or "?"), title=_("Different publisher"))
	out["high_risk"] = _high_risk(out.get("permissions", []))
	out["filename"] = f"{extension}-{row.latest_version}.zip"
	out["source"] = {**out["source"], "type": updates.normalize_source(out["source"]["type"])}   # stored as the legacy name
	return out


@frappe.whitelist(methods=["POST"])
def install_upload(sha256: str, confirm_id: str):
	"""Stage an inspected upload and hand it to the store's agent (background job).

	Args:
	    sha256: the upload reference returned by ``inspect_upload``.
	    confirm_id: must equal the extension id — the typed confirmation
	        ("you are installing unreviewed code"). The page's Update sends it
	        itself (the manager confirms in its dialog).

	Returns:
	    dict: {"extension": id, "job_state": "Staging", "mode": "install" | "upgrade"}
	    — poll list_extensions. An install appears as Staged, then Installing once
	    the agent restarts the store; an upgrade (the id is on this server and the
	    package is a newer version from upload or git) as Staged, then Upgrading.
	    A same-id package that may not upgrade it is refused with the reason, and
	    nothing starts while another extension is being installed, upgraded or
	    removed, or the store is waiting to restart.
	"""
	frappe.only_for("System Manager")
	from ffl_core.extensions.sideload import inspected

	info = inspected(sha256)
	app_id = info.get("app_id") or ""
	if not info.get("ok"):
		frappe.throw(_("The package does not pass the checks ({0} errors); fix them and upload again.").format(info.get("errors")), title=_("Not installable"))
	if (confirm_id or "").strip() != app_id:
		frappe.throw(_("Type the extension id exactly ({0}) to confirm.").format(app_id), title=_("Not confirmed"))
	upgrade = info.get("upgrade")
	if upgrade and not upgrade["allowed"]:
		frappe.throw(upgrade["reason"], title=_("Cannot upgrade"))
	_refuse_while_busy()
	mode = "upgrade" if upgrade else "install"
	frappe.enqueue(f"ffl_core.extensions.sideload.{'stage_upgrade' if upgrade else 'stage'}", queue="long", timeout=1800,
		job_id=f"ext-stage:{app_id}", deduplicate=True, sha256=sha256, app_id=app_id, user=frappe.session.user)
	return {"extension": app_id, "job_state": "Staging", "mode": mode}


@frappe.whitelist(methods=["GET"])
def upload_failure(sha256: str):
	"""Why a staged upload failed where no registry row says so (the page shows it after Install / Upgrade).

	An install whose code never reached the registry has no row; an upgrade refused because the store
	was busy, or because the id's last upgrade did not finish, leaves its row as it was.

	Args:
	    sha256: the upload reference.

	Returns:
	    dict | None: {"app", "message"} or None.
	"""
	frappe.only_for("System Manager")
	from ffl_core.extensions.sideload import failure_for

	return failure_for(sha256)

