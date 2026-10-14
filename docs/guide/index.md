# User guide

This guide is for the people who run an FFL gun shop on OpenGunstore: counter staff, inventory staff, managers and the bookkeeper. It assumes retail experience and no knowledge of ERPNext or Frappe.

Pick the section that matches your job.

| Section | For | What it covers |
|---|---|---|
| [Getting started](getting-started/index.md) | Everyone | Signing in, the Dashboard and sidebar, and which roles open which pages |
| [Selling](selling/index.md) | Counter staff | The register, firearm sales, customer transfers, payments, the cash drawer and closing the day |
| [Inventory](inventory/index.md) | Inventory staff, managers | Receiving goods, trade-ins, items and serial numbers, photos, the store map, counts and corrections |
| [Compliance](compliance/index.md) | Counter staff, managers | The bound book, Form 4473 and NICS, FastBound, verifying an FFL and the daily checks |
| [Online channels](channels/index.md) | Inventory staff, counter staff | Your WooCommerce store, GunBroker and the orders that come back |
| [Distributors](distributors/index.md) | Purchasing staff, managers | Ordering from wholesale houses, tracking orders and drop-shipping |
| [Reports](reports/index.md) | Managers, bookkeeper | Every report, the Sales Report and the month-end checklist |
| [Settings](settings/index.md) | Managers, administrators | FFL Settings and the connection pages for each integration |
| [Reference](reference/glossary.md) | Everyone | The [glossary](reference/glossary.md), [troubleshooting](reference/troubleshooting.md) and [what the system does on its own](reference/background-jobs.md) |

## How the pieces fit

One record follows a gun from the door to the buyer. A firearm arrives through [Receive Goods](inventory/receive-goods.md), which creates its Serial No record, asks where it goes on the shelf, books the acquisition into the [bound book](compliance/bound-book.md), then pushes it to [FastBound](compliance/fastbound.md). While it sits in the safe it can be listed on the [web store](channels/woocommerce.md) and on [GunBroker](channels/gunbroker.md). When it sells, the [4473](compliance/4473.md) gate holds the order until the form and the background check clear. Only then does the sale complete, the disposition post and the gun leave stock. The money side flows into the [cash drawer](selling/cash-drawer.md), the [Sales Report](reports/sales-report.md) and the accounts.

## Conventions in this guide

- **Bold** text is a button, field or tab as it appears on screen.
- Messages in quotes or code blocks are the wording the system uses.
- A warning box marks something that affects federal recordkeeping. Read it before you act.
- Developer and self-hosting material is in the [developer documentation](../developers/index.md).
