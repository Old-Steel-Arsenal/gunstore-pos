"""Distributors hub — the Settings-page entry for EVERY wholesale house.

One custom page (`/app/distributor-hub`) replaces the per-provider Settings
shortcut: card per Distributor row with flag toggles, a read-only summary line
(Ordering / Ship to store), feed/order health, a connection probe and priority
ordering. Provider-specific settings stay in their own Single doctype
(provider.settings_doctype) — the hub only links to it ("Account & shipping"),
so onboarding a new house never touches the workspace again.

Permission model mirrors the Distributor doctype: the four read roles see the
page, only System/Purchase Manager mutate (toggle/reorder).
"""

from __future__ import annotations

import frappe

from ffl_integrations.catalog_local import sync as catalog_sync
from ffl_integrations.distributor import dropship_hooks
from ffl_integrations.distributor.api import get_provider, get_providers, has_remote_catalog

READ_ROLES = ("System Manager", "Purchase Manager", "Purchase User", "Stock Manager")
WRITE_ROLES = ("System Manager", "Purchase Manager")

# The only Distributor fields the hub may flip. An open setattr endpoint would
# be a privilege hole (e.g. rewriting markup rules through the toggle API).
TOGGLABLE_FLAGS = ("enabled", "dropship_enabled", "stocking_enabled", "poll_enabled")

# Distributor Order statuses the card surfaces: money either not yet committed
# (draft), in flight at the house, or needing a human. Complete/Cancelled are
# history and stay off the card.
_IN_FLIGHT = ("Queued", "Placed", "Acknowledged", "Partially Shipped")
_ATTENTION = ("Hold", "Error")

# The settings tab each summary item opens. Both distributor settings pages name
# their tabs alike (pinned by tests/test_distributor_settings_layout.py).
SUMMARY_TABS = {"live_orders": "tab_account", "ship_to_store": "tab_freight"}


@frappe.whitelist()
def overview() -> dict:
	"""Everything the distributor-hub page renders, in one round trip.

	Heartbeats are guarded per provider — a dead catalog service greys that
	provider's health line instead of blanking the page.

	Returns:
	    {distributors: [per-house rows with flags/priority/health and
	    summary: [{label, value, tab}]], providers: [{code,
	    settings_doctype, used_by}], can_write, catalog_local: {local,
	    worker_running, warning}, can_sync_now (Local catalog and System Manager),
	    capabilities: {catalog} (the data_service capability the page shows)}, and
	    per row catalog_lanes (Local catalog only, else null): {lanes: [{lane,
	    last_success_at, rows, last_status, error_class}], offers_total,
	    offers_active}. A house whose catalog reads are off (its own service, else
	    the site's) has health {catalog_off: true} instead of a probe.

	Permissions:
	    Hub read roles.
	"""
	frappe.only_for(READ_ROLES)

	rows = frappe.get_all(
		"Distributor",
		fields=["name", "distributor_name", "provider", "supplier", "priority",
			"enabled", "dropship_enabled", "stocking_enabled", "poll_enabled",
			"last_poll_run_at"],
		order_by="priority asc, modified asc")
	dropship_hooks.decorate_hub_rows(rows)  # the drop-ship layer's markup column

	from ffl_integrations.data_service import CATALOG, OFF, capability

	catalog = capability(CATALOG)
	registry = get_providers()
	health_by_provider = {}
	facts_by_provider = {}
	for code in {r.provider for r in rows}:
		provider = registry.get(code)
		if provider is None:
			health_by_provider[code] = {"error": frappe._(
				"Provider '{0}' is not registered — its integration code is "
				"missing.").format(code)}
			continue
		# Local settings only, so outside the probe guard: a dead catalog service
		# must not hide whether the house can take orders.
		facts_by_provider[code] = getattr(provider, "hub_summary", dict)() or {}
		if capability(CATALOG, house=code) == OFF:
			# A state, not an error: the page shows the "not set up" notice.
			health_by_provider[code] = {"catalog_off": True}
			continue
		try:
			health_by_provider[code] = {
				"feed_heartbeat": getattr(provider, "feed_heartbeat", lambda: None)(),
				"catalog_heartbeat": getattr(
					provider, "catalog_content_heartbeat", lambda: None)(),
			}
		except Exception as exc:  # noqa: BLE001 — a failed probe only degrades the health row
			health_by_provider[code] = {"error": str(exc)}

	local = catalog_sync.status()
	order_counts: dict = {}
	for dist, status, n in frappe.db.sql(
			"""select distributor, status, count(*) from `tabDistributor Order`
			group by distributor, status"""):
		bucket = order_counts.setdefault(
			dist, {"draft": 0, "in_flight": 0, "attention": 0})
		if status == "Draft":
			bucket["draft"] += n
		elif status in _IN_FLIGHT:
			bucket["in_flight"] += n
		elif status in _ATTENTION:
			bucket["attention"] += n

	for r in rows:
		provider = registry.get(r.provider)
		r["provider_registered"] = provider is not None
		r["settings_doctype"] = getattr(provider, "settings_doctype", "") or ""
		# Drives the card's "Catalog Service" jump: a house we order from by
		# phone has no service-side health page to open.
		r["remote_catalog"] = has_remote_catalog(provider)
		r["health"] = health_by_provider.get(r.provider) or {}
		r["summary"] = _card_summary(r, facts_by_provider.get(r.provider) or {})
		r["orders"] = order_counts.get(
			r.name, {"draft": 0, "in_flight": 0, "attention": 0})
		r["catalog_lanes"] = catalog_sync.lane_health(r.provider) if local["local"] else None

	return {
		"distributors": rows,
		"providers": [{
			"code": code,
			"settings_doctype": getattr(p, "settings_doctype", "") or "",
			"used_by": [r.name for r in rows if r.provider == code],
		} for code, p in sorted(registry.items())],
		"can_write": bool(frappe.has_permission("Distributor", ptype="write")),
		# The Catalog Service and Drop-ship Center pages are the drop-ship layer's:
		# no buttons without it, nor while its extension is switched off.
		"dropship_layer": dropship_hooks.layer_active(),
		# Local catalog only: "catalog worker not running" instead of a catalog
		# that silently stops syncing.
		"catalog_local": local,
		"can_sync_now": bool(local["local"]) and "System Manager" in frappe.get_roles(),
		"capabilities": {CATALOG: catalog},
	}


def _card_summary(row, facts: dict) -> list:
	"""The card's read-only line from the provider's hub_summary facts: Ordering
	is live only when this row is enabled AND the provider's Allow live orders
	switch is on, and says which layer is off otherwise."""
	out = []
	if "live_orders" in facts:
		off = [reason for reason, is_off in (
			(frappe._("disabled"), not row.enabled),
			(frappe._("Allow live orders is off"), not facts["live_orders"])) if is_off]
		out.append({"label": frappe._("Ordering"),
			"value": frappe._("off ({0})").format(", ".join(off)) if off else frappe._("live"),
			"tab": SUMMARY_TABS["live_orders"]})
	if facts.get("ship_to_store"):
		out.append({"label": frappe._("Ship to store"), "value": facts["ship_to_store"],
			"tab": SUMMARY_TABS["ship_to_store"]})
	return out


@frappe.whitelist()
def toggle_flag(distributor: str, flag: str, value) -> dict:
	"""Flip one routing flag on one distributor.

	Doc-level write permission enforced on top of the role gate (a Purchase
	Manager without a doc share still passes — doctype perms grant write —
	but User Permissions are honoured).

	Args:
	    distributor: Distributor name.
	    flag: One of the hub's togglable flags (others are refused).
	    value: Truthy/falsy.

	Returns:
	    {name, flag, value: 0|1}.

	Permissions:
	    Hub write roles + doc-level write check.
	"""
	frappe.only_for(WRITE_ROLES)
	if flag not in TOGGLABLE_FLAGS:
		frappe.throw(frappe._("Flag '{0}' cannot be toggled from the hub.").format(flag))
	doc = frappe.get_doc("Distributor", distributor)
	doc.check_permission("write")
	doc.db_set(flag, 1 if frappe.utils.cint(value) else 0)
	return {"name": doc.name, "flag": flag, "value": int(doc.get(flag))}


@frappe.whitelist()
def reorder(names) -> dict:
	"""Persist a FULL permutation of distributor names as priorities 1..N.

	Partial lists are rejected — a missing row would silently keep a stale
	priority that may collide with the new ones.

	Args:
	    names: Every Distributor name exactly once, in the new order —
	        list or JSON string.

	Returns:
	    {"order": names}.

	Permissions:
	    Hub write roles.
	"""
	frappe.only_for(WRITE_ROLES)
	if isinstance(names, str):
		names = frappe.parse_json(names)
	names = [str(n) for n in (names or [])]
	existing = {r.name for r in frappe.get_all("Distributor")}
	if set(names) != existing or len(names) != len(existing):
		frappe.throw(frappe._(
			"Reorder must list every distributor exactly once (got {0}, have {1})."
		).format(len(names), len(existing)))
	for idx, name in enumerate(names, start=1):
		frappe.db.set_value(
			"Distributor", name, "priority", idx, update_modified=False)
	return {"order": names}


@frappe.whitelist()
def test_connection(distributor: str) -> dict:
	"""Run the provider's live connection probe for one distributor.

	Read-role gated: it is a diagnostic with no order-side effects
	(providers keep probes read-only).

	Args:
	    distributor: Distributor name.

	Returns:
	    The provider's connection_test() result.

	Permissions:
	    Hub read roles.
	"""
	frappe.only_for(READ_ROLES)
	code = frappe.db.get_value("Distributor", distributor, "provider")
	if not code:
		frappe.throw(frappe._("Unknown distributor '{0}'.").format(distributor))
	return get_provider(code).connection_test()


@frappe.whitelist()
def sync_now(distributor: str) -> dict:
	"""Queue every lane of one house's local catalog now (the hub's Sync now).

	Each lane's schedule is skipped; no guard is: a short, truncated or shrunk
	feed file is refused as on a scheduled run.

	Args:
	    distributor: Distributor name.

	Returns:
	    {enqueued: [lane sources], deduplicated: [lane sources already queued or running], worker_running}.
	"""
	frappe.only_for("System Manager")
	from ffl_integrations.catalog_local import is_local

	if not is_local():
		frappe.throw(frappe._("Sync now is for a local catalog (Data Service Settings › Catalog source = Local)."))
	code = frappe.db.get_value("Distributor", distributor, "provider")
	if not code:
		frappe.throw(frappe._("Unknown distributor '{0}'.").format(distributor))
	return catalog_sync.sync_now(code)
