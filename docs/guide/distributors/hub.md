# Distributors hub

The hub at **/app/distributor-hub** is where a manager sets up and watches every wholesale house. Open it from Settings → **Distributors**. It shows one card per Distributor record. Anyone with a purchasing or stock role can read it. Changing a switch or the order needs System Manager or Purchase Manager.

Each card is one wholesale house: its switches, a summary of its settings page, feed health, open orders and actions.

## What a card shows

Across the top of a card sit the priority number, the house name and its provider code, plus the linked Supplier when the record names one. A house whose provider code has no integration behind it is marked **not registered** in red, and its **Test Connection** button is disabled.

### The switches

| Switch | What turning it on means |
|---|---|
| **Enabled** | The house is in use. Off, it is not searched, quoted or ordered from |
| **Stocking** | You may raise stocking and procure-to-store orders |
| **Poll** | Every 30 minutes, the system pulls status, tracking and serials for this house's live orders |

A card with **Enabled** off is greyed. The switches save the moment you click them, and a failure puts the box back where it was.

### The summary line

For RSR and Sports South, a read-only line under the switches sums up the house's own settings page.

| Item | Reads | What it means |
|---|---|---|
| **Ordering** | **live** | **Enabled** is ticked and **Allow live orders** is on |
| **Ordering** | **off (disabled)** | **Enabled** is unticked on this card |
| **Ordering** | **off (Allow live orders is off)** | The switch on the settings page is off |
| **Ship to store** | **free over $500.00** | The freight rule for orders shipped to your store |

Ordering reads **live** only when both layers are on. Otherwise it names the layer that is off, or both. While **Allow live orders** is off, nothing is sent to the house. The Ship to store wording follows the house's own rules: RSR reads **free** or **free over** an amount, and Sports South gives one threshold for handguns and one for everything else.

Click **Ordering** to open the settings page on its Account tab, or **Ship to store** to open it on the Freight tab. See [Integration settings](../settings/integrations.md).

### The health line

| Item | What it tells you |
|---|---|
| **Quantity feed** | How long ago the live quantity heartbeat last arrived |
| **Catalog** | How long ago the catalog content heartbeat last arrived |
| **Last order poll** | When this house's orders were last polled. Shown only while **Poll** is on |

Any of these can read "never". When the provider itself cannot be reached, the health line is replaced by the error in red, and the rest of the card still draws. A house whose catalog reads are off shows **Catalog: off**, which is a state and not a fault.

When Data Service Settings has **Catalog source** set to **Local**, a further line shows **Local catalog** with the offers loaded and, for each feed, when it last succeeded, how many rows it read and whether the last run failed.

### The order line

Below the health line, a clickable summary reads as **N draft**, **N in flight** and **N need attention**. Clicking it opens the Distributor Order list filtered to this house. In flight covers Queued, Placed, Acknowledged and Partially Shipped. Need attention covers Hold and Error, and it turns red when it is not zero. Complete and Cancelled orders are history and are not counted.

## The buttons

| Button | What it does |
|---|---|
| **Test Connection** | Runs the provider's live probe and shows each check with a green, red or grey dot and a detail line. It is read-only and places no order |
| **Sync now** | Queues every due feed of a house read from the local catalog. Shown only in Local catalog mode, to a System Manager |
| **Pricing & stock** | Opens the Distributor record. See below |
| **Account & shipping** | Opens the provider's own settings page: account, ordering and freight. Shown only when the provider has one |
| **Add Distributor** | At the bottom of the page. Asks which provider to use, then opens a new Distributor form |

Priority is changed with the small **↑** and **↓** buttons in a card's header. They appear once there is more than one house.

## Priority and what it drives

Priority orders the cards, lowest number first, and that order is the order the system prefers. It sets the column order in the fulfillment planner and picks the default house when nothing else names one. Houses on the same number sort oldest first, so a never-ordered house at 0 sits at the top. Moving a card rewrites every house's priority as 1 through N, so the list is always a clean sequence.

## The Distributor record

**Pricing & stock** takes you to the house's own record. It has these tabs.

| Tab | What it holds |
|---|---|
| **General** | **Code**, **Name**, **Provider**, **Supplier**, the switches (the last one is labelled **Poll orders** here) and **Priority** |
| **Catalog service** | **Endpoint**, **Client ID** and **Secret**, for a house read from its own catalog service. Blank uses the site-wide connection on Data Service Settings |
| **Status** | **Last order poll** |

**Supplier** is the vendor on receipts and cost invoices for this house. A house with no Supplier set cannot be received from a distributor order.
