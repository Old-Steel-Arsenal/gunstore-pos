# Selling guns on consignment

This page covers selling a gun that belongs to someone else. The owner, the **consignor**, leaves the gun with the store. The store sells it, keeps a commission and pays the consignor the rest. A stock manager takes the gun in on [Receive Goods](receive-goods.md), the register sells it like any other gun, and a bookkeeper pays the consignor from the amount the system sets aside at each sale.

## How it works

1. The gun is received with an **Acquisition Type** of `Consignment` from a private seller. It enters stock at $0, because the store did not buy it. The consignor and the commission rate are written onto the gun's Serial No.
2. The gun sells at the register, on the web store or on GunBroker, the same way as any other gun.
3. When the sale is submitted, the system books the consignor's share as an amount owed to them. The store's profit on the sale is the commission.
4. Someone pays the consignor out of that balance with an ordinary Payment Entry.

The commission is the store's cut of the sale price before tax. The consignor is owed the rest. On a $1,000 gun at 20%, the store keeps $200 and owes the consignor $800. Sales tax is charged on the full price and is not part of the split.

## Before you start

- You need the **Stock Manager** or **System Manager** role to receive the gun. See [Receive Goods](receive-goods.md#before-you-start).
- The consignor must be a **Customer** record. Receive Goods records them as a private seller, so it asks for their name, address and photo ID. See [Buying from a private party](receive-goods.md#buying-from-a-private-party).
- The **Acquisition Type** list comes from your FastBound account, and it must include `Consignment`. If the list is empty or the choice is missing, a System Manager clicks **Sync FastBound Lists** on FastBound Settings. See [Integrations](../settings/integrations.md). Without FastBound there is nothing to pick the type from, and the gun cannot be received as a consignment from this page.
- Decide the commission. The store-wide default is 20%. A manager changes it in [FFL Settings](../settings/ffl-settings.md) under **Default Consignment Commission %**. You can also set a different rate for each gun as you receive it.
- Agree the price with the consignor. It goes in the **Sell Price** box on the line card.

## Taking a gun in on consignment

1. Open **Purchasing → Receive goods**.
2. Set **Acquisition Source** to **Individual**. A consignor is a private seller. With **FFL Dealer** selected, the receive fails when you submit.
3. Fill in the seller block: **Seller (Customer)**, the name fields, **Seller ID Type**, **Seller ID Number** and the address. Picking the customer prefills the name and address.
4. Pick a **Source**. The default list has a `Consignment` entry. It is only a label for your own reporting and does not change how the gun is handled.
5. Set **Acquisition Type** to `Consignment`. A **Consignment Commission %** box appears, prefilled with the store default.
6. Scan the gun and fill in its card. Leave **Cost** at **0** and do not tick **Cost really was $0**, because the page does not ask about a zero cost on a consignment. Enter the **Sell Price**, the serial number and the firearm details as for any gun. A **Consignment Commission %** box on the card is prefilled from the one above. Change it for this gun if it differs.
7. Click **Add to Receipt**, then **Submit & Receive**.

No payment dialog opens, because nobody is paid at receipt. The consignor is paid after the gun sells.

!!! warning "Do not enter a cost"
    A cost above $0 is booked as what the store paid for the gun. Leave it at zero. The consignor's share is worked out from the sale price when the gun sells.

!!! warning "Federal recordkeeping"
    The receive writes the usual bound-book acquisition, with the consignor as the seller, and pushes it to FastBound. The consignor's ID and address are recorded exactly as for any private-party acquisition.

### What the system records

Submitting the receipt stamps each gun's Serial No with three fields:

| Field | What it holds |
|---|---|
| **Consignment** | Ticked. Marks the gun as someone else's. |
| **Consignment Commission %** | The store's cut. It comes from the line card, or from the store default when the line is blank or zero. |
| **Consignor** | A Supplier record named after the seller's customer name. It is created the first time that name is seen and reused on every later consignment from the same name. |

The consignor needs a Supplier record because the system tracks what it owes them as a supplier balance. You do not create or maintain it by hand.

The gun is received at no cost, so it adds nothing to Stock In Hand. On Inventory Receipts it shows with the receipt type `Consignment`.

## Selling the gun

Nothing changes at the counter. The cashier picks the gun's serial and rings it up as usual. The same gun can also sell on the web store or GunBroker. Consigned guns are listed on those channels like any other gun in stock.

When the sale is submitted the system:

1. Freezes the commission % onto the invoice line, so a later edit to the Serial No cannot change an old sale.
2. Books a Journal Entry that charges the consignor's share to cost of goods sold and credits it to a liability account named **Consignment Payable**, against the consignor. The sale itself posts at the full price, so the store's profit on the line is the commission.

If the commission % resolves to zero, or to more than 100, the invoice is refused with a message that names the gun. A 100% commission is allowed, and books nothing owed to the consignor.

Two guns with different commission rates cannot share one invoice line, because the line holds only one rate. Put each gun on its own line.

### Changing the commission for one sale

The commission on the invoice line starts from the gun's own rate. A manager who wants a different rate for a single sale can set **Consignment Commission %** on the item row of the draft invoice in Desk before submitting. A blank or zero value on the line means "use the gun's rate". A commission of zero is not possible.

## What the store owes the consignor

Each consignor's balance sits in the **Consignment Payable** account, which the system creates for the company. It is separate from ordinary payables. Two standard reports show the balances:

- **General Ledger**. Set **Account** to `Consignment Payable`, add a date range, and set **Group By** to `Group by Party` to see every consignor, or pick a **Party** for one. The closing balance is what the store owes.
- **Accounts Payable** or **Accounts Payable Summary**. Set **Payable Account** to the Consignment Payable account, not the default trade payables account, and set **Party Type** to `Supplier`. The **Outstanding** column is the amount owed per consignor.

Use the account filter in both. If a consignor's name also belongs to a Supplier you buy from, their balances would otherwise mix.

### Paying the consignor

Payment is a standard Payment Entry. It needs the **Accounts User** or **Accounts Manager** role.

1. Create a new **Payment Entry**.
2. Set **Payment Type** to `Pay`, **Party Type** to `Supplier` and **Party** to the consignor.
3. Set **Paid From** to the cash or bank account the money comes from.
4. Set **Paid To** to **Consignment Payable**. ERPNext proposes the ordinary payables account, which is wrong here. Change it, or the next step finds nothing to allocate against.
5. Use **Get Outstanding Invoices**, or add rows under References. The consignment Journal Entries for that consignor appear as `Journal Entry` references. Allocate the amount you are paying.
6. Save and submit.

The Payment Entry reduces the consignor's balance straight away in both reports.

## Seeing what is on consignment

- **Dashboard**. The **Firearms in stock** tile adds "N on consignment" under the count. See [The Dashboard](../getting-started/dashboard.md).
- **Serial numbers** list. Filter on **Consignment** ticked and Status `Active`. The Serial No form shows the **Consignor** and the commission rate.
- **Inventory Receipts**. Set **Type** to `Consignment` to see what came in over a period. See [Reports](../reports/index.md).
- **Sales Report**. A sold consigned gun is marked on the Profit cell with a teal triangle, and the **Tax lines** view carries a **Consignment?** column. See [Sales Report](../reports/sales-report.md).

## Giving a gun back unsold

The core system has no **Return to consignor** button. A gun that goes back to its owner leaves stock and the bound book like any gun leaving the store, and the system does not do either step for you. Book it out the way your compliance process requires, then check the result against FastBound with the Firearms In Stock report. See [Reports](../reports/index.md).

If the gun was received on consignment by mistake and has not left the store, a manager can cancel the Purchase Receipt. The Serial No then goes back to the way it was before that receipt, and the acquisition is cancelled. See [What the system does](receive-goods.md#what-the-system-does).

## Cancelling or returning a sale

- **A consigned sale cannot be returned.** Saving or submitting a return invoice that holds a consigned gun is refused: "Consignment sales cannot be returned (invoice …, gun …). Cancel the original sale instead." The refusal fires on the first draft save.
- **To undo a consigned sale, cancel the original invoice.** Cancelling cancels the consignor's Journal Entry too, so the amount owed disappears with it.
- **Check whether the consignor has been paid first.** If the Payment Entry already pays that Journal Entry, cancelling the invoice does not stop you and does not claw anything back. ERPNext detaches the payment from the cancelled entry and leaves it as an unallocated amount on the Payment Entry, so the money is still paid out. Look at the consignor's outstanding balance before you cancel. If it is already zero for that sale, cancel the Payment Entry first so the books stay clean.

## Flagging a gun that came in as the store's own

A gun received as an ordinary purchase can be turned into a consignment gun afterwards. Open its Serial No, tick **Consignment** and pick the **Consignor**. If the **Consignment Commission %** is blank it fills in the store default when you save. Saving without a consignor is refused. This changes who the sale pays. It does not change the bound book or the gun's cost.

Use it to fix a gun missed at receive. For a new gun, receive it as above.

## Limits and gotchas

- **Receive Goods only.** The trade-in dialog and the other ways of getting a gun in have no consignment option.
- **The type decides, not the Source.** Only the **Acquisition Type** makes a gun consigned. A `Consignment` entry under **Source** with a different type does not.
- **One name, one consignor record.** The consignor Supplier is matched by name. Two different people with the same name share one balance, and a consignor named like an existing Supplier uses that Supplier.
- **Correct Cost is not available** on a consigned gun, because it was never the store's inventory.
- **A zero-percent commission does not exist.** The store always keeps a cut.
- **Mixed lines report oddly.** If one qty line holds a consigned gun and a store-owned gun together, the consignor's share is booked correctly but the Sales Report's cost for that line is overstated. Sell them on separate lines.
- **Taking a gun back in clears its old owner.** If the store sells a consigned gun and later receives it again, for example as a trade-in, the new receipt decides who owns it. The earlier consignor is not paid on the second sale.
- **No payout screen.** There is no consignor statement or payout button, only the two reports and the Payment Entry above.
- **Nothing runs on a schedule.** No job reminds you that a consignor is owed.

## Troubleshooting

| Message you see | What it means | Fix |
|---|---|---|
| `Consignor name required to book a consignment payable.` | The receive was submitted with **Acquisition Source** set to **FFL Dealer**, so no seller name was recorded. | Receive it again with **Acquisition Source** set to **Individual** and the seller block filled in. |
| `Select a Consignor for a consignment firearm — the sale proceeds (net of commission) are owed to them.` | **Consignment** is ticked on the Serial No with no **Consignor**. | Pick the consignor. |
| `Consignment commission % for … must be greater than 0 and at most 100 …` | The invoice line or the gun carries no usable rate. | Set a rate on the Serial No or on the invoice line. |
| `The guns on line … carry different consignment commission rates …` | Two guns with different rates share one invoice line. | Put each gun on its own line. |
| `Consignment sales cannot be returned (invoice …, gun …). Cancel the original sale instead.` | A return invoice includes a consigned gun. | Cancel the original sale. |
| `Consignment sale …: cannot book the consignor payable for … — no COGS account/cost center is configured.` | The company has no default expense account or cost center. | Set both on the Company record, then submit again. |
| `Row #N: cost of $0 for … needs confirming.` | **Acquisition Type** is not `Consignment`, so the zero cost is read as a purchase. | Set the type to `Consignment`, or enter the real cost if the store bought the gun. |

## Related pages

- [Receive Goods](receive-goods.md) for the receiving page in full.
- [Items and serial numbers](items-and-serials.md) for the Serial No form.
- [Sales Report](../reports/sales-report.md) for how profit is shown on a consigned sale.
- [Month-end close](../reports/month-end.md) for the consignor balances check.
- [The bound book](../compliance/bound-book.md) for what an acquisition record holds.
