# Consignment Payout Guide

Operator/bookkeeper guide for tracking and paying out what the store owes
consignors. No custom code — this is 100% native ERPNext Accounts Payable
(AP). For the mechanics of how the numbers get onto the books in the first
place, see `ffl_core/compliance/consignment.py` (the commission freeze and
`post_consignment_payable`) and `ffl_core/consignment.py` (consignor Suppliers
and the commission maths); for pay-on-sale suppliers, which settle through a
different path, `ffl_core/compliance/pay_on_sale.py`. This doc only covers the
operator-facing "what do I click" side.

## What "Consignment Payable" is

Each company has its own **`Consignment Payable`** account (e.g.
`Consignment Payable - FT`) — a Liability / Payable account created
automatically, separate from ordinary trade `Accounts Payable`. It holds
everything the store currently owes to consignors for guns that have sold
but not yet been paid out.

Every time a consignment gun sells (on a submitted, non-return invoice), the
system posts a Journal Entry:

- **Dr** the COGS account, for the consignor's share of the sale
  (`net price × (1 − commission %)`)
- **Cr** `Consignment Payable`, for the same amount, tagged
  **`Party Type = Supplier`, `Party = <the consignor>`**

The `party` tag is what makes balances trackable **per consignor**, not just
as one lump liability — every consignor who has ever consigned a gun already
has a matching `Supplier` record (created automatically at intake), and
that's the "Party" their payable accrues against.

## Checking what's owed to a consignor

Two standard reports show this; both are searchable by name in the Awesomebar
(top search bar):

**General Ledger** — filter `Company`, `Account = Consignment Payable - <co>`,
and a date range. Optionally add `Party Type = Supplier` and a specific
`Party` to see one consignor only, or leave Party blank and set
`Group By = Group by Party` to see everyone at once. The closing balance for
a given party is what the store currently owes them.

**Accounts Payable** (or **Accounts Payable Summary**) — filter `Company`,
`Payable Account = Consignment Payable - <co>` (the "Payable Account" filter
on this report — don't leave it on the default trade-AP account), and
`Party Type = Supplier`. This lists every outstanding consignment payable
per consignor with an `Outstanding` column — the same number as the GL
approach, in an aging-report layout.

Filtering by the `Consignment Payable` account (not just by Party) matters
because a consignor's `Supplier` record is a plain Supplier — if the same
person/entity were ever also used for ordinary purchasing, their combined
Party balance would mix trade payables with consignment payables. The
account filter keeps consignment balances isolated.

## Paying a consignor

A native **Payment Entry** — no custom screen:

1. New **Payment Entry**.
2. **Payment Type** = `Pay`.
3. **Party Type** = `Supplier`, **Party** = the consignor.
4. **Paid From** = your Cash/Bank account (where the money is actually
   coming from).
5. **Paid To** = **`Consignment Payable - <co>`**. ERPNext will not default
   this to the right account on its own — it typically proposes the
   company's ordinary trade-payables account, which is *not* what the sale
   credited. Change it explicitly, or the reference match in the next step
   will fail to find anything to allocate against.
6. Use **Get Outstanding Invoices** (or add a row under References
   manually) — the consignment Journal Entry(ies) for that consignor show up
   as `Journal Entry` references, the same way an unpaid Purchase Invoice
   would. Allocate the amount you're paying against the entry (or entries)
   you're settling.
7. Save, Submit.

That's it — the Payment Entry's own GL posting (Dr Consignment Payable /
Cr Cash-Bank) reduces the consignor's balance, visible immediately in either
report above.

## Consignment sales are not returnable

**Owner policy — enforced in code, not just process.** A consignment gun
sale cannot be returned through the normal POS/Sales Invoice return flow.
Trying to save or submit a return (`is_return` invoice) that contains any
consignment gun line is rejected immediately with: *"Consignment sales
cannot be returned (invoice `<name>`). Cancel the original sale instead."*
(`ffl_core.compliance.consignment.block_consignment_returns`, wired onto both
POS Invoice's and Sales Invoice's `validate` — it fires on the first draft
save, not just at submit, so the cashier finds out right away.)

Why: a consignment sale already booked a specific, commission-derived
payable to a specific consignor (the Journal Entry described above) — and by
the time anyone thinks to return it, that payable may already be sitting
partly or fully **paid out** via a Payment Entry. A return would have to
answer questions the owner would rather not leave to an ad-hoc POS return:
does the payable claw back automatically? What if the consignor's already
been paid? Rather than guess, the system disallows the return outright.

**To unwind a consignment sale, cancel the original invoice instead of
returning it.** Cancelling automatically reverses the consignor-payable
Journal Entry (`reverse_consignment_payable`) — there is no separate manual
GL step. See "Cancelling a sale after the consignor has already been paid"
immediately below for what happens (and what to check first) when that
payable has already been paid out. If the gun itself also needs to physically
go back to the consignor or back onto the shelf, that's a separate stock
step outside this invoice-cancel flow.

## Cancelling a sale after the consignor has already been paid

**Verified behavior — read this before assuming the system will stop you.**
Cancelling the Sales/POS Invoice for an already-paid-out consignment sale
does **not** hard-block, even though the consignment Journal Entry has a
Payment Entry allocated against it. By default ERPNext's global
`Accounts Settings → Unlink Payment on cancellation of invoice` (checked out
of the box) auto-unlinks instead of blocking:

- The consignment Journal Entry cancels normally (its `docstatus` goes to 2).
- The Payment Entry itself is **not** cancelled and **not** touched in a way
  that stops the cancel — it survives, but the reference row pointing at
  that Journal Entry is stripped and its allocated amount (e.g. $800) moves
  from *allocated* to **unallocated** on the Payment Entry. The money is
  still on the books as paid to that consignor; it's just no longer tied to
  anything.

In other words: the system will **not** stop you from cancelling a
consignment sale you've already paid out on, and it will **not** claw the
money back or reconcile it for you either — it just quietly detaches the
payment and leaves it sitting as an unallocated amount against that
consignor.

**Recommended practice:** before cancelling a consignment sale, check
whether it's already been paid (Accounts Payable report, above, filtered to
that consignor — if their outstanding balance for that entry is already
zero, it's been paid). If so, **unwind the Payment Entry yourself first**
(open it, Cancel) rather than relying on the invoice cancel to sort it out.
It isn't required — the auto-unlink above means cancelling won't error — but
doing it yourself keeps the books clean and avoids leaving a stray
unallocated Payment Entry that someone has to notice and clean up later
(re-allocate it elsewhere, or refund it, per store policy).

If you'd rather the system hard-block this instead of silently unlinking,
that's a storewide toggle (`Accounts Settings → Unlink Payment on
cancellation of invoice`, off) — it affects cancellation of *every* invoice
and payment in the system, not just consignment sales, so treat it as a
bigger policy decision and confirm with the owner/accountant before
flipping it.

## Not built yet

A convenience "Consignor Balances" report (one row per consignor, current
owed) would be a nice-to-have on top of the two reports above, but isn't
built — the General Ledger / Accounts Payable filters above already give the
same numbers. Add it later if the manual filtering becomes a real workflow
pain point.
