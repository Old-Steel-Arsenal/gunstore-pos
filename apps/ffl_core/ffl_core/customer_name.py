"""Individual customer name: keep Customer Name and the structured parts in sync.

ERPNext's Customer has one required free-text ``customer_name`` (this site's
``cust_master_name`` is "Customer Name", so it is also the docname). The
bound-book identity lives in the custom parts ``ffl_first_name`` /
``middle_name`` / ``ffl_last_name`` / ``name_suffix`` (setup/customer_identity).
Until now the two had to be typed twice at the register. Rule, applied to
Individuals only:

* Customer Name blank, parts present → compose ``First Middle Last Suffix``.
  Runs in ``before_insert`` (the docname is taken from customer_name during
  insert, BEFORE validate) and again in ``validate`` for later saves.
* Parts blank, Customer Name present → split the display name (generational
  suffix pulled out even mid-name). This keeps every "customer_name only"
  creation path — Woo order sync, Desk, imports — valid, and gives such
  customers a bound-book identity the Trade-in prefill can read. When an
  existing customer's Customer Name is later corrected, the parts are split
  again from the new name, so the Trade-in prefill (which prefers the stored
  parts) never shows a stale spelling.

Compose never overwrites a typed display name; split only overwrites parts
when the display name itself was changed.
"""
from __future__ import annotations

import frappe

from ffl_core.setup.customer_identity import _SUFFIX_OPTIONS

# Canonical spellings are the name_suffix Select's own options (the value must
# be one of them or Frappe drops it); keyed by lower-case for matching.
NAME_SUFFIXES = {opt.lower(): opt for opt in _SUFFIX_OPTIONS.split("\n") if opt}


def split_display_name(name: str | None) -> dict:
	"""'pat lee IV Sampleton' → first pat, middle lee, last
	Sampleton, suffix IV. Single token → first only."""
	suffix, core = "", []
	for tok in (name or "").split():
		canonical = NAME_SUFFIXES.get(tok.lower().strip("."))
		if not suffix and canonical and core:
			suffix = canonical
		else:
			core.append(tok)
	return {
		"first": core[0] if core else "",
		"middle": " ".join(core[1:-1]) if len(core) > 2 else "",
		"last": core[-1] if len(core) > 1 else "",
		"suffix": suffix,
	}


def compose_display_name(first, middle, last, suffix) -> str:
	return " ".join(p for p in ((first or "").strip(), (middle or "").strip(),
		(last or "").strip(), (suffix or "").strip()) if p)


def sync_individual_name(doc, method=None) -> None:
	"""doc_events before_insert + validate on Customer (see module docstring)."""
	if doc.get("customer_type") != "Individual":
		return
	first = (doc.get("ffl_first_name") or "").strip()
	last = (doc.get("ffl_last_name") or "").strip()
	display = (doc.get("customer_name") or "").strip()
	if not display and (first or last):
		doc.customer_name = compose_display_name(first, doc.get("middle_name"), last, doc.get("name_suffix"))
		return
	if not display:
		return
	before = doc.get_doc_before_save() if not doc.is_new() else None
	renamed = bool(before) and (before.get("customer_name") or "").strip() != display
	if renamed or (not first and not last):
		parts = split_display_name(display)
		doc.ffl_first_name = parts["first"]
		doc.ffl_last_name = parts["last"]
		doc.middle_name = parts["middle"] if renamed else ((doc.get("middle_name") or "").strip() or parts["middle"])
		doc.name_suffix = parts["suffix"] if renamed else ((doc.get("name_suffix") or "").strip() or parts["suffix"])


# (doctype, column naming the customer, extra condition) for every customer-linked
# document that keeps a copy of the display name. A Quotation names its party in
# ``party_name`` and may be addressed to a Lead.
_NAME_SNAPSHOTS = (
	("Sales Invoice", "customer", ""),
	("Sales Order", "customer", ""),
	("POS Invoice", "customer", ""),
	("Delivery Note", "customer", ""),
	("Quotation", "party_name", " and quotation_to='Customer'"),
)


def refresh_customer_name_snapshots(customer: str, customer_name: str) -> None:
	"""Write *customer_name* onto *customer*'s open sales documents.

	Shared by the Edit form's rename (``manual_order.rename_customer``) and the
	customer merge (``customer_profile.merge_customer``): Sales Invoice, Sales
	Order, POS Invoice, Delivery Note and Quotation (drafts and submitted).
	Cancelled documents keep their historical spelling on purpose, and
	bound-book records (FFL Disposition / 4473) are never touched.
	"""
	for doctype, column, extra in _NAME_SNAPSHOTS:
		frappe.db.sql(
			"""update `tab{0}` set customer_name=%s
			where {1}=%s{2} and docstatus < 2""".format(doctype, column, extra),
			(customer_name, customer))
