# Glossary

These are the terms you will meet in this system, in alphabetical order. They are written for a new hire on their first week.

**4473.** ATF Form 4473, the Firearms Transaction Record. A buyer fills one in for every firearm they take away from a licensed dealer, and the dealer keeps it. In this system a firearm sale cannot be completed until the order's 4473 status is Completed and the NICS status is Proceed. See [Form 4473](../compliance/4473.md).

**4473 record.** The POS's own permanent row for each completed Form 4473, electronic or paper, with the outcome and, for an electronic form, a private copy of the signed PDF. It cannot be deleted, and it survives the order being deleted or the form being redone. The **Form 4473 Records** report lists them. See [4473 records](../compliance/4473-records.md).

**A&D, bound book.** The Acquisition and Disposition record every licensee must keep: what firearms came in, from whom, and what went out, to whom. It is the federal record of the gun's chain of custody through the store. In this system it lives in [FastBound](../compliance/fastbound.md), and the system writes to it automatically. See [the bound book](../compliance/bound-book.md).

**ACH.** Automated Clearing House, the American bank-to-bank transfer network. It exists here as a mode of payment for a manual bank transfer, alongside Zelle, check and wire. There is no gateway behind it. Someone confirms the money arrived and records the tender.

**Acquisition.** A firearm entering your inventory, and the bound book entry that records it. Every gun that comes through the door creates one, whether it was bought from a distributor, taken in trade or shipped in for a customer transfer. See [Receive Goods](../inventory/receive-goods.md).

**Antique.** A firearm made before 1899, marked **Antique** on its item. It is serialized, but it is never entered in the bound book and needs no 4473 at sale. A record booked for an antique by mistake can be marked by a manager as not in the bound book, so it stops being retried. See [FastBound](../compliance/fastbound.md).

**Approval code.** The code the card terminal or the Payroc Virtual Terminal gives for an approved charge. A card already run on the Virtual Terminal is recorded with **Already charged on Virtual Terminal** and its approval code, instead of being charged again. See [Taking payment](../selling/payments.md).

**Brady date.** The third business day after a background check was started. If NICS is still Delayed past that date, federal law permits the dealer to transfer at their own discretion. The [Pending 4473 Orders report](../reports/index.md) prints it as **Brady Day 3** and highlights a Delayed row once it has passed.

**C&R, Type 03.** A Curio and Relic collector's license. A Type 03 holder is a real licensee, so a collectible firearm may ship directly to their home address with no dealer at the far end. The license number's fourth group is `03`, as in `1-23-456-03-7C-89012`. The ATF's public FFL directory does not list Type 03 collectors, so their license cannot be looked up and a person has to read the copy at disposition time.

**Card charge check.** A daily job that reads the last week's card charges back from Payroc and emails the managers about any that were voided or refunded outside the POS. See [What runs automatically](background-jobs.md).

**Cash drawer.** The page where the day's cash is counted and closed, and where every cash movement outside a sale is recorded. Terms on it:

- **Expected** cash is what the drawer should hold according to the books.
- **Close the day** is one count and one confirmation. A **count difference** above **Count difference that needs a reason** in Cash Drawer Settings ($20 by default) needs a reason.
- **Deposit to bank** takes cash to the bank. **Add cash from bank** brings it back. A **Cash expense** pays for something out of the drawer.
- **Undo** reverses an entry. Cash records are never deleted.

See [The cash drawer](../selling/cash-drawer.md).

**Charge exactly.** A box under the total on the register and on Create Order. Type the total the customer pays, tax included, and the line prices are worked back from it.

**Commission.** The store's cut of a consigned gun's sale price before tax. The consignor is owed the rest. A gun carries its own rate, which starts from the store default. See [Selling guns on consignment](../inventory/consignment.md).

**Connector Audit Log.** The permanent record of every call made through the store's remote MCP connectors: who, which tool, with what, and whether it worked. See [The MCP server](../../ai/mcp.md).

**Consignment.** A gun that belongs to someone else, left with the store to sell. Its Serial No is ticked **Consignment** and names the **Consignor**. The store sells it, keeps the commission and owes the consignor the rest. See [Selling guns on consignment](../inventory/consignment.md).

**Consignor.** The person who left a gun with the store on consignment. The system keeps a Supplier record for them, and what the store owes them sits in the **Consignment Payable** account until it is paid out.

**Custody warehouse.** A stock location holding firearms that belong to customers rather than to the store, typically a gun received for a transfer and waiting for its owner to collect it. A gun in a custody warehouse is not yours to sell, so it is excluded from sellable stock and hidden from Inventory Receipts unless you ask for it.

**Customer status.** **Active**, **Inactive**, **Prospect** or **Banned** on a customer's Profile tab. A banned customer needs a reason, and shows in red on the customer list. Two records for one person are joined with **Merge into…**. See [Customers](../selling/customers.md).

**Dashboard.** The page staff land on after signing in: today's sales, open 4473s, guns waiting on pickup, the cash drawer, records not yet in the bound book, and distributor orders arriving, with **All records** at the bottom. See [the Dashboard](../getting-started/dashboard.md).

**Disposition.** A firearm leaving your inventory, and the bound book entry that records it. A retail sale, a dealer transfer, a destroyed or stolen gun: each is a disposition, and each must name where the gun went.

**Distributor invoice.** A read-only copy of an invoice on a distributor account, pulled every hour, matched to the POS order it belongs to. One with no POS order behind it is marked **Outside POS**. See [Distributor orders](../distributors/restock.md).

**Distributor order.** One purchase from one wholesale house, numbered like `DO-2609-0001` (year, month, running number). It waits in Draft until a person confirms it. See [Distributor orders](../distributors/restock.md).

**Drop-ship.** A distributor ships a product straight to the customer or to the receiving dealer without it passing through your building. The order and the paperwork are yours. The box never touches your shelf. See [Drop-shipping](../distributors/dropship.md).

**Extension.** An add-on that brings its own pages and records, installed and switched on or off under Settings → Extensions. See [Extensions](../../extensions/README.md).

**eZ Check.** The ATF's public service for confirming that an FFL number is valid and unexpired. This system checks other dealers against it before receiving or shipping firearms, either by querying the ATF form directly or through a shared data service. See [FFL verification](../compliance/ffl-verification.md).

**FastBound.** The commercial electronic bound book service this system connects to. It holds the authoritative A&D record and it is where an electronic 4473 is completed. Your acquisitions and dispositions are pushed to it, and its webhook tells the POS when a 4473 finishes. See [FastBound](../compliance/fastbound.md).

**FFL.** Federal Firearms License, and by extension the business that holds one. "Ship it to their FFL" means ship it to the licensed dealer who will run the buyer's 4473. An FFL number looks like `1-23-456-78-9A-01234`.

**FFL type codes.** The two-digit group inside the license number that says what kind of licensee it is. The common ones are 01 for a dealer in firearms, 02 for a pawnbroker, 03 for a Curio and Relic collector, 07 for a manufacturer and 08 for an importer. Type 03 is the one that matters day to day, because a collector's license changes where a gun may be shipped.

**Incoming shipments.** The list on Receive Goods of distributor parcels on their way to the store, including parcels ordered outside the POS. See [Receive Goods](../inventory/receive-goods.md).

**Inventory count.** A stocktake: scan or type what is on the shelves, see what is missing or unexpected, and correct the quantities of items without serial numbers in one step. Guns are only reported, never adjusted. See [Counting the shop](../inventory/inventory-count.md).

**Item groups.** The families items are filed under. Firearms are **Pistol**, **Revolver**, **Rifle**, **Shotgun**, **Other Firearm** and **Receiver**. NFA items have their own group. Gun parts are under **Parts**: regular items with no serial number. The old Handgun, Long Gun, New Firearm and Used Firearm groups are retired. See [Items and serial numbers](../inventory/items-and-serials.md).

**Item versus serial.** An **Item** is the model: "Glock 19 Gen5 9mm". A **Serial No** is one physical gun with its own serial number, its own cost, its own photos and its own bound book history. Ammunition and accessories are items with a quantity and no serials. Firearms are items whose individual units are each a Serial No. Pricing, listings and reports all distinguish the two. See [Items and serials](../inventory/items-and-serials.md).

**Manager Override 4473.** A manager's button that finishes a firearm sale whose 4473 was completed but is stuck electronically, or was done directly in the bound book. It records who overrode it and why, and every override lands on the Hand-Asserted 4473 Dispositions report. See [Form 4473](../compliance/4473.md).

**MAP.** Minimum Advertised Price. The lowest price a manufacturer or distributor permits you to advertise a product at, which is not the same as the lowest price you may sell it at. The counter price is never raised to MAP.

**MCP connector.** The address an AI assistant connects to so it can read, or work, the POS, signed in with a person's own POS account. Each store has a read-only one and a full one, switched on in MCP Settings. See [The MCP server](../../ai/mcp.md).

**MSRP.** Manufacturer's Suggested Retail Price. The list price from the manufacturer. It is a starting point for pricing, not a rule.

**NICS.** The National Instant Criminal Background Check System, run by the FBI. Every retail firearm buyer goes through it. It answers Proceed, Delayed or Denied. A firearm sale needs Proceed before it can complete. A Delayed answer starts the Brady date clock above. Denied means the gun does not leave.

**Not placed.** A tab on the Storage Locations page listing guns and stock with no location yet, and units sold without saying which location they came from. See [Storage locations](../inventory/storage-locations.md).

**Notifications bell.** The bell at the top of every desk page. It rings in the app, never by email, for NICS verdicts, new web and GunBroker orders, a distributor shipping to the store, and a record FastBound refused. See [the Dashboard](../getting-started/dashboard.md).

**Outside POS.** A distributor invoice, or a parcel, for an order placed somewhere other than the POS, such as the distributor's website.

**Pickup ticket.** A $0 POS draft that hands over a gun somebody already bought on a counter order or a web order collected at your counter. The money stays on the order. The ticket carries the gun and its serial so the Form 4473 can run on the register and the gun leaves the bound book at Complete Order. Its label in Recent Orders reads the order's money: **Paid**, **Deposit … · … due** or **Unpaid · … due**. See [Point of Sale](../selling/pos.md).

**POS Invoice versus Sales Invoice.** A **POS Invoice** is a counter sale rung up on the register. A **Sales Invoice** is the ordinary ERPNext invoice, used for web orders, marketplace orders and anything built through Create Order. At the end of the day the register's POS Invoices are consolidated into Sales Invoices, which is why the [Sales Report](../reports/sales-report.md) reads both and excludes consolidated invoices, so nothing is counted twice.

**Private seller.** A person, not a business, who sells you a gun. The seller is paid on Receive Goods or in the Trade-in dialog by Cash, Zelle, Check or ACH. A purchase with no seller payment recorded shows on the cash drawer page and on the **Seller not settled** card of the Inventory Receipts report. See [Receive Goods](../inventory/receive-goods.md).

**Purchase Receipt.** The ERPNext document that books goods into stock when they arrive. Receive Goods creates one, and submitting it is what puts the units on the shelf and creates the bound book acquisitions for any firearms on it.

**Redo 4473.** Starting a sale's 4473 again. The earlier form shows as Cancelled in the 4473 records, and a paper 4473 or a manager override can finish the sale afterwards. See [Form 4473](../compliance/4473.md).

**Serial No.** The per-gun record. It carries the serial number itself, the manufacturer, model, caliber, importer, condition and year, the purchase cost, the selling price, the warehouse it sits in, its storage location, its photos, its listing state on each sales channel and its link to the FastBound item. Almost everything the store does to a single gun happens on this record.

**ShipStation.** The shipping service used to buy labels and get tracking numbers for outbound parcels. The Ship button on the Pending Order page pushes to it, and a scheduled job reads tracking numbers back. See [Integration settings](../settings/integrations.md).

**Storage location.** Where a gun or an item sits in the store. A **zone** is either a run of numbered **slots**, one gun per slot, or one open location such as a shelf, case or safe that holds anything. The **store map** draws the zones on a floor plan. Locations are a tracking layer: moving something changes no stock and no books. See [Storage locations](../inventory/storage-locations.md).

**Store pays customer.** What the register shows when a trade-in is worth more than the sale: one ticket, no sales tax, and the store pays the difference by Cash, Zelle, Check or ACH. See [Trade-ins](../inventory/trade-ins.md).

**Transfer.** A customer buys a gun somewhere else, has it shipped to you, and completes their 4473 here. You charge a transfer fee, run the 4473 and NICS, and hand over a gun that was never your inventory to sell. The gun still passes through the bound book both ways. See [Customer transfers](../selling/transfers.md).

**Virtual Terminal.** Payroc's web page for keying a card by hand. A charge taken there is recorded in the POS with its approval code, not charged again.

**Warehouse.** A named stock location in the books. A store has ordinary sales warehouses, plus special ones such as a custody warehouse for customer property. Which warehouse a gun is in decides whether it can be sold, so it is more than a shelf label. Where it sits on the floor is its storage location.
