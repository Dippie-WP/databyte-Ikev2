#!/usr/bin/env bash
# seed_real_tiers.sh — Seed the 3 production tiers (3GB, 10GB, 15GB).
#
# Idempotent. Use INSERT OR IGNORE on the UNIQUE name constraint.
# Run on the LXC HOST (not inside the container).
#
# Usage:
#   bash quota/seed_real_tiers.sh
#
# Adds 3 rows to `tiers`:
#   tier_3gb  | 3 GB  |  3 * 1024^3 =  3,221,225,472 bytes
#   tier_10gb | 10 GB | 10 * 1024^3 = 10,737,418,240 bytes
#   tier_15gb | 15 GB | 15 * 1024^3 = 16,106,127,360 bytes
#
# price_zar is left NULL — pricing happens in 5D (commercial), out of scope for 5B.

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
TS=$(date -u +%s)

if [ ! -s "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH missing or empty" >&2
    exit 1
fi

# Compute byte values (math in shell, then pass to sqlite)
GB3=$((3 * 1024 * 1024 * 1024))    # 3221225472
GB10=$((10 * 1024 * 1024 * 1024))  # 10737418240
GB15=$((15 * 1024 * 1024 * 1024))  # 16106127360

echo "=== Seed real tiers ==="
echo "  DB:     $DB_PATH"
echo "  Tiers:  tier_3gb ($GB3), tier_10gb ($GB10), tier_15gb ($GB15)"

sqlite3 "$DB_PATH" <<SQL
INSERT OR IGNORE INTO tiers (name, display_name, data_limit_bytes, price_zar, is_active, created_at, notes)
VALUES
  ('tier_3gb',  '3 GB',  $GB3,  NULL, 1, $TS, 'Standard tier — 3 GB'),
  ('tier_10gb', '10 GB', $GB10, NULL, 1, $TS, 'Standard tier — 10 GB'),
  ('tier_15gb', '15 GB', $GB15, NULL, 1, $TS, 'Standard tier — 15 GB');

INSERT INTO audit_log (actor, action, target_type, payload, created_at)
VALUES ('system', 'seed_real_tiers', 'system',
        json_object('tiers_added', 'tier_3gb,tier_10gb,tier_15gb', 'ts', $TS),
        $TS);

SELECT '--- tiers after seed ---';
SELECT id, name, display_name, data_limit_bytes, is_active FROM tiers ORDER BY id;
SQL

echo "  Status: OK"
