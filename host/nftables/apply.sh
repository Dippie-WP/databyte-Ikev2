#!/usr/bin/env bash
# apply.sh — Install /etc/nftables.conf from this directory
# --------------------------------------------------
# Usage: sudo bash host/nftables/apply.sh [vps-prod]
#
# Validates syntax with `nft -c -f` before installing. Run from repo root.
# Idempotent: running twice = same result.

set -euo pipefail

VARIANT="${1:-vps-prod}"
SRC="$(cd "$(dirname "$0")" && pwd)/nftables.conf.${VARIANT}"
DST="/etc/nftables.conf"

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: source not found: $SRC" >&2
    echo "Available variants:" >&2
    ls "$(dirname "$0")"/nftables.conf.* 2>/dev/null | sed "s|.*/||" >&2
    exit 1
fi

echo "=== Validating $SRC ==="
if ! sudo /usr/sbin/nft -c -f "$SRC"; then
    echo "ERROR: syntax check failed — NOT installing" >&2
    exit 1
fi

echo "=== Backing up current $DST ==="
if [[ -f "$DST" ]]; then
    sudo cp "$DST" "${DST}.bak-$(date +%Y%m%d-%H%M%S)"
fi

echo "=== Installing $SRC -> $DST ==="
sudo install -m 644 -o root -g root "$SRC" "$DST"

echo "=== Reloading nftables.service ==="
sudo systemctl reload nftables.service || sudo systemctl restart nftables.service

echo "=== Verifying live ruleset (first 10 lines) ==="
sudo /usr/sbin/nft list ruleset | head -10

echo ""
echo "DONE. Live nftables.conf is now from $SRC"
