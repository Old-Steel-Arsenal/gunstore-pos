# Quick start

For developers. One command in the OpenGunstore repository creates a working extension: a handler that honors the store's switch, a gated endpoint, a button on the sale screen, a release note, its tests and its API reference. Check it with a second command, make it yours, and package it for the Extensions page.

## Before you start

- A checkout of the OpenGunstore repository with its development stack running (`make dev-up`). The first time, create the development site as [Developers](../developers/index.md#run-the-dev-stack) describes.
- Docker and a host `python3`. The scaffolding script uses only the standard library.
- Run every command below from the root of the core checkout unless a step says otherwise.

## 1. Create the extension

```bash
make ext-new ID=acme_hello TITLE="Acme Hello" PUBLISHER="Acme Software LLC"
```

- `ID` is `<vendor>_<name>`: lowercase letters, digits and underscores, with a vendor of 2 to 12 characters. It becomes the folder, the Python package and the extension id.
- `TITLE` is what staff read: letters, digits, spaces and hyphens, at most 30 characters, starting with a letter.
- The extension is created next to the core checkout, in `../acme_hello`. Add `OUT=<folder>` to put it somewhere else. An existing, non-empty folder is never overwritten.

The command prints `created acme_hello in ../acme_hello (<n> files)` and writes:

```
acme_hello/
├── acme_hello/
│   ├── __init__.py                     __version__ = "0.1.0"
│   ├── hooks.py                        the manifest and the hooks
│   ├── modules.txt                     one line: Acme Hello
│   ├── patches.txt
│   ├── acme_hello/__init__.py          the module package
│   ├── api/greet.py                    two gated, documented endpoints: greet (POST) and ping (GET)
│   ├── hooks_impl/customer.py          a Customer after_insert handler, wrapped in @extension_hook
│   ├── public/js/acme_hello.bundle.js  the Say hi button, registered with the POS
│   ├── change_log/v0/v0_1_0.md         the release note staff read
│   └── tests/test_greet.py             switched off, no role, repeat call
├── docs/api/                           the API reference, generated from the code
├── README.md                           For the counter / For the manager / For developers
├── pyproject.toml
├── LICENSE
└── .gitignore
```

It follows the [developer specification](developer-spec.md) as generated: the lint finds nothing and the tests pass. The repository keeps one copy of this output, `ext_hello`, in `deploy/prod/tests/ext_hello`. Read it if you want to see the result before running anything.

## 2. Check it

```bash
make ext-test ID=acme_hello DIR=../acme_hello
```

This copies the extension into the development stack, installs it on the development site, and runs three checks. They are the same three the core's CI runs on first-party extensions.

1. **Tests:** `bench run-tests --app acme_hello`.
2. **Lint:** the checks the Extensions page runs on every upload.
3. **API docs:** `docs/api/` must match the code.

It ends with one line:

```
ext-test acme_hello: tests OK (5 ran) · lint OK · docs OK
```

The exit status is 0 when all three pass. It is 1 when the install, the tests, the lint or a defect in the API docs failed, 2 when the folder is refused before anything is installed, and 3 when the only problem is a stale `docs/api/`.

The extension stays installed on the development site. Running the command again after a change copies the new code, migrates the development site (so new or changed DocTypes, fields, fixtures and patches are in place) and repeats the three checks. Back the site up before the first run if you care about its data: an extension whose own tests leave records cannot be uninstalled again. [Extensions](README.md) has the removal steps.

`make ext-test` does not install bundled wheels yet. An extension with a `wheels/` folder (see step 4) is refused with a message saying so.

## 3. Make it yours

- In `acme_hello/hooks.py`, fill in `homepage` and `support` in the manifest. The manager sees both on the Extensions page. Declare a permission for everything your code does (specification §5.3). The lint names what is missing.
- Replace the greeting with your feature. Every endpoint opens with `require_enabled(APP, roles=…)` and carries a docstring with `Args:` and `Returns:` (specification §9). Every `doc_events` handler is wrapped in `@extension_hook(APP)`.
- Keep the three kinds of tests for every endpoint and hook you add: switched off, no role, and a repeat call that does not write twice.
- After changing an endpoint or a DocType, regenerate the API reference and commit it:

    ```bash
    python3 scripts/generate_api_docs.py --app ../acme_hello
    ```

- Run `make ext-test ID=acme_hello DIR=../acme_hello` again.

## 4. Package it

In the extension's folder:

```bash
cd ../acme_hello
git init
git add -A
git commit -m "Acme Hello 0.1.0"
git archive --format=zip -o acme_hello-0.1.0.zip HEAD
```

The zip root is the app root (`pyproject.toml` next to the `acme_hello/` package). Keep it under 50 MB and free of `.git`, `.env`, keys and build caches. The lint refuses them, and `git archive` leaves out everything `.gitignore` ignores.

If the repository is public, a tag is enough: the manager can fetch `https://github.com/<you>/acme_hello` at tag `v0.1.0` from the same tab instead of uploading a file. A library your extension needs goes into `wheels/` as a pure-Python wheel and into `dependencies` in `pyproject.toml` (specification §12).

## 5. Install it

Hand the zip to the store manager. On **Settings → Extensions → Upload** they choose the file, read the review card and the permission chips, and type `acme_hello` to confirm. The store does the rest: the code is staged, the POS restarts once, and the extension is installed and switched on. The server side of this (the agent that restarts the stack, and the rollback recipe) is in the [Extensions section of the self-hosting guide](../self-hosting.md#extensions).

To ship a new version, raise the version in `acme_hello/__init__.py` and in the manifest in `hooks.py`, add `change_log/v0/v0_2_0.md`, and hand over the new zip or tag. The manager uploads it the same way. The review card shows it as an upgrade, and the store backs up the database, swaps the code and runs your patches. The POS restarts during an upgrade, so do it outside business hours.

## What to read next

- [Developer specification](developer-spec.md): every rule the lint enforces and why.
- [Core hooks and the registry](hooks.md): what the POS offers to plug into.
