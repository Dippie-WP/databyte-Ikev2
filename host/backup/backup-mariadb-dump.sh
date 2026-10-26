#!/usr/bin/env bash
# backup-mariadb-dump.sh
# ----------------------------------------------------------------------------
# Backup prod VPN MariaDB `radius` schema to RustFS.
# Runs on OC. SSHes to vpn-prod-01 to run mariadb-dump, then pushes via rclone.
#
# Why OC and not prod: prod (Xneelo VPS) cannot route to the LAN rustfs endpoint
# at 192.168.10.89:30293. The WireGuard tunnel only carries 10.50.0.0/24
# (peer AllowedIPs = 10.50.0.2/32) — not 192.168.10.0/24. Direct curl from
# prod times out 10s, ping 100% loss. rclone from prod therefore hangs until
# the local timeout 90s kills it (rc=124), every day, since 2026-09-25.
#
# OC is on the LAN and can reach both prod (SSH over public internet via
# id_ed25519_xneelo) and rustfs (LAN rclone, same as backup-vpn-portal-config).
# This mirrors the proven pattern from backup-vpn-portal-config.sh.
#
# Schedule: backup-mariadb-dump.timer on OC, 04:30 UTC daily.
# Retention: 14 daily + 8 weekly (matches strongswan-db-backup pattern).
# Storage: rustfs:open-claw-push/mariadb-dump/{daily,weekly}/
#
# Log: /var/log/mariadb-dump-backup.log (also journald via systemd unit).
# ----------------------------------------------------------------------------

set -euo pipefail

# --dry-run flag (added 2026-10-18): all rclone calls become no-ops; SSH pulls
# and the dump still execute locally so you can verify the script end-to-end
# without committing data anywhere.
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    shift || true
fi

PROD_HOST="vpn-prod-01"
PROD_SSH_KEY="/root/.ssh/id_ed25519_xneelo"
DB_SCHEMA="radius"
REMOTE="rustfs:open-claw-push/mariadb-dump"
# Secondary destination: Pi+USB at 192.168.20.10, /mnt/kopia-usb, SFTP via
# the [pi-usb] rclone remote. Best-effort: pi-usb failures warn, do not fail
# the script (rustfs always has the data).
PI_USB_BASE="pi-usb:/mnt/kopia-usb/openclaw-backups"
PI_USB_DAILY_BASE="$PI_USB_BASE/mariadb-dump/daily"
PI_USB_WEEKLY_BASE="$PI_USB_BASE/mariadb-dump/weekly"
LOCAL_STAGE="/var/backups/mariadb-dump"
LOG="/var/log/mariadb-dump-backup.log"

DATE_DAY=$(date -u +%Y-%m-%d)
DATE_WEEK=$(date -u +%G-W%V)
TS=$(date -u +%FT%TZ)

mkdir -p "$LOCAL_STAGE"

log() { echo "[$TS] $*" | tee -a "$LOG"; }

log "=== mariadb-dump-backup started (OC-side relay) ==="

# SSH to prod and stream the dump. prod uses unix_socket auth for
# root@localhost, so root@OC's SSH session invokes mariadb-dump directly
# without a password. mariadb-dump binary at /usr/bin/mariadb-dump on prod
# (MariaDB 11.0+ removed the mysqldump symlink).
DUMP_FILE="$LOCAL_STAGE/radius-$DATE_DAY.sql.gz"

ssh -i "$PROD_SSH_KEY" "root@$PROD_HOST" \
    "mariadb-dump \
        --user=root \
        --socket=/var/run/mysqld/mysqld.sock \
        --single-transaction \
        --quick \
        --triggers \
        --routines \
        --events \
        --databases $DB_SCHEMA" \
    | gzip -9 > "$DUMP_FILE"

# Verify the dump is non-empty
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

# Push daily + weekly to rustfs (overwrite same slot, same as strongswan-db).
# OC's rclone config has [rustfs] with the correct endpoint, so no inline
# --s3-* flags needed here (unlike prod's broken script).
DAILY_REMOTE="$REMOTE/daily/radius-$DATE_DAY.sql.gz"
WEEKLY_REMOTE="$REMOTE/weekly/radius-$DATE_WEEK.sql.gz"

# rclone flag passthrough for --dry-run
RCLONE_DRY=""
[ "$DRY_RUN" = true ] && RCLONE_DRY="--dry-run"

rclone copyto $RCLONE_DRY "$DUMP_FILE" "$DAILY_REMOTE" 2>&1 | tee -a "$LOG" >/dev/null \
    && log "Pushed daily -> $DAILY_REMOTE" \
    || { RC=$?; log "WARNING: daily push failed (rc=$RC) - local dump still valid"; }

# Weekly: only on Sundays (matches strongswan-db + prod-side pattern)
DOW=$(date -u +%u)
if [ "$DOW" = "7" ]; then
    rclone copyto $RCLONE_DRY "$DUMP_FILE" "$WEEKLY_REMOTE" 2>&1 | tee -a "$LOG" >/dev/null \
        && log "Pushed weekly -> $WEEKLY_REMOTE (today IS Sunday)" \
        || { RC=$?; log "WARNING: weekly push failed (rc=$RC) - local dump still valid"; }
else
    log "Skipping weekly (only on Sundays, today is day-of-week=$DOW)"
fi

# Parallel push to pi-usb (best-effort: warn on failure, do not fail script)
PI_USB_DAILY="$PI_USB_DAILY_BASE/radius-$DATE_DAY.sql.gz"
PI_USB_WEEKLY="$PI_USB_WEEKLY_BASE/radius-$DATE_WEEK.sql.gz"
rclone copyto $RCLONE_DRY "$DUMP_FILE" "$PI_USB_DAILY" 2>&1 | tee -a "$LOG" >/dev/null \
    && log "Pushed pi-usb daily -> $PI_USB_DAILY" \
    || { RC=$?; log "WARNING: pi-usb daily push failed (rc=$RC) - rustfs still has it"; }
if [ "$DOW" = "7" ]; then
    rclone copyto $RCLONE_DRY "$DUMP_FILE" "$PI_USB_WEEKLY" 2>&1 | tee -a "$LOG" >/dev/null \
        && log "Pushed pi-usb weekly -> $PI_USB_WEEKLY (today IS Sunday)" \
        || { RC=$?; log "WARNING: pi-usb weekly push failed (rc=$RC) - rustfs still has it"; }
fi

# Verify size match (skipped in dry-run — would always warn because we
# didn't actually push the new local dump, so the remote is stale)
if [ "$DRY_RUN" = true ]; then
    log "DRY-RUN: skipping size verify (would compare local $BACKUP_SIZE to rustfs after push)"
else
    RUSTFS_SIZE=$(rclone ls "$DAILY_REMOTE" 2>/dev/null | awk '{print $1}' | head -1)
    if [ "$RUSTFS_SIZE" = "$BACKUP_SIZE" ] && [ -n "$RUSTFS_SIZE" ]; then
        log "Verify OK: $BACKUP_SIZE bytes both sides"
    elif [ -z "$RUSTFS_SIZE" ]; then
        log "WARNING: rustfs daily not yet visible - local dump is still valid"
    else
        log "WARNING: RustFS size ($RUSTFS_SIZE) != local size ($BACKUP_SIZE) - local dump is still valid"
    fi
fi

# Prune: keep 14 daily + 8 weekly (rustfs side)
DAILY_LIST=$(rclone lsf --format "tp" "$REMOTE/daily/" 2>/dev/null | awk '{print $2}' | sort -r)
DAILY_COUNT=$(echo "$DAILY_LIST" | grep -c '^radius-' || true)
if [ "$DAILY_COUNT" -gt 14 ]; then
    PRUNE=$(echo "$DAILY_LIST" | tail -n +15)
    for f in $PRUNE; do
        rclone deletefile $RCLONE_DRY "$REMOTE/daily/$f" 2>&1 | tee -a "$LOG" >/dev/null
        log "Pruned old daily: $f"
    done
fi

WEEKLY_LIST=$(rclone lsf --format "tp" "$REMOTE/weekly/" 2>/dev/null | awk '{print $2}' | sort -r)
WEEKLY_COUNT=$(echo "$WEEKLY_LIST" | grep -c '^radius-' || true)
if [ "$WEEKLY_COUNT" -gt 8 ]; then
    PRUNE=$(echo "$WEEKLY_LIST" | tail -n +9)
    for f in $PRUNE; do
        rclone deletefile $RCLONE_DRY "$REMOTE/weekly/$f" 2>&1 | tee -a "$LOG" >/dev/null
        log "Pruned old weekly: $f"
    done
fi

# Prune: keep 14 daily + 8 weekly (pi-usb side, best-effort)
PI_USB_DAILY_LIST=$(rclone lsf --format "tp" "$PI_USB_DAILY_BASE/" 2>/dev/null | awk '{print $2}' | sort -r)
PI_USB_DAILY_COUNT=$(echo "$PI_USB_DAILY_LIST" | grep -c '^radius-' || true)
if [ "$PI_USB_DAILY_COUNT" -gt 14 ]; then
    PRUNE=$(echo "$PI_USB_DAILY_LIST" | tail -n +15)
    for f in $PRUNE; do
        rclone deletefile $RCLONE_DRY "$PI_USB_DAILY_BASE/$f" 2>&1 | tee -a "$LOG" >/dev/null \
            && log "Pruned pi-usb daily: $f" \
            || log "WARNING: pi-usb daily prune failed for $f"
    done
fi

PI_USB_WEEKLY_LIST=$(rclone lsf --format "tp" "$PI_USB_WEEKLY_BASE/" 2>/dev/null | awk '{print $2}' | sort -r)
PI_USB_WEEKLY_COUNT=$(echo "$PI_USB_WEEKLY_LIST" | grep -c '^radius-' || true)
if [ "$PI_USB_WEEKLY_COUNT" -gt 8 ]; then
    PRUNE=$(echo "$PI_USB_WEEKLY_LIST" | tail -n +9)
    for f in $PRUNE; do
        rclone deletefile $RCLONE_DRY "$PI_USB_WEEKLY_BASE/$f" 2>&1 | tee -a "$LOG" >/dev/null \
            && log "Pruned pi-usb weekly: $f" \
            || log "WARNING: pi-usb weekly prune failed for $f"
    done
fi

log "=== mariadb-dump-backup done ==="
