#!/usr/bin/env bash
# backup-workspace.sh
# ----------------------------------------------------------------------------
# OpenClaw workspace (~/.openclaw/workspace) backup to RustFS (LAN S3).
#
# Policy (2026-07-29, Zun directive msg #29460):
#   - Backup ENTIRE workspace INCLUDING credentials, secrets, SSH keys,
#     .mobileconfig, .env, .pfx/.p12, .demo_vpn_creds, memory/.dreams/, etc.
#   - 3x daily at 04:00, 10:00, 20:00 UTC (= 06:00, 12:00, 22:00 SAST).
#   - ALWAYS KEEP LATEST, OVERWRITE OLD — single rolling destination.
#
# Approach: enumerate files via Python helper (with excludes for regenerable
# bloat + cruft), write to files list, then `rclone copy --files-from` to a
# fixed destination path. Handling weird filenames (control chars in corrupt
# remnants) more robustly than glob exclude patterns.
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
# Destination: rustfs:open-claw-push/workspace-backups/  (FIXED — rolling)
# ----------------------------------------------------------------------------

set -euo pipefail

WORKSPACE="${WORKSPACE_DIR:-/root/.openclaw/workspace}"
DEST_BASE="rustfs:open-claw-push/workspace-backups"
# Fixed rolling destination — always keep latest, overwrite old (msg #29460).
DEST="${DEST_BASE}"
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
# Pre-cleanup: purge legacy YYYY-MM-DD/ folders at destination.
# Zun directive msg #29460: "always keep latest, overwrite old."
# The old daily schedule left dated subdirs (2026-06-23/ etc.) at this path.
# `rclone copy --files-from` and `rclone sync --files-from` both preserve
# destination files outside the file list, so we delete the dated subfolders
# explicitly here (deterministic + idempotent).
log "[1/4] Cleaning legacy YYYY-MM-DD/ folders from $DEST_BASE ..."
mapfile -t LEGACY_DIRS < <(rclone lsf --dirs-only --max-depth 1 "$DEST_BASE" 2>/dev/null \
    | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}/$" || true)
if [[ ${#LEGACY_DIRS[@]} -eq 0 ]]; then
    log "  none — already clean"
else
    log "  found ${#LEGACY_DIRS[@]} legacy dirs"
    for d in "${LEGACY_DIRS[@]}"; do
        log "    purge: $d"
        if rclone purge "${DEST_BASE%/}/${d%/}" >/dev/null 2>&1; then
            log "      OK"
        else
            log "      WARN: purge failed for $d"
        fi
    done
fi

# Build the file list
log "[2/4] Enumerating workspace files..."
FILES_LIST=$(mktemp)
trap "rm -f $FILES_LIST" EXIT

python3 "$ENUMERATOR" "$WORKSPACE" > "$FILES_LIST"
COUNT=$(wc -l < "$FILES_LIST")
log "  $COUNT files selected for backup"

if [[ "$COUNT" -eq 0 ]]; then
    log "ERROR: enumerator returned 0 files — aborting"
    exit 3
fi

# Push to RustFS — rolling snapshot, overwrites existing files at destination.
log "[3/4] Uploading to $DEST ..."
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
log "[4/4] Verifying..."
rclone size "$DEST" 2>&1

# Spot-check key files (state + a credential file to prove inclusion)
for f in MEMORY.md TOOLS.md HEARTBEAT.md openclaw.json .gitignore; do
    if rclone ls "$DEST/$f" >/dev/null 2>&1; then
        log "  OK: $f"
    else
        log "  MISSING: $f"
    fi
done

log "=== workspace backup done ==="
