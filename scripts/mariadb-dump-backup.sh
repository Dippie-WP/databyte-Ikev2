#!/usr/bin/env bash
# mariadb-dump-backup.sh — Backup prod VPN MariaDB `radius` schema to RustFS.
#
# Modeled on strongswan-db-backup.sh (proven pattern: flock + integrity_check +
# rclone copyto + 14 daily + 8 weekly retention + size verify + /var/log/).
#
# Schedule: /etc/cron.d/mariadb-dump-backup at 04:30 UTC daily (= 06:30 SAST).
#
# Why daily + ISO-week: schema is small (~3.3 MB) so storage cost is zero.
# Two-tier retention gives 14 days of granularity + multi-month history.
#
# Per-host setup:
#   1. rclone configured with `rustfs` remote → endpoint http://192.168.10.89:30293
#   2. mariadb-dump binary at /usr/bin/mariadb-dump (MariaDB 11.0+ removed mysqldump symlink)
#   3. MariaDB root@localhost uses unix_socket auth (PRIMARY) — cron runs as OS root, no -p flag needed
#   4. Drop the cron file at /etc/cron.d/mariadb-dump-backup
#
# Auth note: /root/.mariadb-root-pw holds a BACKUP password for non-root contexts
# (e.g. running from inside a container where unix_socket isn't available).
# This is UNRELATED to TKT-024 T7 sudo gap (which is for ansible control user `debian`).

set -uo pipefail  # set -e removed: rclone push is best-effort, never kills cron

DB_SCHEMA="radius"
SOCKET="/var/run/mysqld/mysqld.sock"
REMOTE="rustfs:open-claw-push/mariadb-dump"
LOCAL_STAGE="/var/backups/mariadb-dump"
LOCK="/var/run/mariadb-dump-backup.lock"
LOG="/var/log/mariadb-dump-backup.log"

DATE_DAY=$(date -u +%Y-%m-%d)
DATE_WEEK=$(date -u +%G-W%V)
TS=$(date -u +%FT%TZ)

mkdir -p "$LOCAL_STAGE"

log() { echo "[$TS] $*" | tee -a "$LOG"; }

log "=== mariadb-dump-backup started ==="

# Consistent InnoDB dump (radius tables are all InnoDB on MariaDB default).
# --quick avoids loading entire result set in memory for large tables.
# --triggers + --routines + --events preserves server-side objects.
DUMP_FILE="$LOCAL_STAGE/radius-$DATE_DAY.sql.gz"

mariadb-dump \
    --user=root \
    --socket="$SOCKET" \
    --single-transaction \
    --quick \
    --triggers \
    --routines \
    --events \
    --databases "$DB_SCHEMA" \
    | gzip -9 > "$DUMP_FILE"

# Verify the backup file is non-empty
if [ ! -s "$DUMP_FILE" ]; then
    log "ERROR: dump file $DUMP_FILE is empty — aborting"
    exit 4
fi

# Verify gzip integrity
if ! gzip -t "$DUMP_FILE" 2>/dev/null; then
    log "ERROR: dump file failed gzip integrity check"
    exit 5
fi

BACKUP_SIZE=$(stat -c%s "$DUMP_FILE")
log "Local backup OK: $DUMP_FILE ($BACKUP_SIZE bytes)"

# Push daily + weekly to RustFS (overwrite same slot, matches strongswan-db).
# CRITICAL: single-line rclone invocation. Multi-line continuations with blank
# lines between `--s3-provider Other \` and source path cause bash to interpret
# the source path as a command name (Permission denied on the dump file).
DAILY_REMOTE="$REMOTE/daily/radius-$DATE_DAY.sql.gz"
WEEKLY_REMOTE="$REMOTE/weekly/radius-$DATE_WEEK.sql.gz"
S3_FLAGS="--s3-force-path-style --s3-no-check-bucket --s3-region us-east-1 --s3-endpoint http://192.168.10.89:30293 --s3-provider Other"

set +e
timeout 90 rclone copyto $S3_FLAGS "$DUMP_FILE" "$DAILY_REMOTE" > /tmp/rclone-daily.log 2>&1
DAILY_RC=$?
timeout 90 rclone copyto $S3_FLAGS "$DUMP_FILE" "$WEEKLY_REMOTE" > /tmp/rclone-weekly.log 2>&1
WEEKLY_RC=$?
set -e

if [ $DAILY_RC -eq 0 ]; then
    log "Pushed daily -> $DAILY_REMOTE"
else
    log "WARNING: daily rclone copyto failed (rc=$DAILY_RC) - local dump still valid, cron continues. rclone stderr: $(tail -1 /tmp/rclone-daily.log 2>/dev/null | head -c 200)"
fi
if [ $WEEKLY_RC -eq 0 ]; then
    log "Pushed weekly -> $WEEKLY_REMOTE"
else
    log "WARNING: weekly rclone copyto failed (rc=$WEEKLY_RC) - local dump still valid, cron continues. rclone stderr: $(tail -1 /tmp/rclone-weekly.log 2>/dev/null | head -c 200)"
fi

# Prune: keep 14 daily + 8 weekly
S3_FLAGS="--s3-force-path-style --s3-no-check-bucket --s3-region us-east-1 --s3-endpoint http://192.168.10.89:30293 --s3-provider Other "

DAILY_LIST=$(timeout 30 rclone lsf $S3_FLAGS --format "tp" "$REMOTE/daily/" 2>/dev/null | awk '{print $2}' | sort -r)
DAILY_COUNT=$(echo "$DAILY_LIST" | grep -c '^radius-' || true)
if [ "$DAILY_COUNT" -gt 14 ]; then
    PRUNE=$(echo "$DAILY_LIST" | tail -n +15)
    for f in $PRUNE; do
        timeout 90 rclone deletefile $S3_FLAGS "$REMOTE/daily/$f" 2>&1 | tee -a "$LOG" >/dev/null
        log "Pruned old daily: $f"
    done
fi

WEEKLY_LIST=$(timeout 30 rclone lsf $S3_FLAGS --format "tp" "$REMOTE/weekly/" 2>/dev/null | awk '{print $2}' | sort -r)
WEEKLY_COUNT=$(echo "$WEEKLY_LIST" | grep -c '^radius-' || true)
if [ "$WEEKLY_COUNT" -gt 8 ]; then
    PRUNE=$(echo "$WEEKLY_LIST" | tail -n +9)
    for f in $PRUNE; do
        timeout 90 rclone deletefile $S3_FLAGS "$REMOTE/weekly/$f" 2>&1 | tee -a "$LOG" >/dev/null
        log "Pruned old weekly: $f"
    done
fi

# Verify the latest push landed correctly (size match, matches strongswan-db pattern).
# Single-line invocation (same blank-line-continuation bug as above).
RUSTFS_SIZE=$(timeout 30 rclone ls $S3_FLAGS "$DAILY_REMOTE" 2>/dev/null | awk '{print $1}' | head -1)
if [ "$RUSTFS_SIZE" = "$BACKUP_SIZE" ] && [ -n "$RUSTFS_SIZE" ]; then
    log "Verify OK: $BACKUP_SIZE bytes both sides"
elif [ -z "$RUSTFS_SIZE" ]; then
    log "WARNING: rustfs file not yet visible (rclone may still be uploading) - local dump is still valid"
else
    log "WARNING: RustFS size ($RUSTFS_SIZE) != local size ($BACKUP_SIZE) - local dump is still valid"
fi
log "=== mariadb-dump-backup done ==="
