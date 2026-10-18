#!/usr/bin/env bash
#
# provision-freeradius.sh — Apply host/freeradius/ overlay to /etc/freeradius/3.0/
#
# Idempotent. Safe to run multiple times. Skips files that already match.
# Only restarts FreeRADIUS if files actually changed.
#
# Usage:
#   bash provision-freeradius.sh           # apply overlay + smoke test
#   bash provision-freeradius.sh --check   # just check drift, exit 1 if drift
#   bash provision-freeradius.sh --no-restart   # apply but don't restart (testing)
#
# Companion to:
#   - host/freeradius/README.md
#   - CORR-2026-07-13-035 in ~/self-improving/corrections.md
#   - docs/RUNBOOK-DR-REBUILD-AND-HA.md §2.3 step 14a
#

set -euo pipefail

# ------------------------------------------------------------------
# Locate ourselves and repo root
# ------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If SCRIPT_DIR looks like .../host/freeradius, repo root is two levels up.
# Otherwise (e.g. /usr/local/bin/provision-freeradius.sh), fall back to
# env REPO_ROOT or /opt/strongswan-vpn-gateway.
if [[ "$(basename "$SCRIPT_DIR")" == "freeradius" ]] \
   && [[ "$(basename "$(dirname "$SCRIPT_DIR")")" == "host" ]]; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
elif [[ -n "${REPO_ROOT:-}" ]]; then
    REPO_ROOT="${REPO_ROOT}"
else
    # Standard vps-01 install location
    for candidate in /opt/strongswan-vpn-gateway /root/projects/strongswan-vpn-gateway; do
        if [[ -d "$candidate/host/freeradius" ]]; then
            REPO_ROOT="$candidate"
            break
        fi
    done
fi

if [[ -z "${REPO_ROOT:-}" ]] || [[ ! -d "$REPO_ROOT/host/freeradius" ]]; then
    echo "FATAL: cannot locate strongswan-vpn-gateway repo." >&2
    echo "  This script must be run from inside the repo, OR installed at" >&2
    echo "  /usr/local/bin/provision-freeradius.sh with REPO_ROOT set." >&2
    echo "  Common locations:" >&2
    echo "    /opt/strongswan-vpn-gateway/host/freeradius/provision-freeradius.sh" >&2
    echo "    /root/projects/strongswan-vpn-gateway/host/freeradius/provision-freeradius.sh" >&2
    exit 1
fi

# Overlay source = repo's host/freeradius/
SRC_BASE="$REPO_ROOT/host/freeradius"

echo "Repo root: $REPO_ROOT"
echo

# Target = live /etc/freeradius/3.0/
DST_BASE="/etc/freeradius/3.0"

# Backup root
BACKUP_ROOT="/var/lib/databyte/freeradius-backups"

# RADIUS secret for smoke test (must match /etc/freeradius/3.0/clients.conf)
# Use localhost's NAS client secret. If changed, update both places.
RADIUS_SECRET="9f32746a2845c1ba72d2b60f71631c61dc24f496c85548f38c8d828839a0ebd2"

# ------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------
CHECK_ONLY=0
NO_RESTART=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --no-restart) NO_RESTART=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

# ------------------------------------------------------------------
# Sanity checks
# ------------------------------------------------------------------
if [[ ! -d "$SRC_BASE" ]]; then
    echo "FATAL: overlay source not found at $SRC_BASE" >&2
    echo "  Are you running this from inside the strongswan-vpn-gateway repo?" >&2
    exit 1
fi

if [[ ! -d "$DST_BASE" ]]; then
    echo "FATAL: target /etc/freeradius/3.0/ does not exist" >&2
    echo "  Install FreeRADIUS first: apt install freeradius freeradius-mysql" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "FATAL: must run as root (sudo $0)" >&2
    exit 1
fi

# Verify freeradius user exists
if ! id freerad &>/dev/null; then
    echo "FATAL: freerad user does not exist; install FreeRADIUS first" >&2
    exit 1
fi

# ------------------------------------------------------------------
# Files to overlay (relative to host/freeradius/ AND /etc/freeradius/3.0/)
# Format: <src-relative> <dst-relative>
# ------------------------------------------------------------------
# Use '|' as separator (paths may contain spaces; space-delimiting is fragile)
OVERLAY_FILES=(
    "mods-available/sql_last_seen|mods-available/sql_last_seen"
    "mods-config/sql/main/mysql/queries.conf|mods-config/sql/main/mysql/queries.conf"
    "sites-enabled/default|sites-enabled/default"
)

# ------------------------------------------------------------------
# Step 1: Check drift
# ------------------------------------------------------------------
echo "=== FreeRADIUS overlay drift check ==="
echo "Source: $SRC_BASE"
echo "Target: $DST_BASE"
echo
DRIFT=0
declare -A FILE_STATUS
for entry in "${OVERLAY_FILES[@]}"; do
    src_rel="${entry%%|*}"
    dst_rel="${entry#*|}"
    src="$SRC_BASE/$src_rel"
    dst="$DST_BASE/$dst_rel"

    if [[ ! -f "$src" ]]; then
        echo "  [MISSING-SRC] $src_rel  (overlay file absent from repo)"
        FILE_STATUS["$src_rel"]="MISSING-SRC"
        DRIFT=1
        continue
    fi

    if [[ ! -f "$dst" ]]; then
        echo "  [MISSING-DST] $dst_rel  (target file absent; will create)"
        FILE_STATUS["$src_rel"]="MISSING-DST"
        DRIFT=1
        continue
    fi

    src_md5=$(md5sum "$src" | awk '{print $1}')
    dst_md5=$(md5sum "$dst" | awk '{print $1}')

    if [[ "$src_md5" == "$dst_md5" ]]; then
        echo "  [MATCH]      $src_rel"
        FILE_STATUS["$src_rel"]="MATCH"
    else
        echo "  [DIFF]       $src_rel"
        echo "              src: $src_md5"
        echo "              dst: $dst_md5"
        FILE_STATUS["$src_rel"]="DIFF"
        DRIFT=1
    fi
done

echo
echo "Filesystem state:"
RADACCT_DIR="/var/log/freeradius/radacct"
RADACCT_OWNER=$(stat -c '%U:%G' "$RADACCT_DIR" 2>/dev/null || echo "MISSING")
if [[ "$RADACCT_OWNER" == "freerad:freerad" ]]; then
    echo "  [OK] $RADACCT_DIR owner = freerad:freerad"
else
    echo "  [DRIFT] $RADACCT_DIR owner = $RADACCT_OWNER (need freerad:freerad)"
    DRIFT=1
fi

echo
if [[ $DRIFT -eq 0 ]]; then
    echo "NO DRIFT — overlay matches target."
    if [[ $CHECK_ONLY -eq 1 ]]; then
        exit 0
    fi
    # Even if no drift, run the smoke test (Step 6)
    echo
    echo "=== Smoke test (radclient Accounting-Request) ==="
    SMOKE_SESSION_ID="smoke-$(date +%s)"
    if printf "Acct-Status-Type = Start\nUser-Name = zunaid-win11-en-laptop\nNAS-IP-Address = 154.65.110.44\nAcct-Session-Id = %s\nFramed-IP-Address = 10.99.0.99\n" "$SMOKE_SESSION_ID" \
        | radclient -x 127.0.0.1:1813 acct "$RADIUS_SECRET" 2>&1 | grep -q "Received Accounting-Response"; then
        echo "  [OK] FreeRADIUS accounting chain working"
        mariadb -e "DELETE FROM radius.radacct WHERE acctsessionid='$SMOKE_SESSION_ID';" 2>/dev/null || true
        exit 0
    else
        echo "  [FAIL] FreeRADIUS did not respond to Accounting-Request" >&2
        exit 1
    fi
fi

echo "DRIFT DETECTED — will apply overlay."
if [[ $CHECK_ONLY -eq 1 ]]; then
    exit 1
fi

# ------------------------------------------------------------------
# Step 2: Backup current state
# ------------------------------------------------------------------
BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
echo
echo "=== Backing up current state to $BACKUP_DIR ==="
mkdir -p "$BACKUP_DIR"
for entry in "${OVERLAY_FILES[@]}"; do
    dst_rel="${entry#*|}"
    src="$DST_BASE/$dst_rel"
    if [[ -f "$src" ]]; then
        dst_in_backup="$BACKUP_DIR/$dst_rel"
        mkdir -p "$(dirname "$dst_in_backup")"
        cp -a "$src" "$dst_in_backup"
        echo "  backed up $dst_rel"
    fi
done
echo

# ------------------------------------------------------------------
# Step 3: Apply overlay
# ------------------------------------------------------------------
CHANGED=0
for entry in "${OVERLAY_FILES[@]}"; do
    src_rel="${entry%%|*}"
    dst_rel="${entry#*|}"
    src="$SRC_BASE/$src_rel"
    dst="$DST_BASE/$dst_rel"

    if [[ "${FILE_STATUS[$src_rel]:-MATCH}" == "MATCH" ]]; then
        continue
    fi

    echo "=== Applying $src_rel -> $dst_rel ==="
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"

    # Ownership: root:freerad for config files, mode 640
    # (Default Debian package installs them this way too.)
    chown root:freerad "$dst"
    chmod 640 "$dst"

    CHANGED=1
done

# ------------------------------------------------------------------
# Step 4: Filesystem state — chown radacct directory
# ------------------------------------------------------------------
echo
echo "=== Fixing radacct directory ownership ==="
mkdir -p /var/log/freeradius/radacct
chown -R freerad:freerad /var/log/freeradius/radacct/
chmod 755 /var/log/freeradius/radacct/
echo "  /var/log/freeradius/radacct/ -> freerad:freerad"

# ------------------------------------------------------------------
# Step 5: Restart if anything changed
# ------------------------------------------------------------------
if [[ $CHANGED -ne 0 ]]; then
    if [[ $NO_RESTART -eq 1 ]]; then
        echo
        echo "=== --no-restart set; skipping systemctl restart ==="
        echo "    Run: systemctl restart freeradius"
    else
        echo
        echo "=== Restarting FreeRADIUS (config files changed) ==="
        systemctl restart freeradius
        sleep 3
        systemctl is-active freeradius
        echo
        echo "=== Verify listening sockets ==="
        ss -lnup | grep -E ":(1812|1813)" || {
            echo "WARNING: FreeRADIUS not listening on 1812/1813!" >&2
            exit 1
        }
    fi
else
    echo
    echo "=== No config files changed; skipping FreeRADIUS restart ==="
fi

# ------------------------------------------------------------------
# Step 6: Smoke test (always, even on no-op)
# ------------------------------------------------------------------
echo
echo "=== Smoke test (radclient Accounting-Request) ==="
SMOKE_SESSION_ID="smoke-$(date +%s)"
if printf "Acct-Status-Type = Start\nUser-Name = zunaid-win11-en-laptop\nNAS-IP-Address = 154.65.110.44\nAcct-Session-Id = %s\nFramed-IP-Address = 10.99.0.99\n" "$SMOKE_SESSION_ID" \
    | radclient -x 127.0.0.1:1813 acct "$RADIUS_SECRET" 2>&1 | grep -q "Received Accounting-Response"; then
    echo "  [OK] FreeRADIUS accounting chain working"
    # Cleanup smoke test row
    mariadb -e "DELETE FROM radius.radacct WHERE acctsessionid='$SMOKE_SESSION_ID';" 2>/dev/null || true
else
    echo "  [FAIL] FreeRADIUS did not respond to Accounting-Request" >&2
    echo "  Debug with: freeradius -X" >&2
    exit 1
fi

# ------------------------------------------------------------------
# Step 7: Print final state
# ------------------------------------------------------------------
echo
echo "=== Final state ==="
echo "md5sum (live):"
for entry in "${OVERLAY_FILES[@]}"; do
    dst_rel="${entry#*|}"
    dst="$DST_BASE/$dst_rel"
    if [[ -f "$dst" ]]; then
        md5sum "$dst" | sed 's/^/  /'
    fi
done
echo "Backup location: $BACKUP_DIR"
echo
echo "DONE. Provisioning successful."
