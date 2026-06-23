#!/usr/bin/env bash
# Apply portal customer-table column extensions (idempotent).
#
# Usage: sudo bash apply_customers_extensions.sh /var/lib/strongswan/ipsec.db
#
# Adds billing_id + email to the customers table (referenced by app.py but
# not in strongSwan's base schema). Safe to run multiple times — SQLite ALTER
# TABLE ADD COLUMN with same name will fail on re-run, so we check first.

set -euo pipefail

DB_PATH="${1:-/var/lib/strongswan/ipsec.db}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$DB_PATH" ]]; then
  echo "ERROR: DB not found: $DB_PATH" >&2
  exit 1
fi

# Check current columns
echo "Checking customers schema in $DB_PATH..."
EXISTING=$(sqlite3 "$DB_PATH" "PRAGMA table_info(customers);" | awk -F'|' '{print $2}')

ADD_BILLING_ID=1
ADD_EMAIL=1

if echo "$EXISTING" | grep -qx "billing_id"; then
  echo "  billing_id already present — skipping"
  ADD_BILLING_ID=0
fi
if echo "$EXISTING" | grep -qx "email"; then
  echo "  email already present — skipping"
  ADD_EMAIL=0
fi

if [[ $ADD_BILLING_ID -eq 1 ]]; then
  echo "  adding billing_id..."
  sqlite3 "$DB_PATH" "ALTER TABLE customers ADD COLUMN billing_id TEXT;"
fi
if [[ $ADD_EMAIL -eq 1 ]]; then
  echo "  adding email..."
  sqlite3 "$DB_PATH" "ALTER TABLE customers ADD COLUMN email TEXT;"
fi

echo "Done. New schema:"
sqlite3 "$DB_PATH" ".schema customers" | grep -E "billing_id|email"