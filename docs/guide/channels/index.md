# Online channels

Inventory staff list stock on the online channels and counter staff finish the orders that come back. Read this page first, because every channel sells out of the same safe.

The shop sells through three channels. The counter is one of them: a customer walks in and buys at the register, or orders something to be shipped. The other two are online. The **WooCommerce store** is the public shop front. **GunBroker** is the auction and fixed-price marketplace. Both online channels sell the same physical guns out of the same safe, so each of them has to be told the moment a gun is spoken for anywhere else.

## Who owns price and stock

The POS does. It is the system of record for what a gun costs and whether it is available, and it pushes both out to the store and to GunBroker. Editing a price on WooCommerce or on a GunBroker listing does not change the POS, and the next push from the POS overwrites it.

Orders travel the other way. A sale made on either online channel comes back into the POS, which is where the money, the stock movement and the bound book entry are recorded. Web and GunBroker orders both land on the Pending Order page, **Orders** in the sidebar, alongside counter orders. See [the Pending Order queue](../selling/pending-orders.md).

The one thing the online channels keep for themselves is their own order record. The POS does not edit or cancel a web order, and it cannot unwind a GunBroker sale at all.

## Where to find online orders

The sidebar's **Online** section has two lists:

| Sidebar entry | What it opens |
|---|---|
| **Web orders** | Every order the WooCommerce store sent in |
| **GunBroker orders** | Every order read from GunBroker |

Each entry shows only while its integration is switched on. Day to day, work the orders from **Orders** under **Sales** instead. See [the Dashboard and sidebar](../getting-started/dashboard.md).

## How you hear about a new order

The notifications bell at the top of the screen rings when an order comes in: "New web order #…" or "New GunBroker order #…", with the buyer and the order total. It rings for counter staff and managers, inside the app only. Nothing is emailed. A web order still awaiting payment rings later, when WooCommerce moves it on. See [the Dashboard](../getting-started/dashboard.md).

## In this section

| Page | What it covers |
|---|---|
| [WooCommerce store](woocommerce.md) | Listing guns and accessories on the web store, categories, pushes, delisting, and the settings behind them. |
| [GunBroker](gunbroker.md) | Posting listings, auctions, won orders, and getting paid. |
| [Web orders](web-orders.md) | The life of a WooCommerce order inside the POS, from soft-reserved serial to disposition. |
