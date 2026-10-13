#!/usr/bin/env bash
# Vendor (or upgrade) the Redoc standalone bundle used by docs/api/index.html.
# Usage: scripts/fetch_redoc.sh [version]   (default: 2.5.0)
set -euo pipefail
VERSION="${1:-2.5.0}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/docs/api/assets"

curl -fsSL -o "$DEST/redoc.standalone.js" \
  "https://cdn.jsdelivr.net/npm/redoc@${VERSION}/bundles/redoc.standalone.js"
curl -fsSL -o "$DEST/redoc.standalone.js.LICENSE.txt" \
  "https://cdn.jsdelivr.net/npm/redoc@${VERSION}/bundles/redoc.standalone.js.LICENSE.txt"
curl -fsSL -o "$DEST/redoc.LICENSE" \
  "https://cdn.jsdelivr.net/npm/redoc@${VERSION}/LICENSE"

echo "redoc ${VERSION} vendored into $DEST"
shasum -a 256 "$DEST/redoc.standalone.js" "$DEST/redoc.standalone.js.LICENSE.txt"
echo "→ update the version + sha256 table in $DEST/README.md"
