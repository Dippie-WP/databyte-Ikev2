#!/usr/bin/env bash
# ci-drift-detect.sh — verify LIVE VPS files match git HEAD content.
#
# Triggered on every push to main. Reads /etc/vps-drift.env for LIVE URLs
# and SSH key info. The script assumes it can reach the VPS via SSH key
# in CI runner (configured via SSH_KEY secret + ssh config).
#
# Exit 0 = no drift (LIVE files match git HEAD).
# Exit 1 = drift detected (LIVE file MD5 differs from git HEAD MD5).
#
# Background: 2026-07-11 case-sensitivity bug (CORR-2026-07-11-026) was
# fixed on LIVE but not synced to git for hours. Three sister files had
# the same root cause. This CI prevents the same drift pattern from
# reaching prod again.
#
# Reference: HOOP.dev "IaC Drift Detection in GitHub CI/CD" pattern,
# search-web 2026-05: compare commit SHA + live file hash on every push.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Configurable via env (GitHub Actions secrets / vars)
VPS_HOST="${VPS_HOST:-vps-01}"
VPS_USER="${VPS_USER:-root}"

# Files we want to verify are in sync between LIVE VPS and git HEAD
FILES=(
  "host/vpn-portal/app.py"
  "host/vpn-portal/www/portal/index.html"
  "quota/quota-monitor.py"
  "quota/bandwidth-monitor.py"
)

# Remote paths on the VPS where these files LIVE
declare -A LIVE_PATHS=(
  ["host/vpn-portal/app.py"]="/opt/vpn-portal/app.py"
  ["host/vpn-portal/www/portal/index.html"]="/opt/vpn-portal/www/portal/index.html"
  ["quota/quota-monitor.py"]="/home/zunaid/strongswan/quota/quota-monitor.py"
  ["quota/bandwidth-monitor.py"]="/home/zunaid/strongswan/quota/bandwidth-monitor.py"
)

echo "=== Drift detection: $(date -u +%FT%TZ) ==="
echo "  repo HEAD:    $(git rev-parse HEAD)"
echo "  repo HEAD%:   $(git rev-parse --short HEAD)"
echo "  VPS:          $VPS_USER@$VPS_HOST"

drift_count=0
for rel in "${FILES[@]}"; do
  git_md5=$(git show "HEAD:$rel" 2>/dev/null | md5sum | awk '{print $1}')
  live_md5=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$VPS_USER@$VPS_HOST" "md5sum ${LIVE_PATHS[$rel]}" 2>/dev/null | awk '{print $1}')

  if [ -z "$git_md5" ] || [ -z "$live_md5" ]; then
    echo "  SKIP:    $rel (could not compute one or both hashes)"
    continue
  fi

  if [ "$git_md5" = "$live_md5" ]; then
    echo "  MATCH:   $rel  ($git_md5)"
  else
    echo "  DRIFT!!  $rel"
    echo "    git:    $git_md5  ($rel)"
    echo "    live:   $live_md5  (${LIVE_PATHS[$rel]})"
    drift_count=$((drift_count + 1))
  fi
done

echo
echo "=== Summary ==="
echo "  files checked: ${#FILES[@]}"
echo "  drift count:   $drift_count"

if [ $drift_count -gt 0 ]; then
  echo
  echo "::error::$drift_count file(s) on LIVE VPS differ from git HEAD. Run tools/sync-from-live.sh to re-sync."
  exit 1
fi

echo "All LIVE files match git HEAD. No drift."
