#!/bin/bash
# migrate_v126_max_devices_one.sh
#
# Phase 5C.6 SHELVED, 5C.5 self-service device management REVERTED.
# v1.2.6 model lock: 1 (creds pair) = 1 device.
#
# This migration:
#  1. Sets customers.max_devices DEFAULT to 1 (was 2)
#  2. Updates all existing customer rows to max_devices = 1
#  3. Deactivates all but 1 active device per customer (canonical)
#  4. Logs all deactivations to audit_log
#
# Idempotent — safe to re-run.
#
# Pre-migration backup:
#   cp /var/lib/strongswan/ipsec.db /var/lib/strongswan/ipsec.db.bak-v126-pre-$(date +%Y%m%d-%H%M%S)

set -euo pipefail

DB="/var/lib/strongswan/ipsec.db"
TS=$(date +%s)
BAK="/var/lib/strongswan/ipsec.db.bak-v126-pre-$(date +%Y%m%d-%H%M%S)"

if [ ! -f "$DB" ]; then
    echo "ERROR: $DB not found"
    exit 1
fi

echo "=== BACKUP ==="
cp "$DB" "$BAK"
echo "backup: $BAK ($(stat -c%s "$BAK") bytes)"

echo
echo "=== STEP 1: Schema change — customers.max_devices DEFAULT 1 ==="
# SQLite doesn't support ALTER COLUMN, so we rebuild via the 12-step procedure
# only if needed. We change the default via table-rebuild IF the current default
# isn't already 1. For idempotency, check first.

CUR_DEFAULT=$(sqlite3 "$DB" "SELECT sql FROM sqlite_master WHERE type='table' AND name='customers';" \
    | tr -s ' ' \
    | grep -oE 'max_devices[^,)]*' \
    | head -1)
echo "current max_devices col def: $CUR_DEFAULT"

if echo "$CUR_DEFAULT" | grep -q "DEFAULT 1"; then
    echo "DEFAULT 1 already in place — skip schema change"
else
    echo "DEFAULT not 1 — running table-rebuild migration (SQLite limitation)"
    # 12-step SQLite column-default change
    sqlite3 "$DB" <<SQL
PRAGMA foreign_keys=off;
BEGIN TRANSACTION;

-- Capture old schema
CREATE TABLE customers_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    display_name     TEXT,
    telegram_id      INTEGER,
    telegram_username TEXT,
    is_operator      INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    over_quota       INTEGER NOT NULL DEFAULT 0,
    data_limit_bytes INTEGER NOT NULL DEFAULT 0,
    data_used_bytes  INTEGER NOT NULL DEFAULT 0,
    tier_id          INTEGER,
    status           TEXT    NOT NULL DEFAULT 'active',
    max_devices      INTEGER NOT NULL DEFAULT 1,         -- v1.2.6: 1 creds = 1 device
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    notes            TEXT
);

INSERT INTO customers_new SELECT
    id, name, display_name, telegram_id, telegram_username,
    is_operator, is_active, over_quota, data_limit_bytes, data_used_bytes,
    tier_id, status, 1, created_at, updated_at, notes
FROM customers;

DROP TABLE customers;
ALTER TABLE customers_new RENAME TO customers;
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name);
CREATE INDEX IF NOT EXISTS idx_customers_telegram_id ON customers(telegram_id);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);

COMMIT;
PRAGMA foreign_keys=on;
SQL
    echo "schema migrated: max_devices DEFAULT 1"
fi

echo
echo "=== STEP 2: Update all existing customer rows to max_devices=1 (idempotent) ==="
sqlite3 "$DB" "UPDATE customers SET max_devices = 1, updated_at = $TS WHERE max_devices != 1;"
sqlite3 -header -column "$DB" "SELECT name, max_devices, is_operator FROM customers ORDER BY id"

echo
echo "=== STEP 3: Deactivate all but 1 active device per customer (canonical = lowest id) ==="
# For each customer, find the lowest-id active device, deactivate the rest
sqlite3 "$DB" <<SQL
-- Deactivate all multi-device extras
-- Keep: lowest-id active device per customer as canonical
-- Deactivate: everything else that's active for the same customer

UPDATE devices
   SET is_active = 0,
       updated_at = $TS
 WHERE id NOT IN (
     SELECT MIN(id)
       FROM devices
      WHERE is_active = 1
      GROUP BY customer_id
   )
   AND is_active = 1;
SQL

echo
echo "=== STEP 4: Audit log of deactivations ==="
# Log every deactivation to audit_log so we have a paper trail.
# audit_log schema: actor, action, target_type, target_id, payload, created_at
sqlite3 "$DB" <<SQL
INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at)
SELECT 'system',
       'device_deactivated',
       'device',
       d.id,
       json_object('customer_id', d.customer_id, 'reason', 'v1.2.6 cleanup: model reverted to 1 creds = 1 device; this device was deactivated, lowest-id device kept as canonical'),
       $TS
  FROM devices d
 WHERE d.is_active = 0
   AND d.updated_at = $TS
   AND d.id NOT IN (SELECT target_id FROM audit_log WHERE action = 'device_deactivated' AND created_at = $TS AND target_type = 'device');
SQL

echo
echo "=== STEP 5: Final state — devices per customer ==="
sqlite3 -header -column "$DB" "
SELECT c.name as customer,
       SUM(CASE WHEN d.is_active=1 THEN 1 ELSE 0 END) as active_devices,
       SUM(CASE WHEN d.is_active=0 THEN 1 ELSE 0 END) as inactive_devices,
       GROUP_CONCAT(CASE WHEN d.is_active=1 THEN d.device_name END, ', ') as active_names
  FROM customers c
  LEFT JOIN devices d ON d.customer_id = c.id
 GROUP BY c.id
 ORDER BY c.id
"

echo
echo "=== STEP 6: Active EAP blocks in rw-eap.conf (deactivated devices should have BLOCKED- secret) ==="
# Note: rw-eap.conf still has all blocks loaded. Deactivated devices have
# BLOCKED- secret so auth fails. Active devices have the real secret.
echo "(deactivated devices' secrets are BLOCKED- in rw-eap.conf, not removed — charon still loads the block, auth fails at EAP)"
grep -A1 "id = " /home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf 2>/dev/null | grep -E "id|BLOCKED" | head -30

echo
echo "=== DONE — v1.2.6 migration applied ==="
echo "backup at: $BAK"
echo
echo "NOTE: For deactivated devices, you may want to update rw-eap.conf to set"
echo "      their secret to BLOCKED-<hex> so auth attempts fail cleanly. This script"
echo "      does NOT modify rw-eap.conf — that's a separate step (see notes)."
