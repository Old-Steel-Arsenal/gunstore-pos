# Drop-shipping

Drop-shipping means the distributor ships the goods, not you. An accessory goes to the buyer's own address. A firearm goes to the buyer's transfer FFL, never to a doorstep. Operations staff confirm each order. Nothing is placed with a distributor automatically.

This page covers the drop-ship and ship-to-store legs of an order built on [Create Order](../selling/create-order.md).

## Before you start

- The house needs **Enabled** and **Drop-ship** ticked on the [Distributors hub](hub.md).
- A drop-shipped firearm needs the buyer's transfer license captured on the order and resolved against the ATF directory before an order can be raised.
- For a drop-ship leg the customer must have paid. An unpaid drop-ship order never reaches the distributor. A ship-to-store leg may be placed on a deposit, or unpaid, because the goods come to your own shelf.

## The order types

| Order Type | Ships to | Used for |
|---|---|---|
| **Dropship Accessory** | The buyer's address | Anything that is not a firearm |
| **Dropship Firearm** | The buyer's transfer FFL | A gun, always |
| **Procure To Store** | Your store | Brands or items the house will not ship direct, and anything you choose to handle yourself |

Each order carries only the destination it is allowed to know. An accessory order carries the buyer's address. A firearm order carries the receiving license and not the buyer's street address. A procure-to-store order carries neither, because the goods come to your store's address on file with the house.

Three kinds of product always go **Procure To Store**:

- A brand whose [Dropship Brand Policy](hub.md#dropship-brand-policy) is **To Store**.
- An NFA item, such as a suppressor. It is never drop-shipped, whatever a brand policy or a setting says. The customer completes the transfer at the store.
- A part the house ships to dealers only, such as a barrel or an upper.

## Counter orders

An order built on [Create Order](../selling/create-order.md) makes its own draft legs, one for each house and lane. Different lanes use different accounts at the house, so they never share an order. They are confirmed from the [Pending Order](../selling/pending-orders.md) card with **Place order**, or one by one from the Distributor Order form.

Placement is real. Cancelling an order here never reaches the distributor, so once an order is live with the house, undoing it is a phone call and a return. Read the confirmation before pressing the button. The order form, its buttons and its statuses are on [Distributor orders](restock.md).

## When a drop-shipped sale goes wrong

Everything in this section is manual on purpose. Three facts force a person into the loop.

- **Cancelling here never reaches the house.** Once an order is Placed, stopping or recalling it means a phone call. Cancelling the Distributor Order in this system changes only your record, and alerts operations unless you record that the house has confirmed.
- **The gun was never in your bound book.** It went from the distributor's inventory to the receiving dealer's. There is no acquisition and no disposition of yours to reverse. Your record is the sale, carrying the distributor's serial number, plus the Distributor Order.
- **You may still owe for it.** Whether the cost reverses depends on whether the house takes the goods back and issues a credit. Reversing the purchase invoice on your own initiative erases a debt that still exists.

### The buyer's 4473 is denied at the transfer dealer

1. Record it on the Distributor Order. Put the reason and the dealer's name in the hold reason, with the date and who you spoke to.
2. Refund the customer. The revenue side is safe to reverse, because there is no disposition behind it. Cancel the sale, or cancel and amend it to the net amount if you are keeping a restocking fee. Never add a line for minus one firearm. That corrupts the Sales Report quantities.
3. Settle the gun's fate with the distributor, then handle the cost.

### The distributor accepts the return

1. Get the credit memo number and amount. The house charges its own restocking fee, separate from your store policy, so the credit is usually less than you were billed.
2. Book a purchase return against the original purchase invoice for the amount actually credited. The difference between what you were billed and what you were credited stays in cost of goods sold. That difference is a real cost of the failed sale.
3. Record the credit number on the Distributor Order so the month-end review can close the line.

### The distributor refuses the return

Do not reverse the purchase invoice. You bought the gun, and the cost and the payable are both real.

Then decide where the gun goes.

- **Brought back to the shop.** It is now your inventory and must be acquired into the bound book from the receiving dealer, as an ordinary FFL acquisition at the cost you paid. Tell your bookkeeper, because the cost has to move out of cost of goods sold and back onto the balance sheet. Otherwise you have expensed a gun you are still holding.
- **Sold by the receiving dealer, or otherwise disposed of.** No acquisition. The cost stays in cost of goods sold and whatever you recover is recorded against it.

### The house shipped fewer units than you ordered

Common when fill or kill is off. The system alerts once per order, and the cost invoice bills only the units acknowledged, so the ledger and the Sales Report are already right. Sports South credit memos for short units are read each night and folded onto their orders, with an alert for the refund. The customer paid for the full quantity, so refunding the short units is a manual step. No cost adjustment is needed.

### The buyer never collects

Chase it as an abandoned transfer with the receiving dealer, then follow the return path the distributor agrees to. Whether you refund is store policy, not an accounting rule.

## Month-end review

Two short lists go with the [month-end close](../reports/month-end.md).

1. **Refunded but not credited.** Orders with a cancelled or amended sales invoice whose purchase invoice has no matching return. Each line is either a credit still pending, which is fine if you note the date, or a refusal, which is fine because it stays in cost of goods sold. A line with no explanation is the one to chase.
2. **Guns that came back to the shop.** Confirm each one has a bound-book acquisition, and that its cost moved out of cost of goods sold and into inventory.
