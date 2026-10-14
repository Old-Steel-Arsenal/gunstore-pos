# Developers

How OpenGunstore is put together, how to run it on your machine, how changes reach a release, and where the extension and AI interfaces are documented. Read this before your first pull request.

## Architecture in brief

OpenGunstore is three Frappe apps installed on top of stock Frappe and ERPNext v16. ERPNext stays the source of truth for stock, customers and accounting. The apps never patch Frappe or ERPNext. They change behavior only through Frappe's own extension points: doc events, custom-field fixtures, client scripts and whitelisted methods. The few overrides that exist are declared in `ffl_core/hooks.py` and are kept small: a document class mixin that lets a ticket go negative only when trade-in credit takes it there, and two replaced whitelisted methods (the POS opening-entry check, and Frappe's OAuth client registration for MCP clients). `make ci-upgrade-test` re-checks them against each new ERPNext release.

| App | Owns |
|---|---|
| `ffl_core` | Firearm and serial rules, acquisitions and dispositions (the bound book), the Form 4473 workflow, trade-ins and receiving, consignment, the cash drawer, inventory counts, storage locations, the extension layer (`ffl_core.extensions`), the remote MCP connector, and the release notes. |
| `ffl_integrations` | Outbound integrations: FastBound, Payroc, RSR, Sports South, ShipStation, ATF eZ Check and GunBroker, plus the client for the optional [Data Service](../data-service.md). |
| `ffl_woo_sync` | The WooCommerce connector: product and stock push, order intake, refunds and fulfillment status. |

The apps run on MariaDB and Redis. The pinned versions of everything are in [Pinned versions](../versions.md).

Three boundaries are worth knowing before you read code:

- **Extensions.** A site adds behavior with a separate Frappe app installed from the Extensions page, not by editing the core. The core offers named hooks and a small SDK. See [Extensions](../extensions/README.md).
- **AI assistants.** The POS is an OAuth authorization server for the Model Context Protocol. The MCP server code lives in its own repository, [opengunstore-mcp](https://github.com/Old-Steel-Arsenal/opengunstore-mcp). See [The MCP server](../ai/mcp.md).
- **The Data Service.** Distributor catalogs, offers and the national FFL directory come from an optional service behind a published contract. The POS works without it. See [Data service](../data-service.md).

The repository layout:

| Path | What is there |
|---|---|
| `apps/` | The three apps. |
| `deploy/dev/` | The development stack (Docker Compose). |
| `deploy/prod/` | The production image build, Compose files, upgrade scripts and the extension agent. |
| `scripts/` | Version bump, API-docs generator, extension scaffolding, store setup, and the CI helpers in `scripts/ci/`. |
| `docs/` | These docs. `docs/api/` is generated. |

## Run the dev stack

You need Docker, git, GNU make and a host `python3`. Every entry point is a Makefile target. `make help` lists them. Do not run `docker compose` directly: the targets pick the right project name, and a bare `docker compose` in a worktree attaches to the main checkout's containers.

```bash
make dev-up                # start the containers (MariaDB, Redis, bench)
make dev-bench-init        # one time: initialize the bench with Frappe + ERPNext
make dev-site-create       # create the dev.localhost site (admin password: admin)
make dev-install-our-apps  # install ffl_core, ffl_integrations, ffl_woo_sync
make dev-serve             # serve in the foreground at http://dev.localhost:8000
```

Day to day:

| Target | What it does |
|---|---|
| `make dev-up` / `make dev-down` | Start the containers / stop them and keep the volumes. |
| `make dev-logs`, `make dev-shell` | Tail the bench log / open a shell in the container. |
| `make dev-down-volumes` | Stop and wipe the database volume. |
| `make dev-reset` | Wipe everything, including the `frappe-bench/` folder. |
| `make dev-names` | Show which Compose project and ports this checkout uses. |

### Working in a git worktree

Run `make dev-up && make dev-bench-clone` in the worktree. It clones the main checkout's bench into its own containers and database volume, so several checkouts can run tests side by side. On macOS the clone takes seconds (APFS copy-on-write). Elsewhere it is a full copy. A worktree publishes no ports by default. Start it with `make dev-up PORT_OFFSET=10` and `make dev-serve` to reach it at `http://dev.localhost:8010`. `make dev-down-volumes` releases it.

### Tests and generated docs

```bash
make test              # JS unit tests, then the full suite for all three apps, in parallel (WORKERS=6)
make test-serial       # the same suite one app after another on one site, to trace a failure
make ci-upgrade-test   # upgrade the dev bench to the latest v16 Frappe/ERPNext, then re-run the suite
make api-docs          # regenerate docs/api/ after changing an endpoint or a docstring
make api-docs-check    # the CI gate: fails when docs/api/ is stale
```

The suite must pass twice in a row on the same site. The rules and the known traps are in [Testing](../testing.md).

Code you write has a few rules the checks enforce:

- A whitelisted endpoint needs a docstring with a one-line summary, `Args:` and `Returns:`. The [API reference](../api/index.html) is generated from them, and the permission line is derived from the code. Do not write role lists in a docstring.
- Commit through `ffl_core.utils.commit`, not `frappe.db.commit()`. Inside a test it does nothing, so the test's rollback still works.
- Change compliance logic (Form 4473, the bound book, validation hooks) only after adding the test.
- Never change Frappe or ERPNext core.

## Branches and releases

The full rules are in [CONTRIBUTING.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/main/CONTRIBUTING.md). In short:

- `main` is the release branch. It is protected: a pull request, and CI must pass. Merging to `main` tags the release.
- `develop` is the integration branch. Cut `feature/*` and `fix/*` branches from it and open pull requests back into it.
- A pull request into `develop` runs a light gate (API docs and the image build). Add the `ci:full` label to run the whole suite, or `ci:upgrade` for a pull request that adds a patch. A draft pull request runs nothing.
- A release is prepared on `develop`: write the release note in `apps/ffl_core/ffl_core/change_log/`, then `make bump-version VERSION=x.y.z`. The note is what every user reads in What's New, so write it in plain English for the counter.
- A release pull request from `develop` to `main` runs the full gate. Nobody tags a release by hand.

## Where to go next

| To do this | Read |
|---|---|
| Write an extension | [Extensions](../extensions/README.md), then the [Quick start](../extensions/quickstart.md) |
| Know exactly what an extension may and must not do | [Developer specification](../extensions/developer-spec.md) and [Core hooks](../extensions/hooks.md) |
| Call the POS from another program | [API reference](../api/index.html) and [Frappe REST](../api/frappe-rest.md) |
| Connect an AI assistant | [The MCP server](../ai/mcp.md) and [Working as an AI agent](../ai/operating.md) |
| Run it on a server | [Self-hosting](../self-hosting.md) |
| Understand the optional hosted service | [Data service](../data-service.md) |
| Report a security problem | [SECURITY.md](https://github.com/Old-Steel-Arsenal/opengunstore/blob/main/SECURITY.md). Do not open a public issue. |
