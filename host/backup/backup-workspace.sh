#!/usr/bin/env bash
# backup-workspace.sh
# ----------------------------------------------------------------------------
# Daily backup of OpenClaw workspace (~/.openclaw/workspace) to RustFS (LAN S3).
#
# Approach: enumerate safe files via Python helper, write to files list, then
# `rclone copy --files-from`. This handles weird filenames (control chars in
# corrupt remnants) more robustly than glob exclude patterns.
#
# Excluded (sensitive — NEVER backup):
#   - credentials/                       (telegram bot tokens)
#   - .demo_vpn_creds                    (VPN PSK)
#   - *.mobileconfig                     (contain VPN PSK / password)
#   - *.pfx, *.p12, .env                 (private keys / secrets)
#   - **/id_rsa*, **/id_ed25519*         (SSH private keys — defensive)
#
# Excluded (regenerable — not source-of-truth):
#   - .git/, **/__pycache__/, **/node_modules/, **/dist/
#   - mempalace_env/                     (Python venv, ~365M)
#   - ops-tracker*/node_modules/,        (Node modules)
#   - reports/pdf-tool/, reports/weather-beacon-versions/  (old binaries)
#   - *.log, *.log.*
#
# Excluded (cruft):
#   - tmp.bak-*/, http.bak-*/, *.bak-*, app.py.bak-v13pre
#   - Files with control chars in name (corruption remnants)
#
# Destination: rustfs:open-claw-push/workspace-backups/<YYYY-MM-DD>/
# ----------------------------------------------------------------------------

set -euo pipefail

WORKSPACE="${WORKSPACE_DIR:-/root/.openclaw/workspace}"
DEST_BASE="rustfs:open-claw-push/workspace-backups"
DEST="${DEST_BASE}/$(date -u +%Y-%m-%d)"
LOG_DIR="/var/log/workspace-backup"
LOG_FILE="$LOG_DIR/backup-$(date -u +%Y-%m-%d).log"

# Use repo's helper (falls back to local /tmp if not installed)
HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENUMERATOR="${HELPER_DIR}/workspace_files_enumerator.py"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "[$(date -u +%FT%TZ)] $*"; }

log "=== workspace backup start ==="
log "Source: $WORKSPACE"
log "Dest:   $DEST"
log "Log:    $LOG_FILE"

# Pre-flight
if [[ ! -d "$WORKSPACE" ]]; then
    log "ERROR: $WORKSPACE not found"
    exit 1
fi
if [[ ! -f "$ENUMERATOR" ]]; then
    log "ERROR: enumerator not found at $ENUMERATOR"
    exit 1
fi
if ! command -v rclone >/dev/null; then
    log "ERROR: rclone not installed"
    exit 1
fi
if ! rclone lsf "${DEST_BASE}/" --max-depth 1 >/dev/null 2>&1; then
    log "ERROR: rclone cannot reach $DEST_BASE"
    exit 2
fi

# Build the file list
log "[1/3] Enumerating workspace files..."
FILES_LIST=$(mktemp)
trap "rm -f $FILES_LIST" EXIT

python3 "$ENUMERATOR" "$WORKSPACE" > "$FILES_LIST"
COUNT=$(wc -l < "$FILES_LIST")
log "  $COUNT files selected for backup"

if [[ "$COUNT" -eq 0 ]]; then
    log "ERROR: enumerator returned 0 files — aborting"
    exit 3
fi

# Push to RustFS
log "[2/3] Uploading to $DEST..."
rclone copy "$WORKSPACE" "$DEST" \
    --files-from "$FILES_LIST" \
    --transfers=4 \
    --checkers=4 \
    --s3-no-check-bucket \
    --stats=30s \
    --stats-one-line

RC=$?
if [[ $RC -ne 0 ]]; then
    log "ERROR: rclone exited $RC"
    exit $RC
fi

# Post-flight
log "[3/3] Verifying..."
rclone size "$DEST" 2>&1

# Spot-check key files
for f in MEMORY.md TOOLS.md HEARTBEAT.md memory/2026-06-23.md; do
    if rclone ls "$DEST/$f" >/dev/null 2>&1; then
        log "  OK: $f"
    else
        log "  MISSING: $f"
    fi
done

# Defense: verify NO sensitive content made it
# `|| true` because grep returns 1 when no match (which would otherwise trip
# `set -euo pipefail`).
SENSITIVE_HITS=$(rclone lsf -R "$DEST" 2>/dev/null | grep -iE "mobileconfig|^credentials|demo_vpn_creds|\.env$" | head -5) || true
if [[ -n "$SENSITIVE_HITS" ]]; then
    log "WARNING: sensitive files detected in backup!"
    echo "$SENSITIVE_HITS" | while read line; do
        log "  LEAK: $line"
    done
    # Don't fail — just alert
fi

log "=== workspace backup done ==="
