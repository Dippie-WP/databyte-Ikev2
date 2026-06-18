#!/usr/bin/env bash
# build-image.sh — Build the strongSwan Docker image
#
# Usage:  bash scripts/build-image.sh [TAG]
#         bash scripts/build-image.sh zun/strongswan:6.0.7-mschapv2-attrsql
#         bash scripts/build-image.sh zun/strongswan:6.0.7-mschapv2-attrsql-v1.3
#
# Build time: ~5 min on a typical server. Needs internet for the strongSwan
# tarball + apt packages.

set -euo pipefail

IMAGE="${1:-zun/strongswan:6.0.7-mschapv2-attrsql}"

cd "$(dirname "$0")/../docker"

echo "=== Building $IMAGE ==="
echo "  Context: $(pwd)"
echo ""

docker build -t "$IMAGE" .

echo ""
echo "=== Built ==="
echo "  Image:  $IMAGE"
echo "  Size:   $(docker images --format '{{.Size}}' "$IMAGE")"
echo ""
echo "Next:"
echo "  bash scripts/seed-db.sh    # create the SQLite DB on host"
echo "  docker compose --profile vpn up -d   # start the container"
echo "  docker logs strongswan --tail 30    # verify"
