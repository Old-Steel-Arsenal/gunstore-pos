# FastBound API — Integration Notes

> Source: `https://cloud.fastbound.com/swagger/index.html`
> Spec snapshots fetched into this directory (git-ignored, not committed):
> - `v1-account.json` — main API (95 endpoints)
> - `v1-partner.json` — account creation + transfer between FastBound accounts (2 endpoints)
> - `v1-webhookmodels.json` — webhook event payload schemas

Fetch (or refresh) them with `scripts/fetch_fastbound_spec.sh`; the specs are git-ignored. The script fails on HTTP errors, verifies TLS, and only replaces a spec once the download parses as JSON. When a spec changed, the previous copy is kept next to it as `<name>.prev.json`, so `diff docs/fastbound/v1-account.prev.json docs/fastbound/v1-account.json` shows upstream API changes (useful in ERPNext upgrade reviews). Re-running when nothing changed leaves the `.prev.json` alone.

---

## 1. Authentication & request envelope

The spec does not expose security schemes. FastBound's documented scheme, which the client uses, is HTTP Basic with the account's API key as the username and an empty password (details in section 6; the key and webhook secret live in the FastBound Settings doctype, encrypted).

Every Account-API call carries:

| Where | Name | Required | Purpose |
|---|---|---|---|
| Path | `accountNum` | yes | FastBound account number |
| Header | `X-AuditUser` | yes | Who-changed-what audit (the POS sends the Audit user from FastBound Settings) |
| Header | `x-api-version` | optional | API version pin (the client always sends `1.0`) |
| Auth | `Authorization: Basic ...` | yes | API key as username, empty password |

Base URL: `https://cloud.fastbound.com/{accountNum}/api/...`

## 2. Namespaces (13 tags)

| Tag | Endpoints | Our use |
|---|---|---|
| **DispositionsV1** | 27 | Sale to individual / FFL transfer / Theft-Loss / Destroyed |
| **AcquisitionsV1** | 18 | Trade-in / new stock receipt / dealer transfer in |
| **ContactsV1** | 13 | Customer & receiving FFL records |
| **ItemsV1** | 11 | Firearm Item lookup (≈ ERPNext Item with serial) |
| SmartListsV1 | 14 | Saved searches (probably skip for v1) |
| WebhooksV1 | 5 | Event subscriptions (NICS callback pathway) |
| Form4473sV1 | 1 | Download printed 4473 PDF (only) |
| MultipleSaleReportsV1 | 1 | 3310.4 multi-handgun report |
| InventoryV1 | 1 | Inventory query |
| AccountV1 / AttachmentsV1 / DownloadsV1 / UsersV1 | 1 each | Admin |

**Note**: Form4473s exposes only `Download`. Filling out a 4473 happens **inside FastBound's hosted UI**, not via API. The POS holds the order in Draft and sends the cashier to FastBound's Form4473s page; FastBound fires `form4473.completed` when the form is done, and the webhook handler matches the disposition's items back to the invoice by serial (`fastbound/pos_4473.py`).

## 3. Webhooks (54 event schemas)

Event categories observed:

- **Acquisition**: Created, Committed, Edited, Deleted, Transfer, ItemsAdded, ItemsRemoved
- **Disposition**: Created, Committed, Edited, Deleted, ItemsAdded, ItemsRemoved
- **Contact**: Created, Edited, Deleted

Subscribe via `POST /{accountNum}/api/Webhooks`; available event types via `GET /Webhooks/Events`.

The POS subscribes to `form4473.completed` and `disposition.committed` (section 6): a committed 4473 plus NICS Proceed lets the POS mark the related Sales Invoice as compliance-cleared and unblock its submission.

## 4. CreateDisposition Command — top-level fields

| Field | Type | Notes |
|---|---|---|
| `externalId` | string | **Use ERPNext SI/SO name** for round-trip mapping |
| `date` | date-time | Transaction date |
| `type` | string | **required**; e.g. Sale / FFL Transfer / Theft / Loss / Destroyed |
| `note` | string | Free text |
| `ttsn` | string | Transaction Tracking Serial Number |
| `generateTTSN` | boolean | Server-generated TTSN |
| `otsn` | string | Original TTSN (corrections) |
| `purchaseOrderNumber` | string | |
| `invoiceNumber` | string | **Map to ERPNext SI name** |
| `shipmentTrackingNumber` | string | UPS / FedEx |
| `isManufacturingDisposition` | boolean | NA for retail/dealer |

Items, contact, 4473 link arrive via secondary endpoints (`AttachContact`, `Items`, etc.) — see the fetched `v1-account.json` for the full path tree.

## 5. How the integration is wired

1. **Settings doctype** `FastBound Settings` (in `ffl_integrations`): account number, API key (encrypted), base URL, audit user, webhook secret, look-up-FFL switch, request timeout (section 6 explains the fields).
2. **Client** `ffl_integrations.fastbound.client.FastBoundClient`: a thin REST wrapper that reads the settings, paginates on the consumer's side and retries 429/5xx with exponential back-off.
3. **Webhook receiver** `ffl_integrations.fastbound.webhook.fastbound_event`: verifies FastBound's `X-FastBound-Signature` header (`t=<ts>,v1=<hex>`, HMAC-SHA256 over `<ts>.<raw body>`) with the webhook secret. It answers 401 when the signature is missing, malformed or wrong (and, with the same response, when the integration is switched off on the site) and 200 otherwise, so FastBound does not retry or disable the webhook over a handler error; handler errors are logged, not returned.
4. **Outbound pushes**, wired in `hooks.py`:
   - `FFL Acquisition` submit enqueues an Acquisition push, cancel enqueues the delete; `FFL Disposition` submit and cancel push or cancel non-4473 dispositions (transfers, destruction). Receive Goods pushes its acquisitions synchronously inside the receive transaction, so a firearm is in FastBound and in the POS or in neither.
   - Retail 4473 dispositions are never created through the API (POST `/Form4473s` is 405): the cashier completes the 4473 in FastBound and the webhook ties it back.
5. **Reconciliation**: scheduled jobs retry failed pushes and deletes and alert on items stuck in FastBound; a per-gun diff (`fastbound/reconcile.py`) compares a Serial No's identity fields with the FastBound item and applies the operator's per-field choice; the bound-book sync previews, then removes on confirmation, guns that FastBound shows as disposed but that are still in stock here (guns held at a consignment dealer are left alone).

## 6. Settings page: what the one-line descriptions leave out

The form keeps one sentence per field. The reasoning behind the fields that
have any lives here.

- **Enable FastBound** — unchecked, nothing is pushed to the electronic bound
  book, no webhook is accepted and the sync/reconcile jobs stop, but FFL
  Acquisition / FFL Disposition records are still created locally, so the POS
  keeps its own A&D book. Firearm sales then need **FFL Settings → Paper 4473
  Mode**, which replaces the FastBound 4473 flow with a counter attestation.
  Takes effect on the next POS page load — reload any till that is already
  open, or it keeps acting on the old setting.
- **Base URL** — required and deliberately not defaulted. A default would point
  a brand-new site at the live bound book (`https://cloud.fastbound.com`) out of
  the box, so a dev or test site would write real acquisitions and dispositions
  to the real ledger before anyone had stated an intent. Set the production URL
  only on the production site; use the FastBound test account's URL everywhere
  else. Blank stops the integration rather than redirecting it.
- **API key** — sent as the HTTP Basic username with an empty password, which
  is FastBound's documented scheme. Stored encrypted. The API version header
  (`x-api-version: 1.0`) is a constant in the client, not a setting: the
  request shapes are tied to it.
- **Audit user** — the `X-AuditUser` header FastBound records on every change.
- **Webhook** — register
  `https://<your-site>/api/method/ffl_integrations.fastbound.webhook.fastbound_event`
  in FastBound → Settings → Webhooks, subscribed to `form4473.completed` and
  `disposition.committed`; the shared HMAC secret from that screen verifies the
  inbound POSTs.
- **Look up FFL on push** — FFL Dealer acquisitions send `lookupFFL=true` so
  FastBound populates the contact from its own FFL database. Switch it off only
  on a test account whose FFL numbers that lookup does not know; FastBound then
  accepts the FFL string as-is.
- **Cached lists** — the AcquireType / DisposeType values (presets + custom)
  behind the Acquisition Type and Disposition Type dropdowns. Sync FastBound
  Lists refreshes them; the scheduler also does.
