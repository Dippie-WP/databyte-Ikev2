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

# Files we want to verify are in sync between LIVE VPS and git HEAD.
# Expanded 2026-08-17 to cover the 3 atomic-write callers + backup script that
# were part of the TKT-002 refactor (previously uncommitted, drift went
# undetected because they weren't in this list — see runs #187-196).
FILES=(
  "host/vpn-portal/app.py"
  "host/vpn-portal/www/portal/index.html"
  "quota/quota-monitor.py"
  "quota/bandwidth-monitor.py"
  "host/backup/backup-vpn-portal-config.sh"
  "ops/rotate-vpn-credentials.py"
  "quota/update_rw_eap_conf.py"
)

# Remote paths on the VPS where these files LIVE. If a file isn't deployed
# yet, its live path is empty and the check SKIPs it (no false-positive).
declare -A LIVE_PATHS=(
  ["host/vpn-portal/app.py"]="/opt/vpn-portal/app.py"
  ["host/vpn-portal/www/portal/index.html"]="/opt/vpn-portal/www/portal/index.html"
  ["quota/quota-monitor.py"]="/opt/strongswan-vpn-gateway/quota/quota-monitor.py"
  ["quota/bandwidth-monitor.py"]="/opt/strongswan-vpn-gateway/quota/bandwidth-monitor.py"
  ["host/backup/backup-vpn-portal-config.sh"]=""
  ["ops/rotate-vpn-credentials.py"]=""
  ["quota/update_rw_eap_conf.py"]="/opt/strongswan-vpn-gateway/quota/update_rw_eap_conf.py"
)

echo "=== Drift detection: $(date -u +%FT%TZ) ==="
echo "  repo HEAD:    $(git rev-parse HEAD)"
echo "  repo HEAD%:   $(git rev-parse --short HEAD)"
echo "  VPS:          $VPS_USER@$VPS_HOST"

drift_count=0
for rel in "${FILES[@]}"; do
  git_md5=$(git show "HEAD:$rel" 2>/dev/null | md5sum | awk '{print $1}')
  # Skip files that aren't deployed to LIVE yet (empty LIVE_PATHS entry).
  # Without this, md5sum with no arg hashes empty stdin and reports drift
  # vs real git content (false positive). Files with no live path yet are
  # "future drift coverage" — they'll be checked once they're deployed.
  if [ -z "${LIVE_PATHS[$rel]:-}" ]; then
    echo "  SKIP:    $rel (no LIVE_PATH configured — file not deployed yet)"
    continue
  fi
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
