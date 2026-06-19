#!/usr/bin/env bash
# reset_demo.sh — Reset the demo customer's data_used_bytes to 0 and clear over_quota.
#
# Use this AFTER a client demo to get the demo account back to pristine state.
# Does NOT touch customers.data_limit_bytes (the 100 MB tier allowance is kept).
# Does NOT touch the operator (zun-operator) account.
# Does NOT touch any other customers.
#
# Idempotent — running on a fresh demo account is a no-op.
# Run on the LXC HOST (not inside the container).
#
# Usage:
#   bash quota/reset_demo.sh
#   bash quota/reset_demo.sh --yes   # skip the confirmation prompt

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
TS=$(date -u +%s)

if [ ! -s "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH missing or empty" >&2
    exit 1
fi

# --- preflight: check demo-customer exists and show current state ---
CURRENT=$(sqlite3 "$DB_PATH" "SELECT id, data_used_bytes, data_limit_bytes, over_quota, is_active FROM customers WHERE name='demo-customer';")
if [ -z "$CURRENT" ]; then
    echo "ERROR: demo-customer not found. Run seed_5B1.sh first." >&2
    exit 1
fi

echo "=== Demo customer current state ==="
echo "  id=$CURRENT"

# Parse current values
IFS='|' read -r DEMO_ID DATA_USED DATA_LIMIT OVER_QUOTA IS_ACTIVE <<< "$CURRENT"
echo "  data_used_bytes:   $DATA_USED / $DATA_LIMIT"
echo "  over_quota:        $OVER_QUOTA"
echo "  is_active:         $IS_ACTIVE"

if [ "$DATA_USED" = "0" ] && [ "$OVER_QUOTA" = "0" ]; then
    echo "  Already at pristine state. No action needed."
    exit 0
fi

# --- confirm unless --yes ---
if [ "${1:-}" != "--yes" ]; then
    echo
    echo "  About to reset: data_used_bytes -> 0, over_quota -> 0, is_active -> 1"
    echo "  data_limit_bytes ($DATA_LIMIT) is NOT changed."
    read -p "  Continue? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "  Aborted."
        exit 1
    fi
fi

# --- reset ---
sqlite3 "$DB_PATH" <<SQL
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

UPDATE customers
SET data_used_bytes = 0,
    over_quota      = 0,
    is_active       = 1,
    updated_at      = $TS
WHERE name = 'demo-customer';

INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at)
VALUES ('zun', 'reset_demo', 'customer',
        (SELECT id FROM customers WHERE name='demo-customer'),
        json_object('data_used_before', $DATA_USED, 'data_limit_bytes_unchanged', $DATA_LIMIT, 'ts', $TS),
        $TS);

COMMIT;

SELECT '--- after reset ---' AS section;
SELECT id, name, data_used_bytes, data_limit_bytes, over_quota, is_active FROM customers WHERE name='demo-customer';
SQL

echo "  Status: OK — demo-customer reset to pristine state"
