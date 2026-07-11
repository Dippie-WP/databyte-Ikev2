#!/usr/bin/env bash
# sync-from-live.sh — pull ALL four high-risk files from LIVE VPS into the
# working tree, then `git diff` for operator review. Does NOT auto-commit.
#
# Use when: CI drift-detect fails, or you manually edited live and want
# the repo to reflect the live state. After this script runs, review the
# diff with `git diff`, then commit + push as usual.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VPS_HOST="${VPS_HOST:-vps-01}"
VPS_USER="${VPS_USER:-root}"

FILES=(
  "host/vpn-portal/app.py:/opt/vpn-portal/app.py"
  "host/vpn-portal/www/portal/index.html:/opt/vpn-portal/www/portal/index.html"
  "quota/quota-monitor.py:/home/zunaid/strongswan/quota/quota-monitor.py"
  "quota/bandwidth-monitor.py:/home/zunaid/strongswan/quota/bandwidth-monitor.py"
)

mkdir -p host/vpn-portal/www/portal

for mapping in "${FILES[@]}"; do
  rel="${mapping%%:*}"
  live="${mapping#*:}"
  live_dir="$(dirname "$live")"
  printf "  fetch: %s\n         <- %s:%s\n" "$rel" "$VPS_HOST" "$live"
  ssh -o BatchMode=yes "$VPS_USER@$VPS_HOST" "cat $live" > "$REPO_ROOT/$rel"
done

echo
echo "Working tree updated. Review with:"
echo "  git diff --stat"
echo "  git diff"
echo
echo "When ready:"
echo "  git add -A"
echo "  git commit -m 'fix: sync live VPS files (drift detected)'"
