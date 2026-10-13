"""Backfill the pay-on-sale stamp onto existing in-stock guns (LABEL ONLY, zero GL).

NOT registered in patches.txt (see the commented entry there) — this does NOT
fire on `bench migrate`. Like backfill_consignment_serials, it is a deliberate,
owner-driven, one-time data operation, run manually and reviewed by the CPA:

    bench --site <site> execute ffl_core.patches.backfill_pay_on_sale_serials.execute

Why manual + why zero GL: a pay-on-sale gun's acquisition cost is ALREADY parked
in SRBNB — either automatically (an owned Purchase Receipt from a pay-on-sale
supplier credits Stock Received But Not Billed) or by a hand-posted reconciliation
+ JE (such a JE puts the gun's cost into SRBNB). The GL is therefore
already correct; all this backfill does is write the per-serial STAMP the sale
settlement reads (pay_on_sale / pay_on_sale_supplier / srbnb_cost /
pay_on_sale_settled=0), so guns received before feature #3 shipped settle
correctly when they sell. It posts NO Journal Entry and NO stock movement.

Which guns: in-stock (status=Active), OWNED (not consignment), firearm serials
whose source supplier is flagged pay_on_sale — resolved per gun from its FFL
Acquisition. srbnb_cost is taken as the serial's persisted purchase_rate (the
firearm-cost SSOT, kept equal to the SRBNB-parked amount and to valuation by the
receive stamp and by the PS-7 cost-correction path). A gun whose supplier cannot
be resolved to a pay_on_sale Supplier, or whose purchase_rate is not positive, is
LEFT UNTAGGED — it will then fail closed at sale (settlement BLOCKs on a missing
supplier) rather than be mis-stamped, prompting a manual fix. Idempotent: never
re-stamps an already-tagged serial.

Supplier resolution chain (per serial, first hit wins), all off the submitted
FFL Acquisition for the serial:
  1. Acquisition.purchase_receipt -> Purchase Receipt.supplier   (the direct link)
  2. Acquisition.source_ffl       -> Supplier.ffl_number          (FFL-number match)
  3. Acquisition.source_name      -> Supplier.supplier_name       (name match)
Only a resolved Supplier with pay_on_sale=1 triggers a stamp.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt


def resolve_pay_on_sale_supplier(serial: str) -> str | None:
    """The pay_on_sale Supplier this serial was acquired from, or None.

    Resolves off the serial's submitted FFL Acquisition (purchase_receipt →
    source_ffl → source_name) and returns the Supplier name ONLY when it exists
    and is flagged pay_on_sale. Returns None otherwise (no acquisition, no
    resolvable supplier, or a non-pay-on-sale supplier)."""
    acq = frappe.db.get_value(
        "FFL Acquisition",
        {"serial": serial, "docstatus": 1},
        ["purchase_receipt", "source_ffl", "source_name"],
        as_dict=True, order_by="creation desc",
    )
    if not acq:
        return None

    candidate = None
    if acq.purchase_receipt:
        candidate = frappe.db.get_value("Purchase Receipt", acq.purchase_receipt, "supplier")
    if not candidate and (acq.source_ffl or "").strip():
        candidate = frappe.db.get_value("Supplier", {"ffl_number": acq.source_ffl.strip()}, "name")
    if not candidate and (acq.source_name or "").strip():
        candidate = frappe.db.get_value("Supplier", {"supplier_name": acq.source_name.strip()}, "name")

    if not candidate or not frappe.db.exists("Supplier", candidate):
        return None
    if not frappe.db.get_value("Supplier", candidate, "pay_on_sale"):
        return None
    return candidate


def execute() -> dict:
    """Stamp the pay-on-sale marker onto every eligible in-stock gun. Idempotent;
    label-only (zero GL). Returns {stamped, skipped}."""
    from ffl_core.firearm import is_firearm_item
    from ffl_core.setup.pay_on_sale import ensure

    # Fixture-ordering safety (matches backfill_consignment_serials): the stamp
    # columns must exist before we read/write them, even if this is ever invoked
    # before after_migrate has run.
    ensure()

    rows = frappe.get_all(
        "Serial No",
        filters={"status": "Active", "is_consignment": 0, "pay_on_sale": 0},
        fields=["name", "item_code", "purchase_rate"],
    )
    stamped = 0
    skipped = 0
    for r in rows:
        if not is_firearm_item(r.item_code):
            continue
        cost = flt(r.purchase_rate)
        if cost <= 0:
            skipped += 1
            continue  # no determinable SRBNB-parked amount — leave untagged
        supplier = resolve_pay_on_sale_supplier(r.name)
        if not supplier:
            skipped += 1
            continue  # not a resolvable pay-on-sale source — leave untagged (fail closed at sale)
        frappe.db.set_value(
            "Serial No", r.name,
            {
                "pay_on_sale": 1,
                "pay_on_sale_supplier": supplier,
                "srbnb_cost": cost,
                "pay_on_sale_settled": 0,
            },
            update_modified=False,
        )
        stamped += 1

    if stamped:
        print(f"backfill_pay_on_sale_serials: stamped {stamped} Serial No record(s), "
              f"skipped {skipped}")
    return {"stamped": stamped, "skipped": skipped}
