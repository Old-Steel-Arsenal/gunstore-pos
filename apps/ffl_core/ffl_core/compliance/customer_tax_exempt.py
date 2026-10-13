"""Customer standing tax exemption — enforced on every Sales Order / Sales
Invoice validate, not just at create.

Customer.is_tax_exempt (a resale/exemption certificate on file — see
setup/customer_tax.py) is the ONE persisted signal driving 0% tax for a
customer's orders; ffl_core.api.manual_order.create_order applies it by
leaving the Sales Order / Sales Invoice's taxes_and_charges blank at
construction (see manual_order._reassert_tax_exemption).

Scope: Sales Order + Sales Invoice ONLY — the two doctypes create_order
builds. POS Invoice (ERPNext's native Point-of-Sale screen) is deliberately
NOT wired to this hook: every tax-exempt sale in this business is a B2B
dealer/resale order that goes through Create Order (manual_order.py), never
the walk-in POS screen, and POS Invoice already has its own tax semantics —
guard_pos_exemption (interstate_tax.py) treats an in-store pickup as always
taxed and only guards against a mis-picked Out-of-State template. If a
tax-exempt customer ever needs to be rung up through the native POS screen,
that's a new requirement for product/compliance to scope, not something this
hook should silently start doing.

That alone doesn't hold. ERPNext's AccountsController.validate() ALWAYS calls
`self.set_missing_values(for_validate=True)` (unconditionally, on every
insert/save/submit — see accounts_controller.py around
`if self.get("_action") ... self.set_missing_values(for_validate=True)`).
For an is_pos Sales Invoice that reruns set_pos_fields, and — because
for_validate=True's semantics is "fill in whatever is still BLANK" — it
refills taxes_and_charges from the POS Profile's own default template. A
mere pre-save clear (manual_order._reassert_tax_exemption) gets silently
undone by that SAME save's own validate() call, because blank is exactly
what triggers the refill. Prod evidence (2026-07-09):
A tax-exempt order paid in full at the counter came
back 8.25% taxed for exactly this reason.

This doc_events hook is the actual enforcement point. frappe's
Document.hook() decorator runs the doctype's own validate() method FIRST,
THEN the registered doc_events hooks in order (see frappe/model/document.py's
`compose`) — so re-clearing here, positioned LAST in the Sales Invoice /
Sales Order "validate" doc_events list (after apply_interstate_tax AND
apply_shipping_charge), runs strictly after everything else that could touch
tax and wins unconditionally. It survives ERPNext's automatic refill on every
future re-validate of the document too, not just the moment create_order
first builds it.

Preserves the shipping charge "Actual" row (compliance.shipping_charge) —
that's freight, not sales tax, and must survive an exempt customer's clear
exactly like it survives on the Sales Order. clear_tax_keep_freight (below)
is the ONE place that decides what survives the clear; manual_order's own
imperative pre-save call (_reassert_tax_exemption) reuses it too, so the two
call sites can never diverge on freight handling again (
they used to each reimplement this and disagreed: the
imperative version wiped the freight row too, harmless only because
apply_shipping_charge unconditionally rebuilds it from ffl_shipping_amount on
the very next validate, but a maintenance trap waiting for that ordering to
change).

Wiring order matters for BOTH reasons this hook needs to run last:
  1. after apply_interstate_tax — an exempt customer's 0% must win over
     anything that hook picked (including the Out-of-State template).
  2. after apply_shipping_charge — that hook is what puts the freight
     "Actual" row into `taxes` in the first place (materialized from
     ffl_shipping_amount); clear_tax_keep_freight's `keep` logic only has a
     freight row to preserve if apply_shipping_charge already ran THIS
     validate cycle. Running before it would still self-heal on the next
     validate (apply_shipping_charge doesn't care what else is in the table),
     but would leave a momentary freight-less state between the two hooks.

The Sales Order never independently reintroduces tax today (SellingController
only pulls a default template through ERPNext's Tax Rule doctype, which this
business doesn't use), but is wired for symmetry with interstate_tax and in
case that ever changes.
"""
from __future__ import annotations

import frappe

from ffl_core.compliance.channel_invoice import is_channel_mirrored_invoice
from ffl_core.compliance.shipping_charge import apply_shipping_charge
from ffl_core.setup.shipping_charge import shipping_account_name


def clear_tax_keep_freight(doc) -> None:
	"""Blank taxes_and_charges and strip every `taxes` row EXCEPT the shipping
	charge freight row (identified by its dedicated account head — see
	compliance.shipping_charge) — THE one definition of what "tax-exempt, but
	still billing freight" looks like on a Sales Order / Sales Invoice.
	Shared by enforce_customer_tax_exemption (this module's validate hook) AND
	manual_order._reassert_tax_exemption (the imperative pre-save clear) so
	the two can never again disagree on freight handling.
	No-op if already clean, to avoid a needless recompute on every validate."""
	company = getattr(doc, "company", None)
	shipping_account = shipping_account_name(company) if company else None
	taxes = doc.get("taxes") or []
	keep = [t for t in taxes if shipping_account and t.account_head == shipping_account]
	if not doc.get("taxes_and_charges") and len(taxes) == len(keep):
		return
	doc.taxes_and_charges = None
	doc.set("taxes", keep)
	# Re-derive the freight row with its tax rows gone: on a tax-inclusive order
	# it held S − Σt and is the whole customer-paid shipping again. The amount
	# rule lives only in shipping_charge; when it rebuilt, it also recalculated.
	if doc.get("ffl_shipping_tax_inclusive") and apply_shipping_charge(doc):
		return
	doc.calculate_taxes_and_totals()


def enforce_customer_tax_exemption(doc, method: str | None = None) -> None:
	if int(getattr(doc, "is_return", 0) or 0) or int(getattr(doc, "is_consolidated", 0) or 0):
		return
	# A channel-mirrored invoice (web, GunBroker) carries the tax that channel
	# collected at checkout — the exemption engine (interstate_tax) keeps its
	# hands off those; do the same.
	if is_channel_mirrored_invoice(doc):
		return
	customer = getattr(doc, "customer", None)
	if not customer:
		return
	if not frappe.db.get_value("Customer", customer, "is_tax_exempt"):
		return
	clear_tax_keep_freight(doc)
