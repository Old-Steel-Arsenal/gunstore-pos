# Extensions — what lives in this repository

The contract for an extension is [hooks.md](hooks.md), the docstrings of the SDK in `ffl_core.extensions`, and the lint (`make ext-lint`), which checks the rules mechanically.
This folder holds the core-side contracts; the tooling is below.

- [hooks.md](hooks.md) — the named hooks the core reads on behalf of extensions.

## Commands (from the repository root)

| Command | What it does |
|---|---|
| `make ext-new ID=<vendor>_<name> TITLE="…" PUBLISHER="…" [OUT=../<id>]` | A new extension from `deploy/prod/tests/ext_template/` (`scripts/ext_new.py`, stdlib): the skeleton with its tests (switched off / no role / repeat call) and a generated `docs/api/`. Refuses a non-empty `OUT`. |
| `make ext-test ID=<id> DIR=<extension folder>` | In this checkout's dev stack: unpacks a copy of the folder and checks it — the id (the lint's rule, not a core app), `<id>/hooks.py`, no `wheels/`, no `apps/<id>` the script did not make — before it replaces `frappe-bench/apps-ext/<id>` (a refused folder changes nothing there), wires it the way replay does (`apps/<id>` symlink + `.pth`), installs it on the dev site — or, when it is installed already, runs `bench migrate` so changed DocTypes, fields, fixtures and patches land — then tests → lint → API-docs check (`scripts/ci/ext_test.sh`, the script CI runs). The script exits 2 when it refuses the folder; 1 when wiring, install or migrate, the tests, the lint or the API docs failed — an endpoint without its docstring, `Args` or `Returns`, a parity failure, or a `Permissions` section whose role list contradicts a gate made of the extension's own roles; 3 when the docs' only problem is drift from the core — a stale `docs/api/`, or a `Permissions` role list that differs from a gate resolved through the core (`PERMISSIONS DRIFT`); the summary line says `docs STALE`, make reports `Error 3`. An extension with `wheels/` is refused: bundled wheels are not installed here yet. |
| `make ext-lint DIR=<dir or zip>` | The lint alone, inside the dev container: `DIR` is resolved from `/workspace/frappe-bench` (e.g. `apps-ext/<id>` after `make ext-test`). |
| `python3 scripts/generate_api_docs.py --app <folder> [--out DIR] [--check]` | The extension's `docs/api/` (openapi.json + reference/), gates resolved through the core; `--check` exits 1 on a parity or docstring failure (a `Permissions` role list contradicting a gate of the extension's own roles included), 3 when stale files or `PERMISSIONS DRIFT` are the only problem. Without `--app`: the core's own `docs/api/`, unchanged (`--check` exits 0 or 1; a contradicting `Permissions` role list is 1 there, under its own heading). |
| `make ext-smoke IMAGE=<image>` | Replay and agent smoke on an image, driven by `deploy/prod/tests/ext_hello`. |

`deploy/prod/tests/ext_hello` is the skeleton generated for `ext_hello`; `scripts/test_ext_new.py` fails when
template and fixture drift. After changing the template, regenerate the fixture:
`rm -rf deploy/prod/tests/ext_hello && python3 scripts/ext_new.py --id ext_hello --title "Ext Hello" --publisher "Acme Software LLC" --out deploy/prod/tests/ext_hello --date 2026-09-13`
(keep the date unless the release note should change).

`make ext-test` leaves the extension installed on the dev site. Back the site up before the first run
(`bench --site dev.localhost backup`): an extension whose own tests leave records refuses `uninstall-app` —
osa_consignment does — and restoring that backup is then the only way back. To take it off:
`bench --site dev.localhost uninstall-app <id>`, or `bench --site dev.localhost restore <that backup>`; then delete
`frappe-bench/apps/<id>` (the link), `frappe-bench/apps-ext/<id>` (the copy), `env/lib/python*/site-packages/<id>.pth`
and the `<id>` line of `sites/apps.txt`, and run `make test` only after that.

## First-party extensions in CI

The maintainers test their first-party extensions against every core release, and against every merge to
`develop`, in their own deployment pipeline. This repository's own CI runs no extension tests. The pipeline calls
this repository's `.github/workflows/_app-tests.yml` (a reusable workflow) with:

| Input or secret | What it is |
|---|---|
| `only_extensions: true` | Run the extensions leg alone, without the core suites. A missing token or an empty list fails the run instead of warning. (`extensions: true` adds the leg next to the core suites; without the token it is skipped with a warning.) |
| `extension_list` | JSON array of `{id, repo, ref}`: the extensions to test. Each entry is validated before anything is cloned. |
| `repository`, `ref` | The core under test, `owner/name` and a commit. Empty = the calling repository and the triggering commit. |
| `EXT_REPOS_TOKEN` | A fine-grained PAT with contents:read on the extension repositories. |
| `CHECKOUT_TOKEN` | Contents:read on `repository`; needed only while that repository is private. |

Anyone who wants the same gate for their own extensions can call it the same way from their repository. Each
extension gets its own test runner and a site built the same way as the core suites' (`scripts/ci/ext_test.sh`).
Locally, `make ext-test` runs the same script (above).

The rules below apply wherever the leg runs.

- **Red:** `ext_test.sh` refuses the extension (exit 2, the refusals listed under `make ext-test` above) or fails to
  wire, install or migrate it (exit 1); its tests or lint fail; or its API docs have a defect of the extension's own
  — an endpoint without its docstring, `Args` or `Returns`, a parity failure, or a `Permissions` role list that
  contradicts a gate made of its own roles (exit 1; `CONFORMANCE FAIL` / `PARITY FAIL` in the log). That fails
  the leg, and with it the gate in front of the release. Unblock by fixing the extension.
- **Warning only** (exit 3, `docs STALE`, nothing else failed). The check runs this core's generator and role names
  against what the extension committed, so a core change trips it with nothing wrong in the extension:
  - *stale `docs/api/`* (`STALE`): the core changed the generator or a role name, or the extension changed an
    endpoint without regenerating. Regenerate the extension's docs with the core that is merged
    (`python3 scripts/generate_api_docs.py --app <extension>`) and merge them to its `main`.
  - *`PERMISSIONS DRIFT`*: a docstring's `Permissions` section hand-writes a role list, and a gate that resolves
    through the core (`require_enabled(APP, roles=COUNTER_ROLES)`) now derives other roles. Drop the hand-written
    list — the derived line the docs publish is authoritative, and a docstring without the list passes with the
    core before the change and after it — merge that to the extension, then regenerate as above.
- **Merge order.** The listed ref (`main`) is tested against the core under test. Merge the core change first;
  then regenerate the extension's docs with that core and merge them to its `main`. If a core change breaks the
  extension's tests, first merge an extension change that works with both cores, then the core.
- **Token expired or lost access:** the fetch step fails with an error naming `EXT_REPOS_TOKEN` (or, with
  `only_extensions` and no token at all, the gate fails first). Rotate the token: a new fine-grained PAT with
  contents:read on the listed repositories.
