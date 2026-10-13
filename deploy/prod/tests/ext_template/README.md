# {{title}}

An extension for GunStore POS, started from `make ext-new`. It works as
shipped — a **Say hi** button on the sale screen and a welcome message for new
customers — so every seam is in place before you replace the greeting with your
feature. The rules it follows are in the core's `docs/extensions/hooks.md`
and the extension lint (`make ext-lint`).

## For the counter

With {{title}} switched on, **Say hi** on the sale screen greets the customer on
the ticket, and a customer gets a short welcome when they are added.

## For the manager

- Install: Settings → Extensions → Upload, with the zip of this folder or its
  public Git address and a tag. The POS restarts once.
- Settings: none. Roles: anyone who rings a sale (Sales User, Sales Manager,
  Stock Manager, System Manager).
- External accounts: none; nothing leaves the store.
- Switching it off hides the button and stops the welcome message; nothing is
  deleted.

## For developers

- Core surface used: `ffl_core.extensions` (`extension_hook`, `require_enabled`),
  `ffl_core.roles.COUNTER_ROLES`, the POS registry
  (`gunstore.extensions.register`), and `doc_events` on Customer.
- Data model: none yet.
- Where things are: `{{id}}/hooks.py` (manifest and hooks), `hooks_impl/`
  (handlers), `api/` (endpoints), `public/js/{{id}}.bundle.js` (POS registry),
  `change_log/` (release notes), `tests/`.
- From a gunstore-pos checkout, with its dev stack up (`make dev-up`):
  - install on the dev site, run the tests, lint, check the API docs:
    `make ext-test ID={{id}} DIR=<this folder>`; run it again after a change
    (it copies the folder again and migrates the dev site)
  - lint only, on the copy `make ext-test` installed: `make ext-lint DIR=apps-ext/{{id}}`
  - after changing an endpoint, regenerate the API reference in `docs/api/`
    (`make ext-new` wrote the first one):
    `python3 scripts/generate_api_docs.py --app <this folder>`
- Package: `git archive --format=zip -o {{id}}-0.1.0.zip HEAD`.
