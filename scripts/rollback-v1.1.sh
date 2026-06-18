#!/usr/bin/env bash
# rollback-v1.1.sh — 3-min rollback to the v1.1 image (no attr-sql, PSK+EAP only)
#
# Use this when v1.2 is broken in a way that you can't fix in <10 min.
# The old image (zun/strongswan:6.0.7-mschapv2) is still in the registry
# (we kept it for exactly this scenario).
#
# Steps:
#   1. Switch docker-compose.yml image to v1.1
#   2. Re-create container
#   3. The DB is bind-mounted, NOT in the image — your data is preserved
#   4. Reconnect clients using PSK only (EAP creds still work but VIP pinning won't)
#
# Recovery (after v1.2 fix):
#   1. Switch docker-compose.yml back to v1.2
#   2. Re-create container
#   3. DB intact, VIPs preserved
#   4. Reconnect clients normally

set -euo pipefail

cd "$(dirname "$0")/../docker"

V11="zun/strongswan:6.0.7-mschapv2"
V12="zun/strongswan:6.0.7-mschapv2-attrsql"

echo "=== Rolling back to v1.1 ==="
echo "  v1.2 image (current):  $V12"
echo "  v1.1 image (rollback): $V11"
echo ""

read -r -p "Are you sure? (type 'yes' to proceed): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted"
    exit 0
fi

echo ""
echo "=== Verifying v1.1 image is in registry ==="
if ! docker image inspect "$V11" >/dev/null 2>&1; then
    echo "ERROR: $V11 not in local registry"
    echo "If you've already pruned, you'll need to rebuild from v1.1 source (git tag v1.1.0)"
    exit 1
fi

echo "=== Switching image ==="
sed -i.bak "s|$V12|$V11|" docker-compose.yml
echo "  docker-compose.yml: $V12 → $V11 (backup: docker-compose.yml.bak)"

echo ""
echo "=== Re-creating container ==="
docker compose --profile vpn up -d

echo ""
echo "=== Done ==="
echo ""
echo "  Container: $(docker ps --filter name=strongswan --format '{{.Status}}')"
echo "  Image:     $(docker inspect strongswan --format '{{.Config.Image}}')"
echo ""
echo "Test:"
echo "  docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas"
echo ""
echo "Recovery (back to v1.2):"
echo "  cd docker"
echo "  sed -i 's|$V11|$V12|' docker-compose.yml"
echo "  docker compose --profile vpn up -d"
