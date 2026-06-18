#!/usr/bin/env bash
# strongswan-db-backup.sh — Backup the strongSwan SQLite DB to RustFS
#
# Run via cron: /etc/cron.d/strongswan-db-backup at 03:00 UTC daily.
# Retention: 14 daily + 8 weekly (ISO week slot, overwrites).
#
# Why daily + ISO-week: DB is small (~200KB) so storage cost is zero.
# Two-tier retention gives 14 days of granularity for "which day did X break"
# AND multi-month history for "did this drift since the last release".
#
# Per-host setup:
#   1. Install rclone: https://rclone.org/install/
#   2. Configure RustFS remote: `rclone config` → name: rustfs
#      - type: s3
#      - provider: Other
#      - endpoint: http://YOUR_TRUENAS_IP:30293 (S3 NodePort)
#      - access_key_id, secret_access_key: from TrueNAS RustFS app
#      - force_path_style: true
#      - region: us-east-1
#      - no_check_bucket: true
#   3. Create the bucket: `rclone mkdir rustfs:open-claw-push`
#   4. Drop the cron file: `cp host/cron/strongswan-db-backup /etc/cron.d/`
#
# Cron entry (already in /etc/cron.d/strongswan-db-backup on the live LXC):
#   0 3 * * * root /usr/bin/flock -n /var/run/strongswan-db-backup.lock /usr/local/bin/strongswan-db-backup.sh

set -euo pipefail

DB_PATH="/var/lib/strongswan/ipsec.db"
REMOTE="rustfs:open-claw-push/strongswan-db"
LOCAL_STAGE="/var/backups/strongswan-db"
LOCK="/var/run/strongswan-db-backup.lock"
LOG="/var/log/strongswan-db-backup.log"

DATE_DAY=$(date -u +%Y-%m-%d)
DATE_WEEK=$(date -u +%G-W%V)
TS=$(date -u +%FT%TZ)

mkdir -p "$LOCAL_STAGE"

log() { echo "[$TS] $*" | tee -a "$LOG"; }

log "=== strongswan-db-backup started ==="

# Refuse to back up a missing or zero-byte DB
if [ ! -s "$DB_PATH" ]; then
    log "ERROR: $DB_PATH missing or empty — aborting"
    exit 2
fi

# Use SQLite's online backup API for a consistent snapshot (charon may be writing)
BACKUP_FILE="$LOCAL_STAGE/ipsec-$DATE_DAY.db"
sqlite3 "$DB_PATH" ".timeout 5000" ".backup '$BACKUP_FILE'"

# Verify the backup file is a valid SQLite DB
if ! sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check" | grep -q "^ok$"; then
    log "ERROR: backup file failed integrity check"
    exit 3
fi

BACKUP_SIZE=$(stat -c%s "$BACKUP_FILE")
log "Local backup OK: $BACKUP_FILE ($BACKUP_SIZE bytes)"

# Push daily + weekly to RustFS (overwrite same slot)
DAILY_REMOTE="$REMOTE/daily/ipsec-$DATE_DAY.db"
WEEKLY_REMOTE="$REMOTE/weekly/ipsec-$DATE_WEEK.db"

rclone copyto "$BACKUP_FILE" "$DAILY_REMOTE" 2>&1 | tee -a "$LOG" >/dev/null
log "Pushed daily → $DAILY_REMOTE"

rclone copyto "$BACKUP_FILE" "$WEEKLY_REMOTE" 2>&1 | tee -a "$LOG" >/dev/null
log "Pushed weekly → $WEEKLY_REMOTE"

# Prune: keep 14 daily + 8 weekly
DAILY_LIST=$(rclone lsf --format "tp" "$REMOTE/daily/" 2>/dev/null | awk '{print $2}' | sort -r)
DAILY_COUNT=$(echo "$DAILY_LIST" | grep -c '^ipsec-' || true)
if [ "$DAILY_COUNT" -gt 14 ]; then
    PRUNE=$(echo "$DAILY_LIST" | tail -n +15)
    for f in $PRUNE; do
        rclone deletefile "$REMOTE/daily/$f" 2>&1 | tee -a "$LOG" >/dev/null
        log "Pruned old daily: $f"
    done
fi

WEEKLY_LIST=$(rclone lsf --format "tp" "$REMOTE/weekly/" 2>/dev/null | awk '{print $2}' | sort -r)
WEEKLY_COUNT=$(echo "$WEEKLY_LIST" | grep -c '^ipsec-' || true)
if [ "$WEEKLY_COUNT" -gt 8 ]; then
    PRUNE=$(echo "$WEEKLY_LIST" | tail -n +9)
    for f in $PRUNE; do
        rclone deletefile "$REMOTE/weekly/$f" 2>&1 | tee -a "$LOG" >/dev/null
        log "Pruned old weekly: $f"
    done
fi

# Verify the latest push landed correctly
RUSTFS_SIZE=$(rclone ls "$DAILY_REMOTE" 2>/dev/null | awk '{print $1}' | head -1)
if [ "$RUSTFS_SIZE" != "$BACKUP_SIZE" ]; then
    log "WARNING: RustFS size ($RUSTFS_SIZE) != local size ($BACKUP_SIZE)"
    exit 4
fi

log "Verify OK: $BACKUP_SIZE bytes both sides"
log "=== strongswan-db-backup done ==="
