# Receiving distributor parcels — scan first

Who this is for: whoever opens boxes at the store. You do not need to know which
order a parcel belongs to. Scan, answer one question, submit.

## 1. Scan the barcode

Open **Receive Goods** and scan the UPC on the product (not the serial — that
comes later, typed off the gun).

- **The item has an order waiting for it** → a small window asks
  *"Hold it for this order?"* and names the customer, the order, the
  distributor order (DO) and *parcel n of m*. Press **Enter** (Yes) and the
  card opens with "→ hold for …" on it. Choose **No — into store stock** if
  the unit is not for that order.
- **Several orders want it** → pick one in the dropdown; the earliest order is
  offered first.
- **Nothing is waiting** → the card opens as plain store stock.

The answer is remembered for that item for the rest of the session, so ten
magazines ask once. Click **change** on the card to answer again.

If the barcode matches no Item at all but a distributor lists that UPC, the
*Create Item* dialog opens pre-filled from the catalog.

## 2. Guns: type the serial

Type the serial off the gun. The page checks it against what the distributor
said it shipped:

- on the chosen order → nothing to see;
- on **another** open order of the same model → the unit is moved to that
  order automatically (the invoice beats first-come-first-served);
- on no order → an orange note asks you to check the box label. You may
  continue.

## 3. The Incoming shipments panel

At the top of the page: every parcel on its way, grouped by customer order,
with the distributor, DO number, status, tracking number and *n of m
outstanding*.

- **Receive this parcel** loads that DO's outstanding lines into the session
  as *UNCONFIRMED* rows (guns one per unit with the echoed serial pre-filled).
  Scan each unit: the matching row ticks ✓. Whatever is still unconfirmed when
  you are done is what was missing from the box.
- **Receive whole order** does the same for every parcel of a customer order.
- **Check status now** asks the distributor for the latest invoice, tracking
  and serials — use it when a parcel arrives before the half-hourly poll has.

## 4. Submit

**Submit & Receive** books one receipt per distributor order (supplier, cost
and A&D source come from the order — nothing to pick) and one for store
stock. A green message names each receipt and who the goods are held for. A
receipt that fails stays in the session so you can fix it and submit again.

If you pick a distributor's supplier by hand for a plain receive while that
house has parcels on the way, the page warns you and lists them — the goods
should be received through the panel so their orders are held.

## 5. On the Pending Order queue

- **Fulfill from stock** on a ship-to-store line still on its way serves the
  customer from the shelf now (a gun: pick an unheld in-stock serial). The
  distributor order stays placed; when its parcel lands it is plain restock.
- A red pill means the house **killed** a line, **short-shipped**, or has
  **held** the order past the limit in its Settings. Such a line also offers
  **Re-source** (order it again from another house — a Draft DO to place from
  the queue) and **Cancel line** (goes to Edit).
- **Neither RSR nor Sports South can be cancelled from the POS.** Every one of
  these actions changes only this store's system and shows the order number
  and web reference to quote when you phone the distributor. If the parcel
  ships anyway, receive it as restock. Once the house confirms the cancel,
  open the DO and use **Actions → Distributor confirmed cancel**.

## Settings

Each house's Settings page (Distributor Hub → Account & shipping):

- **Flag a hold after (days)** — when a held order shows as trouble on the
  queue (default 3).
- **Alert when invoiced freight differs by more than** — the poll compares the
  freight the house invoiced with the figure booked at placement and emails
  the managers when the gap exceeds this (default $5).
