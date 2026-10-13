#!/usr/bin/env bash
# Fetch local copies of FastBound's swagger specs into docs/fastbound/ (git-ignored).
# Run after any FastBound note that the API has changed. Each spec is downloaded
# to a temp file (HTTP errors fail the run, TLS is verified) and checked to be
# valid JSON before it replaces the old copy; when the content changed, the old
# copy is kept as <name>.prev.json so you can see what moved:
#   diff docs/fastbound/v1-account.prev.json docs/fastbound/v1-account.json
set -euo pipefail

cd "$(dirname "$0")/.."
dir=docs/fastbound
mkdir -p "$dir"

tmp=$(mktemp "$dir/.fetch.XXXXXX")
trap 'rm -f "$tmp"' EXIT

for n in v1-account v1-partner v1-webhookmodels; do
  url="https://cloud.fastbound.com/swagger/$n/swagger.json"
  echo "fetching $url"
  curl -fsSL --max-time 60 "$url" -o "$tmp"
  python3 -m json.tool "$tmp" >/dev/null || { echo "$url did not return valid JSON; $dir/$n.json left as it was" >&2; exit 1; }
  if [ -f "$dir/$n.json" ] && cmp -s "$tmp" "$dir/$n.json"; then
    echo "  unchanged"
    continue
  fi
  [ -f "$dir/$n.json" ] && cp "$dir/$n.json" "$dir/$n.prev.json"
  cp "$tmp" "$dir/$n.json"
  chmod 644 "$dir/$n.json"
done

ls -lh "$dir"/*.json
