#!/usr/bin/env bash
# backup-vpn-portal-config.sh
# ----------------------------------------------------------------------------
# Backup VPN portal secrets + DB + strongSwan swanctl configs to RustFS.
# Files backed up:
#   1. /etc/vpn-portal.env (Argon2id hashes, DB path, cookie flag)
#   2. /etc/ssl/cloudflare/databyte.co.za.{crt,key} (Origin CA cert + key)
#   3. /var/lib/strongswan/ipsec.db (live DB — sqlite3 .backup for consistency)
#   4. /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf
#      (ADDED 2026-08-11 per Zun msg #34088 — today's outage cause: file
#       was truncated to 2875B between 07:33 and 07:35 UTC, with no
#       off-host backup. This step closes the gap.)
#   5. /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-psk.conf
#      (defensive — same directory, same write hazard)
#   6. vpn-extra-config.tar.gz — the config set that was in NO backup and had
#      to be hand-reconstructed during the 2026-11-02 TKT-037 DR drill:
#        /etc/freeradius/3.0   (FreeRADIUS config incl. sql + clients)
#        /etc/nginx            (nginx.conf + conf.d: portal-limits, adminer-*)
#        /var/lib/vpn-portal/.ssh  (portal -> charon SSH key)
#        /etc/databyte-vpn-bot (Telegram bot token + webhook secret)
#        /opt/strongswan-vpn-gateway/quota (quota-monitor + scripts)
#
# Target:   rustfs:open-claw-push/vpn-portal-config/<YYYY-MM-DDTHHMM>/ (steps 1-4)
#           rustfs:open-claw-push/strongswan-configs/daily/strongswan-YYYY-MM-DD.tar.gz (step 5)
#           rustfs:open-claw-push/strongswan-configs/weekly/strongswan-YYYY-WNN.tar.gz (step 5, Sundays only)
# Snapshots use a per-fire timestamp so 2 fires per day each get their own
# deduped subdir (e.g. 2026-07-30T0400 vs 2026-07-30T1000).
#
# Retention: ALWAYS KEEP LAST 2 (per Zun msg #29483, 2026-07-29 10:11 UTC).
# strongswan-configs/{daily,weekly} are separate: step [7/7] keeps 14 daily + 8 weekly
# (added 2026-10-20; MEMORY.md:979 documented this but no code enforced it).
# The [6/7] prune step deletes older dated subdirs after every successful push,
# so the destination at any time contains exactly KEEP_N=2 snapshots
# (most recent by lex-sort = chronological). Old YYYY-MM-DD/ subdirs from the
# previous schedule also match the prune regex, so they get rolled forward too.
#
# Schedule: 2x daily at 06:00 and 12:00 SAST (= 04:00 and 10:00 UTC) via
#   backup-vpn-portal-config.timer.
#
# NEW FLAG (2026-08-11): --dry-run
#   Performs SSH pulls + verify the files look correct, but skips the
#   rclone push and prune. Use this to test the script safely.
# ----------------------------------------------------------------------------

# Detect --dry-run BEFORE set -euo pipefail so the flag is in env
DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    shift || true
fi

set -euo pipefail

# Persistent log fan-out: tee writes to a dated log file under
# /var/log/vpn-portal-config-backup/, while a process substitution pipes each
# line into `logger -t backup-vpn-portal-config` so journald (via
# SyslogIdentifier=backup-vpn-portal-config in the .service unit) also gets
# every line. Mirrors the workspace backup's log-file pattern while preserving
# journald observability. Zun directive msg #29479, shipped 2026-07-29 10:10 UTC.
LOG_DIR="/root/.openclaw/workspace/databyte-vpn/vpn-portal-config-backup-logs"
LOG_FILE="$LOG_DIR/backup-$(date -u +%Y-%m-%d).log"
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE" >(logger -t backup-vpn-portal-config)) 2>&1

VPS_HOST="vpn-prod-01"
# Default-value expansion: uses $VPS_SSH_KEY from systemd Environment if set,
# else falls back to the canonical key path. (Masked in display as `…`,
# rendered here as `/root/.ssh/id_ed25519_xneelo`.)
VPS_SSH_KEY="${VPS_SSH_KEY:-/root/.ssh/id_ed25519_xneelo}"

# Retry wrapper for every SSH pull. A transient WAN path blip must NOT fail the
# whole backup: on 2026-11-11 10:00 UTC step [1/6] died on
#   ssh: connect to host vpn-prod-01 port 22: Connection timed out
# after ~135s and, with no retry and `set -e`, that single blip marked the run
# failed — which then re-alerted every 30 min until the next timer fire.
# 3 attempts, 0s/10s/30s backoff, 15s connect timeout, then real failure.
ssh_vps() {
    local attempt=1 rc=0
    local delays=(0 10 30)
    while :; do
        if ssh -i "$VPS_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 \
               -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
               "root@$VPS_HOST" "$@"; then
            return 0
        else
            rc=$?
        fi
        if (( attempt >= 3 )); then
            echo "  ssh_vps: FAILED after ${attempt} attempts (last rc=${rc})"
            return "$rc"
        fi
        sleep "${delays[$attempt]}"
        attempt=$(( attempt + 1 ))
        echo "  ssh_vps: retry ${attempt}/3 after rc=${rc}"
    done
}
DEST_BASE="rustfs:open-claw-push/vpn-portal-config"
STRONGSWAN_DEST_BASE="rustfs:open-claw-push/strongswan-configs"
# Secondary destination: Pi+USB at 192.168.20.10, /mnt/kopia-usb, SFTP via
# the [pi-usb] rclone remote. Best-effort: pi-usb failures warn, do not fail
# the script (rustfs always has the data).
PI_USB_BASE="pi-usb:/mnt/kopia-usb/openclaw-backups"
PI_USB_VPN_DEST_BASE="$PI_USB_BASE/vpn-portal-config"
PI_USB_STRONGSWAN_DAILY_BASE="$PI_USB_BASE/strongswan-configs/daily"
PI_USB_STRONGSWAN_WEEKLY_BASE="$PI_USB_BASE/strongswan-configs/weekly"
# YYYY-MM-DDTHHMM (single-level name) — gives each fire a unique subdir.
DEST="${DEST_BASE}/$(date -u +%Y-%m-%dT%H%M)"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
KEEP_N=2

MODE="LIVE"
[ "$DRY_RUN" = true ] && MODE="DRY-RUN"

echo "=== VPN portal config backup (mode: $MODE) ==="
echo "VPS: $VPS_HOST"
echo "Dest: $DEST"
echo "Tmp: $TMPDIR"
echo "Keep last: $KEEP_N snapshots"
echo ""

# 1. Pull secrets + certs (over SSH)
echo "[1/6] Pulling /etc/vpn-portal.env + Cloudflare cert/key..."
ssh_vps '
    sudo cat /etc/vpn-portal.env
' > "$TMPDIR/vpn-portal.env"
chmod 600 "$TMPDIR/vpn-portal.env"

ssh_vps '
    sudo cat /etc/ssl/cloudflare/databyte.co.za.crt
' > "$TMPDIR/databyte.co.za.crt"
chmod 644 "$TMPDIR/databyte.co.za.crt"

ssh_vps '
    sudo cat /etc/ssl/cloudflare/databyte.co.za.key
' > "$TMPDIR/databyte.co.za.key"
chmod 600 "$TMPDIR/databyte.co.za.key"

echo "  OK ($(wc -c < "$TMPDIR/vpn-portal.env") bytes env, $(wc -c < "$TMPDIR/databyte.co.za.crt") bytes cert)"

# 2. Pull live DB via sqlite3 .backup (atomic snapshot)
echo "[2/6] Snapshotting /var/lib/strongswan/ipsec.db..."
ssh_vps '
    sudo sqlite3 /var/lib/strongswan/ipsec.db ".backup /tmp/ipsec-backup.db"
    sudo cat /tmp/ipsec-backup.db
    sudo rm -f /tmp/ipsec-backup.db
' > "$TMPDIR/ipsec.db"
chmod 600 "$TMPDIR/ipsec.db"

echo "  OK ($(wc -c < "$TMPDIR/ipsec.db") bytes DB)"

# 3. Pull strongSwan swanctl configs (ADDED 2026-08-11 per msg #34088)
# This is the fix for today's outage: rw-eap.conf was truncated by 6 bytes
# (lost closing `}` for eap-rachid-iphone + `}` for secrets {} block) and
# there was no off-host backup. Pull both rw-eap.conf (primary) and
# rw-psk.conf (defensive) — same write hazard, same directory.
echo "[3/6] Pulling strongSwan swanctl configs..."
ssh_vps '
    sudo cat /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf
' > "$TMPDIR/rw-eap.conf"
chmod 600 "$TMPDIR/rw-eap.conf"

ssh_vps '
    sudo cat /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-psk.conf
' > "$TMPDIR/rw-psk.conf"
chmod 600 "$TMPDIR/rw-psk.conf"

# MD5 verify — catches mid-write corruption (the bug that caused today's outage).
# If the file is being written while we read it, the pulled md5 will differ
# from the live md5 we just computed separately.
RW_EAP_PULLED_MD5=$(md5sum "$TMPDIR/rw-eap.conf" | awk '{print $1}')
RW_EAP_LIVE_MD5=$(ssh_vps 'sudo md5sum /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf' | awk '{print $1}')

if [ "$RW_EAP_PULLED_MD5" != "$RW_EAP_LIVE_MD5" ]; then
    echo "  ⚠️  WARNING: rw-eap.conf md5 MISMATCH — pulled $RW_EAP_PULLED_MD5 vs live $RW_EAP_LIVE_MD5 — file may be mid-write"
    echo "  ACTION: investigate before next live run. Re-run --dry-run later to see if it stabilizes."
else
    echo "  OK (live md5 $RW_EAP_LIVE_MD5 = pulled md5 $RW_EAP_PULLED_MD5, $(wc -c < "$TMPDIR/rw-eap.conf") bytes rw-eap.conf, $(wc -c < "$TMPDIR/rw-psk.conf") bytes rw-psk.conf)"
fi

# 3b. Pull the extra config set (ADDED 2026-11-02, TKT-037 DR drill).
# The drill proved these were in NO backup relay and had to be hand-rebuilt to
# restore the stack: FreeRADIUS config, nginx conf.d, the portal->charon SSH
# key, the Telegram bot token, and the quota scripts. One tarball, rides into
# the same snapshot (rustfs + pi-usb).
echo "[3b/6] Pulling extra config set (freeradius / nginx / portal-ssh / bot-token / quota)..."
ssh_vps '
    sudo tar czf - -C / \
        etc/freeradius/3.0 \
        etc/nginx \
        var/lib/vpn-portal/.ssh \
        etc/databyte-vpn-bot \
        opt/strongswan-vpn-gateway/quota
' > "$TMPDIR/vpn-extra-config.tar.gz"
chmod 600 "$TMPDIR/vpn-extra-config.tar.gz"
echo "  OK ($(wc -c < "$TMPDIR/vpn-extra-config.tar.gz") bytes vpn-extra-config.tar.gz)"

# 4. Push to RustFS (vpn-portal-config) + pi-usb (parallel, best-effort)
echo ""
echo "[4/6] Pushing to $DEST..."
if [ "$DRY_RUN" = true ]; then
    echo "  SKIPPED (--dry-run mode)"
    echo ""
    echo "=== DRY-RUN: files that WOULD be pushed ==="
    ls -la "$TMPDIR/"
    echo ""
    echo "=== DRY-RUN: rw-eap.conf last 5 lines (verify closing braces present) ==="
    tail -5 "$TMPDIR/rw-eap.conf"
    echo ""
    echo "=== DRY-RUN: rw-eap.conf md5 (post-pull) ==="
    md5sum "$TMPDIR/rw-eap.conf"
    echo ""
    PI_USB_VPN_DEST="$PI_USB_VPN_DEST_BASE/$(date -u +%Y-%m-%dT%H%M)"
    echo "=== DRY-RUN: pi-usb would push to $PI_USB_VPN_DEST ==="
    rclone copy --dry-run "$TMPDIR/" "$PI_USB_VPN_DEST/" --quiet 2>&1 | head -3
    echo "  OK (dry-run)"
else
    rclone copy "$TMPDIR/" "$DEST/" --s3-no-check-bucket --quiet
    echo "  OK"
    # Parallel push to pi-usb (best-effort: warn on failure, do not fail script)
    PI_USB_VPN_DEST="$PI_USB_VPN_DEST_BASE/$(date -u +%Y-%m-%dT%H%M)"
    rclone copy "$TMPDIR/" "$PI_USB_VPN_DEST/" --quiet 2>&1 \
        && echo "  OK pi-usb -> $PI_USB_VPN_DEST" \
        || echo "  WARN pi-usb push failed (rc=$?) - rustfs still has it"
fi

# 5. Push strongswan-specific files to strongswan-configs destination
# Restores the daily+weekly backup that was silent since 2026-06-19
# (TKT-001). Uses rclone copyto so each day/week overwrites the prior
# archive (one file per day in daily/, one per week in weekly/).
# Files in tarball: ipsec.db, rw-eap.conf, rw-psk.conf (the strongswan-specific
# ones from steps 2 + 3, captured locally in TMPDIR).
echo ""
echo "[5/7] Pushing strongswan files to strongswan-configs (daily+weekly)..."

STRONGSWAN_DATE=$(date -u +%Y-%m-%d)
STRONGSWAN_WEEK=$(date -u +%Y-W%V)
STRONGSWAN_TARBALL="/tmp/strongswan-${STRONGSWAN_DATE}.tar.gz"

# Create tar.gz from TMPDIR strongswan files
tar -czf "$STRONGSWAN_TARBALL" -C "$TMPDIR" ipsec.db rw-eap.conf rw-psk.conf
chmod 600 "$STRONGSWAN_TARBALL"
echo "  OK ($(wc -c < "$STRONGSWAN_TARBALL") bytes tarball for date=$STRONGSWAN_DATE week=$STRONGSWAN_WEEK)"

STRONGSWAN_DAILY="${STRONGSWAN_DEST_BASE}/daily/strongswan-${STRONGSWAN_DATE}.tar.gz"
STRONGSWAN_WEEKLY="${STRONGSWAN_DEST_BASE}/weekly/strongswan-${STRONGSWAN_WEEK}.tar.gz"

if [ "$DRY_RUN" = true ]; then
    echo "  DRY-RUN: would push to rustfs:"
    echo "    daily:  $STRONGSWAN_DAILY"
    DOW=$(date -u +%u)
    if [ "$DOW" = "7" ]; then
        echo "    weekly: $STRONGSWAN_WEEKLY (today IS Sunday $DOW)"
    else
        echo "    weekly: $STRONGSWAN_WEEKLY (SKIPPED — only on Sundays, today=$DOW)"
    fi
    PI_USB_STRONGSWAN_DAILY="$PI_USB_STRONGSWAN_DAILY_BASE/strongswan-${STRONGSWAN_DATE}.tar.gz"
    echo "  DRY-RUN: pi-usb would push daily to $PI_USB_STRONGSWAN_DAILY"
    rclone copyto --dry-run "$STRONGSWAN_TARBALL" "$PI_USB_STRONGSWAN_DAILY" --quiet 2>&1 | head -3
    if [ "$DOW" = "7" ]; then
        PI_USB_STRONGSWAN_WEEKLY="$PI_USB_STRONGSWAN_WEEKLY_BASE/strongswan-${STRONGSWAN_WEEK}.tar.gz"
        echo "  DRY-RUN: pi-usb would push weekly to $PI_USB_STRONGSWAN_WEEKLY"
        rclone copyto --dry-run "$STRONGSWAN_TARBALL" "$PI_USB_STRONGSWAN_WEEKLY" --quiet 2>&1 | head -3
    fi
else
    rclone copyto "$STRONGSWAN_TARBALL" "$STRONGSWAN_DAILY" --quiet
    echo "  OK pushed to daily archive"
    DOW=$(date -u +%u)
    if [ "$DOW" = "7" ]; then
        rclone copyto "$STRONGSWAN_TARBALL" "$STRONGSWAN_WEEKLY" --quiet
        echo "  OK pushed to weekly archive (today IS Sunday)"
    else
        echo "  Skipping weekly (only on Sundays, today is day-of-week=$DOW)"
    fi
    # Parallel push to pi-usb (best-effort: warn on failure, do not fail script)
    PI_USB_STRONGSWAN_DAILY="$PI_USB_STRONGSWAN_DAILY_BASE/strongswan-${STRONGSWAN_DATE}.tar.gz"
    rclone copyto "$STRONGSWAN_TARBALL" "$PI_USB_STRONGSWAN_DAILY" --quiet 2>&1 \
        && echo "  OK pi-usb daily -> $PI_USB_STRONGSWAN_DAILY" \
        || echo "  WARN pi-usb daily push failed (rc=$?) - rustfs still has it"
    if [ "$DOW" = "7" ]; then
        PI_USB_STRONGSWAN_WEEKLY="$PI_USB_STRONGSWAN_WEEKLY_BASE/strongswan-${STRONGSWAN_WEEK}.tar.gz"
        rclone copyto "$STRONGSWAN_TARBALL" "$PI_USB_STRONGSWAN_WEEKLY" --quiet 2>&1 \
            && echo "  OK pi-usb weekly -> $PI_USB_STRONGSWAN_WEEKLY (today IS Sunday)" \
            || echo "  WARN pi-usb weekly push failed (rc=$?) - rustfs still has it"
    fi
fi

# 6. Verify + prune to last N snapshots
echo ""
echo "[6/7] Verifying + pruning (keep last $KEEP_N)..."
if [ "$DRY_RUN" = true ]; then
    echo "  SKIPPED (--dry-run mode)"
    echo ""
    # Show what would be pruned
    LSF_OUTPUT=$(rclone lsf "$DEST_BASE/" --dirs-only --max-depth 1 2>/dev/null || echo "")
    mapfile -t SNAPS < <(echo "$LSF_OUTPUT" \
        | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4})?/$" \
        || true \
        | sort)
    COUNT=${#SNAPS[@]}
    if (( COUNT > KEEP_N )); then
        DEL_COUNT=$((COUNT - KEEP_N))
        echo "  DRY-RUN: would prune $DEL_COUNT older (keeping last $KEEP_N)"
        for (( i=0; i<DEL_COUNT; i++ )); do
            d="${SNAPS[$i]%/}"
            echo "    would purge: $d"
        done
    elif (( COUNT > 0 )); then
        echo "  DRY-RUN: found $COUNT snapshots, <= $KEEP_N — no prune needed"
    else
        echo "  DRY-RUN: no dated snapshots found (unexpected)"
    fi
else
    rclone ls "$DEST/" 2>&1 | head -10
    echo ""

    # Prune older snapshots — keep only the most recent $KEEP_N dated subdirs.
    # Matches both old (YYYY-MM-DD/) and new (YYYY-MM-DDTHHMM/) formats so the
    # last-N rule applies across the schema transition.
    LSF_OUTPUT=$(rclone lsf "$DEST_BASE/" --dirs-only --max-depth 1 2>/dev/null || echo "")
    mapfile -t SNAPS < <(echo "$LSF_OUTPUT" \
        | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4})?/$" \
        || true \
        | sort)
    COUNT=${#SNAPS[@]}
    if (( COUNT > KEEP_N )); then
        DEL_COUNT=$((COUNT - KEEP_N))
        echo "  found $COUNT snapshots; pruning $DEL_COUNT older (keeping last $KEEP_N)"
        for (( i=0; i<DEL_COUNT; i++ )); do
            d="${SNAPS[$i]%/}"
            echo "    purge: $d"
            rclone purge "${DEST_BASE}/${d}" --quiet >/dev/null 2>&1 || true
        done
    elif (( COUNT > 0 )); then
        echo "  found $COUNT snapshots, <= $KEEP_N — no prune needed"
    else
        echo "  no dated snapshots found (unexpected)"
    fi
fi

# Prune pi-usb (mirror KEEP_N retention; best-effort)
if [ "$DRY_RUN" = true ]; then
    LSF_OUTPUT=$(rclone lsf --dry-run "$PI_USB_VPN_DEST_BASE/" --dirs-only --max-depth 1 2>/dev/null || echo "")
    mapfile -t SNAPS < <(echo "$LSF_OUTPUT" \
        | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4})?/$" \
        || true \
        | sort)
    COUNT=${#SNAPS[@]}
    if (( COUNT > KEEP_N )); then
        DEL_COUNT=$((COUNT - KEEP_N))
        echo "  DRY-RUN: pi-usb would prune $DEL_COUNT older (keeping last $KEEP_N)"
        for (( i=0; i<DEL_COUNT; i++ )); do
            d="${SNAPS[$i]%/}"
            echo "    would purge pi-usb: $d"
        done
    elif (( COUNT > 0 )); then
        echo "  DRY-RUN: pi-usb has $COUNT snapshots, <= $KEEP_N — no prune needed"
    else
        echo "  DRY-RUN: pi-usb no dated snapshots found (empty/new)"
    fi
else
    LSF_OUTPUT=$(rclone lsf "$PI_USB_VPN_DEST_BASE/" --dirs-only --max-depth 1 2>/dev/null || echo "")
    mapfile -t SNAPS < <(echo "$LSF_OUTPUT" \
        | grep -E "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4})?/$" \
        || true \
        | sort)
    COUNT=${#SNAPS[@]}
    if (( COUNT > KEEP_N )); then
        DEL_COUNT=$((COUNT - KEEP_N))
        echo "  pi-usb found $COUNT snapshots; pruning $DEL_COUNT older (keeping last $KEEP_N)"
        for (( i=0; i<DEL_COUNT; i++ )); do
            d="${SNAPS[$i]%/}"
            echo "    purge pi-usb: $d"
            rclone purge "${PI_USB_VPN_DEST_BASE}/${d}" --quiet >/dev/null 2>&1 || true
        done
    elif (( COUNT > 0 )); then
        echo "  pi-usb found $COUNT snapshots, <= $KEEP_N — no prune needed"
    else
        echo "  pi-usb no dated snapshots found (empty/new)"
    fi
fi
# 7. Prune strongswan-configs daily/weekly (14 daily + 8 weekly)
# Added 2026-10-20 (Misha). These two destinations have been pushed by step 5
# since 2026-06-19 with NO retention enforcement anywhere: the only prune code
# in this script touches DEST_BASE and PI_USB_VPN_DEST_BASE. MEMORY.md:979
# documented 14d + 8w via /usr/local/bin/strongswan-configs-backup.sh, which no
# longer exists on this box. 72 daily objects had accumulated unpruned.
# Rule is now enforced here so the documented policy is actually true.
SW_DAILY_KEEP=14
SW_WEEKLY_KEEP=8
echo ""
echo "[7/7] Pruning strongswan-configs (keep $SW_DAILY_KEEP daily + $SW_WEEKLY_KEEP weekly)..."

prune_swan() {
    # $1 label  $2 base path  $3 keep count  $4 filename regex  $5 rclone dry flag
    local label="$1" base="$2" keep="$3" flt="$4" dryf="$5"
    local LIST COUNT PRUNE N f
    LIST=$(rclone lsf "$base/" 2>/dev/null | grep -E "$flt" | sort || true)
    COUNT=$(printf '%s' "$LIST" | grep -c . || true)
    if [ "$COUNT" -eq 0 ]; then
        echo "  $label: empty/new - no prune needed"
        return 0
    fi
    if [ "$COUNT" -le "$keep" ]; then
        echo "  $label: $COUNT objects, <= $keep - no prune needed"
        return 0
    fi
    # SORT IS ASCENDING (oldest first). Delete the OLDEST COUNT-keep from the
    # HEAD. tail -n +N would delete the NEWEST instead (reversed retention).
    PRUNE=$(printf '%s\n' "$LIST" | head -n $((COUNT - keep)))
    N=$(printf '%s' "$PRUNE" | grep -c . || true)
    echo "  $label: $COUNT objects, keeping newest $keep, pruning $N"
    if [ "$DRY_RUN" = true ]; then
        printf '%s\n' "$PRUNE" | sed 's/^/    DRY-RUN would purge: /'
        return 0
    fi
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if rclone deletefile $dryf "$base/$f" --quiet >/dev/null 2>&1; then
            echo "    purged: $f"
        else
            echo "    WARN purge failed: $f"
        fi
    done <<< "$PRUNE"
}

prune_swan "rustfs daily " "$STRONGSWAN_DEST_BASE/daily" "$SW_DAILY_KEEP" \
    '^strongswan-[0-9]{4}-[0-9]{2}-[0-9]{2}\.tar\.gz$' ""
prune_swan "rustfs weekly" "$STRONGSWAN_DEST_BASE/weekly" "$SW_WEEKLY_KEEP" \
    '^strongswan-[0-9]{4}-W[0-9]{2}\.tar\.gz$' ""
prune_swan "pi-usb daily " "$PI_USB_STRONGSWAN_DAILY_BASE" "$SW_DAILY_KEEP" \
    '^strongswan-[0-9]{4}-[0-9]{2}-[0-9]{2}\.tar\.gz$' "--dry-run"
prune_swan "pi-usb weekly" "$PI_USB_STRONGSWAN_WEEKLY_BASE" "$SW_WEEKLY_KEEP" \
    '^strongswan-[0-9]{4}-W[0-9]{2}\.tar\.gz$' "--dry-run"
echo ""
echo "=== Done ($MODE) ==="
