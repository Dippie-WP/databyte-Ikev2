#!/usr/bin/env bash
# Apply TKT-011 v2.0 session tracking migration (idempotent).
#
# Usage: sudo bash apply_migration_v2.0_session_tracking.sh
#
# Adds 4 cumulative tracking columns to customers table:
#   - total_session_time_seconds
#   - active_days_count
#   - last_session_at
#   - last_session_duration_seconds
#
# These columns are populated by quota-monitor.py on session stop.
# NOT used for kill logic (kill is time-based via expires_at).
#
# Idempotent. Safe to re-run. Tested on MariaDB 11.8.6.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION_FILE="$SCRIPT_DIR/sql/migrations/v2.0_session_tracking.sql"

if [[ ! -f "$MIGRATION_FILE" ]]; then
  echo "ERROR: migration file not found: $MIGRATION_FILE" >&2
  exit 1
fi

if ! command -v mariadb >/dev/null 2>&1; then
  echo "ERROR: mariadb client not found. apt install mariadb-client" >&2
  exit 1
fi

echo "Applying TKT-011 v2.0 session tracking migration to radius DB..."
echo "  source: $MIGRATION_FILE"
echo ""
mariadb radius < "$MIGRATION_FILE"
echo ""
echo "Done. Live column verify:"
mariadb radius -e "SHOW COLUMNS FROM customers WHERE Field IN ('total_session_time_seconds','active_days_count','last_session_at','last_session_duration_seconds');"
