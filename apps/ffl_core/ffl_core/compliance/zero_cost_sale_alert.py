"""Warn — never block — when a gun with no recorded cost is being sold.

A firearm whose acquisition cost was never captured sells at ~100% gross
margin on paper: the sale books revenue with essentially no COGS, so the P&L, the
Sales Report and every margin figure downstream are wrong by the whole cost of
the gun. The receive-side guard (feature A) stops NEW guns entering that way, but
the guns already in stock are only discovered when someone sells one — which is
exactly the moment this alert exists for.

**It does not block the sale**. A customer is
standing at the counter; the cost is a bookkeeping fact that can be corrected
after the receipt prints, and a hard stop at the register would trade a wrong
margin for a lost sale.

So this is a plain `msgprint` — deliberately a MODAL, not the toast
(`alert=True`) that dealer_transfer.auto_enable_update_stock_for_dealer_shipment
uses for its own non-blocking notice. The difference is that this message is
ACTIONABLE: it carries a link per gun to the Serial No form where the
"Correct Cost" button sits, and a toast dismisses itself before anyone can click
one. The volume justifies it too — the trigger set is the ~36 known unpriced
guns, so this interrupts the register rarely, not on every sale.

Three kinds of $0 are deliberately NOT warned about, because for them $0 is the
fact rather than a gap:
  * ``zero_cost_confirmed`` — someone already stated on the record that this gun
    was free, with a reason (feature A). Warning again would train people to
    dismiss the alert, which is how alerts stop working.
  * ``is_consignment`` — a gun consigned TO us was never bought; its payout is
    computed from the sale price by compliance/consignment.py, not from a cost.
  * ``is_transfer_gun`` — a customer's own gun in custody. It is not ours and is
    $0 by design (see compliance/purchase.py's custody branch).

Reads ``Serial No.purchase_rate`` rather than the stock ledger: it is the
firearm-COGS SSOT the Sales Report itself uses, cost_correction keeps it equal to
the ledger valuation, and this runs on the POS hot path where a
per-serial ledger read would cost far more than it tells us.

Wired onto POS Invoice + Sales Invoice ``validate`` (hooks.py).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from ffl_core.firearm import is_firearm_item
from ffl_core.serials import serials_for_line

# At or below this, a recorded cost is a placeholder rather than a price — the
# $0.01 migration guns and $0 receipts. Mirrors api/cost_correction._HINT_OLD_MAX,
# which flags the same guns from the other end (correcting one).
_PLACEHOLDER_MAX = 1.0


def warn_on_unpriced_firearm_sale(doc, method: str | None = None) -> None:
    """Alert (without blocking) when a gun being sold has no recorded cost.

    Wired onto POS Invoice + Sales Invoice `validate` (hooks.py).
    """
    if int(getattr(doc, "is_return", 0) or 0):
        return  # a credit note sells nothing; the cost gap belongs to the sale
    if int(getattr(doc, "is_consolidated", 0) or 0):
        return  # the POS Invoice this rolls up already warned at the register

    unpriced = []
    for row in (getattr(doc, "items", None) or []):
        # Firearm lines only — the same first filter every sibling hook applies
        # (compliance/purchase.py, firearm.py). A serialized accessory carries no
        # bound-book cost story, and its $0 is not the gap this warns about.
        if not is_firearm_item(row.item_code):
            continue
        for serial in serials_for_line(row):
            info = frappe.db.get_value(
                "Serial No", serial,
                ["purchase_rate", "zero_cost_confirmed", "is_consignment",
                 "is_transfer_gun"],
                as_dict=True,
            )
            if not info:
                continue
            if info.zero_cost_confirmed or info.is_consignment or info.is_transfer_gun:
                continue
            if flt(info.purchase_rate) <= _PLACEHOLDER_MAX:
                unpriced.append(serial)

    if not unpriced:
        return

    links = ", ".join(
        '<a href="/app/serial-no/{0}" target="_blank">{1}</a>'.format(
            frappe.utils.quote(serial), frappe.utils.escape_html(serial))
        for serial in dict.fromkeys(unpriced)
    )
    frappe.msgprint(
        _("{0} no recorded acquisition cost, so this sale will show ~100% "
          "margin until it is corrected. The sale is NOT blocked — finish the "
          "customer first, then open the gun and use <b>Correct Cost</b>: {1}"
          ).format(
            _("These guns have") if len(unpriced) > 1 else _("This gun has"), links),
        title=_("Gun sold with no recorded cost"),
        indicator="orange",
    )
