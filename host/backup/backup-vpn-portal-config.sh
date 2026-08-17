#!/usr/bin/env bash
# backup-vpn-portal-config.sh
# ----------------------------------------------------------------------------
# Backup VPN portal secrets + DB + strongSwan swanctl configs to RustFS.
# Files backed up:
#   1. /etc/vpn-portal.env (Argon2id hashes, DB path, cookie flag)
#   2. /etc/ssl/cloudflare/databyte.co.za.{crt,key} (Origin CA cert + key)
#   3. /var/lib/strongswan/ipsec.db (live DB — sqlite3 .backup for consistency)
#   4. /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf
#      (ADDED 2026-08-11 per Zun msg #34088 — today's outage cause: file
#       was truncated to 2875B between 07:33 and 07:35 UTC, with no
#       off-host backup. This step closes the gap.)
#   5. /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-psk.conf
#      (defensive — same directory, same write hazard)
#
# Target:   rustfs:open-claw-push/vpn-portal-config/<YYYY-MM-DDTHHMM>/ (steps 1-4)
#           rustfs:open-claw-push/strongswan-configs/daily/strongswan-YYYY-MM-DD.tar.gz (step 5)
#           rustfs:open-claw-push/strongswan-configs/weekly/strongswan-YYYY-WNN.tar.gz (step 5, Sundays only)
# Snapshots use a per-fire timestamp so 2 fires per day each get their own
# deduped subdir (e.g. 2026-07-30T0400 vs 2026-07-30T1000).
#
# Retention: ALWAYS KEEP LAST 2 (per Zun msg #29483, 2026-07-29 10:11 UTC).
# The [6/6] prune step deletes older dated subdirs after every successful push,
# so the destination at any time contains exactly KEEP_N=2 snapshots
# (most recent by lex-sort = chronological). Old YYYY-MM-DD/ subdirs from the
# previous schedule also match the prune regex, so they get rolled forward too.
#
# Schedule: 2x daily at 06:00 and 12:00 SAST (= 04:00 and 10:00 UTC) via
#   backup-vpn-portal-config.timer.
#
# NEW FLAG (2026-08-11): --dry-run
#   Performs SSH pulls + verify the files look correct, but skips the
#   rclone push and prune. Use this to test the script safely.
# ----------------------------------------------------------------------------

# Detect --dry-run BEFORE set -euo pipefail so the flag is in env
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    shift || true
fi

set -euo pipefail

# Persistent log fan-out: tee writes to a dated log file under
# /var/log/vpn-portal-config-backup/, while a process substitution pipes each
# line into `logger -t backup-vpn-portal-config` so journald (via
# SyslogIdentifier=backup-vpn-portal-config in the .service unit) also gets
# every line. Mirrors the workspace backup's log-file pattern while preserving
# journald observability. Zun directive msg #29479, shipped 2026-07-29 10:10 UTC.
LOG_DIR="/var/log/vpn-portal-config-backup"
LOG_FILE="$LOG_DIR/backup-$(date -u +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE" >(logger -t backup-vpn-portal-config)) 2>&1

VPS_HOST="vpn-prod-01"
# Default-value expansion: uses $VPS_SSH_KEY from systemd Environment if set,
# else falls back to the canonical key path. (Masked in display as `…`,
# rendered here as `/root/.ssh/id_ed25519_xneelo`.)
VPS_SSH_KEY="${VPS_SSH_KEY:-/root/.ssh/id_ed25519_xneelo}"
DEST_BASE="rustfs:open-claw-push/vpn-portal-config"
STRONGSWAN_DEST_BASE="rustfs:open-claw-push/strongswan-configs"
# YYYY-MM-DDTHHMM (single-level name) — gives each fire a unique subdir.
DEST="${DEST_BASE}/$(date -u +%Y-%m-%dT%H%M)"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
KEEP_N=2

MODE="LIVE"
[ "$DRY_RUN" = true ] && MODE="DRY-RUN"

echo "=== VPN portal config backup (mode: $MODE) ==="
echo "VPS: $VPS_HOST"
echo "Dest: $DEST"
echo "Tmp: $TMPDIR"
echo "Keep last: $KEEP_N snapshots"
echo ""

# 1. Pull secrets + certs (over SSH)
echo "[1/6] Pulling /etc/vpn-portal.env + Cloudflare cert/key..."
ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo cat /etc/vpn-portal.env
' > "$TMPDIR/vpn-portal.env"
chmod 600 "$TMPDIR/vpn-portal.env"

ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo cat /etc/ssl/cloudflare/databyte.co.za.crt
' > "$TMPDIR/databyte.co.za.crt"
chmod 644 "$TMPDIR/databyte.co.za.crt"

ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo cat /etc/ssl/cloudflare/databyte.co.za.key
' > "$TMPDIR/databyte.co.za.key"
chmod 600 "$TMPDIR/databyte.co.za.key"

echo "  OK ($(wc -c < "$TMPDIR/vpn-portal.env") bytes env, $(wc -c < "$TMPDIR/databyte.co.za.crt") bytes cert)"

# 2. Pull live DB via sqlite3 .backup (atomic snapshot)
echo "[2/6] Snapshotting /var/lib/strongswan/ipsec.db..."
ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo sqlite3 /var/lib/strongswan/ipsec.db ".backup /tmp/ipsec-backup.db"
    sudo cat /tmp/ipsec-backup.db
    sudo rm -f /tmp/ipsec-backup.db
' > "$TMPDIR/ipsec.db"
chmod 600 "$TMPDIR/ipsec.db"

echo "  OK ($(wc -c < "$TMPDIR/ipsec.db") bytes DB)"

# 3. Pull strongSwan swanctl configs (ADDED 2026-08-11 per msg #34088)
# This is the fix for today's outage: rw-eap.conf was truncated by 6 bytes
# (lost closing `}` for eap-rachid-iphone + `}` for secrets {} block) and
# there was no off-host backup. Pull both rw-eap.conf (primary) and
# rw-psk.conf (defensive) — same write hazard, same directory.
echo "[3/6] Pulling strongSwan swanctl configs..."
ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo cat /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf
' > "$TMPDIR/rw-eap.conf"
chmod 600 "$TMPDIR/rw-eap.conf"

ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo cat /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-psk.conf
' > "$TMPDIR/rw-psk.conf"
chmod 600 "$TMPDIR/rw-psk.conf"

# MD5 verify — catches mid-write corruption (the bug that caused today's outage).
# If the file is being written while we read it, the pulled md5 will differ
# from the live md5 we just computed separately.
RW_EAP_PULLED_MD5=$(md5sum "$TMPDIR/rw-eap.conf" | awk '{print $1}')
RW_EAP_LIVE_MD5=$(ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" 'sudo md5sum /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf' | awk '{print $1}')

if [ "$RW_EAP_PULLED_MD5" != "$RW_EAP_LIVE_MD5" ]; then
    echo "  ⚠️  WARNING: rw-eap.conf md5 MISMATCH — pulled $RW_EAP_PULLED_MD5 vs live $RW_EAP_LIVE_MD5 — file may be mid-write"
    echo "  ACTION: investigate before next live run. Re-run --dry-run later to see if it stabilizes."
else
    echo "  OK (live md5 $RW_EAP_LIVE_MD5 = pulled md5 $RW_EAP_PULLED_MD5, $(wc -c < "$TMPDIR/rw-eap.conf") bytes rw-eap.conf, $(wc -c < "$TMPDIR/rw-psk.conf") bytes rw-psk.conf)"
fi

# 4. Push to RustFS (vpn-portal-config)
echo ""
echo "[4/6] Pushing to $DEST..."
if [ "$DRY_RUN" = true ]; then
    echo "  SKIPPED (--dry-run mode)"
    echo ""
    echo "=== DRY-RUN: files that WOULD be pushed ==="
    ls -la "$TMPDIR/"
    echo ""
    echo "=== DRY-RUN: rw-eap.conf last 5 lines (verify closing braces present) ==="
    tail -5 "$TMPDIR/rw-eap.conf"
    echo ""
    echo "=== DRY-RUN: rw-eap.conf md5 (post-pull) ==="
    md5sum "$TMPDIR/rw-eap.conf"
    echo ""
else
    rclone copy "$TMPDIR/" "$DEST/" --s3-no-check-bucket --quiet
    echo "  OK"
fi

# 5. Push strongswan-specific files to strongswan-configs destination
# Restores the daily+weekly backup that was silent since 2026-06-19
# (TKT-001). Uses rclone copyto so each day/week overwrites the prior
# archive (one file per day in daily/, one per week in weekly/).
# Files in tarball: ipsec.db, rw-eap.conf, rw-psk.conf (the strongswan-specific
# ones from steps 2 + 3, captured locally in TMPDIR).
echo ""
echo "[5/6] Pushing strongswan files to strongswan-configs (daily+weekly)..."

STRONGSWAN_DATE=$(date -u +%Y-%m-%d)
STRONGSWAN_WEEK=$(date -u +%Y-W%V)
STRONGSWAN_TARBALL="/tmp/strongswan-${STRONGSWAN_DATE}.tar.gz"

# Create tar.gz from TMPDIR strongswan files
tar -czf "$STRONGSWAN_TARBALL" -C "$TMPDIR" ipsec.db rw-eap.conf rw-psk.conf
chmod 600 "$STRONGSWAN_TARBALL"
echo "  OK ($(wc -c < "$STRONGSWAN_TARBALL") bytes tarball for date=$STRONGSWAN_DATE week=$STRONGSWAN_WEEK)"

STRONGSWAN_DAILY="${STRONGSWAN_DEST_BASE}/daily/strongswan-${STRONGSWAN_DATE}.tar.gz"
STRONGSWAN_WEEKLY="${STRONGSWAN_DEST_BASE}/weekly/strongswan-${STRONGSWAN_WEEK}.tar.gz"

if [ "$DRY_RUN" = true ]; then
    echo "  DRY-RUN: would push to rustfs:"
    echo "    daily:  $STRONGSWAN_DAILY"
    DOW=$(date -u +%u)
    if [ "$DOW" = "7" ]; then
        echo "    weekly: $STRONGSWAN_WEEKLY (today IS Sunday $DOW)"
    else
        echo "    weekly: $STRONGSWAN_WEEKLY (SKIPPED — only on Sundays, today=$DOW)"
    fi
else
    rclone copyto "$STRONGSWAN_TARBALL" "$STRONGSWAN_DAILY" --quiet
    echo "  OK pushed to daily archive"
    DOW=$(date -u +%u)
    if [ "$DOW" = "7" ]; then
        rclone copyto "$STRONGSWAN_TARBALL" "$STRONGSWAN_WEEKLY" --quiet
        echo "  OK pushed to weekly archive (today IS Sunday)"
    else
        echo "  Skipping weekly (only on Sundays, today is day-of-week=$DOW)"
    fi
fi

# 6. Verify + prune to last N snapshots
echo ""
echo "[6/6] Verifying + pruning (keep last $KEEP_N)..."
if [ "$DRY_RUN" = true ]; then
    echo "  SKIPPED (--dry-run mode)"
    echo ""
    # Show what would be pruned
    LSF_OUTPUT=$(rclone lsf "$DEST_BASE/" --dirs-only --max-depth 1 2>/dev/null || echo "")
    mapfile -t SNAPS < <(echo "$LSF_OUTPUT" \
        | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4})?/$" \
        || true \
        | sort)
    COUNT=${#SNAPS[@]}
    if (( COUNT > KEEP_N )); then
        DEL_COUNT=$((COUNT - KEEP_N))
        echo "  DRY-RUN: would prune $DEL_COUNT older (keeping last $KEEP_N)"
        for (( i=0; i<DEL_COUNT; i++ )); do
            d="${SNAPS[$i]%/}"
            echo "    would purge: $d"
        done
    elif (( COUNT > 0 )); then
        echo "  DRY-RUN: found $COUNT snapshots, <= $KEEP_N — no prune needed"
    else
        echo "  DRY-RUN: no dated snapshots found (unexpected)"
    fi
else
    rclone ls "$DEST/" 2>&1 | head -10
    echo ""

    # Prune older snapshots — keep only the most recent $KEEP_N dated subdirs.
    # Matches both old (YYYY-MM-DD/) and new (YYYY-MM-DDTHHMM/) formats so the
    # last-N rule applies across the schema transition.
    LSF_OUTPUT=$(rclone lsf "$DEST_BASE/" --dirs-only --max-depth 1 2>/dev/null || echo "")
    mapfile -t SNAPS < <(echo "$LSF_OUTPUT" \
        | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4})?/$" \
        || true \
        | sort)
    COUNT=${#SNAPS[@]}
    if (( COUNT > KEEP_N )); then
        DEL_COUNT=$((COUNT - KEEP_N))
        echo "  found $COUNT snapshots; pruning $DEL_COUNT older (keeping last $KEEP_N)"
        for (( i=0; i<DEL_COUNT; i++ )); do
            d="${SNAPS[$i]%/}"
            echo "    purge: $d"
            rclone purge "${DEST_BASE}/${d}" --quiet >/dev/null 2>&1 || true
        done
    elif (( COUNT > 0 )); then
        echo "  found $COUNT snapshots, <= $KEEP_N — no prune needed"
    else
        echo "  no dated snapshots found (unexpected)"
    fi
fi
echo ""
echo "=== Done ($MODE) ==="
