"""What this site can do without (or with) the Data Service — the capability model
(docs/data-service.md). One function decides, every surface follows it:

- ``catalog``: ``data_service`` | ``local`` | ``off`` — the distributor catalog's
  source (Data Service Settings › Catalog source) and whether it is set up;
- ``category_tree``: ``data_service`` | ``off`` — only the Data Service ships a tree;
- ``ffl_national_search``: ``data_service`` | ``not_chosen`` | ``off`` — the national
  FFL listing in the FFL search. ``not_chosen``: connected, but FFL Settings verifies
  through another provider (local records, as the store chose; no notice). ``off``:
  no Data Service (this store's ATF FFL Records only, with the notice). EZ Check
  follows its own provider setting in FFL Settings;
- ``extension_updates``: ``data_service`` | ``off`` — the Extensions page's update feed;
- ``ai``: ``connected`` | ``off`` — what an AI extension answers through the
  ``gunshop_ai_connected`` hook (core never imports the extension).

Any capability is ``unknown`` when its settings could not be read: not off, so no
notice, no refusal and no clean-up; callers behave as before the model existed.

``capabilities()`` is cached for the transaction (it is dropped on every commit and
rollback, like Frappe's own single-value cache) and when Data Service Settings is
saved (or FFL Settings, whose provider decides the national FFL search). The desk gets it as ``frappe.boot.ffl_capabilities``; pages that refresh
call ``get_capabilities``. An endpoint that cannot work without a capability opens
with ``require(name)``: a typed ``CapabilityOff`` (HTTP 417) with one message, never
a 500. The catalog selector (``remote_catalog._backend``) raises the same exception
quietly, so nothing reaches ``data_service/client.py`` for an off catalog.
"""
from __future__ import annotations

import frappe

from ffl_integrations.data_service.client import DataServiceUnavailable

CATALOG = "catalog"
CATEGORY_TREE = "category_tree"
FFL_NATIONAL_SEARCH = "ffl_national_search"
EXTENSION_UPDATES = "extension_updates"
AI = "ai"

OFF = "off"
DATA_SERVICE = "data_service"
LOCAL = "local"
CONNECTED = "connected"
# The Data Service is connected, but FFL Settings verifies through another provider:
# the FFL search uses this store's records only, by the store's choice (no notice).
NOT_CHOSEN = "not_chosen"
# The settings could not be read (a database hiccup, a site mid-migrate): neither on
# nor off. Callers carry on as before the model existed; jobs skip the tick and
# never clean up on it.
UNKNOWN = "unknown"

AI_HOOK = "gunshop_ai_connected"
SETTINGS = "Data Service Settings"
_CACHE_KEY = "__ffl_capabilities__"
_AI_WARNING_KEY = "ffl_capability_ai_hook_failed"

# One sentence per capability; the desk's notice component (ffl_core
# public/js/capability_notice.js) carries the same words.
MESSAGES = {
	CATALOG: "Distributor catalog is not set up. Set it up in Data Service Settings "
		"(Catalog source offers Local).",
	CATEGORY_TREE: "The category tree comes from the Data Service, which is not set up "
		"on this site (Data Service Settings).",
	FFL_NATIONAL_SEARCH: "National FFL search is not set up: only this store's FFL "
		"records are searched. Connect the Data Service in Data Service Settings, then "
		"choose it as the live check provider in FFL Settings.",
	EXTENSION_UPDATES: "Update service not configured.",
	AI: "The AI assistant is not connected.",
}


class CapabilityOff(frappe.ValidationError, DataServiceUnavailable):
	"""A feature this site has not set up (``capability`` names it). A
	ValidationError, so an endpoint answers HTTP 417 with the message; also a
	DataServiceUnavailable, so every caller that already degrades when the service
	cannot be reached degrades the same way here (and should not log it)."""

	def __init__(self, message: str = "", capability: str | None = None):
		super().__init__(message)
		self.capability = capability


NAMES = (CATALOG, CATEGORY_TREE, FFL_NATIONAL_SEARCH, EXTENSION_UPDATES, AI)
# ``capability(CATALOG)`` without ``house``: the site as a whole (boot, notices,
# endpoint guards). With ``house``: the one read about to happen.
_SITE = object()
# ``_house_service``: the house has no service of its own, its reads go to the site's.
SITE_SERVICE = "site"


def capabilities() -> dict:
	"""``{catalog, category_tree, ffl_national_search, extension_updates, ai}`` for
	this site (values above). Never raises."""
	return {name: capability(name) for name in NAMES}


def capability(name: str, *, house=_SITE) -> str:
	"""One capability's value; reads only the settings that one needs (the catalog
	selector asks for ``catalog`` on every read: no AI hook, no FFL Settings). Each
	input is True / False, or None when it could not be read (``UNKNOWN``).

	``catalog`` alone takes ``house``, for the read that is about to happen (the
	catalog selector): a house code reads through that house's own service when
	its Distributor row names one (``client.own_service_row``), else the site's;
	``None`` is a cross-house read (batch, changes, unnamed search), which only Data
	Service Settings itself serves. Without ``house``: the site as a whole — on when
	some catalog read can succeed (the site's service, or a house's own).

	An unknown ``name`` is a programming error (``ValueError``), never "off"."""
	if name not in NAMES:
		raise ValueError(f"unknown capability {name!r} (one of {', '.join(NAMES)})")
	if name == AI:
		return _input("ai", _ai)
	service = _input("service", _service_configured)
	if name == EXTENSION_UPDATES:
		return _tri(service, DATA_SERVICE)
	if name == FFL_NATIONAL_SEARCH:
		if service is not True:
			return _tri(service, DATA_SERVICE)
		chosen = _input("ffl_provider", _ffl_provider_is_service)
		return UNKNOWN if chosen is None else DATA_SERVICE if chosen else NOT_CHOSEN
	# Catalog source is read live (frappe's own single-value cache): cheap, and a
	# test that patches catalog_local.is_local sees it. Unreadable: unknown. Tests:
	# an installed catalog stub stands in for a configured Data Service.
	local = _local()
	if local is None:
		return UNKNOWN
	if _test_stub():
		service = True
	if name == CATEGORY_TREE:
		return OFF if local else _tri(service, DATA_SERVICE)
	# CATALOG
	if local:
		return LOCAL
	if house is _SITE:
		if service is True:
			return DATA_SERVICE
		own = _input("distributor_service", _distributor_service)
		if own is True:
			return DATA_SERVICE
		return UNKNOWN if service is None or own is None else OFF
	if not house:   # cross-house: the site's own service or nothing
		return _tri(service, DATA_SERVICE)
	own = _input(f"house:{house}", lambda: _house_service(house))
	if own == SITE_SERVICE:
		return _tri(service, DATA_SERVICE)
	return _tri(own, DATA_SERVICE)


def _tri(value, on: str) -> str:
	return UNKNOWN if value is None else on if value else OFF


def is_off(name: str, **kwargs) -> bool:
	return capability(name, **kwargs) == OFF


def message(name: str) -> str:
	return frappe._(MESSAGES.get(name) or "{0} is not set up.").format(name)


def require(name: str, *, notify: bool = True, house=_SITE) -> str:
	"""The capability's value, or ``CapabilityOff`` when it is off. ``notify`` (the
	default, for whitelisted endpoints) also shows the message to the user;
	``notify=False`` raises quietly, for code whose callers degrade on their own.
	``house``: as in ``capability`` (the read about to happen). An unknown ``name``
	raises ``ValueError``, as ``capability`` does."""
	value = capability(name, house=house)
	if value != OFF:
		return value
	if name == CATALOG and _tests_expect_the_stub_guard():
		return UNKNOWN
	exc = CapabilityOff(message(name), capability=name)
	if notify:
		try:   # the desk's handler names the capability in its notice and drops this
			# message from the ones it shows (capability_notice.js)
			frappe.local.response["capability_off"] = name
			frappe.local.response["capability_off_message"] = exc.args[0]
		except Exception:  # noqa: BLE001 — no request: nothing to tell
			pass
		frappe.throw(exc.args[0], exc=exc, title=frappe._("Not set up"))
	raise exc


def clear_capabilities(*_args, **_kwargs) -> None:
	"""Forget the cached answer (settings changes; tests)."""
	try:
		frappe.db.value_cache.pop(_CACHE_KEY, None)
	except Exception:  # noqa: BLE001 — no connection: nothing cached
		pass


def _drop_cached_answers() -> None:
	clear_capabilities()
	try:
		frappe.cache.delete_key("bootinfo")
	except Exception:  # noqa: BLE001 — a cache hiccup must not undo a committed save
		pass


def on_settings_change(doc=None, method=None) -> None:
	"""doc_events (Data Service Settings, FFL Settings, Distributor) and the
	extension switch: this transaction re-reads at once; the desk boot carrying
	``ffl_capabilities`` is dropped only AFTER the commit, so no other request can
	rebuild it from the values before the save."""
	clear_capabilities()
	try:
		frappe.db.after_commit.add(_drop_cached_answers)
	except Exception:  # noqa: BLE001 — no transaction: drop now
		_drop_cached_answers()


def on_extension_toggled(app=None, effective=None) -> None:
	"""``gunshop_extension_toggled``: an AI extension switched on or off changes ``ai``."""
	on_settings_change()


@frappe.whitelist()
def get_capabilities() -> dict:
	"""The site's capabilities, for desk pages that refresh without a reload.

	Returns:
	    {catalog: "data_service"|"local"|"off", category_tree: "data_service"|"off",
	    ffl_national_search: "data_service"|"not_chosen"|"off", extension_updates:
	    "data_service"|"off", ai: "connected"|"off"} — any of them "unknown" when its
	    settings could not be read; the same dict as ``frappe.boot.ffl_capabilities``.

	Permissions:
	    Any signed-in user (it names features, no data).
	"""
	return capabilities()


# ---------------------------------------------------------------- internals

def _input(key: str, compute):
	"""One settings read, cached with frappe's single-value cache (dropped on every
	commit / rollback, and by ``clear_capabilities``)."""
	try:
		cache = frappe.db.value_cache.setdefault(_CACHE_KEY, {})
	except Exception:  # noqa: BLE001 — no connection: nothing to cache in
		return compute()
	if key not in cache:
		cache[key] = compute()
	return cache[key]


def _tests_expect_the_stub_guard() -> bool:
	"""Tests (no stub installed — that counts as a configured service — and not Local):
	a bare "catalog off" is not enforced, so a test that reaches the catalog meets
	``remote_catalog.CatalogStubMissing`` (what extensions' tests rely on) or runs on
	its patched provider, as before the capability model. A test of the model sets
	``frappe.flags.catalog_real_capability_in_test`` to get the real refusal."""
	from ffl_integrations.distributor.remote_catalog import REAL_CAPABILITY_FLAG

	return bool(getattr(frappe.flags, "in_test", False)
		and not getattr(frappe.flags, REAL_CAPABILITY_FLAG, False))


def _test_stub() -> bool:
	from ffl_integrations.distributor.remote_catalog import _STUB_ATTR

	return bool(getattr(frappe.flags, "in_test", False)
		and getattr(frappe.flags, _STUB_ATTR, None) is not None)


def _local() -> bool | None:
	"""Catalog source = Local; None when it cannot be read (unknown, not "no")."""
	from ffl_integrations.catalog_local import is_local

	return is_local(strict=True)


def _secret_stored(doctype: str, name: str, field: str) -> bool:
	"""A Password field holds a value (``__Auth``), without decrypting it."""
	return bool(frappe.db.sql("""SELECT 1 FROM `__Auth` WHERE doctype = %s AND name = %s
		AND fieldname = %s AND IFNULL(password, '') != '' LIMIT 1""", (doctype, name, field)))


def _service_configured() -> bool | None:
	"""Endpoint, client id and secret are all on Data Service Settings (the fields
	``client.get_connection`` requires), read past frappe's single-value cache so a
	save earlier in this transaction is seen (the answer is cached by ``_input``).
	None when they cannot be read: unknown."""
	try:
		endpoint = (frappe.db.get_single_value(SETTINGS, "endpoint", cache=False) or "").strip()
		client_id = (frappe.db.get_single_value(SETTINGS, "client_id", cache=False) or "").strip()
		return bool(endpoint and client_id and _secret_stored(SETTINGS, SETTINGS, "secret"))
	except Exception:  # noqa: BLE001 — a hiccup is not "not configured"
		return None


def _distributor_service() -> bool | None:
	"""Some house a store buys from (a provider with an ENABLED Distributor) reads a
	complete catalog service of its own (docs/catalog-api: one store reading two
	services), chosen exactly as the client chooses it (``_house_service``). None
	when it cannot be read."""
	try:
		codes = {r.provider for r in frappe.get_all("Distributor", filters={"enabled": 1},
			fields=["provider"]) if r.provider}
		answers = [_house_service(code) for code in sorted(codes)]
	except Exception:  # noqa: BLE001
		return None
	if any(a is True for a in answers):
		return True
	return None if any(a is None for a in answers) else False


def _house_service(code: str):
	"""Where a read for house ``code`` goes, as ``client.get_connection(code)`` will
	send it: ``SITE_SERVICE`` (no row names an own endpoint), True (the chosen row's
	own service is complete: endpoint, client id, secret), False (that row's service
	is incomplete: the client refuses the read). None when it cannot be read."""
	from ffl_integrations.data_service.client import own_service_row

	try:
		name = own_service_row(code)
		if not name:
			return SITE_SERVICE
		row = frappe.db.get_value("Distributor", name, ["catalog_endpoint", "catalog_client_id"],
			as_dict=True) or {}
		return bool((row.get("catalog_endpoint") or "").strip()
			and (row.get("catalog_client_id") or "").strip()
			and _secret_stored("Distributor", name, "catalog_secret"))
	except Exception:  # noqa: BLE001 — a hiccup is not "not configured"
		return None


def _ffl_provider_is_service() -> bool | None:
	"""The national listing is searched when FFL Settings' live check provider is
	the Data Service (atf/search.py); otherwise the FFL search is local only."""
	try:
		return (frappe.db.get_single_value("FFL Settings", "live_check_provider", cache=False) or "").strip() \
			== "Data Service"
	except Exception:  # noqa: BLE001
		return None


def _ai() -> str:
	for path in frappe.get_hooks(AI_HOOK):
		try:
			if frappe.get_attr(path)():
				return CONNECTED
		except Exception:  # noqa: BLE001 — a broken provider reads as not connected
			_warn_ai_hook(path)
	return OFF


def _warn_ai_hook(path: str) -> None:
	"""Logged at most once an hour: boot asks on every desk load."""
	try:
		if frappe.cache().get_value(_AI_WARNING_KEY):
			return
		frappe.cache().set_value(_AI_WARNING_KEY, 1, expires_in_sec=3600)
		from ffl_integrations.notifications import safe_log_error

		safe_log_error(f"{AI_HOOK} provider failed: {path}", frappe.get_traceback())
	except Exception:  # noqa: BLE001
		pass
