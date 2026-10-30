#!/usr/bin/env bash
# Apply TKT-032 v3.0 data_limit_bytes + over_quota migration (idempotent).
#
# Usage: sudo bash apply_migration_v3.0_data_limit_bytes.sh
#
# Adds data_limit_bytes + over_quota to radius.customers.
# Adds data_limit_bytes to radius.tiers.
# Idempotent. Safe to re-run. Tested on MariaDB 11.8.6.
#
# PRE-DEPLOY BACKUP per HOT-208 — already exists at
# /var/backups/pre-001-data-limit-bytes-{T074334Z,T074614Z,T074857Z}.sql
#
# DOWNGRADE (if needed):
#   ALTER TABLE tiers DROP COLUMN data_limit_bytes;
#   ALTER TABLE customers DROP COLUMN over_quota;
#   ALTER TABLE customers DROP COLUMN data_limit_bytes;

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION_FILE="$SCRIPT_DIR/sql/migrations/v3.0_data_limit_bytes.sql"

if [[ ! -f "$MIGRATION_FILE" ]]; then
  echo "ERROR: migration file not found: $MIGRATION_FILE" >&2
  exit 1
fi
if ! command -v mariadb >/dev/null 2>&1; then
  echo "ERROR: mariadb client not found. apt install mariadb-client" >&2
  exit 1
fi

echo "Applying TKT-032 v3.0 data_limit_bytes + over_quota migration to radius DB..."
echo "  source: $MIGRATION_FILE"
echo ""
mariadb radius < "$MIGRATION_FILE"
echo ""
echo "=== Done. Live column verify ==="
mariadb radius -e "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='radius' AND TABLE_NAME IN ('customers','tiers') AND COLUMN_NAME IN ('data_limit_bytes','over_quota') ORDER BY TABLE_NAME, COLUMN_NAME;"
