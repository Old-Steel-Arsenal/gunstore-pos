"""Pay-on-sale auto-settlement: book the supplier payable at sale (submit hook)
and reverse it on cancel/return — the pay-on-sale twin of compliance/consignment.py.

A pay-on-sale supplier's gun is received as OWNED inventory with its acquisition
cost parked in **Stock Received But Not Billed** (SRBNB, a liability). The store
owes that money only once the gun sells, so at sale we reclassify the parked cost
from the SRBNB pool into a named Account Payable:

    Dr  Stock Received But Not Billed              srbnb_cost   [no party]
    Cr  Creditors [party_type=Supplier, party=…]   srbnb_cost

**No COGS leg**: the gun was received at full owned cost, so COGS is
already booked once by the sale's own perpetual-inventory stock relief. Adding a
COGS leg here would double-count it. This JE is a pure liability reclass —
P&L net zero. The actual cash Payment stays manual.

Reuses the consignment pipeline shape — the on_submit/on_cancel trigger points,
the `is_return`/`is_consolidated` skips, per-serial resolution off the lines, a
source-tag Data field for reversal — but the GL legs and the trigger data differ:

  consignment: read is_consignment/consignor off the serial; Dr COGS / Cr
               Consignment Payable = net*(1-commission%).
  pay-on-sale: read pay_on_sale/pay_on_sale_supplier/srbnb_cost off the serial;
               Dr SRBNB / Cr Creditors = srbnb_cost, no COGS.

Whether a gun's cost is still in SRBNB cannot be read from the GL (SRBNB carries
no serial and no party), so it is answered entirely by the per-serial stamp
 `pay_on_sale=1 AND srbnb_cost>0 AND pay_on_sale_settled=0`. The
settlement amount is the PERSISTED `srbnb_cost` — the exact figure
credited to SRBNB at park time — so settling clears the gun's SRBNB contribution
to zero. We never re-derive it from a live valuation (which, by settle time, the
sale's stock relief may already have zeroed).

Wired onto POS Invoice + Sales Invoice on_submit / on_cancel (hooks.py).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core.firearm import audit_trace
from ffl_core.serials import receipts_of, serials_for_line
from ffl_core.setup.pay_on_sale import payable_account, srbnb_account

# The title of the payable-kept trace: the Error Log's title (kept ~14 days by
# default), and the first words of the Serial No comment a bookkeeper searches
# for at month-end (Comment list, Reference Type = Serial No).
PAYABLE_KEPT = "Pay-on-sale payable kept: the gun was received again after the sale"


def settle_pay_on_sale_on_submit(doc, method: str | None = None) -> None:
    """Book (sale) or reverse (return) the pay-on-sale supplier payable for every
    qualifying firearm SERIAL — the on_submit hook for BOTH sale and return docs.
    Guns, not lines: one qty>1 line can carry several serials, each settled on its
    own (web-order fulfilment builds exactly that shape).

    Ordinary sale (is_return=0): for each firearm serial stamped pay_on_sale=1 &
    srbnb_cost>0 & !settled, post one Journal Entry Dr SRBNB / Cr Creditors[party]
    = srbnb_cost (no COGS leg), tag it ffl_pay_on_sale_source=doc.name, stamp
    pay_on_sale_settled=1.

    Return credit note (is_return=1): a pay-on-sale gun is OWNED and therefore
    returnable, unlike a consignment gun (block_consignment_returns). A return
    that is NOT a cancel of the original sale would otherwise leave the payable
    standing for a gun no longer sold (MAJOR-1), so is_return is NOT skipped — it
    posts the MIRROR JE Dr Creditors[party] / Cr SRBNB for each RETURNED serial
    and clears pay_on_sale_settled=0 so the gun's cost re-parks in SRBNB and it
    can re-sell + re-settle. Only serials ON THE CREDIT NOTE are touched, so a
    PARTIAL return never over-reverses a sibling gun that wasn't returned. What
    is reversed is what the returned sale BOOKED for the gun (_reversal_pair),
    not what its stamp says now.

    Skips is_consolidated (a POS Invoice's consolidated SI re-fires this hook on
    the same line). Per-serial idempotency via pay_on_sale_settled. BLOCKs (zero
    writes) on an unresolvable party (both directions) and, on the sale path only,
    on an incompletely-stamped gun (srbnb_cost<=0 with a real purchase_rate).

    Wired onto POS Invoice + Sales Invoice on_submit (hooks.py).
    """
    if int(getattr(doc, "is_consolidated", 0) or 0):
        return
    _book_pay_on_sale(doc, reverse=bool(int(getattr(doc, "is_return", 0) or 0)))


def _book_pay_on_sale(doc, reverse: bool) -> None:
    """Post one settlement (reverse=False) or reversal (reverse=True) Journal Entry
    over every qualifying pay-on-sale firearm serial on `doc`, then flip each
    serial's pay_on_sale_settled flag. Shared by the sale and return directions so
    the two stay exact mirrors."""
    company = doc.company
    serials = _doc_serials(doc)
    if not serials:
        return
    # A credit note against a sale reverses what THAT sale booked; one without a
    # sale to read falls back on the stamp, as before.
    sale = _booked_by(doc) if reverse and getattr(doc, "return_against", None) else None
    pairs = []  # (serial, supplier, cost, flip its settled flag)
    for serial in serials:
        pair = _reversal_pair(doc, serial, sale) if sale else _stamp_pair(doc, serial, reverse)
        if pair:
            pairs.append(pair)

    if not pairs:
        return

    srbnb = srbnb_account(company)
    creditors = payable_account(company)
    if not srbnb or not creditors:
        frappe.throw(_(
            "Pay-on-sale settlement for {0}: the company {1} has no {2} configured. Set the "
            "company's Stock Received But Not Billed and default Payable accounts."
        ).format(doc.name, company,
            _("Stock Received But Not Billed account") if not srbnb else _("default Payable account")))
    cost_center = frappe.get_cached_value("Company", company, "cost_center")

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = doc.posting_date
    je.ffl_pay_on_sale_source = doc.name
    je.user_remark = "Pay-on-sale {0} for {1} {2}".format(
        "reversal" if reverse else "settlement", doc.doctype, doc.name)
    for serial, supplier, cost, _flip in pairs:
        # The serial rides BOTH rows of its pair in the standard Journal Entry
        # Account.user_remark (no custom field, no fixture). It is what makes a
        # 10-row JE auditable per gun, and it is how the cancel path learns which
        # guns this JE actually booked instead of guessing from the doc's lines.
        srbnb_line = {"account": srbnb, "cost_center": cost_center, "user_remark": serial}
        creditors_line = {"account": creditors, "cost_center": cost_center,
            "party_type": "Supplier", "party": supplier, "user_remark": serial}
        if reverse:
            # Reversal: Dr Creditors[party] (AP down) / Cr SRBNB (cost re-parks).
            creditors_line["debit_in_account_currency"] = cost
            srbnb_line["credit_in_account_currency"] = cost
        else:
            # Settlement: Dr SRBNB (liability down) / Cr Creditors[party] (named AP up).
            srbnb_line["debit_in_account_currency"] = cost
            creditors_line["credit_in_account_currency"] = cost
        je.append("accounts", srbnb_line)
        je.append("accounts", creditors_line)
    je.flags.ignore_permissions = True
    je.insert()
    je.submit()

    new_flag = 0 if reverse else 1
    for serial, _supplier, _cost, flip in pairs:
        if flip:
            frappe.db.set_value(
                "Serial No", serial, "pay_on_sale_settled", new_flag, update_modified=False)


def _stamp_pair(doc, serial: str, reverse: bool):
    """(serial, supplier, cost, True) off the gun's stamp, or None when there is
    nothing to settle (or reverse). The settlement always reads the stamp: at
    sale the stamp IS the stay being sold."""
    info = frappe.db.get_value(
        "Serial No", serial,
        ["pay_on_sale", "pay_on_sale_supplier", "srbnb_cost",
         "pay_on_sale_settled", "purchase_rate"],
        as_dict=True,
    )
    if not info or not info.pay_on_sale:
        return None
    settled = int(info.pay_on_sale_settled or 0)
    # Settle acts on an unsettled gun; reverse acts on a settled one. The
    # opposite state is a no-op (a re-fired hook, or a return of a gun whose
    # settlement was skipped at sale).
    if settled == (0 if reverse else 1):
        return None

    cost = flt(info.srbnb_cost)
    if cost <= 0:
        # Sale path: never guess the amount (PS-4). A real purchase cost with no
        # persisted srbnb_cost = incomplete stamp -> fail closed; a
        # genuinely costless gun owes nothing -> skip. Reverse path: a
        # settled gun always had srbnb_cost>0, so <=0 here is a corrupt edge -
        # skip rather than post a zero/negative reversal.
        if not reverse and flt(info.purchase_rate) > 0:
            frappe.throw(_(
                "Pay-on-sale gun {0} on {1} has no recorded SRBNB cost but a purchase "
                "cost of {2}. Its pay-on-sale stamp is incomplete - run the pay-on-sale "
                "backfill (or set SRBNB Parked Cost on the Serial No) before selling it. "
                "The settlement amount is never guessed."
            ).format(serial, doc.name, flt(info.purchase_rate, 2)))
        return None

    supplier = (info.pay_on_sale_supplier or "").strip()
    if not supplier or not frappe.db.exists("Supplier", supplier):
        # PS-5: the payable (or its reversal) must hang on a specific, ageable
        # Supplier sub-ledger - never a blank or guessed party.
        frappe.throw(_(
            "Pay-on-sale gun {0} on {1} cannot be {2}: its pay-on-sale supplier "
            "is {3}. Stamp a valid Pay-on-Sale Supplier on the Serial No - the payable "
            "is never booked to a blank or guessed party."
        ).format(serial, doc.name, _("reversed") if reverse else _("settled"),
            supplier and _("missing '{0}'").format(supplier) or _("not set")))
    return (serial, supplier, cost, True)


def _booked_by(doc):
    """What the sale a credit note returns booked:
    its settlement rows by gun — the serial rides each row's user_remark since
    2026-08-12 — any older rows that name no gun, the guns another credit note
    on that sale already reversed, and when the sale was posted. None when the
    sale cannot be read."""
    # lazy: a desk credit note against a CONSOLIDATED POS sale finds nothing here
    # (the settlement is sourced to the POS Invoice) and falls back on the stamp;
    # upgrade path: add the POS Invoices whose consolidated_invoice is the sale.
    at = _posted_at(doc.doctype, doc.return_against)
    if not at:
        return None
    # This note's own rows count among the reversals, so a re-run of its hook
    # finds its guns reversed and posts nothing again.
    returns = frappe.get_all(doc.doctype, filters={"return_against": doc.return_against, "docstatus": 1},
        pluck="name")
    sale = frappe._dict(name=doc.return_against, at=at, serials={}, unnamed=[], reversed=set())
    for r in _supplier_rows("je.ffl_pay_on_sale_source in %(sources)s",
            {"sources": (sale.name, doc.name, *returns)}):
        serial = (r.user_remark or "").strip().upper()
        if r.source == sale.name and flt(r.credit) > 0:
            if serial:
                sale.serials[serial] = (r.party, flt(r.credit))
            else:
                sale.unnamed.append((r.party, flt(r.credit)))
        elif r.source != sale.name and flt(r.debit) > 0 and serial:
            sale.reversed.add(serial)
    return sale


def _reversal_pair(doc, serial: str, sale):
    """B1: what `sale` booked for one gun on a credit note against it — reversed
    once per sale and gun, and only while the gun is still in the stay that sale
    ended. A gun received again since then is in another stay: the refund is money
    only, the supplier is still owed, and a trace says so."""
    if not sale.serials:
        # A settlement from before its rows named their guns, or none sourced to
        # the sale (booked by hand; a desk note against a consolidated POS sale):
        # the stamp answers, as it always did — while the stay is still that
        # sale's, which is also when its srbnb_cost is still the figure booked.
        # The stay is settled first: a gun in another stay is left alone whatever
        # its stamp says — that stamp is the other stay's, and _stamp_pair would
        # refuse a broken one instead of letting the note through.
        later = _received_since(serial, sale.at)
        if later:
            if sale.unnamed:
                # Its rows name no gun, so the note cannot say this one was settled.
                _note_payable_kept(serial, doc, sale.name, None, None, later)
            return None
        return _stamp_pair(doc, serial, reverse=True)
    key = serial.upper()
    if key not in sale.serials or key in sale.reversed:
        return None
    supplier, cost = sale.serials[key]
    later = _received_since(serial, sale.at)
    if later:
        _note_payable_kept(serial, doc, sale.name, supplier, cost, later)
        return None
    stamp = frappe.db.get_value("Serial No", serial,
        ["pay_on_sale", "pay_on_sale_supplier", "pay_on_sale_settled"], as_dict=True)
    own = bool(stamp and cint(stamp.pay_on_sale) and stamp.pay_on_sale_supplier == supplier)
    if own and not cint(stamp.pay_on_sale_settled):
        # The stay's own stamp says its settlement is reversed already — by a note
        # this sale's rows cannot see (one against its consolidated invoice), or
        # by hand. Reversing again would take the supplier's payable twice.
        return None
    return (serial, supplier, cost, own)


def _note_payable_kept(serial: str, doc, sale: str, supplier: str | None, cost: float | None,
        receipt: str) -> None:
    owed = _("owes {0} {1} for this gun").format(supplier, flt(cost, 2)) if supplier else \
        _("predates rows that name their guns (check whether it covered this one)")
    body = _("{0}: {1} — {2} {3} credits {4}, whose settlement {5}, but the gun was received again on Purchase "
        "Receipt {6} after that sale. A refund after that is money only: the payable stands and nothing was "
        "reversed. Check it by hand at month-end close.").format(
            PAYABLE_KEPT, serial, _(doc.doctype), doc.name, sale, owed, receipt)
    audit_trace("Serial No", serial, PAYABLE_KEPT, body)


def _received_since(serial: str, at) -> str | None:
    """The first standing receipt of the gun after `at`: it has started another
    stay since then."""
    # lazy: only a Purchase Receipt starts a stay here — a gun brought back in on
    # a Material Receipt is not seen, so its refund reverses as if in the same
    # stay (the receipt hook has the same blind spot; the month-end check lists
    # in-stock Serial Nos with pay_on_sale=1 and pay_on_sale_settled=1 that are not
    # on an open special-order invoice, and clears the stale stamp); upgrade path:
    # count Stock Entry inward bundles too.
    later = [r.voucher_no for r in receipts_of(serial) if r.posting_datetime > at]
    return later[-1] if later else None


def _posted_at(doctype: str, name: str):
    from erpnext.stock.utils import get_combine_datetime

    posted = frappe.db.get_value(doctype, name, ["posting_date", "posting_time"])
    return get_combine_datetime(*posted) if posted else None


def booked_since(serial: str, since, source_field: str = "ffl_pay_on_sale_source", party: str | None = None):
    """The newest supplier row booked for the gun under `source_field` (the
    pay-on-sale or the consignment tag) since `since` — a stay's receipt
    submission — or None. A stay rebuilt after a cancel reads its settlement and
    its consignor here rather than from today's stamps."""
    # lazy: a row from before rows named their guns (2026-08-12) is not seen, so
    # such a stay rebuilds as unsettled; and a row booked by hand for an EARLIER
    # stay after this receipt's submission reads as this stay's (the window is
    # by creation, not by the sale the row belongs to); upgrade path: match the
    # sale's lines, and take the stay off the sale's date.
    rows = _supplier_rows("jea.user_remark = %(serial)s and je.creation > %(since)s"
        + (" and jea.party = %(party)s" if party else ""),
        {"serial": serial, "since": since, "party": party}, source_field,
        order="order by je.creation desc limit 1")
    return rows[0] if rows else None


def _supplier_rows(where: str, values: dict, source_field: str = "ffl_pay_on_sale_source",
        order: str = "") -> list:
    """Supplier rows of the submitted Journal Entries tagged `source_field` — the
    one query B1 (the sale's settlement) and B2 (a stay's latest entry) share."""
    return frappe.db.sql(f"""
        select je.{source_field} as source, jea.user_remark, jea.party,
            jea.credit_in_account_currency as credit, jea.debit_in_account_currency as debit
        from `tabJournal Entry Account` jea join `tabJournal Entry` je on je.name = jea.parent
        where je.docstatus = 1 and ifnull(je.{source_field}, '') != '' and jea.party_type = 'Supplier'
            and {where}
        {order}""", values, as_dict=True)


def reverse_pay_on_sale_on_cancel(doc, method: str | None = None) -> None:
    """Undo whatever this doc's on_submit posted (on_cancel hook).

    Cancels every submitted Journal Entry tagged ffl_pay_on_sale_source==doc.name
    (ERPNext posts the reversing GL automatically), then restores each affected
    serial's pay_on_sale_settled flag to its pre-submit value:
      - cancelling an ordinary sale (is_return=0) undoes a settlement -> 1 back to 0.
      - cancelling a return credit note (is_return=1) undoes a reversal, so the
        sale it reversed stands again -> 0 back to 1.
    WHICH guns get restored is read back off the cancelled JEs themselves — each
    row carries its gun's serial in user_remark (stamped at book time) — never
    from the doc's lines. The two are NOT the same set: a gun settled by an
    EARLIER invoice is deliberately skipped at submit here, yet still sits on this
    doc's line, and resetting its flag would strand it settled=0 while the other
    invoice's payable still stands in the GL — the payable would then be booked a
    second time on re-sale. A JE booked before that stamp existed (the prod
    backlog) yields no serials, and only that JE falls back to the doc's lines.

    The flag is then only flipped if it still holds what this doc's JE left it in
    AND the gun carries a real srbnb_cost, so a re-run cancel is a no-op.

    Gated on THIS doc owning a JE (`if not names: return`): a non-pay-on-sale doc,
    a consolidated SI (no JE carries its name), or an already-reversed doc no-op.

    Wired onto POS Invoice + Sales Invoice on_cancel (hooks.py).
    """
    names = frappe.get_all(
        "Journal Entry",
        filters={"ffl_pay_on_sale_source": doc.name, "docstatus": 1}, pluck="name")
    if not names:
        return
    booked: list[str] = []  # the guns THIS doc's JEs actually acted on
    parties: dict[str, str] = {}  # SERIAL → the supplier its row booked
    for name in names:
        je = frappe.get_doc("Journal Entry", name)
        stamped = [s for s in
            ((row.user_remark or "").strip() for row in je.accounts) if s]
        parties.update({row.user_remark.strip().upper(): row.party
            for row in je.accounts if row.party and (row.user_remark or "").strip()})
        je.flags.ignore_permissions = True
        je.cancel()
        booked.extend(stamped or _doc_serials(doc))
    is_return = int(getattr(doc, "is_return", 0) or 0)
    # after = the state this doc's JE moved qualifying serials INTO; before = the
    # pre-submit value to restore.
    after, before = (0, 1) if is_return else (1, 0)
    # A credit note that took no gun back can be cancelled after the gun came in
    # again; the flag then belongs to that new stay, not to the sale the note
    # returned. (A sale, or a note that did take the gun back, cannot be
    # cancelled past a later movement of its serial — ERPNext's future-entry check.)
    sale_at = _posted_at(doc.doctype, doc.return_against) \
        if is_return and getattr(doc, "return_against", None) else None
    for serial in dict.fromkeys(booked):
        info = frappe.db.get_value(
            "Serial No", serial,
            ["pay_on_sale", "pay_on_sale_supplier", "srbnb_cost", "pay_on_sale_settled"], as_dict=True)
        if not (info and info.pay_on_sale and flt(info.srbnb_cost) > 0
                and int(info.pay_on_sale_settled or 0) == after):
            continue
        if sale_at and (_received_since(serial, sale_at)
                or parties.get(serial.upper(), info.pay_on_sale_supplier) != info.pay_on_sale_supplier):
            continue
        frappe.db.set_value(
            "Serial No", serial, "pay_on_sale_settled", before, update_modified=False)


def _doc_serials(doc) -> list[str]:
    """Every DISTINCT serial on `doc`, in line order.

    A firearm line is NOT always qty 1: web-order fulfilment (dispose_web_order)
    puts several guns of the same item on ONE qty>1 line, both serials in the one
    row. Each gun carries its own pay-on-sale stamp and owes its own parked cost,
    so settlement iterates guns, not lines. Reuses serials_for_line so both
    storage paths (newline text / Serial and Batch Bundle) are covered.

    Deduped because a gun can only be owed for once: the same serial repeated
    across lines (or inside one line's text) would otherwise accumulate a second
    Dr/Cr pair, and pay_on_sale_settled cannot catch it — every read happens
    before any flag write.
    """
    serials: list[str] = []
    for row in (getattr(doc, "items", None) or []):
        serials.extend(serials_for_line(row))
    return list(dict.fromkeys(serials))
