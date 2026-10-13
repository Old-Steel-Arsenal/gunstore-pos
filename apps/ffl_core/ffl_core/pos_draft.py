"""Building a POS Invoice server-side — what the POS UI does for you and an
insert() does not.

Shared by every path that opens a counter draft without a cashier in the POS
screen: a web order's in-store pickup (ffl_woo_sync.woocommerce.pickup) and a
counter order's pickup ticket (ffl_core.api.counter_pickup). The differences
from a UI-built invoice are load-bearing and were found one at a time in
production; they live here ONCE so a third builder inherits them.
"""
from __future__ import annotations

import frappe
from frappe import _

# Fields the POS UI gets from the profile and a server-side insert does NOT.
# `set_pos_fields` copies these inside `if not for_validate`
# (erpnext/accounts/doctype/pos_invoice/pos_invoice.py), and an insert() reaches
# it solely through `set_missing_values(for_validate=True)`. Left unset,
# `update_stock` stays 0 — the gun never leaves stock on Complete Order, and
# `ffl_core.listing.status_sync.on_sales_invoice_submit` marks the listing
# "Reserved" instead of "Sold". Copied explicitly so a server-built invoice
# behaves like every other counter sale.
#
# Deliberately NOT `set_warehouse` / `account_for_change_amount`: those two are
# assigned ABOVE that guard and so already arrive on a server-side insert — and
# `set_warehouse` is a POS Invoice field whose profile counterpart is called
# `warehouse`, so asking the profile for it by that name just errors.
PROFILE_DEFAULTS = ("update_stock", "ignore_pricing_rule")


def open_shift(company: str, *, throw: bool = True) -> frappe._dict | None:
	"""The POS Opening Entry a server-built counter invoice must be rung under.

	NOT "any enabled profile": ``POSInvoice.validate`` calls
	``validate_pos_opening_entry`` unconditionally and throws when the chosen
	profile has no Open entry — on the draft insert too. An earlier cut picked
	the alphabetically-first profile, which worked only while the open shift
	happened to be on that one; on any other register Pickup died with an error
	naming a profile nobody had chosen, and when it did match it filed the sale
	under whoever's shift that was.

	Prefers the caller's own open shift, so the sale lands on the till the person
	clicking is actually working.

	``throw=False`` answers None instead of raising — for a background path
	(the arrival of a ship-to-store gun) that must not fail when the register
	is closed. One query either way.
	"""
	shifts = frappe.get_all(
		"POS Opening Entry",
		filters={"status": "Open", "docstatus": 1, "company": company},
		fields=["name", "pos_profile", "user"],
		order_by="period_start_date desc",
	)
	if not shifts:
		if not throw:
			return None
		frappe.throw(_(
			"No POS shift is open, so a counter order cannot be rung. Open the "
			"register first, then press Pickup again."
		), title=_("No Open Shift"))
	mine = [s for s in shifts if s.user == frappe.session.user]
	return frappe._dict(mine[0] if mine else shifts[0])


def open_drafts_by(link_field: str, names) -> dict[str, str]:
	"""``{linked document: its open draft POS Invoice}`` for many links in ONE
	query — the idempotency lookup both pickup builders make (a web order's
	``woo_online_order``, a counter order's ``ffl_counter_order``), and what
	the Pending Order page asks on every load for every row.

	Two staff on two screens, or one impatient double-click, must not produce
	two drafts: a second draft is a second 4473 target for one gun. The oldest
	draft wins so the answer is stable. Column-guarded: the app code is live
	before ``bench migrate`` adds a new link field.
	"""
	names = [n for n in (names or []) if n]
	if not names or not frappe.db.has_column("POS Invoice", link_field):
		return {}
	out: dict[str, str] = {}
	for row in frappe.get_all("POS Invoice",
			filters={link_field: ("in", names), "docstatus": 0},
			fields=["name", link_field], order_by="creation"):
		link = row.get(link_field)
		if link:
			out.setdefault(link, row.name)
	return out


def profile_defaults(pos_profile: str) -> dict:
	return {
		field: frappe.db.get_value("POS Profile", pos_profile, field)
		for field in PROFILE_DEFAULTS
	}
