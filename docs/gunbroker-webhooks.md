# GunBroker webhooks

GunBroker can tell POS the moment something happens instead of waiting for POS to ask. Without
webhooks a gun that sold (or took a bid) on GunBroker can still look available on the web store and
at the counter for up to 15 minutes, until the next order or auction poll. With them the gap is
seconds.

Webhooks only speed things up. The polling (orders and auctions every 15 minutes, listings hourly)
stays on as the safety net: GunBroker documents no retry for a webhook that fails, so a lost
delivery costs at most one poll interval and nothing else. POS never takes a webhook's word for
what happened. It uses the event as a prompt to look.

## Set up

1. In POS open **GunBroker Settings → Advanced → Webhooks**. Enter a **Webhook user name** and a
   **Webhook password** (at least 12 characters; the user name cannot contain a colon) and save.
   Webhook requests are refused until both are set.
2. On the same form, **Webhook URLs** lists five addresses built from this site's own URL.
3. Sign in at <https://www.gunbroker.com/account/webhooks> (the seller account POS lists on) and add
   one webhook per event, pasting the matching address and the user name and password from step 1.
   GunBroker authenticates with HTTP Basic, so the site must be served over HTTPS.
4. After GunBroker delivers its first event, **Last webhook received** and **Last webhook event** on the
   same form show when and what. If they stay empty, GunBroker is not reaching POS (see below).

The URLs have the form
`https://<your-site>/api/method/ffl_integrations.gunbroker.webhooks.<event>`:

| GunBroker event | `<event>` | What POS does |
|---|---|---|
| ItemsSold | `items_sold` | Pulls GunBroker orders right away, so the sold gun is held and taken off the web store and the counter. |
| OrderStatusUpdate | `order_status_update` | Pulls GunBroker orders right away (payment, FFL receipt, shipment, cancellations). |
| WinningBidReceived | `winning_bid_received` | Locks the gun for the auction (off the web store and the counter) without waiting for the next auction poll. A gun POS cannot match triggers a full auction check instead. |
| ItemsUnsoldNotRelisted | `items_unsold_not_relisted` | Re-checks that gun's listing now (a gun that is no longer for sale has its listing ended). The hourly sweep still decides whether a listing is really gone. |
| FixedPriceListingFailed | `fixed_price_listing_failed` | Writes GunBroker's error text into the gun's (or item's) **GunBroker Sync Error** field and re-checks it. |

POS matches a listing to a gun by its SKU (`item_code::serial`), then by GunBroker item number
(including earlier numbers from relists). Ammunition and other stock listed by model carry the bare
item code as SKU. An event that matches nothing is acknowledged and logged without further action.

## Security and privacy

- GunBroker signs nothing; the Basic credentials are the only authentication. Use a long random
  password that is not used anywhere else.
- Any failure to authenticate gets the same `401` answer, whether the credentials are wrong or
  missing, GunBroker is switched off in POS, or the webhook credentials are not set yet. Outsiders
  cannot tell whether a site uses GunBroker.
- The ItemsSold body contains the buyer's name, address, phone and e-mail. POS reads only the order
  number from it. The body is never stored or written to a log. Orders are read from the GunBroker
  API by the normal order sync.
- A webhook arriving while an order sync is already running makes that sync go round again once it
  finishes (up to three passes), so the new order is not left waiting for the next poll.

## If nothing arrives

- Check that **Enable GunBroker** is on and that the user name and password match what is entered at
  GunBroker. A mismatch is invisible by design: GunBroker just receives a 401. With GunBroker on and
  the webhook credentials empty, POS writes one Error Log entry an hour saying so.
- **Run Scheduled Sync** does not stop webhooks. Switching webhooks off is done at GunBroker.
- Everything still works through the polls, just up to 15 minutes later.
