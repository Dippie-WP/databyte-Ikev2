#!/usr/bin/env bash
# backup-vpn-portal-config.sh
# ----------------------------------------------------------------------------
# Backup VPN portal secrets + DB to RustFS (S3-compatible) on the LAN.
# Files backed up:
#   1. /etc/vpn-portal.env (Argon2id hashes, DB path, cookie flag)
#   2. /etc/ssl/cloudflare/databyte.co.za.{crt,key} (Origin CA cert + key)
#   3. /var/lib/strongswan/ipsec.db (live DB — sqlite3 .backup for consistency)
#
# Target:   rustfs:open-claw-push/vpn-portal-config/<YYYY-MM-DDTHHMM>/
# Snapshots now use a per-fire timestamp so 2 fires per day each get their own
# deduped subdir (e.g. 2026-07-30T0400 vs 2026-07-30T1000).
#
# Retention: ALWAYS KEEP LAST 2 (per Zun msg #29483, 2026-07-29 10:11 UTC).
# The [4/4] prune step deletes older dated subdirs after every successful push,
# so the destination at any time contains exactly KEEP_N=2 snapshots
# (most recent by lex-sort = chronological). Old YYYY-MM-DD/ subdirs from the
# previous schedule also match the prune regex, so they get rolled forward too.
#
# Schedule: 2x daily at 06:00 and 12:00 SAST (= 04:00 and 10:00 UTC) via
#   backup-vpn-portal-config.timer. See the [4/4] log step for prune output.
# ----------------------------------------------------------------------------

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
# YYYY-MM-DDTHHMM (single-level name) — gives each fire a unique subdir.
DEST="${DEST_BASE}/$(date -u +%Y-%m-%dT%H%M)"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
KEEP_N=2

echo "=== VPN portal config backup ==="
echo "VPS: $VPS_HOST"
echo "Dest: $DEST"
echo "Tmp: $TMPDIR"
echo "Keep last: $KEEP_N snapshots"
echo ""

# 1. Pull secrets + certs (over SSH)
echo "[1/4] Pulling /etc/vpn-portal.env + Cloudflare cert/key..."
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
echo "[2/4] Snapshotting /var/lib/strongswan/ipsec.db..."
ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo sqlite3 /var/lib/strongswan/ipsec.db ".backup /tmp/ipsec-backup.db"
    sudo cat /tmp/ipsec-backup.db
    sudo rm -f /tmp/ipsec-backup.db
' > "$TMPDIR/ipsec.db"
chmod 600 "$TMPDIR/ipsec.db"

echo "  OK ($(wc -c < "$TMPDIR/ipsec.db") bytes DB)"

# 3. Push to RustFS
echo "[3/4] Pushing to $DEST..."
rclone copy "$TMPDIR/" "$DEST/" --s3-no-check-bucket --quiet
echo "  OK"

# 4. Verify + prune to last N snapshots
echo ""
echo "[4/4] Verifying + pruning (keep last $KEEP_N)..."
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
echo ""
echo "=== Done ==="
