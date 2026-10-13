"""Payments hub — the Settings-page entry for EVERY card processor.

One custom page (``/app/payment-hub``) replaces the per-processor Settings
shortcut, the same shape ``distributor-hub`` gave the wholesale houses: one
card per processor with its enable switch, charge-side health and a
connection probe. Credentials stay in each processor's own Single doctype
(``entry.settings_doctype``) — the hub only links to it, so the next
processor is a registry entry here plus its own Single, and the workspace
card never changes again.

Today the registry holds one processor (Payroc). The page exists anyway
because the workspace card is the thing being future-proofed: "Payments"
stays the door when the second processor arrives.

Permission model mirrors Payroc Settings: System Manager only — the enable
switch arms live money movement, so it answers to the same audience as the
credentials it sits next to.
"""

from __future__ import annotations

import frappe
from frappe import _

from ffl_core.utils import single_flag
from ffl_integrations.integration_gate import payroc_enabled, payroc_ready
from ffl_integrations.payroc.setup import MODE_OF_PAYMENT_NAME

ROLES = ("System Manager",)

# Static facts only, so overview() can ship rows to the client as-is.
# Callables live in _READY / _PROBE below, keyed by the same code.
# `health_fields` are (label, fieldname) pairs read off the Single — the
# card's "is money actually flowing" line.
PROCESSORS = (
	frappe._dict(
		code="payroc",
		label="Payroc",
		kind="Credit Card",
		settings_doctype="Payroc Settings",
		mode_of_payment=MODE_OF_PAYMENT_NAME,
		enabled_default=1,
		health_fields=(
			("Token refreshed", "last_token_refreshed_at"),
			("Last charge", "last_charge_at"),
		),
	),
)


def _entry(code: str) -> frappe._dict:
	for p in PROCESSORS:
		if p.code == code:
			return p
	frappe.throw(_("Unknown payment processor '{0}'.").format(code))


@frappe.whitelist()
def overview() -> dict:
	"""Everything the payment-hub page renders, in one round trip.

	``enabled`` is read through ``single_flag`` (raw ``tabSingles`` row,
	docfield default when absent) — the same defence the integration gate
	uses, so a site that never saved the settings form reads ON, not OFF.

	Returns:
	    {processors: [{code, label, kind, settings_doctype, mode_of_payment,
	    enabled, ready, can_write, health: [{label, at}]}], can_write}.
	    ``ready`` is enabled AND credentials present — the switch can be on
	    while the key is missing, and the card has to show that as its own
	    state, not as "working".

	Permissions:
	    System Manager (mirrors Payroc Settings).
	"""
	frappe.only_for(ROLES)
	rows = []
	for p in PROCESSORS:
		can_write = bool(frappe.has_permission(p.settings_doctype, ptype="write"))
		rows.append({
			"code": p.code,
			"label": p.label,
			"kind": p.kind,
			"settings_doctype": p.settings_doctype,
			"mode_of_payment": p.mode_of_payment,
			"enabled": 1 if single_flag(
				p.settings_doctype, "enabled", default=p.enabled_default) else 0,
			"ready": bool(_READY[p.code]()),
			"can_write": can_write,
			"health": [{
				"label": label,
				"at": frappe.db.get_single_value(p.settings_doctype, fieldname),
			} for label, fieldname in p.health_fields],
		})
	return {"processors": rows, "can_write": any(r["can_write"] for r in rows)}


@frappe.whitelist(methods=["POST"])
def toggle(processor: str, value) -> dict:
	"""Flip one processor's enable switch, through the real settings save.

	Deliberately ``doc.save()``, not ``db_set``: the settings form makes the
	credential fields mandatory while the switch is on (a store cannot arm a
	processor it holds no key for), and a hub that wrote the flag straight to
	the table would be a side door around exactly that rule. Disabling never
	trips it — credentials are only demanded while ``enabled`` stays 1.

	Args:
	    processor: Registry code (e.g. ``"payroc"``).
	    value: Truthy/falsy.

	Returns:
	    {code, enabled: 0|1}.

	Permissions:
	    System Manager, plus the doc-level write check ``doc.save()`` runs.
	"""
	frappe.only_for(ROLES)
	p = _entry(processor)
	doc = frappe.get_doc(p.settings_doctype)
	doc.enabled = 1 if frappe.utils.cint(value) else 0
	doc.save()
	return {"code": p.code, "enabled": int(doc.enabled)}


@frappe.whitelist()
def test_connection(processor: str) -> dict:
	"""Run the processor's read-only connection probe.

	Args:
	    processor: Registry code (e.g. ``"payroc"``).

	Returns:
	    {checks: [{label, ok, detail}]} — the shape the distributor hub
	    renders, so the two pages share one mental model. ``ok`` is true,
	    false, or null for "not applicable" (e.g. the switch is off, so
	    nothing was probed — that is a state, not a failure).

	Permissions:
	    System Manager.
	"""
	frappe.only_for(ROLES)
	p = _entry(processor)
	return {"checks": _PROBE[p.code]()}


def _probe_payroc() -> list[dict]:
	"""Payroc's probe rides :func:`payroc.api.discover_terminals` — the
	authenticate call that lists the key's allowed terminals — rather than a
	second HTTP path that could drift from the one the charge flow trusts.
	Its three answers map onto checks: ``{"skip": True}`` after the switch
	said ON can only mean credentials (that is the one other thing the inert
	gate tests), an ``{"error": ...}`` body is the gateway refusing, and a
	normal body proves auth end to end."""
	from ffl_integrations.payroc.api import discover_terminals

	if not payroc_enabled():
		return [{"label": _("Switch"), "ok": None, "detail": _(
			"Payroc is switched off — nothing was probed. The POS records "
			"Credit Card as a plain tender; the swipe happens on the "
			"standalone terminal.")}]
	checks = [{"label": _("Switch"), "ok": True, "detail": _("enabled")}]
	resp = discover_terminals() or {}
	if resp.get("skip"):
		checks.append({"label": _("Credentials"), "ok": False, "detail": _(
			"Merchant API Key is missing — open Credentials and set it.")})
		return checks
	if resp.get("error"):
		checks.append({"label": _("Gateway"), "ok": False,
			"detail": str(resp["error"])[:300]})
		return checks
	terminals = resp.get("allowedTerminals") or []
	checks.append({"label": _("Gateway"), "ok": True, "detail": _(
		"authenticated — {0} terminal(s) allowed on this key").format(len(terminals))})
	default_terminal = frappe.db.get_single_value("Payroc Settings", "default_terminal")
	checks.append({"label": _("Default terminal"),
		"ok": True if default_terminal else None,
		"detail": default_terminal or _("not set")})
	return checks


_READY = {"payroc": payroc_ready}
_PROBE = {"payroc": _probe_payroc}
