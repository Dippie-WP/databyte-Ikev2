#!/usr/bin/env bash
# migrate_v128_device_metadata.sh
# ----------------------------------------------------------------------------
# Phase 5D+ — add device metadata columns to existing devices tables.
# Adds: device_type, os_version, hostname
# Idempotent: safe to re-run (errors are caught).
# Apply to BOTH LXC 903 (lab) and vpn-prod-01 (VPS) when bringing up new env.
# ----------------------------------------------------------------------------
set -euo pipefail

DB="${DB_PATH:-/var/lib/strongswan/ipsec.db}"

echo "=== migrate_v128_device_metadata ==="
echo "DB: $DB"
echo ""

# Check if columns already exist; skip if so
COLS=$(sqlite3 "$DB" "PRAGMA table_info(devices);" 2>/dev/null | awk -F'|' '{print $2}')

add_col() {
  local col="$1"
  if echo "$COLS" | grep -q "^${col}$"; then
    echo "  [skip] $col already exists"
  else
    echo "  [add]  $col"
    sqlite3 "$DB" "ALTER TABLE devices ADD COLUMN $col TEXT DEFAULT NULL;" 2>&1
  fi
}

add_col device_type
add_col os_version
add_col hostname

echo ""
echo "=== Final devices schema ==="
sqlite3 "$DB" ".schema devices"
echo ""
echo "=== Done ==="
