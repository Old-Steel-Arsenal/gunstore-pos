#!/usr/bin/env bash
# Configure a new site for YOUR store: runs ffl_core.setup.store_setup inside the backend
# container with the facts from a store.env file.
#
#   cp deploy/prod/store.env.example deploy/prod/store.env   # then edit it
#   ./scripts/store_setup.sh deploy/prod/store.env
#
# Run it AFTER scripts/prod_bootstrap.sh and the ERPNext setup wizard (it refuses when the
# Company does not exist yet). Safe to run again: it re-applies the file. It exits non-zero
# unless the run printed "STORE_SETUP COMMITTED", which store_setup prints last, once
# everything is written. After a failure part of the setup may be written (some steps commit
# on their own); every step is idempotent, so fix the cause and run it again.
#
# The values are handed to the container by NAME only (docker compose exec -e NAME): they
# never appear in a command line or in this script's output. Only the keys listed in
# deploy/prod/store.env.example are read; any other name in the file is reported and skipped,
# so a stray COMPOSE_PROJECT_NAME or PATH line cannot redirect this script. SITE overrides the
# site name (default: FRAPPE_SITE_NAME_HEADER in deploy/prod/.env).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UP="${UP:-$HERE/../deploy/prod/up.sh}"
ENV_FILE="$HERE/../deploy/prod/.env"
EXAMPLE="$HERE/../deploy/prod/store.env.example"

STORE_ENV="${1:-}"
[ -n "$STORE_ENV" ] || { echo "usage: $0 <store.env>   (start from deploy/prod/store.env.example)" >&2; exit 2; }
[ -f "$STORE_ENV" ] || { echo "Missing $STORE_ENV" >&2; exit 2; }
[ -f "$EXAMPLE" ] || { echo "Missing $EXAMPLE (it is the list of keys this script reads)" >&2; exit 2; }

SITE="${SITE:-}"
if [ -z "$SITE" ]; then
  [ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE and SITE is not set" >&2; exit 2; }
  SITE="$(grep -E '^FRAPPE_SITE_NAME_HEADER=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
fi
: "${SITE:?FRAPPE_SITE_NAME_HEADER not found in .env}"

# The keys we read: the KEY= lines of the example file (a test keeps it equal to the code's list).
known=" $(sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p' "$EXAMPLE" | tr '\n' ' ')"

# Read KEY=value lines without sourcing the file (it is data, not shell). Values can hold
# spaces, quotes, $ and #; one pair of surrounding quotes is stripped.
names=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  value="${line#*=}"
  if [ "$key" = "$line" ] || ! [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
    echo "$STORE_ENV: ignoring a line that is not KEY=value: ${line:0:40}" >&2
    continue
  fi
  case "$known" in
    *" $key "*) ;;
    *) echo "$STORE_ENV: $key is not a store setup key; skipped" >&2; continue ;;
  esac
  if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
  export "$key=$value"
  names+=(-e "$key")
done < "$STORE_ENV"
[ "${#names[@]}" -gt 0 ] || { echo "$STORE_ENV has no KEY=value lines" >&2; exit 2; }

echo "==> store setup on $SITE ($((${#names[@]} / 2)) values from $STORE_ENV)"
status=0
out="$("$UP" exec -T "${names[@]}" backend bench --site "$SITE" execute ffl_core.setup.store_setup.run 2>&1)" || status=$?
printf '%s\n' "$out"

if [ "$status" -ne 0 ] || ! printf '%s\n' "$out" | grep -qx 'STORE_SETUP COMMITTED'; then
  echo "!! store setup did NOT complete (exit $status, no 'STORE_SETUP COMMITTED' line). Fix the cause above and run it again; it is safe to repeat. A failed run may have written part of the setup." >&2
  exit 1
fi

echo
echo "==> VERIFY (sales tax computed per item group; expect your rate on a \$1000 Pistol, 0 on Service):"
printf '%s\n' "$out" | grep -E '^VERIFY ' || echo "(no VERIFY line in the output)"
echo "==> done: $SITE is configured. Next: docs/self-hosting.md > Verify a first sale."
