# Distributors

Purchasing staff order stock from wholesale houses here, and a person confirms every order before anything is placed. Managers set the houses up.

A distributor is a wholesale house you buy from, such as RSR or Sports South. The POS holds one **Distributor** record per house, with its own credentials and its own switches. Everything you order from a house, whether it lands on your shelf or on a customer's doorstep, becomes a **Distributor Order** you can follow from Draft to Complete. Order numbers look like `DO-2609-0001`: the year and month the order was made, then a running number.

## Three ways stock reaches a customer

| Route | Order Type | Where the goods go | When you use it |
|---|---|---|---|
| Stocking order | **Stocking** | Your store, into inventory | Restocking the shelf. You choose what and how many |
| Procure to store | **Procure To Store** | Your store first, then the customer | The customer has bought it, and the goods must pass through your hands |
| Drop-ship | **Dropship Accessory** or **Dropship Firearm** | Straight from the distributor to the buyer, or to the buyer's transfer FFL | A sale of something you do not stock. An accessory goes to the buyer's address. A gun goes to the receiving dealer, never to a doorstep |

Some things always come to the store first:

- Firearms the distributor will not ship direct, and brands with a **To Store** policy. See [Dropship Brand Policy](hub.md#dropship-brand-policy).
- NFA items, such as a suppressor. They are never drop-shipped.
- Parts a distributor ships to dealers only, such as a barrel or an upper.

A gun is never shipped to a consumer. When a drop-shipped item is a firearm, the destination is the buyer's transfer FFL, and the order will not be created until that license is captured and verified.

## Where to find it

| What | Where |
|---|---|
| Ordering and tracking | Sidebar → Purchasing → **Distributor orders** |
| What is on its way | The **Arriving** card on the [Dashboard](../getting-started/dashboard.md) |
| Setting up a house | Settings → **Distributors** |

## The pages

| Page | What it covers |
|---|---|
| [Distributors hub](hub.md) | One card per house. Switches, feed health, connection test, priority and brand policies |
| [Distributor orders](restock.md) | Searching every house at once, building a cart, creating draft orders, tracking open orders and invoices, and receiving what arrives |
| [Drop-shipping](dropship.md) | Drop-ship and ship-to-store legs of a counter order, and what to do when a drop-shipped sale goes wrong |

## What the POS will not do for you

Cancelling a distributor order in the POS never reaches the house. The system has no way to cancel an order with a distributor. Once an order is live at the house, stopping it means a phone call or an email, and the POS says so rather than pretending otherwise. That is why every order waits in **Draft** until a person confirms it.
