#!/usr/bin/env bash
# Thin wrapper around `docker compose` for the GunStore-POS production stack.
# Combines the vendored frappe_docker base compose with the overrides we use
# (mariadb, redis, noproxy, backup-cron) and loads deploy/prod/.env.
#
# noproxy publishes the frappe frontend on HTTP_PUBLISH_PORT (we use
# 127.0.0.1:8080); TLS + routing are handled by a reverse proxy on the host
# (Caddy). See deploy/prod/Caddyfile.example.
#
# Examples:
#   ./deploy/prod/up.sh pull              # pull the image named in .env
#   ./deploy/prod/up.sh up -d             # start the stack
#   ./deploy/prod/up.sh ps
#   ./deploy/prod/up.sh logs -f backend
#   ./deploy/prod/up.sh exec backend bench version
#   ./deploy/prod/up.sh down
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FD="$REPO/deploy/frappe_docker"
ENV_FILE="$HERE/.env"

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE — copy deploy/prod/.env.example to it and fill in."; exit 1; }

# One stack per machine: the Compose project name defaults to `gunstore`. A second
# stack on the same host (a demo, say) sets COMPOSE_PROJECT_NAME to tell itself
# apart from it and from the CI test stacks (dev-*).
exec docker compose \
  --project-name "${COMPOSE_PROJECT_NAME:-gunstore}" \
  --env-file "$ENV_FILE" \
  -f "$FD/compose.yaml" \
  -f "$FD/overrides/compose.mariadb.yaml" \
  -f "$FD/overrides/compose.redis.yaml" \
  -f "$FD/overrides/compose.noproxy.yaml" \
  -f "$FD/overrides/compose.backup-cron.yaml" \
  -f "$HERE/compose.extensions.yaml" \
  "$@"
