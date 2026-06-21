#!/usr/bin/env bash
# reset_demo.sh — Reset the demo customer's data_used_bytes to 0 and clear over_quota.
#
# Use this AFTER a client demo to get the demo account back to pristine state.
# Does NOT touch customers.data_limit_bytes (the 100 MB tier allowance is kept).
# Does NOT touch the operator (zun-operator) account.
# Does NOT touch any other customers.
#
# v1.2.10 — also restores rw-eap.conf if eap-demo-phone secret is KILLED-/BLOCKED-
# (the result of a quota-monitor 100% cut that was never followed by a reset).
#
# Idempotent — running on a fresh demo account (DB pristine AND conf pristine) is a no-op.
# Run on the LXC HOST (not inside the container).
#
# Usage:
#   bash quota/reset_demo.sh
#   bash quota/reset_demo.sh --yes   # skip the confirmation prompt
#
# Env vars (override defaults):
#   DB_PATH            default /var/lib/strongswan/ipsec.db
#   CONF_FILE          default /home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf
#   CONF_BACKUP_DIR    default /home/zunaid/strongswan/swanctl/conf.d/.backups
#   DOCKER_CONTAINER   default strongswan
#   CHARON_URI         default tcp://127.0.0.1:4502

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
CONF_FILE="${CONF_FILE:-/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf}"
CONF_BACKUP_DIR="${CONF_BACKUP_DIR:-/home/zunaid/strongswan/swanctl/conf.d/.backups}"
DOCKER_CONTAINER="${DOCKER_CONTAINER:-strongswan}"
CHARON_URI="${CHARON_URI:-tcp://127.0.0.1:4502}"
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

# --- v1.2.10 fix: also check rw-eap.conf for KILLED/BLOCKED demo-phone secret ---
# v1.2.7.x bug: the script used to exit early if the DB was pristine, even if
# the demo-phone EAP secret in rw-eap.conf had been replaced with KILLED-/BLOCKED-
# during a quota test cut. The iPhone would then fail to authenticate silently.
# Now: if the conf contains a KILLED/BLOCKED secret, restore from the latest
# pre-cut backup regardless of DB state.
KILLED_BLOCKED=false
if [ -f "$CONF_FILE" ]; then
    DEMO_SECRET=$(awk '
        /eap-demo-phone[[:space:]]*\{/ { inblock=1; next }
        inblock && /^  \}/                   { inblock=0 }
        inblock && /secret[[:space:]]*=/ {
            # match: secret = "..."
            match($0, /secret[[:space:]]*=[[:space:]]*"([^"]*)"/, m)
            if (m[1] != "") { print m[1]; exit }
        }
    ' "$CONF_FILE")
    if echo "${DEMO_SECRET:-}" | grep -qE '^(KILLED|BLOCKED)-'; then
        KILLED_BLOCKED=true
    fi
fi

# --- decide whether to do work ---
if [ "$DATA_USED" = "0" ] && [ "$OVER_QUOTA" = "0" ] && [ "$KILLED_BLOCKED" = "false" ]; then
    echo "  Already at pristine state. No action needed."
    exit 0
fi

if [ "$KILLED_BLOCKED" = "true" ]; then
    echo "  !!! rw-eap.conf has KILLED/BLOCKED demo-phone secret — will restore from backup."
    LATEST_BACKUP=$(ls -t "$CONF_BACKUP_DIR"/rw-eap.conf.bak-quotamon-* 2>/dev/null | head -1)
    if [ -z "$LATEST_BACKUP" ]; then
        echo "ERROR: no pre-cut backup found in $CONF_BACKUP_DIR" >&2
        exit 1
    fi
    echo "  Backup to restore: $LATEST_BACKUP"
fi

# --- confirm unless --yes ---
if [ "${1:-}" != "--yes" ]; then
    echo
    if [ "$DATA_USED" != "0" ] || [ "$OVER_QUOTA" != "0" ]; then
        echo "  About to reset: data_used_bytes -> 0, over_quota -> 0, is_active -> 1"
        echo "  data_limit_bytes ($DATA_LIMIT) is NOT changed."
    fi
    if [ "$KILLED_BLOCKED" = "true" ]; then
        echo "  About to restore: rw-eap.conf <- $LATEST_BACKUP"
        echo "  About to reload:  sudo docker exec $DOCKER_CONTAINER swanctl --load-creds"
    fi
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

# --- v1.2.10: restore rw-eap.conf from latest pre-cut backup if needed ---
if [ "$KILLED_BLOCKED" = "true" ]; then
    cp "$LATEST_BACKUP" "$CONF_FILE"
    echo "  Restored: $CONF_FILE <- $LATEST_BACKUP"
    docker exec "$DOCKER_CONTAINER" swanctl --uri="$CHARON_URI" --load-creds
    echo "  Charon creds reloaded."
fi

if [ "$DATA_USED" != "0" ] || [ "$OVER_QUOTA" != "0" ] || [ "$KILLED_BLOCKED" = "true" ]; then
    echo "  Status: OK — demo-customer reset to pristine state"
else
    echo "  Status: no-op — already pristine"
fi
