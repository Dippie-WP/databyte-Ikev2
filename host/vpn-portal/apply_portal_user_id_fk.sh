#!/usr/bin/env bash
# Apply portal-user-id-fk.sql migration (idempotent).
#
# Usage: sudo bash apply_portal_user_id_fk.sh /var/lib/strongswan/ipsec.db
#
# Adds customers.user_id FK to users.id, backfills from existing devices,
# creates idx_customers_user_id index.
#
# Safe to run multiple times (ALTER TABLE ADD COLUMN on existing column is a
# no-op error caught by try/except in conftest.py; index creation is IF NOT
# EXISTS).

set -euo pipefail

DB_PATH="${1:-/var/lib/strongswan/ipsec.db}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$DB_PATH" ]]; then
    echo "ERROR: DB not found: $DB_PATH" >&2
    exit 1
fi
if [[ ! -f "$SCRIPT_DIR/portal-user-id-fk.sql" ]]; then
    echo "ERROR: portal-user-id-fk.sql not found in $SCRIPT_DIR" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "ERROR: sqlite3 not found. apt install sqlite3" >&2
    exit 1
fi

echo "Applying customers.user_id FK migration to $DB_PATH..."

# SQLite has no "ADD COLUMN IF NOT EXISTS", so on re-run the ALTER TABLE fails.
# Apply each statement individually and tolerate "duplicate column" + "duplicate index".
set +e
sqlite3 "$DB_PATH" < "$SCRIPT_DIR/portal-user-id-fk.sql" 2> /tmp/migration_err.txt
RC=$?
set -e
if [[ $RC -ne 0 ]]; then
    if grep -qE "duplicate column|already exists" /tmp/migration_err.txt; then
        echo "Migration already applied (idempotent re-run detected)."
        rm -f /tmp/migration_err.txt
    else
        echo "ERROR: migration failed:" >&2
        cat /tmp/migration_err.txt >&2
        rm -f /tmp/migration_err.txt
        exit 1
    fi
fi

echo "Done."
echo ""
echo "Schema for customers.user_id:"
sqlite3 "$DB_PATH" ".schema customers" | grep user_id
echo ""
echo "Backfill state:"
sqlite3 "$DB_PATH" "SELECT
    COUNT(*) AS total_customers,
    SUM(CASE WHEN user_id IS NOT NULL THEN 1 ELSE 0 END) AS with_user_id,
    SUM(CASE WHEN user_id IS NULL AND is_operator = 1 THEN 1 ELSE 0 END) AS operators_no_user,
    SUM(CASE WHEN user_id IS NULL AND is_operator = 0 THEN 1 ELSE 0 END) AS non_operator_no_user
FROM customers;"
echo ""
echo "Index:"
sqlite3 "$DB_PATH" ".indexes customers" | grep user_id || echo "  (no user_id index yet — odd)"
