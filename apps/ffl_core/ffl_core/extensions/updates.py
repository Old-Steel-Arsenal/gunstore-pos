"""Extension updates — the newer version the store's update service offers
for each extension on this server (the Extensions page's Update button).

The core does not know where updates come from. An app registers a source
(hook ``gunshop_extension_updates``, docs/extensions/hooks.md): a dotted path
to a zero-arg factory returning an object with ``latest() -> {ext_id:
{version, sha256, size, requires_core}}`` and ``fetch(ext_id, version) ->
bytes`` — or None when it is not set up on this site. A source that serves
one publisher's packages says so (``publisher_id`` attribute): only extensions
installed from that publisher are offered its updates. ffl_integrations
registers the Data Service (Data Service Settings). The first source there is is used.

``check`` notes what is newer on each POS Extension row (latest_*); the page
offers Update from those fields, and api.extensions.fetch_update downloads the
package, checks it against the announced sha256 and hands it to the upload
pipeline (sideload.inspect → install_upload → stage_upgrade).
"""
from __future__ import annotations

import frappe
from frappe.utils import now_datetime
from packaging.version import InvalidVersion, Version

from ffl_core.extensions import DOCTYPE

HOOK = "gunshop_extension_updates"
SOURCE_TYPE = "update-service"                # a package from the update service, as every reader sees it
LEGACY_SOURCE_TYPE = "osa-api"                # its name before the rename, still on rows written before 1.14.0
# What extensions.json, the POS Extension row and an upload report STORE for the update service.
# Every reader takes both names (normalize_source), and so does 1.12.0 and later, so a rollback
# to those still knows the rows this writes.
STORED_SOURCE_TYPE = SOURCE_TYPE


def normalize_source(source: str | None) -> str | None:
	"""A stored source as readers use it: the update service's legacy name reads as SOURCE_TYPE."""
	return SOURCE_TYPE if source == LEGACY_SOURCE_TYPE else source

LATEST_FIELDS = ("latest_version", "latest_sha256", "latest_requires_core")


def source():
	"""The update source this site has, or None (no provider, or none set up here)."""
	for hook in frappe.get_hooks(HOOK):
		try:
			src = frappe.get_attr(hook)()
		except Exception:
			frappe.log_error(title=f"{HOOK} provider failed: {hook}")
			continue
		if src is not None:
			return src
	return None


def newer(offered: str | None, installed: str | None) -> bool:
	"""Is ``offered`` a version strictly after ``installed``? (Unreadable = no.)"""
	try:
		return bool(offered) and Version(str(offered)) > Version(str(installed or "0"))
	except InvalidVersion:
		return False


def same_version(a: str | None, b: str | None) -> bool:
	"""PEP 440-equal (1.3.1 and 1.3.1.0, 1.4.0-beta.1 and 1.4.0b1): the service keeps the canonical spelling."""
	try:
		return Version(str(a)) == Version(str(b))
	except InvalidVersion:
		return False


def check() -> dict:
	"""Ask the update source what is newest and note it on each POS Extension
	row that an update may replace (source upload / git / the update service):
	latest_* set where it is newer, cleared where not. Daily (scheduler) and on
	the page's Refresh. A source that fails leaves the rows as they were.

	Returns:
	    {"checked": bool, "offered": {ext_id: version}} — plus "error": True when
	    the source did not answer (the page says the check failed).
	"""
	from ffl_core.extensions.sideload import UPGRADE_SOURCES

	src = source()
	if src is None:
		return {"checked": False, "offered": {}}
	try:
		latest = src.latest() or {}
	except Exception:
		frappe.log_error(title="Extension update check failed")
		return {"checked": False, "offered": {}, "error": True}
	now, offered, publisher = now_datetime(), {}, getattr(src, "publisher_id", None)
	for row in frappe.get_all(DOCTYPE, fields=["name", "version", "source", "publisher_id"]):
		release = latest.get(row.name) or {}
		sha = str(release.get("sha256") or "").strip().lower()
		if (normalize_source(row.source) in UPGRADE_SOURCES and newer(release.get("version"), row.version) and sha
				and (publisher is None or row.publisher_id == publisher)):
			values = {"latest_version": str(release["version"]), "latest_sha256": sha,
				"latest_requires_core": str(release.get("requires_core") or "")}
			offered[row.name] = values["latest_version"]
		else:
			values = dict.fromkeys(LATEST_FIELDS, "")
		frappe.db.set_value(DOCTYPE, row.name, {**values, "latest_checked_at": now}, update_modified=False)
	return {"checked": True, "offered": offered}
