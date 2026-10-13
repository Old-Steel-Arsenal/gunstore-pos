# Sports South drop-ship — operator guide

Sports South (SS) is the second distributor behind the shared drop-ship
framework (`ffl_integrations.distributor`). The catalog (prices, stock,
descriptions, images) arrives through the Data Service like RSR's; **ordering** talks
to SS's SMART web services (`orders.asmx` / `invoices.asmx`) from
`ffl_integrations/sports_south/`.

Policy source: *Order Fulfillment Overview* (SS, 2022-08-22). The rules below
are the ones the code enforces; the fee numbers live in Settings so a rate
change is a form edit, not a deploy.

## 1. One-time setup

1. **Sports South Settings** (Distributors hub → the Sports South card →
   **Account & shipping**; four tabs):
   - Account: account number, API password (your SS web-services
     password), *Allow live orders* off until step 4. The API base URL sits
     in the collapsed *Advanced* section.
   - Ordering → **Ship codes** (SS, 2026-09-08): Ground Premium `G`
     (default), 2-Day Air `2` (default), Next-Day Air `N`; Ground Economy is
     an *empty* ShipVIA — turn on *Accessory / ammunition orders ship Ground
     Economy* to use it for non-firearm orders ($7.95, discount carriers, no
     claims / intercepts). Placement fails closed while a code field is blank.
   - Ordering → delivery options: every option defaults to off — tick Signature / Insurance per order class if wanted
     (uninsured claims cap at $100).
   - Ordering → **Ship-to address override**: leave it blank and stocking /
     procure-to-store orders ship to the company's own address (the Address
     on the Company, the one receipts print). Fill it only to ship somewhere
     else; any one filled field makes the whole block the destination, so
     fill it completely. With neither complete, a store order fails closed.
   - Freight: pre-filled from the Overview fee table (flat-rate freight,
     signature, insurance) plus the ship-to-store thresholds. These are
     *estimates* on the Distributor Order; the invoice's freight replaces them
     once the parcel ships.
   - Status: the daily credit-memo poll's progress and the last error (read-only).
2. **Test Connection** (toolbar). Green on the catalog service and on both
   asmx services before going further.
3. **Distributor hub** (`/app/distributor-hub`): the `Sports South` row is seeded
   disabled. Set its priority (column order in the counter matrix; lowest
   number = default house), then enable *Dropship* and, when ready,
   *Enabled*. **Pricing & stock** opens the Distributor record (markup,
   stock publishing, supplier). The card's summary line shows *Ordering*
   (live only when the row is Enabled and *Allow live orders* is on) and the
   *Ship to store* thresholds; click either to open that tab.
4. Tick *Allow live orders* in Settings (Account tab).

### Manufacturer restrictions (no-drop-ship list)

**Dropship Brand Policy** rows now carry an optional *Distributor* and a
*Reason shown to staff*. A row with a distributor applies only to that
house's catalog and beats a blank (all-distributors) row for the same brand.
Resolution order for a line: materialised listing mode → brand policy
(scoped, then global) → the distributor's own row flags → Direct.

The Overview's restrictions are seeded as rows **scoped to SS**: brands SS
will not fulfil (Beretta, Glock, HK, IWI, Ruger, S&W, Springfield, Tikka,
Walther, …) and the pre-approval brands (1791, Leupold, Sig Sauer, Trijicon)
are `To Store` with the reason text. RSR's drop-ship of the same brands is
untouched. When your rep clears a pre-approval brand, edit its `… @ Sports South` row
to `Direct OK`. Brands not yet in the Brand master are picked up by the
next migrate.

What staff see: the counter pending card and the Fulfillment planner cell
show the reason, e.g. *Ruger: Sports South does not fulfil this manufacturer
— ship to store only (Sports South)*; the web-order fulfilment panel gets the
same text in each candidate's `reason`. To add a no-drop-ship brand for any
distributor (RSR included): New Dropship Brand Policy → brand, distributor
(or blank for all), policy `To Store`, reason.

## 2. What happens on an order

| Step | Mechanism |
|---|---|
| Split | Firearms and non-firearms are always separate SS orders (SS §XII.F). |
| Shipping service | Chosen per order on the Draft DO (**Shipping service…** button): accessories Ground Economy / Ground Premium / 2-Day / Next-Day; long guns Premium / 2-Day / Next-Day; handguns 2-Day / Next-Day only (air only); ammunition ground only (HAZMAT). Default = Ground Premium (2-Day for handguns), or Economy when the Settings switch is on. The estimate on the DO follows the choice; the invoice's freight replaces it. A choice that no longer fits the lines (a handgun added later) is refused at placement. |
| Place | `AddHeader` → SS web order number saved to `web_ref` at once → `AddDetail` per line → `Submit`. Any `false` withdraws the whole order (`DeleteOpenOrder`) and lands the DO in *Error* with the refused SKUs. |
| Ack | `GetDetail` right after Submit: `ORQTYF` is the fill (partial = SHORT; 0 = UNFILLED until an invoice settles it — SS never backorders, so a 0 on the invoice becomes CANCELLED). An all-zero fill at placement raises an alert to check the order on SS's site. `ORPRCF` is the price SS will charge — a drift above *Alert when the filled price exceeds the quote by* raises an admin alert. |
| Firearm FFL email | For a Dropship Firearm order the transfer dealer's FFL copy (a PDF/JPG attached to its **ATF FFL Record**, else the `ffl_document` on the Supplier / Customer carrying that FFL number) is emailed to fulfillment@sportssouth.biz, subject `<account> <PO>`, within the 3-business-day window. Sent once per DO. No file → alert + DO comment: send it by hand or SS cancels the order. |
| Poll | The generic 30 min → 2 h backoff poll reads `GetDetail`, invoices by PO, tracking (`GetTrackingByPo`) and, for firearms, `GetSerialsForInvoice`. A shipped line carries the order's tracking numbers and serials. |
| Credits | Daily: `GetCreditsByDate(yesterday)`. A credit memo (warehouse short after invoicing) reduces the shipped qty on its DO once (idempotent per credit number) and alerts — the consumer refund / re-source is a human step. |
| Cancel | Before `Submit` the code deletes the open order itself. After Submit there is no API cancel: email fulfillment@sportssouth.biz before invoicing (Overview §III.C). |

## 3. First live order checklist

- [ ] Test Connection green (ship codes are pre-filled: G / 2).
- [ ] Sandbox check (optional): Settings → *Sandbox mode* sends `Source=99994`; place one accessory order to the store, confirm it shows on SS's test side, then switch off. *(SS's published test login does not authenticate on orders.asmx — the sandbox is reached with your own credentials + Source 99994.)*
- [ ] The transfer dealer's licence copy exists (File on the ATF FFL Record, or `ffl_document` on the Supplier/Customer) for firearm orders.
- [ ] Sandbox `Submit` once to confirm `ORQTYF` is computed at Submit (only the pre-Submit 0 has been observed).
- [ ] Compare the first invoice's freight (`SHPAMT`) with the estimate on the DO and adjust the Freight tab if SS's table changed.

## 4. Not done in v1

- Per-ZIP `ShipCheck` on quotes (SS's restriction feed is per destination, not per state; the counter matrix does not call it yet).
- `GetPackageContents` per-parcel line mapping (all shipped lines carry the order's tracking numbers).
- Automatic PI freight update from the tracking row (the estimate stays on the DO; reconcile against SS's invoice).
- Next-Day Saver (`R` on the estimate endpoint; not a documented ShipVIA).
