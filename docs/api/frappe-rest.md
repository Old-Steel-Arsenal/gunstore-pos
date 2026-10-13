# Frappe standard REST layer

Besides the custom RPC endpoints ([reference/](reference/README.md)), every
DocType — stock ERPNext ones and the
[custom DocTypes](reference/doctypes.md) — is reachable through Frappe's
generic REST API. Authentication and error
envelope are the same as for RPC calls ([README.md](README.md)).

## CRUD

| Action | Request |
|---|---|
| List | `GET /api/resource/<DocType>` |
| Read | `GET /api/resource/<DocType>/<name>` |
| Create | `POST /api/resource/<DocType>` (JSON body = field values) |
| Update | `PUT /api/resource/<DocType>/<name>` (partial body) |
| Delete | `DELETE /api/resource/<DocType>/<name>` |

List query parameters (values are JSON-encoded):

- `fields=["name","status",...]` — columns to return (default `["name"]`)
- `filters=[["Serial No","status","=","Active"]]` — AND-combined triples
- `or_filters=[...]` — OR-combined triples
- `limit_start=0`, `limit_page_length=20` — pagination (`0` = no limit)
- `order_by="modified desc"`

```bash
curl -sG "$BASE/api/resource/FFL%20License" \
  -H "Authorization: token $KEY:$SECRET" \
  --data-urlencode 'fields=["name","license_number","expiration_date"]' \
  --data-urlencode 'filters=[["expiration_date",">","2026-01-01"]]'
```

## Semantics worth knowing

- **Permissions** are the logged-in user's DocType permissions — the same
  rules as the desk UI. A `403` means the integration user lacks a role
  (see the *roles* column in [doctypes.md](reference/doctypes.md)).
- **Submittable DocTypes** (flagged in doctypes.md) carry `docstatus`
  0 = draft, 1 = submitted, 2 = cancelled. Submit by `PUT`-ing
  `{"docstatus": 1}`; submitted documents are immutable except for fields
  marked *Allow on Submit*. Cancel before amending.
- **Child tables** (flagged `child table`) are not directly addressable —
  they are embedded as arrays on their parent document and replaced
  wholesale on update.
- **Single DocTypes** (flagged `single`, e.g. settings) have exactly one
  record: `GET /api/resource/<DocType>/<DocType>`.
- **Business-rule side effects**: many custom DocTypes have validate/submit
  hooks (A&D book, 4473, sync queues...). Raw REST writes trigger them —
  prefer the purpose-built RPC endpoints where one exists; they encode the
  workflow correctly.

## Generic RPC helpers

Frappe also whitelists generic helpers under `/api/method/frappe.client.*`
(`get_list`, `get_value`, `set_value`, `insert`, `delete`, ...) and file
upload at `/api/method/upload_file` (multipart; pass `doctype`/`docname` to
attach). They obey the same permission checks; the purpose-built endpoints
remain the recommended surface for workflow operations.
