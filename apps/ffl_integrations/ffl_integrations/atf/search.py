"""Whitelisted FFL search: the shared Data Service first, local cache as fallback.

The local ATF FFL Record table only holds dealers this store has actually
touched (Link targets for the A&D book), so a local-only typeahead shows a
sliver of the country. When the Data Service provider is configured, the search
asks the service's full national listing first and merges any locally landed
rows over it; if the service is unreachable the local LIKE answers alone —
exactly the pre-API behavior, so degradation is never worse than before.

Remote rows are flagged ``_remote`` and are NOT persisted here: a dealer only
becomes an ATF FFL Record when someone actually uses it (lookup_and_create),
keeping the land-on-use shape of the cache.
"""
from __future__ import annotations

import frappe

from ffl_core.roles import COUNTER_ROLES

_LOCAL_FIELDS = [
	"ffl_number", "license_type", "licensee_name", "licensee_dba",
	"street", "street2", "city", "state", "zip", "phone", "expiry_date",
]


def _stable_key(ffl_number: str | None) -> str:
	"""Licence identity without segment 5 (the expiration code) — the same
	rule the service applies, so a landed row and its remote twin dedupe
	even when their codes differ."""
	parts = (ffl_number or "").split("-")
	if len(parts) != 6:
		return ffl_number or ""
	return "-".join(parts[:4] + parts[5:])


def _local_rows(q: str, limit: int) -> list[dict]:
	exact = frappe.db.get_value("ATF FFL Record", q, _LOCAL_FIELDS, as_dict=True)
	if exact:
		return [exact]
	pattern = f"%{q}%"
	return frappe.db.sql(
		"""
		SELECT ffl_number, license_type, licensee_name, licensee_dba,
		       street, street2, city, state, zip, phone, expiry_date
		FROM `tabATF FFL Record`
		WHERE licensee_name LIKE %(p)s OR licensee_dba LIKE %(p)s
		ORDER BY licensee_name
		LIMIT %(l)s
		""",
		{"p": pattern, "l": limit},
		as_dict=True,
	)


def _remote_rows(q: str, limit: int) -> list[dict] | None:
	"""Rows from the shared service, or None when national search is not on
	(``data_service.capability``: the Data Service set up AND chosen as the live
	check provider) or the service is unreachable — the caller then serves
	local-only. Settings that could not be read (``unknown``) try the service, as
	before the capability model."""
	from ffl_integrations.data_service import DATA_SERVICE, FFL_NATIONAL_SEARCH, UNKNOWN, capability

	if capability(FFL_NATIONAL_SEARCH) not in (DATA_SERVICE, UNKNOWN):
		return None
	from ffl_integrations.atf.ez_check import EZCheckUnavailable
	from ffl_integrations.atf.data_service_provider import search_remote

	try:
		return search_remote(q, limit=limit)
	except EZCheckUnavailable:
		return None


@frappe.whitelist()
def search(query: str, limit: int = 20) -> list[dict]:
	"""Search the FFL directory (national listing + local cache).

	Matches `query` exactly against ffl_number; otherwise searches the
	shared national listing (when configured) merged with the local cache,
	falling back to a local LIKE.

	Args:
	    query: FFL number or name/city substring (blank returns []).
	    limit: Row cap, clamped to 1–200 (default 20).

	Returns:
	    Up to `limit` FFL record dicts.
	"""
	frappe.only_for(COUNTER_ROLES)
	q = (query or "").strip()
	limit = min(max(1, int(limit)), 200)  # hard cap to prevent DoS via giant limits
	if not q:
		return []

	local = _local_rows(q, limit)
	remote = _remote_rows(q, limit)
	if remote is None:
		return local

	# Local rows win the dedupe: they carry live-verified expiry/phone the
	# monthly listing doesn't have.
	seen = {_stable_key(row.get("ffl_number")) for row in local}
	merged = list(local)
	for row in remote:
		if _stable_key(row.get("ffl_number")) in seen:
			continue
		merged.append(row)
		if len(merged) >= limit:
			break
	return merged
