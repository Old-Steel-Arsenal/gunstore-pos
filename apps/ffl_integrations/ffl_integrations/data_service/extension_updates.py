"""The store's extension update source (core hook ``gunshop_extension_updates``,
docs/extensions/hooks.md): the Data Service's /v1/extensions (scope ``ext:read``)
over the one Data Service connection.

``latest`` asks for what this POS core can install (``?core=``, unsigned like
every query string); ``fetch`` downloads a package (``client.get_bytes``:
streamed, capped, no redirects) and the core checks it against the sha256
``latest`` announced (api.extensions.fetch_update)."""
from __future__ import annotations

from ffl_integrations.data_service import client

DOWNLOAD_TIMEOUT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 5          # the page's Refresh waits on it


class OsaApiSource:
	publisher_id = "osa"               # the update service serves the maintainers' own extensions

	def latest(self) -> dict:
		"""{ext_id: {version, sha256, size, requires_core}} — the newest release of
		each extension that this POS core can run. A store whose client is not
		granted ``ext:read`` (opt-in on the Data Service) simply has no updates offered."""
		from ffl_core.extensions.registry import core_version

		try:
			body = client.get("/v1/extensions/latest", {"core": core_version()}, timeout=CHECK_TIMEOUT_SECONDS)
		except client.DataServiceForbidden:
			return {}
		return {r["ext_id"]: r for r in (body or {}).get("releases") or [] if r.get("ext_id")}

	def fetch(self, ext_id: str, version: str) -> bytes:
		from ffl_core.extensions.lint import MAX_ZIP_BYTES

		return client.get_bytes(f"/v1/extensions/{ext_id}/{version}/package", max_bytes=MAX_ZIP_BYTES,
			timeout=DOWNLOAD_TIMEOUT_SECONDS)


def get_source():
	"""The source, or None when the update feed is off (``extension_updates``:
	Data Service Settings not filled in — no update service on this site)."""
	from ffl_integrations.data_service import EXTENSION_UPDATES, is_off

	if is_off(EXTENSION_UPDATES):
		return None
	return OsaApiSource()
