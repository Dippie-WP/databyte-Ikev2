#!/usr/bin/env bash
# backup-vpn-portal-config.sh
# ----------------------------------------------------------------------------
# Backup VPN portal secrets + DB to RustFS (S3-compatible) on the LAN.
# Files backed up:
#   1. /etc/vpn-portal.env (Argon2id hashes, DB path, cookie flag)
#   2. /etc/ssl/cloudflare/databyte.co.za.{crt,key} (Origin CA cert + key)
#   3. /var/lib/strongswan/ipsec.db (live DB — needs sqlite3 .backup for consistency)
#
# Target: rustfs:open-claw-push/vpn-portal-config/<YYYY-MM-DD>/
#
# Designed to run as a cron job from the OpenClaw host (where rclone is
# configured). Reads VPS files via `ssh vpn-prod-01`. S3 push happens locally.
#
# Run:    bash backup-vpn-portal-config.sh
# Cron:   03:30 SAST daily
# ----------------------------------------------------------------------------

set -euo pipefail

VPS_HOST="vpn-prod-01"
VPS_SSH_KEY="${VPS_SSH_KEY:-/root/.ssh/id_ed25519_xneelo}"
DEST_BASE="rustfs:open-claw-push/vpn-portal-config"
DEST="${DEST_BASE}/$(date -u +%Y-%m-%d)"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "=== VPN portal config backup ==="
echo "VPS: $VPS_HOST"
echo "Dest: $DEST"
echo "Tmp: $TMPDIR"
echo ""

# 1. Pull secrets + certs (over SSH)
echo "[1/3] Pulling /etc/vpn-portal.env + Cloudflare cert/key..."
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
echo "[2/3] Snapshotting /var/lib/strongswan/ipsec.db..."
ssh -i "$VPS_SSH_KEY" "root@$VPS_HOST" '
    sudo sqlite3 /var/lib/strongswan/ipsec.db ".backup /tmp/ipsec-backup.db"
    sudo cat /tmp/ipsec-backup.db
    sudo rm -f /tmp/ipsec-backup.db
' > "$TMPDIR/ipsec.db"
chmod 600 "$TMPDIR/ipsec.db"

echo "  OK ($(wc -c < "$TMPDIR/ipsec.db") bytes DB)"

# 3. Push to RustFS
echo "[3/3] Pushing to $DEST..."
rclone copy "$TMPDIR/" "$DEST/" --s3-no-check-bucket --quiet
echo "  OK"

# 4. Verify
echo ""
echo "=== Verify ==="
rclone ls "$DEST/" 2>&1 | head -10
echo ""
echo "=== Done ==="
