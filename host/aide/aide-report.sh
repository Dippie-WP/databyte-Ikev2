#!/usr/bin/env bash
# /usr/local/bin/aide-report.sh
# AIDE weekly check — emits a human-readable report + machine-parseable JSON
# Called from aide-check.service (systemd). Writes report to /var/log/aide/.
#
# Exit code:
#   0 = no changes (or only expected changes)
#   1 = unexpected changes (FAIL — investigate)
#
# Usage:
#   sudo /usr/local/bin/aide-report.sh

set -euo pipefail

LOG_DIR=/var/log/aide
DATABASE_DIR=/var/lib/aide
REPORT_FILE="$LOG_DIR/aide-report-$(date -u +%Y%m%dT%H%M%SZ).txt"
JSON_FILE="$LOG_DIR/aide-report-$(date -u +%Y%m%dT%H%M%SZ).json"
LATEST_LINK="$LOG_DIR/aide-report-latest.txt"
JSON_LINK="$LOG_DIR/aide-report-latest.json"

# Retention: keep last 12 weekly reports
RETENTION_COUNT=12

mkdir -p "$LOG_DIR" "$DATABASE_DIR"

# Load databyte-specific config (extends /etc/aide/aide.conf)
CONF_FILE=/etc/aide/aide.conf.d/databyte.conf
if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: $CONF_FILE not found. Install host/aide/aide.conf.d/databyte.conf first." >&2
    exit 1
fi

# If first run, initialize the database
# Use /etc/aide/aide.conf (the main config) which @@x_includes this file.
# Don't pass --config=THIS_FILE directly because the Full rule groups are
# defined in /etc/aide/aide.conf and won't be visible otherwise.
MAIN_CONF=/etc/aide/aide.conf
if [[ ! -f "$DATABASE_DIR/aide.db" ]]; then
    echo "[$(date -u +%FT%TZ)] AIDE first run — initializing baseline..." >&2
    aide --config="$MAIN_CONF" --init
    # aide --init creates /var/lib/aide/aide.db.new; rename to aide.db
    if [[ -f "$DATABASE_DIR/aide.db.new" ]]; then
        mv "$DATABASE_DIR/aide.db.new" "$DATABASE_DIR/aide.db"
    fi
    echo "[$(date -u +%FT%TZ)] AIDE baseline initialized. Next run will compare." >&2
    exit 0
fi

# Check against baseline
echo "[$(date -u +%FT%TZ)] AIDE check running..." >&2
set +e
aide --config="$MAIN_CONF" --check > "$REPORT_FILE" 2>&1
RC=$?
set -e

# aide returns:
#   0 = no changes
#   1 = changes detected (WARN)
#   >1 = error

# Move new database into place (so next run compares against the most recent state)
if [[ -f "$DATABASE_DIR/aide.db.new" ]]; then
    mv "$DATABASE_DIR/aide.db.new" "$DATABASE_DIR/aide.db"
fi

# Rotate old reports (keep last N)
ls -1t "$LOG_DIR"/aide-report-*.txt 2>/dev/null | tail -n +$((RETENTION_COUNT + 1)) | xargs -r rm -f
ls -1t "$LOG_DIR"/aide-report-*.json 2>/dev/null | tail -n +$((RETENTION_COUNT + 1)) | xargs -r rm -f

# Update latest symlinks
ln -sf "$(basename "$REPORT_FILE")" "$LATEST_LINK"
ln -sf "$(basename "$JSON_FILE")" "$JSON_LINK"

# Also generate a JSON summary (parseable by monitoring stack)
python3 - "$REPORT_FILE" "$JSON_FILE" <<'PYEOF'
import sys
import json
from datetime import datetime, timezone

report_path = sys.argv[1]
json_path = sys.argv[2]

with open(report_path) as f:
    content = f.read()

# Parse AIDE report: changes look like
#   <entry details>
#   File: /path
#   Size : 1234
#   ...
lines = content.splitlines()
changes = []
i = 0
while i < len(lines):
    line = lines[i].strip()
    if line.startswith("File:"):
        entry = {"file": line.split(":", 1)[1].strip()}
        i += 1
        while i < len(lines) and lines[i].startswith(" "):
            kv = lines[i].strip().split(":", 1)
            if len(kv) == 2:
                entry[kv[0].strip()] = kv[1].strip()
            i += 1
        changes.append(entry)
    else:
        i += 1

summary = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "changed_files": len(changes),
    "changes": changes,
}

with open(json_path, "w") as f:
    json.dump(summary, f, indent=2)
PYEOF

if [[ $RC -eq 0 ]]; then
    echo "[$(date -u +%FT%TZ)] AIDE: no changes. (clean)"
elif [[ $RC -eq 1 ]]; then
    echo "[$(date -u +%FT%TZ)] AIDE: CHANGES DETECTED — review $REPORT_FILE" >&2
    # Print summary to stderr (visible in journalctl)
    tail -50 "$REPORT_FILE" >&2
else
    echo "[$(date -u +%FT%TZ)] AIDE: error (exit code $RC) — see $REPORT_FILE" >&2
fi

exit $RC
