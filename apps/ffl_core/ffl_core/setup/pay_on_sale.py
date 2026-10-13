"""Idempotent setup for pay-on-sale settlement: the Supplier flag + the four
Serial No stamp fields + the Journal Entry source-tag field.

A "pay-on-sale" supplier (e.g. a firearms wholesaler) ships guns the store receives
as OWNED inventory with the cost parked in **Stock Received But Not Billed**
(SRBNB — a liability), settled to a named Account Payable only when the gun
sells. That model needs per-serial state, because SRBNB carries no serial and no
party (verified: a real SRBNB journal line has party=null), so "is this
gun's cost still in SRBNB?" can only be answered by a flag stamped on the Serial
No — never by scanning the GL.

Creates (mirrors setup/consignment.py's ensure-pattern exactly):
  - Supplier: pay_on_sale (Check) — marks a supplier whose guns settle at sale.
    The first such supplier is turned on (via backfill / manually); future same-model suppliers
    can opt in without code.
  - Serial No: pay_on_sale (Check), pay_on_sale_supplier (Link -> Supplier — the
    settlement party, locked at park time), srbnb_cost (Currency — the EXACT
    amount parked in SRBNB = the settlement amount), pay_on_sale_settled
    (Check — per-serial idempotency guard). Stamped at receive / backfill, read
    at sale.
  - Journal Entry: ffl_pay_on_sale_source (Data, read-only) — the source
    POS/Sales Invoice name, stamped by compliance/pay_on_sale.settle_* so the
    on_cancel reversal can find + cancel the JE (mirrors ffl_consignment_source).

Read-only / editability mirrors consignment: pay_on_sale / pay_on_sale_supplier /
srbnb_cost are staff-editable (0) so a mis-stamped gun can be corrected on the
form; pay_on_sale_settled is read_only (1) — flipping it by hand would desync the
GL (the settlement JE is the SSOT for whether AP was posted).

Like setup/consignment.py these fields are created via create_custom_fields at
after_migrate (module=None → NOT captured by the ffl_core Custom Field fixture
filter, and NOT committed to fixtures/custom_field.json — the same route the
inbound-consignment fields take). Lazy + never raises (a thrown after_migrate
hook aborts the whole `bench migrate`); failures are logged and retried on the
next call.
"""
from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.utils import commit

_SUPPLIER_FIELDS = {
    "Supplier": [
        {
            "fieldname": "pay_on_sale",
            "fieldtype": "Check",
            "label": "Pay on Sale (settle at sale)",
            "insert_after": "supplier_type",
            "description": (
                "Guns from this supplier are received as owned inventory with cost "
                "parked in Stock Received But Not Billed, and the payable is booked "
                "automatically when the gun sells."
            ),
            "read_only": 0,
        }
    ]
}
_SERIAL_FIELDS = {
    "Serial No": [
        {
            "fieldname": "pay_on_sale",
            "fieldtype": "Check",
            "label": "Pay on Sale",
            # Anchor on a CORE Serial No field (always present) rather than a
            # sibling custom field, so ordering never depends on which setup
            # module ran first.
            "insert_after": "purchase_rate",
            "read_only": 0,
        },
        {
            "fieldname": "pay_on_sale_supplier",
            "fieldtype": "Link",
            "options": "Supplier",
            "label": "Pay-on-Sale Supplier",
            "insert_after": "pay_on_sale",
            "depends_on": "pay_on_sale",
            "read_only": 0,
        },
        {
            "fieldname": "srbnb_cost",
            "fieldtype": "Currency",
            "label": "SRBNB Parked Cost",
            "insert_after": "pay_on_sale_supplier",
            "depends_on": "pay_on_sale",
            "read_only": 0,
        },
        {
            "fieldname": "pay_on_sale_settled",
            "fieldtype": "Check",
            "label": "Pay-on-Sale Settled",
            "insert_after": "srbnb_cost",
            "depends_on": "pay_on_sale",
            # System state — the settlement JE is the SSOT. Hand-flipping it would
            # desync GL, so read-only in the UI (db.set_value still writes it).
            "read_only": 1,
        },
    ]
}
_JOURNAL_ENTRY_FIELDS = {
    "Journal Entry": [
        {
            # Stamped by compliance/pay_on_sale.settle_pay_on_sale_on_submit with
            # the source POS/Sales Invoice name, so the on_cancel reversal can find
            # — and cancel — the JE this invoice posted (mirrors
            # ffl_consignment_source; a plain Data field, not a GL/party reference,
            # so ERPNext's own cancel-time linked-doc check can't see it — reversing
            # it is entirely the hook's job).
            "fieldname": "ffl_pay_on_sale_source",
            "fieldtype": "Data",
            "label": "Pay-on-Sale Source Invoice",
            "read_only": 1,
            "insert_after": "user_remark",
        }
    ]
}


def srbnb_account(company: str) -> str | None:
    """The company's Stock Received But Not Billed account (the liability the
    gun's cost is parked in until settled). Resolved from the Company master so
    it is portable across sites/abbreviations — never a hard-coded "- <ABBR>"."""
    return frappe.db.get_value("Company", company, "stock_received_but_not_billed")


def payable_account(company: str) -> str | None:
    """The company's default Accounts Payable (Creditors) account — where the
    settlement books the named, party-tagged payable. Pay-on-sale AP lands
    in the standard Creditors sub-ledger (unlike consignment's own Consignment
    Payable account), so a supplier statement reconciles against one Creditors
    balance."""
    return frappe.db.get_value("Company", company, "default_payable_account")


def ensure() -> dict:
    """Create the pay-on-sale custom fields. Idempotent; never raises (runs in
    after_migrate — failures are logged, the next call retries)."""
    try:
        create_custom_fields(
            {**_SUPPLIER_FIELDS, **_SERIAL_FIELDS, **_JOURNAL_ENTRY_FIELDS},
            ignore_validate=True,
        )
        commit()
        return {"ok": 1}
    except Exception:
        frappe.log_error(title="pay_on_sale.ensure failed", message=frappe.get_traceback())
        return {"error": 1}
