#!/usr/bin/env bash
# seed_real_tiers.sh — Seed the 3 production tiers for 5D pre-commercial lineup.
#
#   tier_5gb   | 5 GB  |   5 * 1024^3 =   5,368,709,120 bytes | $3 USD  | Tier 1
#   tier_10gb  | 10 GB |  10 * 1024^3 =  10,737,418,240 bytes | $5 USD  | Tier 2
#   tier_20gb  | 20 GB |  20 * 1024^3 =  21,474,836,480 bytes | $8 USD  | Tier 3
#
# Idempotent. Uses INSERT OR IGNORE on the UNIQUE name constraint.
# Run on the LXC HOST (not inside the container).
#
# Usage:
#   bash quota/seed_real_tiers.sh
#
# Notes:
# - price_zar column kept for legacy; new USD price is in price_cents or
#   notes field. Pricing happens in 5D (commercial) — tier display only.
# - Old tier_3gb / tier_15gb rows are NOT deleted by this script. To
#   archive them, run the explicit migration step (5D-migrate-tiers.sh).

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
TS=$(date -u +%s)

if [ ! -s "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH missing or empty" >&2
    exit 1
fi

GB5=$((5 * 1024 * 1024 * 1024))     # 5368709120
GB10=$((10 * 1024 * 1024 * 1024))   # 10737418240
GB20=$((20 * 1024 * 1024 * 1024))   # 21474836480

# USD cents (Stripe-friendly: integer cents)
USD3=$((3 * 100))   # 300
USD5=$((5 * 100))   # 500
USD8=$((8 * 100))   # 800

echo "=== Seed 5D pre-commercial tiers ==="
echo "  DB:     $DB_PATH"
echo "  Tiers:  tier_5gb ($GB5 bytes, $USD3 cents)  → Tier 1"
echo "          tier_10gb ($GB10 bytes, $USD5 cents) → Tier 2"
echo "          tier_20gb ($GB20 bytes, $USD8 cents) → Tier 3"

sqlite3 "$DB_PATH" <<SQL
INSERT OR IGNORE INTO tiers (name, display_name, data_limit_bytes, price_zar, is_active, created_at, notes)
VALUES
  ('tier_5gb',  '5 GB',  $GB5,  NULL, 1, $TS, 'Tier 1 — 5 GB for \$3 USD (5D pre-commercial, 2026-06-22)'),
  ('tier_10gb', '10 GB', $GB10, NULL, 1, $TS, 'Tier 2 — 10 GB for \$5 USD (5D pre-commercial, 2026-06-22)'),
  ('tier_20gb', '20 GB', $GB20, NULL, 1, $TS, 'Tier 3 — 20 GB for \$8 USD (5D pre-commercial, 2026-06-22)');

SELECT json_object(
    'tiers_added', group_concat(name, ','),
    'ts', $TS
) FROM tiers WHERE name IN ('tier_5gb', 'tier_10gb', 'tier_20gb') AND created_at = $TS;
SQL

echo "=== Done. Verify with: ==="
echo "  sqlite3 $DB_PATH 'SELECT name, display_name, data_limit_bytes, notes FROM tiers WHERE is_active=1;'"
