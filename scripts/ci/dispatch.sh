#!/usr/bin/env bash
# Tell the deploy repo something happened: dispatch.sh <event_type> [key=value ...]
# Sends a repository_dispatch with client_payload {key: value, ...}. Needs DEPLOY_REPO
# (owner/name) and GH_TOKEN (the DEPLOY_DISPATCH_TOKEN environment secret).
set -euo pipefail
event="${1:?usage: dispatch.sh <event_type> [key=value ...]}"
shift
: "${DEPLOY_REPO:?DEPLOY_REPO is not set}"
: "${GH_TOKEN:?DEPLOY_REPO is set but the DEPLOY_DISPATCH_TOKEN environment secret (environment deploy-dispatch) is not}"
args=(-f "event_type=${event}")
for kv in "$@"; do
	[[ "$kv" == ?*=* ]] || { echo "::error::not key=value: ${kv}" >&2; exit 2; }
	args+=(-f "client_payload[${kv%%=*}]=${kv#*=}")
done
gh api "repos/${DEPLOY_REPO}/dispatches" "${args[@]}"
echo "==> told ${DEPLOY_REPO}: ${event} $*"
