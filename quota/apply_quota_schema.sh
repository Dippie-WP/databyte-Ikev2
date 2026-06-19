#!/usr/bin/env bash
# apply_quota_schema.sh — Apply quota_schema.sql to the live strongSwan DB.
#
# Idempotent: safe to run on every container start. Uses IF NOT EXISTS
# throughout quota_schema.sql, so re-running is a no-op.
#
# This script is meant to run on the LXC HOST (not inside the container),
# directly against the host's /var/lib/strongswan/ipsec.db file. The
# strongSwan container bind-mounts this same path, so changes are visible
# to charon immediately (no restart needed for schema additions; charon
# discovers new tables on next query).
#
# Usage:
#   bash quota/apply_quota_schema.sh
#
# Exit codes:
#   0  schema applied (or already applied)
#   1  DB file missing
#   2  schema file missing
#   3  sqlite3 error during apply
#
# Verification (after running):
#   sqlite3 /var/lib/strongswan/ipsec.db ".tables" | grep -E "customers|tiers|devices|purchases|alerts|audit_log"
#   sqlite3 /var/lib/strongswan/ipsec.db "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('customers','tiers','devices','purchases','alerts','audit_log');"
#   # Expected: 6 rows, one per table

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
SCHEMA_FILE="${SCHEMA_FILE:-$(dirname "$0")/quota_schema.sql}"

# --- preflight ---
if [ ! -s "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH missing or empty" >&2
    echo "       Start the strongSwan container once and let attr-sql initialize the upstream schema first." >&2
    exit 1
fi

if [ ! -s "$SCHEMA_FILE" ]; then
    echo "ERROR: $SCHEMA_FILE not found" >&2
    exit 2
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "ERROR: sqlite3 not installed on host. apt-get install sqlite3" >&2
    exit 3
fi

# --- pre-check: tables already exist? ---
EXISTING=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('customers','tiers','devices','purchases','alerts','audit_log');")
EXPECTED=6

echo "=== Apply quota schema ==="
echo "  DB:         $DB_PATH"
echo "  Schema:     $SCHEMA_FILE"
echo "  Existing:   $EXISTING / $EXPECTED quota tables present"

if [ "$EXISTING" -eq "$EXPECTED" ]; then
    echo "  Status:     already applied (no-op)"
    exit 0
fi

# --- apply ---
if ! sqlite3 "$DB_PATH" < "$SCHEMA_FILE"; then
    echo "ERROR: sqlite3 apply failed" >&2
    exit 3
fi

# --- post-check ---
NOW_EXISTING=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('customers','tiers','devices','purchases','alerts','audit_log');")
echo "  After:      $NOW_EXISTING / $EXPECTED quota tables present"

if [ "$NOW_EXISTING" -ne "$EXPECTED" ]; then
    echo "ERROR: expected $EXPECTED tables, got $NOW_EXISTING" >&2
    exit 3
fi

# --- audit log row for this apply ---
TS=$(date -u +%s)
sqlite3 "$DB_PATH" "INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at) VALUES ('system', 'apply_quota_schema', 'system', NULL, '{\"tables\": $NOW_EXISTING}', $TS);"

echo "  Status:     OK"
echo "  Audit:      row added to audit_log (apply_quota_schema, ts=$TS)"
