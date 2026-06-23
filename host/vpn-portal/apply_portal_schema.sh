#!/usr/bin/env bash
# Apply portal schema migration (idempotent).
#
# Usage: sudo bash apply_portal_schema.sh /var/lib/strongswan/ipsec.db
#
# Creates operator_sessions + customer_portal_sessions tables if missing.
# Safe to run multiple times.

set -euo pipefail

DB_PATH="${1:-/var/lib/strongswan/ipsec.db}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: DB not found: $DB_PATH" >&2
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/portal_schema.sql" ]]; then
  echo "ERROR: portal_schema.sql not found in $SCRIPT_DIR" >&2
  exit 1
fi

# sqlite3 must be installed
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "ERROR: sqlite3 not found. apt install sqlite3" >&2
  exit 1
fi

echo "Applying portal schema to $DB_PATH..."
sqlite3 "$DB_PATH" < "$SCRIPT_DIR/portal_schema.sql"
echo "Done. Tables created (idempotent):"
sqlite3 "$DB_PATH" ".tables" | tr -s ' \n' '\n' | grep -E "^(operator_sessions|customer_portal_sessions)$" || true
echo "Schema:"
sqlite3 "$DB_PATH" ".schema operator_sessions"
sqlite3 "$DB_PATH" ".schema customer_portal_sessions"