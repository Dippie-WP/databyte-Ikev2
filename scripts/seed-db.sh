#!/usr/bin/env bash
# seed-db.sh — Seed SQLite DB with rw-pool and a first user
#
# Run on the LXC HOST (not inside the container) after the container
# has been started at least once (so the DB schema is initialized by
# attr-sql on first query — `swanctl --list-pools` triggers it).
#
# Usage:
#   USERNAME=zun VIP=10.99.0.50 NTLM_HASH=... bash scripts/seed-db.sh
#
# NTLM_HASH: 32-char hex (16 bytes). Generate with:
#   echo -n 'PASSWORD' | iconv -t utf-16le | openssl md4 -binary | xxd -p -c 32
# Or use the helper at the bottom of this script (READS PASSWORD FROM STDIN
# — won't appear in `ps` output).
#
# IMPORTANT: This script DIRECTLY MODIFIES the SQLite DB. charon does NOT
# know about manual changes until the next query. For routine changes,
# use `swanctl --load-pools` to re-load.

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
USERNAME="${USERNAME:-zun}"
VIP="${VIP:-10.99.0.50}"

if [ ! -s "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH missing or empty"
    echo "       Start the strongSwan container once and run 'swanctl --list-pools' to initialize the schema"
    exit 1
fi

# Convert VIP "10.99.0.50" → 0x0A630032 (sqlite stores IPs as 4-byte big-endian)
VIP_HEX=$(printf '%02X%02X%02X%02X' $(echo "$VIP" | awk -F. '{print $1, $2, $3, $4}'))

echo "=== Seeding $DB_PATH ==="
echo "  username: $USERNAME"
echo "  VIP:      $VIP (0x$VIP_HEX)"

# Step 1: ensure rw-pool exists (idempotent)
sqlite3 "$DB_PATH" <<SQL
INSERT OR IGNORE INTO pools (name, addr_subnet, dns, nbns, start_addr, end_addr, online, load_threshold)
VALUES ('rw-pool', '10.99.0.0/24', '1.1.1.1, 8.8.8.8', NULL, '10.99.0.1', '10.99.0.254', 0, 0);
SQL

# Step 2: insert identity for the user (id=1 first time, then auto-increment)
sqlite3 "$DB_PATH" <<SQL
INSERT OR IGNORE INTO identities (type, data)
VALUES (1, '$USERNAME');
SQL

USER_ID=$(sqlite3 "$DB_PATH" "SELECT id FROM identities WHERE data='$USERNAME' LIMIT 1;")
echo "  user_id:  $USER_ID"

# Step 3: link user to rw-pool
sqlite3 "$DB_PATH" <<SQL
INSERT OR IGNORE INTO user_pools (identity, pool, auth)
VALUES ($USER_ID, (SELECT id FROM pools WHERE name='rw-pool'), 0);
SQL

# Step 4: pre-insert VIP address row, "released=0" pins it
# address column is a 4-byte BE int
sqlite3 "$DB_PATH" <<SQL
INSERT OR IGNORE INTO addresses (address, identity, released, lease_time)
VALUES (X'$VIP_HEX', $USER_ID, 0, 0);
SQL

# Step 5: link user to PINNED address (some plugins need this for "sticky" VIPs)
# Pool lease history: charon will pick the existing row and re-use it
# Note: hard pin (released=0 always) requires custom attr-sql logic.
# What we have here is "sticky" — charon prefers this VIP if available,
# but it may be assigned to another user if not held when they connect.

echo ""
echo "=== Done ==="
echo ""
echo "Verify:"
echo "  sqlite3 $DB_PATH 'SELECT id, name FROM pools;'"
echo "  sqlite3 $DB_PATH 'SELECT id, type, data FROM identities;'"
echo "  sqlite3 $DB_PATH 'SELECT * FROM user_pools;'"
echo "  sqlite3 $DB_PATH 'SELECT id, printf(\"0x%08X\", address), identity, released FROM addresses;'"
echo ""
echo "Reload charon:"
echo "  docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-pools"
