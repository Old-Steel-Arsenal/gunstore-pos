# Deploying with an AI assistant

This page is for an AI coding agent (Claude Code, Codex, Cursor and the like) that is installing GunStore POS on a server for a person. It is a checklist with checks and stop points. It does not repeat the commands: **[self-hosting.md](self-hosting.md) is the source of truth**, and each step below names the section to run. If this page and that one disagree, follow self-hosting.md and tell the user.

If you are the person and not the agent, read [self-hosting.md](self-hosting.md) instead; it has the same steps with the explanations.

## Rules for the agent

1. **Never invent or guess a fact about the store** (name, abbreviation, address, FFL number, tax rate, e-mail, domain). Ask. A wrong tax rate or FFL number is worse than a pause.
2. **Never print, log, echo or commit a secret** (passwords, API keys, the contents of `deploy/prod/.env` or `deploy/prod/store.env`). Do not paste them into commands you show the user. When you need to show a file for confirmation, mask it: `sed -E 's/((PASSWORD|SECRET|KEY)[A-Z_]*=).*/\1***/' <file>`. Do not ask the user to paste secrets into the chat; have them write them into the file themselves, or into Desk.
3. **Stop and ask at every STOP below**, and do not continue until the user has said yes in this conversation. A STOP is anything irreversible or shared: DNS, the firewall, a shared reverse-proxy file, overwriting or restoring a database, enabling FastBound, the 4473 decision.
4. **Use the project's scripts** (`deploy/prod/up.sh`, `scripts/prod_bootstrap.sh`, `scripts/store_setup.sh`), not a hand-written `docker compose`. `up.sh` supplies the right compose files and env. Never run `docker compose down -v`, `docker volume rm`, `docker system prune`, `bench drop-site` or `bench reinstall`; they destroy the database.
5. **A step is done when its check passes**, not when the command exits 0. Report each step's result to the user in one line.
6. If a step fails and its failure note does not cover it, stop, show the user the error (with secrets masked) and ask. Do not improvise a workaround on a production database.

## 1. Ask the user first

Collect these before touching the server. Write the answers down in the conversation; do not guess any of them.

| Ask | Why |
|---|---|
| Which machine will run it, and how do you reach it (SSH host and user, whether `sudo` works)? | You run everything there. Confirm you are on the target server before each destructive-looking command. |
| The public host name (for example `pos.example.com`), who controls its DNS, and the server's static IP. | The site name must equal the public host; Caddy needs DNS and ports 80/443 to get a certificate. |
| Published image or build on the server? If published: the image name, the tag, and whether the registry needs a login. | Decides `CUSTOM_IMAGE`, `CUSTOM_TAG`, `PULL_POLICY`. |
| The Desk `Administrator` password. | The user chooses it. Do not make one up and say it in chat. |
| Is Caddy already on the server, and is there a shared Caddyfile with other sites? | The proxy step edits a shared file. |
| Is anything already installed here (an existing site, other Docker stacks)? | A second stack needs its own Compose project name and port (`export COMPOSE_PROJECT_NAME=...` in the shell before every `up.sh` call; `.env` does not set it) and its own `HTTP_PUBLISH_PORT`; an existing site changes what is safe to run. |
| The store facts, one by one: company name and short abbreviation, owner login e-mail, street address, city, state, ZIP, FFL number, the sales-tax rate (a percentage; their accountant knows it), and optionally phone, website, logo, receipt footer, California excise tax rate, outgoing-mail settings. | They go into `deploy/prod/store.env`; the full list is in `deploy/prod/store.env.example`. |
| Do they use FastBound, or paper 4473s? | It decides whether firearms can be sold at all; it is their compliance decision, not yours. See step 10. |
| Who will use the register, and what role each person gets? | Step 11. |

## 2. Preconditions

Run these on the server. Each must pass, or stop and tell the user why.

| Need | Check | Expect |
|---|---|---|
| Linux on x86-64 | `uname -sm` | `Linux x86_64`. On `aarch64`/`arm64` the stack cannot run (the image is amd64-only): stop. |
| 8 GB RAM or more | `free -g` | total of at least 8. Fewer: tell the user and let them decide. |
| Disk | `df -h /var/lib/docker .` | Room for the image, the database and backups. self-hosting.md sets no number; report what is free and ask if it looks tight. |
| Docker with Compose v2 | `docker version && docker compose version` | Both print versions. If Docker is missing, see [Docker](self-hosting.md#docker); installing it is a change to the machine, so ask first. |
| git, curl, openssl, bash | `git --version; curl --version \| head -1; openssl version; bash --version \| head -1` | All present. |
| Port 8080 free on localhost | `ss -ltn \| grep ':8080 '` | Nothing listening (or only this stack, on a re-run). |
| Existing stacks | `docker ps --format '{{.Names}} {{.Image}}'` | Know what else runs. If a `gunstore` project is already up, this is a re-run: read section 4. |
| DNS | `dig +short pos.example.com` (use the real name) | Prints the server's IP. Not yet? That is a STOP at step 7, not here. |
| Caddy | `caddy version; systemctl is-active caddy` | A version and `active`, or the user agrees to install it ([Before you start](self-hosting.md#before-you-start)). |

## 3. Steps

Work in the repository root on the server. Steps 1 to 6 build the stack, 7 puts it on the internet, 8 to 12 make it a store, 13 protects it.

**Step 1. Get the code.** Do: [Get the code](self-hosting.md#get-the-code). Expect: the repository is cloned and `deploy/frappe_docker/compose.yaml` exists. Check: `test -f deploy/frappe_docker/compose.yaml && git rev-parse --short HEAD`. If it fails: a network or path problem; show the error and stop. Clone into the server user's home directory (the extension agent expects `~/gunstore-pos`), unless the user says otherwise.

**Step 2. Get an image.** Do: published image, or build it: see [Before you start](self-hosting.md#before-you-start) (the `docker build --platform linux/amd64 ...` recipe, and `docker login` for a private registry). Expect: the image is on the machine. Check: `docker image inspect <image>:<tag> --format '{{.Os}}/{{.Architecture}}'` prints `linux/amd64` (after a pull, or `docker pull` for a published image). If it fails: "pull access denied" means the registry needs a login or the name is wrong (ask the user; never put their token in a command you display); an `arm64` result means it was built without `--platform linux/amd64`, so rebuild.

**Step 3. Write `deploy/prod/.env`.** Do: [Configure `.env`](self-hosting.md#configure-env). Generate `DB_PASSWORD` yourself (`openssl rand -hex 18`) and write it into the file only; tell the user it is stored there and nowhere else. Set `PULL_POLICY=never` for an image built on this machine. Leave `MCP_ENABLED=0`. Do not overwrite an existing `.env` (`test -e deploy/prod/.env` first; if it exists, read it masked and ask). Expect: a complete file. Check: `grep -v '^#' deploy/prod/.env | grep -cE 'CHANGE_ME|<registry>|vX\.Y\.Z'` prints `0` (comment lines excluded); `git check-ignore -v deploy/prod/.env` shows it is ignored; `chmod 600 deploy/prod/.env`. If it fails: fix the leftover placeholder. **Never change `DB_PASSWORD` after the first start**: the database keeps the first one and the site scripts would stop working.

**Step 4. Start the stack.** First, if another stack runs on this Docker host, `export COMPOSE_PROJECT_NAME=<unique name>` in the shell, and keep it exported for every later `up.sh` and script call (`up.sh` reads it from the shell, not from `.env`; without it this stack would reuse the other one's containers and volumes: STOP and ask if you see `gunstore-*` containers that are not yours). Do: `up.sh pull` (skip it for `PULL_POLICY=never`), then `up.sh up -d` and `up.sh ps` ([Start the stack](self-hosting.md#start-the-stack-and-create-the-site)). Expect: the database `(healthy)` and the frontend published on `127.0.0.1:8080`. Check: `./deploy/prod/up.sh ps`; re-run it for a minute if the database is still starting. If it fails: `./deploy/prod/up.sh logs --tail 50 db backend`; a platform error ("does not provide the specified platform") means the image is not amd64 (step 2). Stop and ask if it is anything else.

**Step 5. Create the site.** Do: `scripts/prod_bootstrap.sh` from [the same section](self-hosting.md#start-the-stack-and-create-the-site). It needs `ADMIN_PASSWORD` in its environment. Do not type the password into a command line you display: ask the user to put it in a file with mode 600 (or to run that one command themselves) and read it from there, for example `ADMIN_PASSWORD="$(cat <file>)" ./scripts/prod_bootstrap.sh`. **STOP first if a site already exists**: run `./deploy/prod/up.sh exec -T backend ls sites/`; a directory named `<site>` in the list means it exists (a fresh stack lists only `apps.txt`, `assets` and `common_site_config.json`). The script keeps an existing site and installs only missing apps, but ask before going on. Expect: several minutes of output ending in `Bootstrap complete for <site>.` Check: `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: <site>' http://127.0.0.1:8080/login` prints `200`. If it fails: the script is safe to run again; read the error, and if it names the database password, the `.env` value and the database disagree: stop and ask.

**Step 6. Check the site configuration.** Do: nothing; this is a check. Check: `./deploy/prod/up.sh exec -T backend bench --site <site> execute frappe.utils.get_url` prints `https://<site>`. If it prints `http://...`, `host_name` is missing; see the note under [Start the stack](self-hosting.md#start-the-stack-and-create-the-site) and ask before running `set-config`.

**Step 7. Put it on the internet.** **STOP: ask the user to confirm all three, in this order.** (a) The DNS A record for the site name points at the server (`dig +short <site>` equals the server's IP). You do not edit DNS; the user does. (b) The firewall allows 22, 80 and 443. You do not change the firewall without an explicit yes. (c) You may edit the shared Caddyfile (back it up first). Then do [Reverse proxy](self-hosting.md#reverse-proxy); if `HTTP_PUBLISH_PORT` in `.env` is not the default `127.0.0.1:8080`, set `POS_UPSTREAM` to it in that recipe so the Caddy block points at the right port. Expect: `sudo caddy validate` passes, the reload succeeds, and a certificate is issued within a minute. Check: `curl -sS -o /dev/null -w '%{http_code}\n' https://<site>/login` prints `200`, and `https://<site>/` answers `302` to `/login`. If it fails: a certificate error nearly always means DNS or ports 80/443 are not reachable from the internet yet; do not retry in a loop (Let's Encrypt rate limits), tell the user. If `caddy validate` fails, restore the backup you made and stop.

**Step 8. The ERPNext setup wizard. STOP: the user does this in a browser; do not script it.** Give the user the URL (`https://<site>/app`), the login (`Administrator` and the password they chose) and the answers table in [Set up your store, part 1](self-hosting.md#1-the-erpnext-setup-wizard). Tell them to write down the exact company name and abbreviation. Then wait: do nothing further until they tell you it is finished. Check, once they say so: `./deploy/prod/up.sh exec -T backend bench --site <site> execute frappe.is_setup_complete` prints nothing before the wizard has run and `true` (lowercase) after, and `... execute frappe.db.get_all --args "['Company']"` prints a JSON list such as `[{"name": "Example Firearms LLC"}]` with their company. If the name or abbreviation differs from what you have, use the wizard's version in `store.env`.

**Step 9. Store configuration.** Do: copy the template (or write the file from the answers with a heredoc, `chmod 600`, leaving out optional keys you have no answer for; a missing optional key equals a blank one; only the template's keys are read, other names in the file are reported and skipped), fill it from the answers in section 1 of this page, run the script ([Set up your store, part 2](self-hosting.md#2-the-store-configuration)). **STOP before running it**: show the user the filled `store.env` (masked) and get a yes for the tax rate, the FFL number, the name and the abbreviation. Expect: ends with `STORE_SETUP COMMITTED`, then a `VERIFY` line. Check: in the `VERIFY` line the `Pistol` entry has `sales_tax` equal to 1000 times the rate (8.25 % gives 82.5) and `Service` has `0.0`. If it fails: the script exits non-zero and says so. A failed run may have written part of the setup, and every step is safe to repeat, so fix the cause and run it again. Common messages: `store.env is missing <KEYS>` (fill them in); `Company <name> does not exist - run the setup wizard first` (step 8 is not finished, or `COMPANY_NAME` differs from the wizard's); `COMPANY_ABBR is X but Company ... has abbr Y` (use Y); `FORM_4473_MODE must be blank, fastbound or paper` (fix the value). Fix the file and run it again.

**Step 10. The 4473 decision.** **STOP: this is the user's compliance decision.** Explain the two options from [Selling firearms: FastBound or paper 4473](self-hosting.md#selling-firearms-fastbound-or-paper-4473) in plain words and ask which they use. Then:
- *Paper*: set `FORM_4473_MODE=paper` in `store.env` and re-run `scripts/store_setup.sh`. Remind them the paper form and the background check are still required.
- *FastBound*: set `FORM_4473_MODE=fastbound` (or leave it blank), and tell the user to enter the FastBound account number, API key, webhook secret and audit user in Desk (**FastBound Settings**) and register the webhook ([FastBound webhook](self-hosting.md#fastbound-webhook)). **Enabling FastBound is a STOP**: you do not have their credentials and must not ask for them in chat.
- *Undecided*: leave it blank and say clearly that firearm sales stay blocked until they choose. That is the safe default.

Check: `... execute frappe.db.get_single_value --args "['FFL Settings','paper_4473_mode']"` prints `1` for paper and `0` otherwise. Tell the user open tills must be reloaded after a change.

**Step 11. Staff.** The user adds a User in Desk for each person, with the roles in [Set up your store](self-hosting.md#2-the-store-configuration) ("Staff"), and adds cashiers to the POS Profile. Do not create accounts or passwords for them. Check: ask the user to confirm one cashier can open the register.

**Step 12. First-sale check.** Do: [Verify a first sale](self-hosting.md#verify-a-first-sale), the command-line check. Expect: `FIRST_SALE_CHECK PASS` last. If it fails, the message names the step; the usual causes are a missing POS Profile (step 9 not complete), a tax template error, or an open shift. If it says there is more than one enabled POS Profile, ask the user which one is the store's and pass it (`--kwargs "{'pos_profile': '<name>'}"`). If it says the site already has sales, this is not a new site: use the Desk steps with the user instead and do not force it. **Every passing run keeps one more $20 test sale, and a run that fails after the shift closed leaves its documents too**; tell the user, and see [Verify a first sale](self-hosting.md#verify-a-first-sale) for how to find and cancel them before the store opens. Run it once.

**Step 13. Backups.** Do: take one manual backup now, `./deploy/prod/up.sh exec -T backend bench --site <site> backup`, and list it (`... exec backend ls -lh sites/<site>/private/backups/`). Expect: a `database.sql.gz` with today's time. Tell the user the stack backs up the database every 6 hours on its own, that this is not a substitute for a machine snapshot at their hosting provider, and that [Backups and restore](self-hosting.md#backups-and-restore) says how to restore. Never run a restore unless they ask: it overwrites the database (STOP).

**Step 14. Optional integrations.** Distributor, WooCommerce and Data Service settings are the user's to enter in Desk ([Integration credentials](self-hosting.md#integration-credentials)). Point them there; do not collect keys. Check any one they set up with the items in [Verify the integrations](self-hosting.md#verify-the-integrations).

## 4. Running it again

| Step | Safe to repeat? |
|---|---|
| 1 Get the code | `git clone` fails if the directory exists; use `git pull --ff-only` and re-check `deploy/frappe_docker`. |
| 3 `.env` | **Do not** `cp` over an existing one. Edit the lines that changed. Never change `DB_PASSWORD`. |
| 4 `up.sh up -d` | Yes. It only changes what changed. |
| 5 `prod_bootstrap.sh` | Yes. An existing site is kept; missing apps are installed. |
| 7 Caddy | **Not idempotent**: appending the block twice duplicates the site. Check first: `grep -c '<site>' /etc/caddy/Caddyfile`. If it is already there, only reload. |
| 8 Wizard | Once. `frappe.is_setup_complete` says whether it has run. |
| 9, 10 `store_setup.sh` | Yes, and it is how a fact is changed: the file is re-applied. Edits made in Desk to the things it manages are put back (the list is in [Set up your store](self-hosting.md#2-the-store-configuration)). |
| 12 First-sale check | Runs again while no *other* sales exist and no shift is open, but each run leaves one more test sale. Run it once; on a live store, use the Desk steps. |
| Upgrades | A different procedure: [Upgrades](self-hosting.md#upgrades), in a maintenance window, after a backup. |

## 5. Final checklist

Tick each one with the command's output in front of you, then report to the user.

- [ ] `./deploy/prod/up.sh ps` shows the database healthy and every other service up (nothing restarting).
- [ ] `curl -sS -o /dev/null -w '%{http_code}\n' https://<site>/login` is `200`; `https://<site>/` answers `302` to `/login`.
- [ ] `execute frappe.utils.get_url` prints `https://<site>`.
- [ ] `execute frappe.is_setup_complete` prints `true`, and the company in Desk matches `store.env`.
- [ ] `STORE_SETUP COMMITTED` was printed, and the `VERIFY` line shows the user's tax rate on a $1000 pistol and zero on a service.
- [ ] Serial tracking is on: `execute frappe.db.get_single_value --args "['Stock Settings','enable_serial_and_batch_no_for_item']"` prints `1`.
- [ ] The 4473 choice is made and recorded, or the user knows firearm sales are blocked until it is.
- [ ] `FIRST_SALE_CHECK PASS` (or the Desk steps in [Verify a first sale](self-hosting.md#verify-a-first-sale) were done with the user).
- [ ] A manual backup exists.
- [ ] `git status --short` shows no `.env` or `store.env`, and `git check-ignore -v deploy/prod/.env deploy/prod/store.env` lists both as ignored.
- [ ] Nothing secret was printed in this session. If something was, tell the user which one and that it should be rotated.

## 6. What to tell the user at the end

In plain words: the address of the site and how to sign in; where the secrets live (`deploy/prod/.env` on the server; the Administrator password is theirs) and that you did not copy them anywhere; what you did not do and they still must (DNS, firewall, FastBound or paper decision and credentials, distributor and WooCommerce keys, staff accounts, a machine snapshot at the hosting provider); and the one-line command to run the first-sale check again. Point them at [self-hosting.md](self-hosting.md) for upgrades, backups and restore.
