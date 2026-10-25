#!/usr/bin/env bash
# sync-from-live.sh — pull the deployed files from the LIVE VPS into the
# working tree, then `git diff` for operator review. Does NOT auto-commit.
#
# Use when: CI drift-detect fails, or you manually edited live and want
# the repo to reflect the live state. After this script runs, review the
# diff with `git diff`, then commit + push as usual.
#
# 2026-10-25 repair — this script had drifted from tools/ci-drift-detect.sh
# and running it as it stood would have damaged the working tree:
#
#   * quota/quota-monitor.py was mapped to
#       /home/zunaid/strongswan/quota/quota-monitor.py
#     which does not exist on the VPS. Worse, `ssh ... cat <path> > target`
#     truncates `target` BEFORE cat runs, so one invocation would have
#     zeroed out quota/quota-monitor.py in the working tree and then
#     aborted under `set -e`. The real live path is
#       /opt/strongswan-vpn-gateway/quota/quota-monitor.py
#
#   * host/vpn-portal/bot.py and host/vpn-portal/scripts/bulk_action.py
#     were not listed at all, so 2 of the 4 files drift-detect reports
#     would never be re-synced and CI would stay red.
#
#   * The header said "all four high-risk files" while ci-drift-detect.sh
#     now watches 18.
#
# The mapping below now matches ci-drift-detect.sh's LIVE_PATHS exactly.
# Keep them in sync: if you add a file to one, add it to the other.
#
# Safety: each file is fetched into a temp dir and only moved into place
# after a non-empty read, so a failed or empty fetch can never truncate a
# file that is already correct in the working tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VPS_HOST="${VPS_HOST:-vps-01}"
VPS_USER="${VPS_USER:-root}"

# "rel_path:live_path" — must match ci-drift-detect.sh's LIVE_PATHS
MAPPINGS=(
  "quota/quota-monitor.py:/opt/strongswan-vpn-gateway/quota/quota-monitor.py"
  "quota/bandwidth-monitor.py:/opt/strongswan-vpn-gateway/quota/bandwidth-monitor.py"
  "quota/update_rw_eap_conf.py:/opt/strongswan-vpn-gateway/quota/update_rw_eap_conf.py"
  "quota/quota-exporter.py:/opt/strongswan-vpn-gateway/quota/quota-exporter.py"
  "host/vpn-portal/app.py:/opt/vpn-portal/app.py"
  "host/vpn-portal/bot.py:/opt/vpn-portal/bot.py"
  "host/vpn-portal/portal_auth.py:/opt/vpn-portal/portal_auth.py"
  "host/vpn-portal/requirements.txt:/opt/vpn-portal/requirements.txt"
  "host/vpn-portal/scripts/bulk_action.py:/opt/vpn-portal/scripts/bulk_action.py"
  "host/vpn-portal/scripts/build_installer.py:/opt/vpn-portal/scripts/build_installer.py"
  "host/vpn-portal/www/index.html:/opt/vpn-portal/www/index.html"
  "host/vpn-portal/www/portal/index.html:/opt/vpn-portal/www/portal/index.html"
  "host/vpn-portal/www/static/app.css:/opt/vpn-portal/www/static/app.css"
  "host/vpn-portal/www/static/app.js:/opt/vpn-portal/www/static/app.js"
  "host/vpn-portal/www/static/portal.js:/opt/vpn-portal/www/static/portal.js"
  "host/vpn-portal/nginx/vpn-portal.conf:/opt/vpn-portal/nginx/vpn-portal.conf"
  "docker/strongswan.d/10-eap-radius.conf:/opt/strongswan-vpn-gateway/docker/strongswan.d/10-eap-radius.conf"
  "docker/strongswan.d/debug.conf:/opt/strongswan-vpn-gateway/docker/strongswan.d/debug.conf"
)

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

fetched=0
failed=0

for mapping in "${MAPPINGS[@]}"; do
  rel="${mapping%%:*}"
  live="${mapping#*:}"
  printf "  fetch: %s\n         <- %s:%s\n" "$rel" "$VPS_USER@$VPS_HOST" "$live"

  tmp="$tmpdir/$(printf '%s' "$rel" | tr '/' '_')"

  # Fetch to temp FIRST. Never redirect straight onto the repo file:
  # `> file` truncates before the command runs, so a failed fetch would
  # destroy a file that was already correct.
  if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_USER@$VPS_HOST" "cat '$live'" > "$tmp" 2>/dev/null; then
    printf "  !! SSH FETCH FAILED — leaving %s untouched\n" "$rel"
    failed=$((failed + 1))
    continue
  fi

  if [ ! -s "$tmp" ]; then
    printf "  !! EMPTY RESPONSE (missing on VPS?) — leaving %s untouched\n" "$rel"
    failed=$((failed + 1))
    continue
  fi

  mkdir -p "$(dirname "$rel")"
  mv "$tmp" "$rel"
  fetched=$((fetched + 1))
done

echo
echo "Fetched: $fetched    Failed: $failed"
echo
echo "Working tree updated. Review with:"
echo "  git diff --stat"
echo "  git diff"
echo
echo "When ready:"
echo "  git add -A"
echo "  git commit -m 'fix: sync live VPS files (drift detected)'"

if [ "$failed" -gt 0 ]; then
  echo
  echo "NOTE: $failed file(s) could not be fetched and were left untouched."
  exit 1
fi
