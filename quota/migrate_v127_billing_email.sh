#!/usr/bin/env bash
# v1.2.7 — Add billing_id + email columns to customers table.
#
# Operator-onboarding flow: the portal form has 8 fields including Billing ID
# and Email. The DB needs columns to store them. They are OPTIONAL (NULL OK).
#
# Idempotent: safe to re-run. If columns already exist, no-op.
# Backup: timestamped .db.bak-v127-pre-<ts> before any change.
#
# Live target: LXC 903 at /var/lib/strongswan/ipsec.db
# Run from LXC 902 via: pct push 903 ... && pct exec 903 -- bash ...
#
# Reference: docs/ROADMAP.md v1.2.7
set -euo pipefail

DB="/var/lib/strongswan/ipsec.db"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="${DB}.bak-v127-pre-${TS}"

echo "=== BACKUP ==="
cp "$DB" "$BACKUP"
echo "backup: $BACKUP ($(wc -c < "$BACKUP") bytes)"

echo
echo "=== Check current customers schema ==="
sqlite3 "$DB" "PRAGMA table_info(customers);" | awk -F'|' '{print $2 " (" $3 ")"}'

echo
echo "=== Step 1: add billing_id column (TEXT, nullable) — idempotent ==="
HAS_BILLING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('customers') WHERE name='billing_id';")
if [ "$HAS_BILLING" -eq 0 ]; then
    sqlite3 "$DB" "ALTER TABLE customers ADD COLUMN billing_id TEXT;"
    echo "  ADDED billing_id TEXT (nullable)"
else
    echo "  SKIP — billing_id already exists"
fi

echo
echo "=== Step 2: add email column (TEXT, nullable) — idempotent ==="
HAS_EMAIL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('customers') WHERE name='email';")
if [ "$HAS_EMAIL" -eq 0 ]; then
    sqlite3 "$DB" "ALTER TABLE customers ADD COLUMN email TEXT;"
    echo "  ADDED email TEXT (nullable)"
else
    echo "  SKIP — email already exists"
fi

echo
echo "=== Step 3: verify final schema ==="
sqlite3 "$DB" "PRAGMA table_info(customers);" | awk -F'|' '{print $2 " (" $3 ")"}'

echo
echo "=== Step 4: existing customers — billing_id + email will stay NULL ==="
sqlite3 "$DB" "SELECT id, name, billing_id, email FROM customers ORDER BY id;"

echo
echo "=== DONE — v1.2.7 migration applied ==="
echo "backup at: $BACKUP"
echo
echo "NOTE: billing_id + email are operator-facing optional fields. They do NOT"
echo "      affect the EAP auth path, the quota monitor, or the portal login."
echo "      They show on the customer detail card in the portal."
echo
echo "      Existing 3 customers (zun-operator, demo-customer, friend-customer)"
echo "      have NULL for both — operator can populate via raw SQL or a future"
echo "      PATCH /api/customers/{id} endpoint (not in this PR)."
