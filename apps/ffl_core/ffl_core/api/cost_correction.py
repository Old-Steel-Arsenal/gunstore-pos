"""Manager self-service firearm cost correction (Item page).

Product-ise the manual recipe the owner ran by hand for one gun (cost
0 -> its real cost = Stock Reconciliation + Serial No.purchase_rate)
into a per-serial, manager-gated Item-page action.

Two-layer write for ONE in-stock, owned firearm serial:

  1. **Stock Reconciliation** — a single-serial, valuation-only revaluation.
     ERPNext scopes the revaluation to ONE serial only when it is fed a PRE-BUILT,
     serial-scoped Serial and Batch Bundle (``SerialBatchCreation``) with
     ``reconcile_all_serial_batch=0`` — the config ERPNext's own
     ``test_not_reconcile_all_serial_nos`` exercises (see the inline note at the
     bundle construction below). **Do NOT "simplify" this to the
     ``use_serial_batch_fields=1`` + ``serial_no`` path**: that reads
     ``current_qty`` from the item-warehouse TOTAL, so on an Item with N sibling
     serials it sees current_qty=N and treats ``qty=1`` as "remove N-1 guns"
     (verified: ``quantity_difference=-2`` on a 3-serial Item → sibling loss). That
     is exactly that hazard. Quantity never moves — a non-zero
     ``quantity_difference`` on the draft is a hard error, rolled back before any
     GL posts.

  2. ``Serial No.purchase_rate := new_cost`` — the firearm-COGS SSOT
     (report/sales_report.py). Kept equal to the ledger valuation so report-COGS
     and GL-COGS never split.

Accounting rules:
  * Up-valuation posts ``Dr <the warehouse's stock account> / Cr Stock
    Adjustment`` for the delta; a down-valuation reverses it. Only the COUNTER
    account is ours to pick — the stock leg is resolved by ERPNext from the
    warehouse itself, so a gun out on consignment debits ``Stock at Consignee``
    and one in Stores debits ``Stock In Hand`` with no branching here (pinned by
    test_cost_correction.ConsignedOutStock). **Stock Adjustment is a P&L expense account** —
    this operation records the correction to P&L and touches NO cash/AP account.
    A ~0 -> large jump (an acquisition whose cost was never booked) sets
    ``source_of_cost_hint`` in the response so the operator is reminded a
    bookkeeper may still owe a source-of-cost JE (``Dr Stock Adjustment / Cr
    Cash/AP``). We deliberately do NOT auto-post that JE — whether it is needed
    depends on how the cost was originally (mis)booked, which only a human knows.
  * ``posting_date`` is forced to the server's current day; a frozen
    Accounts/Stock period is rejected cleanly with zero writes.
  * A gun consigned TO us (``Serial No.is_consignment``) is refused
    server-side, not just hidden in the UI. A gun of ours OUT on
    consignment at a dealer is NOT refused — it is still our asset and its
    valuation drives the settlement COGS.
  * The reason is required and stored as a Comment on the reconciliation (the
    doctype has no remarks field) and cross-referenced from the Serial No
    timeline together with the reconciliation number, under the real
    ``frappe.session.user``.

Out of scope (documented for QA/CPA): a gun that is ALREADY SOLD with a wrong
cost is not correctable here (its COGS is already realised) — that needs a
separate JE. FastBound ``cost`` is not synced (not an A&D field; no tax impact).

Pay-on-sale auto-settlement: a ``pay_on_sale``
gun whose acquisition cost is still parked in **SRBNB** (Stock Received But Not
Billed) and not yet settled (``pay_on_sale=1 AND pay_on_sale_settled=0 AND
srbnb_cost>0``) books its correction delta to **SRBNB** — not Stock Adjustment —
so the revaluation adjusts the very liability the store still owes the supplier
(Dr the warehouse's stock account / Cr SRBNB for an up-valuation, reverse for a
down), and ``srbnb_cost`` is re-stamped to the new cost alongside ``purchase_rate`` so the
three-way invariant ``srbnb_cost == purchase_rate == valuation`` holds and the
eventual sale settlement (compliance/pay_on_sale.py) clears SRBNB to zero. The
source-of-cost hint is suppressed on this path (the SRBNB liability IS the
cost source). A settled or non-pay-on-sale (owned-outright) gun keeps the Stock
Adjustment path unchanged — its cost is realised / fully owned.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate, nowtime

from ffl_core.firearm import is_firearm_item

_ROLES = ("Stock Manager", "System Manager")
_SAVEPOINT = "ffl_correct_serial_cost"

# A firearm whose recorded cost is at or below this is treated as a placeholder /
# never-booked acquisition ($0.01 migration guns, $0 pay-on-sale receipts) — a
# jump from there up to a material cost is the case the source-of-cost hint exists to flag.
_HINT_OLD_MAX = 1.0
_HINT_MIN_NEW = 100.0


@frappe.whitelist()
def correct_serial_cost(serial: str, new_cost, reason: str, payout_was_different=0) -> dict:
    """Correct the acquisition cost of one in-stock, owned firearm serial.

    Books a Stock Reconciliation at today's date (the discovery period).
    A pay-on-sale gun whose cost is still parked in SRBNB routes the delta
    to SRBNB instead of Stock Adjustment, keeping the supplier liability
    consistent (PS-7).

    Args:
        serial: Serial No name — must be an Active firearm the store OWNS
            (``is_consignment=0``). Its warehouse does not matter: a gun out on
            consignment at a dealer is still ours and is correctable.
        new_cost: New acquisition cost, strictly > 0 (rounded to 2dp).
        reason: Required free-text audit reason.
        payout_was_different: 1 when the amount actually paid to the seller was different
            (the gun's seller payment was recorded in the cash drawer, on its own or with
            its Receive Goods receipt): the difference is then booked against the same cash
            or bank account. Default 0: only the cost changes and no cash moves.

    Returns:
        {ok, serial, stock_reconciliation, old_cost, new_cost, delta,
        posting_date, payout_adjustment, payout_note, source_of_cost_hint};
        payout_adjustment is the Journal Entry that books the difference when the
        seller's payment for this gun was recorded in the cash drawer, else None;
        payout_note says why nothing was booked when the box was ticked.

    Raises:
        PermissionError: Non-manager caller.
        ValidationError: Every rejected input — always with zero partial
            writes.

    Permissions:
        Stock Manager / System Manager.
    """
    frappe.only_for(_ROLES)

    serial = (serial or "").strip()
    reason = (reason or "").strip()
    if not serial:
        frappe.throw(_("A serial number is required."))
    if not reason:
        frappe.throw(_("A reason is required for the audit trail."))

    try:
        new_cost = flt(new_cost)
    except (TypeError, ValueError):
        frappe.throw(_("New cost must be a number."))
    # Strictly positive. A $0 firearm cost is a write-off/disposal workflow,
    # not a cost correction; a negative cost is nonsense.
    if new_cost <= 0:
        frappe.throw(_("New cost must be greater than 0 (got {0}).").format(new_cost))
    new_cost = flt(new_cost, 2)

    sn = frappe.db.get_value(
        "Serial No", serial,
        ["item_code", "status", "warehouse", "is_consignment", "purchase_rate",
         "pay_on_sale", "pay_on_sale_settled", "srbnb_cost"],
        as_dict=True,
    )
    if not sn:
        frappe.throw(_("Serial No {0} not found.").format(serial))
    if not is_firearm_item(sn.item_code):
        frappe.throw(_("Serial No {0} is not a firearm — cost correction is firearm-only.").format(serial))
    if sn.status != "Active":
        frappe.throw(_(
            "Serial No {0} is {1}, not in stock. Only in-stock (Active) firearms can be "
            "corrected; an already-sold gun's cost is realised and needs a separate journal entry."
        ).format(serial, sn.status or _("unknown")))
    if not sn.warehouse:
        frappe.throw(_("Serial No {0} has no warehouse — it is not in stock.").format(serial))
    # Owned only, enforced server-side (not merely hidden in the picker).
    #
    # OWNED is a property of the GUN, not of where it is standing. A gun OUT on
    # consignment at a dealer is still ours — only its warehouse changed — and
    # its valuation is precisely what decides the settlement COGS, so refusing
    # to correct it left the one stock that most needs correcting with no way to
    # do it (14 guns had to be reconciled by hand). The warehouse guard
    # that used to sit here is therefore gone; the stock leg simply follows that
    # warehouse's own account (see the module docstring).
    #
    # `is_consignment=1` — a gun consigned TO us — stays refused: that one was
    # never store inventory, wherever it is standing.
    if sn.is_consignment:
        frappe.throw(_(
            "Serial No {0} is a consignment firearm — it is not store inventory and its cost "
            "cannot be corrected here."
        ).format(serial))

    company = frappe.db.get_value("Warehouse", sn.warehouse, "company")
    if not company:
        frappe.throw(_("Warehouse {0} has no company.").format(sn.warehouse))

    # Force the posting date to today (the discovery period) and refuse a
    # frozen period cleanly, before any write.
    posting_date = nowdate()
    _reject_if_period_frozen(company, posting_date)

    stock_adj_account, cost_center = _stock_adjustment_accounts(company)
    # PS-7: a pay-on-sale gun whose acquisition cost is still parked
    # in SRBNB (unsettled) books its correction delta to **SRBNB**, not Stock
    # Adjustment — so the revaluation adjusts the very liability the store still
    # owes the supplier (Dr Stock In Hand / Cr SRBNB up, reverse down), keeping
    # book value, srbnb_cost, and the eventual sale-settlement AP all consistent.
    # A settled or non-pay-on-sale gun keeps the Stock Adjustment path (its cost
    # is realised / owned outright — the original #2 behaviour, unchanged).
    srbnb_path = bool(
        sn.pay_on_sale and not sn.pay_on_sale_settled and flt(sn.srbnb_cost) > 0)
    # Symmetry with the settle path's BLOCK: a pay-on-sale, unsettled gun that
    # carries a real purchase cost but NO persisted srbnb_cost is incompletely
    # stamped — its cost may still sit in SRBNB, so silently routing the correction
    # delta to Stock Adjustment (the srbnb_cost<=0 fallthrough) could desync SRBNB
    # once the gun is backfilled + sold. Fail closed and tell the operator to fix
    # the stamp (run the pay-on-sale backfill) first — never guess where the cost is.
    if sn.pay_on_sale and not sn.pay_on_sale_settled and flt(sn.srbnb_cost) <= 0 \
            and flt(sn.purchase_rate) > 0:
        frappe.throw(_(
            "Serial No {0} is flagged pay-on-sale but has no recorded SRBNB cost while carrying "
            "a purchase cost of {1}. Its pay-on-sale stamp is incomplete — run the pay-on-sale "
            "backfill (or set SRBNB Parked Cost on the Serial No) before correcting its cost."
        ).format(serial, flt(sn.purchase_rate, 2)))
    if srbnb_path:
        from ffl_core.setup.pay_on_sale import srbnb_account
        expense_account = srbnb_account(company)
        if not expense_account:
            frappe.throw(_(
                "Serial No {0} is a pay-on-sale gun with its cost still in SRBNB, but company "
                "{1} has no Stock Received But Not Billed account configured."
            ).format(serial, company))
    else:
        expense_account = stock_adj_account

    # Independent, pre-write read of the serial's current book value — the audit
    # "old cost", the hint input, and the up/down direction all derive from it.
    old_cost = _serial_book_value(sn.item_code, sn.warehouse, serial, company)

    user = frappe.session.user

    from erpnext.stock.serial_batch_bundle import SerialBatchCreation

    posting_time = nowtime()
    frappe.db.savepoint(_SAVEPOINT)
    try:
        # Build a Serial and Batch Bundle scoped to EXACTLY this one serial, at the
        # new rate. Feeding the reconciliation a pre-built, serial-scoped bundle
        # (with reconcile_all_serial_batch=0) is the ONLY config under which
        # ERPNext scopes `current_qty` to the named serial. The obvious-looking
        # `use_serial_batch_fields=1` + `serial_no` path does NOT: it takes
        # current_qty from the item-warehouse total, so on an Item with N sibling
        # serials it reads current_qty=N and treats "qty=1" as "remove N-1 guns"
        # (verified: quantity_difference=-2 on a 3-serial Item → sibling loss).
        # This is that hazard; the pre-built bundle is what ERPNext's own
        # test_not_reconcile_all_serial_nos uses.
        sbb = SerialBatchCreation({
            "item_code": sn.item_code,
            "warehouse": sn.warehouse,
            "voucher_type": "Stock Reconciliation",
            "posting_date": posting_date,
            "posting_time": posting_time,
            "qty": 1,
            "avg_rate": new_cost,
            "serial_nos": [serial],
            "type_of_transaction": "Inward",
            "company": company,
            "do_not_submit": True,
        }).make_serial_and_batch_bundle()

        sr = frappe.get_doc({
            "doctype": "Stock Reconciliation",
            "company": company,
            "purpose": "Stock Reconciliation",
            "set_posting_time": 1,
            "posting_date": posting_date,
            "posting_time": posting_time,
            "expense_account": expense_account,
            "cost_center": cost_center,
            "items": [{
                "item_code": sn.item_code,
                "warehouse": sn.warehouse,
                "qty": 1,
                "valuation_rate": new_cost,
                "serial_and_batch_bundle": sbb.name,
                # Scope the reconciliation to this bundle's serial only.
                "reconcile_all_serial_batch": 0,
            }],
        })
        sr.flags.ignore_permissions = True
        sr.insert()

        # Pure-revaluation invariant, asserted on the DRAFT before any GL posts:
        # ERPNext has now scoped current_qty to the single serial (== 1). Anything
        # else means the serial is not a clean single in-stock unit — abort with
        # zero GL written.
        row = sr.items[0]
        qty_diff = flt(row.qty) - flt(row.current_qty)
        if abs(qty_diff) > 1e-6:
            frappe.throw(_(
                "Refusing to proceed: the reconciliation would change quantity by {0} for {1} "
                "(current {2}). Cost correction must be valuation-only."
            ).format(qty_diff, serial, flt(row.current_qty)))

        sr.submit()

        # Layer 2 — keep purchase_rate (report-COGS SSOT) equal to the ledger
        # valuation. update_modified=False: a cost fix is not a doc edit.
        frappe.db.set_value("Serial No", serial, "purchase_rate", new_cost, update_modified=False)

        # PS-7 Layer 3 — for an SRBNB gun, srbnb_cost is the amount the sale
        # settlement will clear from SRBNB, so it must track the new book value
        # exactly (three-way invariant srbnb_cost == purchase_rate == valuation).
        if srbnb_path:
            frappe.db.set_value("Serial No", serial, "srbnb_cost", new_cost, update_modified=False)

        # Cash drawer: only when the operator says the amount paid to the seller was
        # different — a cost can change for other reasons and no cash moved then.
        payout_adjustment = payout_note = None
        if cint(payout_was_different):
            if srbnb_path:
                payout_note = _("Nothing was booked to the cash drawer: this gun's cost is held for the supplier, not paid out.")
            else:
                from ffl_core.cash_drawer import adjust_payout_for_cost_correction
                payout_adjustment = adjust_payout_for_cost_correction(serial, new_cost)
                if not payout_adjustment:
                    payout_note = _("Nothing was booked to the cash drawer: no seller payment is recorded for this gun, "
                        "or it already matches the new cost.")

        _write_audit_trail(sr, serial, old_cost, new_cost, reason, user)
    except Exception:
        frappe.db.rollback(save_point=_SAVEPOINT)
        raise

    return {
        "ok": True,
        "serial": serial,
        "stock_reconciliation": sr.name,
        "old_cost": flt(old_cost, 2),
        "new_cost": new_cost,
        "delta": flt(new_cost - old_cost, 2),
        "posting_date": posting_date,
        "payout_adjustment": payout_adjustment,
        "payout_note": payout_note,
        # An acquisition whose cost was never booked: remind the operator a
        # source-of-cost JE may still be owed. A soft hint, never a hard block.
        # Suppressed on the SRBNB path (PS-7): an SRBNB gun's cost source IS the
        # SRBNB liability the correction just adjusted — no source-of-cost JE is owed.
        "source_of_cost_hint": bool(
            flt(old_cost) <= _HINT_OLD_MAX and new_cost >= _HINT_MIN_NEW) and not srbnb_path,
    }


@frappe.whitelist()
def list_item_serials_for_cost(item_code: str) -> dict:
    """In-stock, owned serials of a firearm Item, for the correction dialog.

    Read-only. Excludes sold/inactive serials and guns consigned TO us
    (``is_consignment=1``) — mirrored on the picker. Guns of OURS that are out
    on consignment at a dealer ARE listed: they stay correctable, and a
    picker that hid them would rebuild the dead end the server guard just
    dropped.

    Args:
        item_code: Firearm Item code; a non-firearm returns an empty list
            with is_firearm=False rather than erroring.

    Returns:
        {item_code, is_firearm, serials: [{serial, warehouse,
        purchase_rate, valuation, cash_payout, goods_payout}]}; cash_payout says the seller's
        payment for the gun was recorded in the cash drawer -- on its own, or with the Receive
        Goods receipt it came in on (goods_payout names that receipt's payment, else null).

    Permissions:
        Stock Manager / System Manager.
    """
    frappe.only_for(_ROLES)
    from ffl_core import cash_drawer as cd

    item_code = (item_code or "").strip()
    firearm = is_firearm_item(item_code)
    if not item_code or not firearm:
        return {"item_code": item_code, "is_firearm": firearm, "serials": []}

    rows = frappe.get_all(
        "Serial No",
        filters={"item_code": item_code, "status": "Active", "is_consignment": 0},
        fields=["name", "warehouse", "purchase_rate"],
        order_by="creation asc",
    )
    wh_company: dict[str, str | None] = {}
    # How each gun's seller was paid, for the whole SKU in a constant number of queries.
    paid = cd.seller_payments_by_serial([r.name for r in rows if r.warehouse])
    serials = []
    for r in rows:
        if not r.warehouse:
            continue
        if r.warehouse not in wh_company:
            wh_company[r.warehouse] = frappe.db.get_value("Warehouse", r.warehouse, "company")
        serials.append({
            "serial": r.name,
            "warehouse": r.warehouse,
            "purchase_rate": flt(r.purchase_rate, 2),
            "valuation": _serial_book_value(item_code, r.warehouse, r.name, wh_company[r.warehouse]),
            "cash_payout": bool(paid[r.name].own or paid[r.name].goods),
            "goods_payout": paid[r.name].goods,
        })
    return {"item_code": item_code, "is_firearm": True, "serials": serials}


# --- internals -------------------------------------------------------------


def _stock_adjustment_accounts(company: str) -> tuple[str, str]:
    """The company's Stock Adjustment expense account + cost center.

    Resolved from the Company (portable across sites/company abbreviations —
    never a hard-coded "- <ABBR>" name). These are the same values ERPNext's own
    Stock Reconciliation.validate would default to; we set them explicitly so the
    voucher's difference posting is unambiguous for the CPA audit.
    """
    expense = frappe.get_cached_value("Company", company, "stock_adjustment_account") or \
        frappe.db.get_value(
            "Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0}, "name")
    cost_center = frappe.get_cached_value("Company", company, "cost_center")
    if not expense:
        frappe.throw(_("No Stock Adjustment account is configured for {0}.").format(company))
    if not cost_center:
        frappe.throw(_("No cost center is configured for {0}.").format(company))
    return expense, cost_center


def _reject_if_period_frozen(company: str, posting_date: str, what: str = "a cost correction") -> None:
    """Refuse a posting into a frozen Accounts or Stock period.

    posting_date is always today, so this only trips if a period was frozen up to
    today or later — but back-dating a revaluation into an already-filed tax year
    silently restates prior-year inventory (BS) and income (via Stock Adjustment,
    P&L). That must never be triggerable from a POS button, so we hard-reject —
    unconditionally, ignoring any frozen-accounts-modifier role bypass (this
    action has no business editing a closed period no matter who clicks it).

    Field homes in this ERPNext v16 (verified against
    accounts/general_ledger.py::check_freezing_date and stock/…/stock_settings):
    the accounting freeze lives on ``Company.accounts_frozen_till_date``; the
    stock freeze on ``Stock Settings.stock_frozen_upto``. Both are also enforced
    by ERPNext at reconciliation submit — this pre-check only makes the rejection
    clean (a clear message, guaranteed zero writes, before the savepoint).
    """
    today = getdate(posting_date)
    acc_frozen = frappe.db.get_value("Company", company, "accounts_frozen_till_date")
    if acc_frozen and today <= getdate(acc_frozen):
        frappe.throw(_(
            "The accounting period for {0} is frozen through {1}; {3} dated {2} "
            "cannot be posted."
        ).format(company, acc_frozen, posting_date, what))
    stock_frozen = frappe.db.get_single_value("Stock Settings", "stock_frozen_upto")
    if stock_frozen and today <= getdate(stock_frozen):
        frappe.throw(_(
            "The stock period is frozen through {0}; {2} dated {1} cannot be posted."
        ).format(stock_frozen, posting_date, what))


def _serial_book_value(item_code: str, warehouse: str, serial: str, company: str | None) -> float:
    """Current per-serial ledger valuation (what issuing this one serial would
    relieve). Independent of the reconciliation internals — used for the audit
    "old cost" and the two-layer consistency check. Returns 0.0 if unpriced."""
    from erpnext.stock.utils import get_incoming_rate
    return flt(get_incoming_rate({
        "item_code": item_code,
        "warehouse": warehouse,
        "posting_date": nowdate(),
        "posting_time": nowtime(),
        "qty": -1,
        "actual_qty": -1,
        "serial_no": serial,
        "company": company,
        "voucher_type": "Stock Entry",
    }, raise_error_if_no_rate=False), 2)


def _write_audit_trail(sr, serial: str, old_cost: float, new_cost: float,
                       reason: str, user: str) -> None:
    """Reason on the reconciliation (no native remarks field) + a Serial No
    timeline entry cross-referencing the reconciliation number, both owned by the
    real operator."""
    detail = _(
        "Firearm cost correction: {old} → {new} (Δ {delta}). Reason: {reason}. By {user}."
    ).format(old=flt(old_cost, 2), new=new_cost, delta=flt(new_cost - old_cost, 2),
             reason=reason, user=user)
    sr.add_comment("Comment", detail)
    frappe.get_doc("Serial No", serial).add_comment(
        "Comment",
        _("Cost corrected {old} → {new} via Stock Reconciliation {reco}. Reason: {reason}. By {user}.")
        .format(old=flt(old_cost, 2), new=new_cost, reco=sr.name, reason=reason, user=user),
    )
