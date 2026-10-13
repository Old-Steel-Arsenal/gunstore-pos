#!/bin/bash
# Links the image-baked assets into the (volume-mounted) sites/ dir on start.
# Vendored from frappe_docker (resources/core/main-entrypoint.sh) so the image
# build doesn't depend on the gitignored deploy/frappe_docker/ checkout.
set -e

ASSETS_PATH="/home/frappe/frappe-bench/sites/assets"
BAKED_PATH="/home/frappe/frappe-bench/assets"

echo "Linking fresh assets to volume..."
rm -rf "$ASSETS_PATH"
mkdir -p "$(dirname "$ASSETS_PATH")"
ln -s "$BAKED_PATH" "$ASSETS_PATH"

# Extension layer: re-link the apps declared in sites/extensions.json (no-op
# without that file). A failed replay stops the container on purpose — see the
# script header.
/usr/local/bin/ext-replay.sh

exec "$@"
