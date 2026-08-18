#!/usr/bin/env bash
# Apply TKT-011 v2.0 time-based pricing migration (idempotent).
#
# Usage: sudo bash apply_migration_v2.0.sh
#
# Adds duration_days + speed_tier to tiers.
# Adds expires_at + mac_address_1 + mac_address_2 to customers.
# Adds idx_customers_expires_at index.
# Retires 4 old data-cap tiers (is_active=0).
# Seeds 20 new time-based packages.
#
# Idempotent. Safe to re-run. Tested on MariaDB 11.8.6.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION_FILE="$SCRIPT_DIR/sql/migrations/v2.0_time_based.sql"

if [[ ! -f "$MIGRATION_FILE" ]]; then
  echo "ERROR: migration file not found: $MIGRATION_FILE" >&2
  exit 1
fi

if ! command -v mariadb >/dev/null 2>&1; then
  echo "ERROR: mariadb client not found. apt install mariadb-client" >&2
  exit 1
fi

echo "Applying TKT-011 v2.0 time-based pricing migration to radius DB..."
echo "  source: $MIGRATION_FILE"
echo ""
mariadb radius < "$MIGRATION_FILE"
echo ""
echo "=== Done. Live tier summary ==="
mariadb radius -e "SELECT name, duration_days, speed_tier, price_zar, is_active FROM tiers ORDER BY is_active DESC, duration_days, FIELD(speed_tier,'10_10','20_20','unlimited');"
