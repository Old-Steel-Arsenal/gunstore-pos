# Models, serials and prices

Everyone who receives, prices or lists guns should read this page first. OpenGunstore tracks firearms differently from a general retail POS, and most of what the other pages describe follows from two choices: what a record stands for, and where a gun's price lives.

## One Item per model, one Serial No per gun

An **Item** is a model: "Glock 19 Gen5 9mm", "Mosin-Nagant M91/30". It holds what every gun of that model shares: manufacturer, model, caliber, firearm type, UPC and MPN, item group and web category.

A **Serial No** is one physical gun filed under its model. It holds what only that gun has: the serial number, its condition, its cost, its photos and description, where it sits in the store, and whether it is on hold, sold or out on transfer.

- **Stock** for a firearm is the number of Active serials under the item. Ten Glock 19s are one Item with ten Serial Nos, not ten items.
- **The bound book** works per serial. Every gun received writes its own acquisition, and every gun sold writes its own disposition. See [The bound book](../compliance/bound-book.md).
- **A sale** names the serial. At the register the cashier picks the gun in hand, and that serial is the one on the Form 4473. See [Point of Sale](../selling/pos.md).
- **A used gun** of a model you already stock goes under the same Item. A model you have never carried gets a new Item, usually filled from the distributor catalog with **Scan UPC / RSR**. See [Items and serial numbers](../inventory/items-and-serials.md).

Anything that is not a firearm (ammunition, optics, parts) is a plain Item counted by quantity, with no serial numbers. Gun parts such as uppers, barrels and triggers are plain items too. A receiver or frame is a firearm and is tracked by serial.

## Two ways to price a firearm

**Uniform Pricing** on the Item decides where the price lives.

| | Uniform Pricing ticked | Uniform Pricing not ticked (per serial) |
|---|---|---|
| **Use it for** | New, interchangeable stock: every unit is the same gun | Used, surplus and collector guns, where condition sets the price |
| **Where the price lives** | The Item's **Selling Price**, shared by every serial | Each Serial No's own **Sell Price** |
| **Set at receive** | The Sell Price you type becomes the item's Selling Price | The Sell Price you type goes on that gun |
| **Sales and promotions** | Sale Price on the Item applies to every unit | Sale Price on each Serial No, per gun. A sale on the Item is ignored |
| **At the register** | Every unit costs the same, so the serial is picked for you | The cashier picks the serial, and the line reprices to that gun |
| **Web store** | One product for the model. Its stock is the number of free guns | One product per gun, SKU `ITEM::SERIAL`, stock 1 or 0 |
| **GunBroker** | Cannot be listed | Listed one gun at a time from the Serial No |

Every channel uses the same rule to work out a gun's price: the Item's Selling Price for a uniform-priced item, the gun's own Sell Price otherwise. The register, the web store and GunBroker therefore never disagree about what a gun costs.

A price of 0 means the gun is not priced yet. It is not a free gun: the web store and GunBroker refuse to publish it, and the register will not check out a per-gun line until the gun has a price (**Pick the gun**).

### Selling a per-serial pool through one listing

A store may keep a model twice: a uniform-priced Item that is the web product, and a per-serial Item holding more guns of the same model for hand-picked sales. **Sells Through Listing** on the per-serial Item names the uniform-priced one, so a web order for the listing can be filled with a gun from the per-serial pool. It is set on the pool, never on the listing, so adding a pool cannot change what an existing listing accepts. Leave it blank on an ordinary item. See [Items and serial numbers](../inventory/items-and-serials.md#sale-and-pricing-rules).

## Choosing

- New guns from a distributor: tick **Uniform Pricing**, set the Item's Selling Price once, and list the model.
- Trade-ins, consignments, used and surplus guns: leave it unticked and price each gun when it is received or afterwards on the Item's **Serial Numbers** tab.
- A model can hold both new and used stock only if they share a pricing mode. When used guns of a model you sell new need their own prices, file them under a separate per-serial Item.

## Related pages

- [Items and serial numbers](../inventory/items-and-serials.md): every field on the two forms.
- [Receiving goods](../inventory/receive-goods.md): where models, serials and prices are first recorded.
- [WooCommerce store](../channels/woocommerce.md) and [GunBroker](../channels/gunbroker.md): how each pricing mode is listed.
