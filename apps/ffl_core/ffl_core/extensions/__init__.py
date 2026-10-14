"""The extension SDK — the stable surface an extension may depend on (present since core 0.1.0).

An extension is a separate Frappe app that lives on the
apps-ext volume. Whether it is *installed* is a bench/site fact;
whether it is *on* is a per-store switch on its ``POS Extension`` row, which
the Extensions page (/app/extensions) flips. Everything an extension plugs
into core must honour that switch through the three helpers here:

	from ffl_core.extensions import extension_hook, require_enabled, is_enabled

	@extension_hook("acme_layaway")        # switched off → the hook returns None
	def on_submit(doc, method): ...

	@frappe.whitelist(methods=["POST"])
	def start_plan(customer):
		require_enabled("acme_layaway", roles=COUNTER_ROLES)   # first statement

Switched off means: hooks no-op, endpoints refuse, the JS registry hides the
extension's actions. Data is never touched — that is the whole point of a
switch that is not an uninstall.

``is_enabled`` reads the row through ``frappe.get_cached_value`` — the platform
document cache, invalidated by every ``save`` / ``db.set_value`` / delete — so a
hook on a hot path costs one cache hit and no writer has to remember to clear
anything. (The name ``require_enabled`` is the extension SDK's stable surface,
kept on purpose.)
"""
from __future__ import annotations

import functools
from collections.abc import Callable

import frappe
from frappe import _

DOCTYPE = "POS Extension"
# Where an operator reads how to run, roll back and repair the extension layer (anchors of docs/self-hosting.md;
# test_extension_upgrade checks both exist). Every message that points at the guide uses these.
EXTENSIONS_DOC = "docs/self-hosting.md#extensions"
ROLLBACK_DOC = "docs/self-hosting.md#roll-back-an-upgrade"
_FIELDS = ("enabled", "installed_on_site", "compatible")


def _row(app: str):
	"""The three switch fields, via the platform document cache (auto-invalidated)."""
	try:
		values = frappe.get_cached_value(DOCTYPE, app, list(_FIELDS))
	except Exception:
		return None
	if not values:
		return None
	return dict(zip(_FIELDS, values))


def effective(row) -> bool:
	"""The switch as it acts: on (the store's intent), installed on this site and
	compatible with this core — all three. ``enabled`` alone is only the intent;
	nothing but the store writes it."""
	return bool(row.get("enabled") and row.get("installed_on_site") and row.get("compatible"))


def _load(app: str) -> bool:
	row = _row(app)
	if row is None:
		# No registry row = off. The row is written by registry.sync (core
		# after_migrate, after_app_install/uninstall, the Extensions page's
		# Refresh, the install job, or `bench execute
		# ffl_core.extensions.registry.sync` — docs/self-hosting.md#extensions); a read path never writes, so a GET can't
		# insert-and-roll-back on every request, and an extension nobody has
		# described is never silently on.
		return False
	return effective(row)


def is_enabled(app: str) -> bool:
	"""True when the extension is installed on this site, compatible and switched on.

	For templates and conditional branches only — an endpoint MUST call
	:func:`require_enabled`, never gate itself on this.
	"""
	return _load(app)


def clear_enabled_cache() -> None:
	"""Drop the desk boot cache (it carries ``gunshop_extensions``); the row
	itself is cached by frappe and invalidated on write."""
	frappe.cache.delete_key("bootinfo")


def title_of(app: str) -> str:
	return frappe.db.get_value(DOCTYPE, app, "title") or app


def require_enabled(app: str, *, roles: tuple[str, ...] | list[str] | None = None) -> None:
	"""Refuse the call unless the extension is on and the user holds one of ``roles``.

	The gate every extension endpoint opens with. Pass
	``roles`` on every endpoint: without it this only checks the switch, and the
	endpoint-gate audit counts only the ``roles=`` form as a gate.
	"""
	if not is_enabled(app):
		frappe.throw(
			_("{0} is turned off for this store. A manager can turn it on under Settings → Extensions.")
			.format(title_of(app)),
			frappe.PermissionError,
		)
	if roles and not (set(roles) & set(frappe.get_roles())):
		frappe.throw(
			_("You do not have permission to use {0}.").format(title_of(app)),
			frappe.PermissionError,
		)


def extension_hook(app: str) -> Callable:
	"""Decorate a hook so it silently does nothing while the extension is off.

	Works for doc_events handlers ``(doc, method)`` and scheduler functions
	alike: the wrapper returns None without calling the function.
	"""
	def decorate(fn: Callable) -> Callable:
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			if not is_enabled(app):
				return None
			return fn(*args, **kwargs)

		wrapper.__gunshop_extension__ = app
		return wrapper

	return decorate


def extend_bootinfo(bootinfo) -> None:
	"""``frappe.boot.gunshop_extensions[id] = {enabled, version, title}`` for the
	extensions installed on this site — what the JS registry renders from."""
	try:
		rows = frappe.get_all(
			DOCTYPE, filters={"installed_on_site": 1},
			fields=["name", "title", "version", "enabled", "installed_on_site", "compatible"],
		)
	except Exception:
		# Boot must never fail because the registry table is not there yet
		# (mid-install of ffl_core itself) — but a broken registry must not go
		# unnoticed either (ffl_integrations/boot.py has the same rule).
		frappe.log_error(title="extension registry: extend_bootinfo failed")
		rows = []
	bootinfo.gunshop_extensions = {
		r.name: {"enabled": effective(r), "version": r.version or "", "title": r.title or r.name}
		for r in rows
	}
