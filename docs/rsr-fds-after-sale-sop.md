# RSR FDS after-sale SOP — denied 4473, refused transfer, returns

> RSR's API has no cancel/return call, and the cost invoice is never reversed
> automatically. Applies to firearms shipped through
> RSR's **FDS** lane, i.e. straight from RSR to the buyer's transfer dealer.
> Accessory drop-ships follow the ordinary store return policy.
>
> Audience: store operations + whoever keeps the books. Nothing here is
> automated, deliberately.

## Why this is manual

Three facts force a human into the loop:

1. **RSR has no cancel or return API.** Once an order is `Placed`, the only way
   to stop or recall it is to phone/email RSR. The system will not pretend
   otherwise — a placed Distributor Order stays `Placed` and raises an alert.
2. **The gun was never in our bound book.** It shipped from RSR's inventory to
   the receiving dealer's, so there is no A&D acquisition and no disposition of
   ours to reverse. Our record is the Woo Online Order (with RSR's serial
   recorded on it) plus the Distributor Order.
3. **We may still owe RSR for it.** Whether the cost reverses depends entirely
   on whether RSR accepts the goods back and issues a credit. Reversing our
   Purchase Invoice on our own initiative would erase a debt that still exists.

## The four situations

### A. Buyer's 4473 is denied at the transfer dealer

The gun is at the receiving FFL and cannot be released to the buyer.

1. **Record it** on the Distributor Order: set the hold reason to
   `4473 denied at <dealer>`, and note the date and who you spoke to.
2. **Refund the customer** — the revenue side is safe to reverse because there
   is no disposition behind it. Use the existing web-refund path (cancel, or
   cancel + amend-to-net if you are keeping a restocking fee). **Never** add a
   −1 firearm line; that corrupts Sales Report quantities.
3. **Decide the gun's fate with RSR** (below), then handle the cost side.

### B. RSR accepts the return and issues a credit

1. Get the **credit memo number and amount** from RSR. Note that RSR charges its
   own restocking fee, which is separate from the store's 15% policy — the
   credit is usually less than what we were billed.
2. Book a **Purchase Return / Debit Note against the original Purchase
   Invoice**, for the amount RSR actually credited:

   ```
   Dr  Creditors - <abbr> [RSR GROUP INC]        <credit amount>
       Cr  Cost of Goods Sold - <abbr>               <goods portion>
       Cr  Freight and Forwarding Charges - <abbr>   <freight portion, if credited>
   ```

   Any difference between what we were billed and what RSR credited stays in
   COGS — that difference is a real cost of the failed sale.
3. Record the credit number on the Distributor Order so the month-end
   "refunded but not credited" review can close the line.

### C. RSR does NOT take it back

1. **Do not reverse the Purchase Invoice.** We bought the gun; the cost and the
   payable are both real. This is exactly the case that rule exists to protect.
2. Decide where the gun goes:
   - **Brought back to the shop** — it is now our inventory and must be
     **acquired into the bound book** from the receiving dealer (an ordinary FFL
     acquisition, with the cost we actually paid). This also moves the cost from
     COGS back onto the balance sheet, so tell the bookkeeper; otherwise we have
     expensed a gun we are still holding.
   - **Sold by the receiving dealer / disposed of otherwise** — no acquisition;
     the cost stays in COGS and whatever we recover is recorded against it.

### C-bis. Short-ship (RSR acked fewer than we ordered)

Common when fill-or-kill is off: RSR ships M of N units. The system **alerts
automatically** (once per distributor order — "Distributor order short-shipped")
and the cost Purchase Invoice already books only the M acknowledged units, so the
GL and the Sales Report are correct without any action.

The customer paid for N, though, so the refund for the N−M un-shipped units is a
**manual** step (v1 does not auto-refund):

1. Refund the customer for the short units through the existing web-refund path
   (cancel + amend-to-net, never a −1 phantom line).
2. No cost adjustment is needed — the PI never billed the un-shipped units.

### D. Buyer simply never collects

Chase it as a normal abandoned transfer with the receiving dealer, then follow
B or C depending on what RSR agrees to. The refund decision is store policy
("all sales final" / 15%), not an accounting rule.

## Month-end review (goes with the accountant's month-end close)

Two lists, both short:

1. **Refunded but not credited** — orders with a cancelled/amended Sales Invoice
   whose Purchase Invoice has no matching return. Each line is either "RSR credit
   still pending" (fine, note the date) or "RSR refused" (fine, it stays in
   COGS). A line with no explanation is the one to chase.
2. **Guns that came back to the shop** — confirm each has a bound-book
   acquisition and that its cost was moved out of COGS into inventory.

## What the system does and does not do

| | Automatic | Manual |
|---|---|---|
| Alert when a cancelled Woo order has a live distributor order | ✅ | |
| Withdraw a not-yet-placed distributor order | ✅ | |
| Cancel a placed order at RSR | | ☎️ no API |
| Reverse the revenue (Sales Invoice) | semi — existing refund path | approval |
| Reverse the cost (Purchase Invoice) | **never** | ✅ after a real credit |
| Bound-book entries for an FDS gun | none exist — nothing to reverse | |
