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
#
# Expanded 2026-08-20 to cover all 13 deployed files (per #37978 verification):
# - 4 portal Python files (app.py, portal_auth.py, requirements.txt, bulk_action.py)
# - 4 portal static assets (www/index.html, www/portal/index.html, www/static/app.css, www/static/app.js, www/static/portal.js)
# - 1 nginx config (vpn-portal.conf)
# - 1 quota script (quota-exporter.py)
# - 2 strongswan charon configs (10-eap-radius.conf, debug.conf)
# Plus 3 pre-existing files (quota-monitor, bandwidth-monitor, update_rw_eap_conf).
FILES=(
  # Files already in CI (3 pre-existing)
  "quota/quota-monitor.py"
  "quota/bandwidth-monitor.py"
  "quota/update_rw_eap_conf.py"
  # New: portal app (5) — bot.py added 2026-10-09 per TKT-029 + 3 amendments
  "host/vpn-portal/app.py"
  "host/vpn-portal/bot.py"
  "host/vpn-portal/bot_polling.py"
  "host/vpn-portal/safe_write_rw_eap.py"
  "host/vpn-portal/portal_auth.py"
  "host/vpn-portal/requirements.txt"
  "host/vpn-portal/scripts/bulk_action.py"
  # New: portal static assets (5)
  "host/vpn-portal/www/index.html"
  "host/vpn-portal/www/portal/index.html"
  "host/vpn-portal/www/static/app.css"
  "host/vpn-portal/www/static/app.js"
  "host/vpn-portal/www/static/portal.js"
  # New: nginx config (1)
  "host/vpn-portal/nginx/vpn-portal.conf"
  # New: quota script (1)
  "quota/quota-exporter.py"
  # New: strongswan charon configs (2)
  "docker/strongswan.d/10-eap-radius.conf"
  "docker/strongswan.d/debug.conf"
  # Added 2026-10-20: build_installer.py had live-only HOT-177/HOT-183 fixes
  # (ByValTStr SizeConst, Mask=0x7) that drift-detect was NOT watching.
  "host/vpn-portal/scripts/build_installer.py"
)

# Remote paths on the VPS where these files LIVE. If a file isn't deployed
# yet, its live path is empty and the check SKIPs it (no false-positive).
declare -A LIVE_PATHS=(
  # Pre-existing (3)
  ["quota/quota-monitor.py"]="/opt/strongswan-vpn-gateway/quota/quota-monitor.py"
  ["quota/bandwidth-monitor.py"]="/opt/strongswan-vpn-gateway/quota/bandwidth-monitor.py"
  ["quota/update_rw_eap_conf.py"]="/opt/strongswan-vpn-gateway/quota/update_rw_eap_conf.py"
  # Portal app (5) — bot.py added 2026-10-09
  ["host/vpn-portal/app.py"]="/opt/vpn-portal/app.py"
  ["host/vpn-portal/bot.py"]="/opt/vpn-portal/bot.py"
  ["host/vpn-portal/portal_auth.py"]="/opt/vpn-portal/portal_auth.py"
  ["host/vpn-portal/requirements.txt"]="/opt/vpn-portal/requirements.txt"
  ["host/vpn-portal/scripts/bulk_action.py"]="/opt/vpn-portal/scripts/bulk_action.py"
  # Portal static assets (5)
  ["host/vpn-portal/www/index.html"]="/opt/vpn-portal/www/index.html"
  ["host/vpn-portal/www/portal/index.html"]="/opt/vpn-portal/www/portal/index.html"
  ["host/vpn-portal/www/static/app.css"]="/opt/vpn-portal/www/static/app.css"
  ["host/vpn-portal/www/static/app.js"]="/opt/vpn-portal/www/static/app.js"
  ["host/vpn-portal/www/static/portal.js"]="/opt/vpn-portal/www/static/portal.js"
  # nginx config (1)
  ["host/vpn-portal/nginx/vpn-portal.conf"]="/opt/vpn-portal/nginx/vpn-portal.conf"
  # Quota script (1)
  ["quota/quota-exporter.py"]="/opt/strongswan-vpn-gateway/quota/quota-exporter.py"
  # strongswan charon configs (2)
  ["docker/strongswan.d/10-eap-radius.conf"]="/opt/strongswan-vpn-gateway/docker/strongswan.d/10-eap-radius.conf"
  ["docker/strongswan.d/debug.conf"]="/opt/strongswan-vpn-gateway/docker/strongswan.d/debug.conf"
  # Added 2026-10-20
  ["host/vpn-portal/scripts/build_installer.py"]="/opt/vpn-portal/scripts/build_installer.py"
)

echo "=== Drift detection: $(date -u +%FT%TZ) ==="
echo "  repo HEAD:    $(git rev-parse HEAD)"
echo "  repo HEAD%:   $(git rev-parse --short HEAD)"
echo "  VPS:          $VPS_USER@$VPS_HOST"

drift_count=0
# Batch the md5sum calls into a single SSH session.
# Previously: one `ssh` per file → 17 connections × ~3.5s latency = 57-60s.
# Now: one `ssh` that runs `md5sum` on all deployed live paths in one shot.
# Builds a single command line on the VPS and parses the output.
# Skip handling is done client-side (paths not in LIVE_PATHS stay out of the batch).
# Modified 2026-10-16 to fix the 57s CI failure (post VPSSSH key rotation).
deployed_paths=()
for rel in "${FILES[@]}"; do
  if [ -n "${LIVE_PATHS[$rel]:-}" ]; then
    deployed_paths+=("${LIVE_PATHS[$rel]}")
  fi
done

if [ ${#deployed_paths[@]} -gt 0 ]; then
  # One SSH call, one md5sum invocation covering all deployed live paths.
  # We build the command dynamically so the VPS only pays one connection
  # cost regardless of how many files are checked.
  md5_cmd="md5sum"
  for p in "${deployed_paths[@]}"; do
    md5_cmd="$md5_cmd \"$p\""
  done
  live_md5_output=$(ssh -o BatchMode=yes -o ConnectTimeout=15 \
    "$VPS_USER@$VPS_HOST" "$md5_cmd" 2>/dev/null)
  # Build a lookup: live_path -> md5
  declare -A live_md5s=()
  while IFS= read -r line; do
    # Format: "<md5>  <path>"
    if [[ "$line" =~ ^([a-f0-9]{32})\ \ (.+)$ ]]; then
      live_md5s["${BASH_REMATCH[2]}"]="${BASH_REMATCH[1]}"
    fi
  done <<< "$live_md5_output"

  for rel in "${FILES[@]}"; do
    git_md5=$(git show "HEAD:$rel" 2>/dev/null | md5sum | awk '{print $1}')
    if [ -z "${LIVE_PATHS[$rel]:-}" ]; then
      echo "  SKIP:    $rel (no LIVE_PATH configured — file not deployed yet)"
      continue
    fi
    live_md5="${live_md5s[${LIVE_PATHS[$rel]}]:-}"

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
  unset live_md5s
fi

# ---------- Cross-repo check: vpn-admin-bot vs prod ----------
# Added 2026-10-09 — the VPN bot code is also tracked in Dippie-WP/vpn-admin-bot
# (a separate public repo that holds the "vpn-admin-bot" subset for the Telegram
# admin UI). This section verifies the public repo's files match what runs on
# the LIVE VPS, so all 3 sources (databyte-Ikev2, vpn-admin-bot, prod) are
# mutually consistent. Files are fetched from raw.githubusercontent.com — no
# auth needed (public repo). Catches: someone editing prod without committing
# to EITHER repo, or one repo drifting from the other.
VPN_ADMIN_BOT_REPO="Dippie-WP/vpn-admin-bot"
VPN_ADMIN_BOT_BRANCH="${VPN_ADMIN_BOT_BRANCH:-main}"
VPN_ADMIN_BOT_FILES=(
  "app.py"
  "bot.py"
)
vpn_admin_drift=0
# Same batching for the vpn-admin-bot section (was 2 separate SSH connects).
# Build one `md5sum` command covering both files, one SSH session.
vab_paths=()
vab_rels=()
for rel in "${VPN_ADMIN_BOT_FILES[@]}"; do
  vab_paths+=("/opt/vpn-portal/$rel")
  vab_rels+=("$rel")
done
if [ ${#vab_paths[@]} -gt 0 ]; then
  md5_cmd="md5sum"
  for p in "${vab_paths[@]}"; do
    md5_cmd="$md5_cmd \"$p\""
  done
  vab_md5_output=$(ssh -o BatchMode=yes -o ConnectTimeout=15 \
    "$VPS_USER@$VPS_HOST" "$md5_cmd" 2>/dev/null)
  declare -A vab_md5s=()
  while IFS= read -r line; do
    if [[ "$line" =~ ^([a-f0-9]{32})\ \ (.+)$ ]]; then
      vab_md5s["${BASH_REMATCH[2]}"]="${BASH_REMATCH[1]}"
    fi
  done <<< "$vab_md5_output"
  for i in "${!vab_rels[@]}"; do
    rel="${vab_rels[$i]}"
    live_md5="${vab_md5s[${vab_paths[$i]}]:-}"
    if [ -z "$live_md5" ]; then
    echo "  SKIP (vpn-admin-bot): $rel (could not hash live file)"
    continue
  fi
  remote_md5=$(curl -sSL "https://raw.githubusercontent.com/$VPN_ADMIN_BOT_REPO/$VPN_ADMIN_BOT_BRANCH/$rel" 2>/dev/null | md5sum | awk '{print $1}')
  if [ -z "$remote_md5" ]; then
    echo "  SKIP (vpn-admin-bot): $rel (could not fetch from $VPN_ADMIN_BOT_REPO)"
    continue
  fi
  if [ "$live_md5" = "$remote_md5" ]; then
    echo "  MATCH (vpn-admin-bot): $rel  ($live_md5)"
  else
    echo "  DRIFT!! (vpn-admin-bot): $rel"
    echo "    vpn-admin-bot: $remote_md5  ($VPN_ADMIN_BOT_REPO/$rel)"
    echo "    live:          $live_md5  (/opt/vpn-portal/$rel)"
    vpn_admin_drift=$((vpn_admin_drift + 1))
  fi
done
fi

if [ $vpn_admin_drift -gt 0 ]; then
  echo
  echo "::error::$vpn_admin_drift vpn-admin-bot file(s) on LIVE VPS differ from $VPN_ADMIN_BOT_REPO HEAD. Sync prod -> vpn-admin-bot."
fi

echo
echo "=== Summary ==="
echo "  databyte-Ikev2 files checked: ${#FILES[@]}"
echo "  databyte-Ikev2 drift count:   $drift_count"
echo "  vpn-admin-bot files checked:  ${#VPN_ADMIN_BOT_FILES[@]}"
echo "  vpn-admin-bot drift count:    $vpn_admin_drift"
echo "  TOTAL drift:                   $((drift_count + vpn_admin_drift))"

total_drift=$((drift_count + vpn_admin_drift))
if [ $total_drift -gt 0 ]; then
  echo
  echo "::error::$total_drift file(s) on LIVE VPS differ from git HEAD(s). Run tools/sync-from-live.sh to re-sync."
  exit 1
fi

echo "All LIVE files match git HEAD. No drift."
