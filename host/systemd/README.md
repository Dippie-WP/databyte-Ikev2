# Host-side systemd units

These run on the LXC 903 host (vpn-gateway), NOT inside the strongSwan container.

## Install

After deploying the `quota/` scripts to `/home/zunaid/strongswan/quota/` on the LXC host:

```bash
# Copy units into systemd's search path
sudo cp host/systemd/quota-schema.service /etc/systemd/system/
sudo cp host/systemd/quota-monitor.service /etc/systemd/system/
sudo cp host/systemd/strongswan-iptables-watchdog.service /etc/systemd/system/

# Copy watchdog script (referenced by ExecStart=)
sudo cp host/systemd/strongswan-iptables-watchdog.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/strongswan-iptables-watchdog.sh

# Reload systemd to pick up the new units
sudo systemctl daemon-reload

# Enable the schema unit (runs at host boot, idempotent — safe to re-run)
sudo systemctl enable --now quota-schema.service

# Enable the watchdog (re-applies rules.v4 on container restart)
sudo systemctl enable --now strongswan-iptables-watchdog.service

# Enable the quota monitor (long-running daemon)
sudo systemctl enable --now quota-monitor.service

# Verify
sudo systemctl status quota-schema.service
sudo systemctl status quota-monitor.service
sudo systemctl status strongswan-iptables-watchdog.service
sudo journalctl -u quota-monitor -n 50 --no-pager
```

## quota-schema.service (Phase 5B.1)

**What it does:** runs `apply_quota_schema.sh` once at host boot. The script is
idempotent (uses `CREATE TABLE IF NOT EXISTS`), so re-running on a DB that
already has the quota tables is a no-op.

**When to re-run manually:**
- After restoring an older DB backup (e.g., disaster recovery)
- After `rm /var/lib/strongswan/ipsec.db` (DB is recreated empty on next charon start)
- After pulling a fresh strongSwan image and reinitializing

**Manual apply (one-off):**
```bash
ssh zunaid@192.168.10.98
sudo systemctl start quota-schema.service
# or directly:
bash /home/zunaid/strongswan/quota/apply_quota_schema.sh
```

## quota-monitor.service (Phase 5B.3)

**What it does:** long-running Python process that:
1. Polls `swanctl --list-sas` every 60s
2. For each active SA: reads iptables-legacy byte counter for that VIP
3. Resolves VIP → leases.address → leases.identity → users.id → devices.strongswan_user_id → customers.id
4. Skips operator (is_operator=1) and inactive customers
5. Computes delta = counter_now - last_session_value, adds to customers.data_used_bytes
6. At 80% threshold: logs alert to `alerts` table (Telegram DM to be added in 5C.3)
7. At 100% threshold: terminates SA via `swanctl --terminate --ike-id <num> --force`, replaces EAP secret in `rw-eap.conf` with `KILLED-<random>`, reloads charon, sets `over_quota=1`, logs alert + audit_log

**Source of truth:**
- "Who's connected" = `swanctl --list-sas` (NOT charon `leases` table — stale on re-acquire)
- "How much data" = iptables-legacy per-VIP ACCEPT counter in FORWARD chain
- "Customer" = DB join users.name → devices.strongswan_user_id → customers.id

**Dependencies:** `quota-schema.service` (tables exist), `strongswan-vpn.service` (charon up), `strongswan-iptables-watchdog.service` (rules.v4 with per-VIP counters loaded).

**Test bed state:** demo-customer (`data_limit_bytes=104857600` = 100 MiB), zun-operator (is_operator=1, no cap).

## strongswan-iptables-watchdog.service (Phase 5B.2 + 5B.6 fix)

**What it does:** watches the strongSwan container via `docker events`. On container lifecycle events, re-applies `/etc/iptables/rules.v4` to ensure the per-VIP quota counters and the MSS clamp survive any external rule flush.

**⚠️ 5B.6 — DO NOT trigger on every docker event.** Earlier version re-applied on `exec_create`/`exec_start`/`health_status*` — which fired on every Prometheus scrape (30s) and every quota-monitor poll (60s), **resetting all 508 per-VIP byte counters to 0**. That was the bug Zun caught when iOS app showed 140 MB but daemon showed 22 MB.

**Correct case statement (as of 5B.6 fix, 2026-06-19 19:48 UTC):**
```bash
case "$action" in
  start|restart|unpause|die|stop|kill|oom)
    sleep 1
    $RULES_BIN $RULES >/dev/null 2>&1 && logger -t strongswan-watchdog "rules re-applied after $action at $time"
    ;;
esac
```

**Why this is correct:** iptables-legacy `restore` does NOT preserve byte counters. Any re-apply wipes them. The watchdog exists to recover from external events (Docker daemon restart, manual flush, etc.) — NOT from internal polling or scraping. Match only on actual container lifecycle events.

**Future migration to nftables:** nftables named counters persist across `nft flush ruleset` reloads. A 5B.6-style bug couldn't happen with nftables. Migration is on the v1.3 backlog (~2-3h work).

**Verify the fix is in place:**
```bash
cat /usr/local/bin/strongswan-iptables-watchdog.sh
# Should show: case "$action" in start|restart|unpause|die|stop|kill|oom)
# NOT: case "$action" in start|restart|unpause|attach|exec_create|exec_start|health_status*)

# Trigger test: 3 docker exec calls in a row should NOT wipe counter
iptables-legacy -L FORWARD -nvx 2>&1 | grep "quota:10.99.0.5" | head -2
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas >/dev/null
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --stats >/dev/null
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas >/dev/null
iptables-legacy -L FORWARD -nvx 2>&1 | grep "quota:10.99.0.5" | head -2
# Counter should have ACCUMULATED (not been wiped to 0)
```

## 5A.7 MSS Clamp — APPLIED 2026-06-19 14:18 UTC

Lives in `*mangle` section of `/etc/iptables/rules.v4`. Forces 5G clients to advertise 1260-byte MSS so server responses fit through the CGNAT path. Used `quota/install_mss_clamp.sh` (idempotent):

1. Applies `iptables-legacy -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260` in memory
2. Edits `/etc/iptables/rules.v4` directly to add/keep a single `*mangle` section with TCPMSS
3. Removes any duplicate `*mangle` sections (`iptables-restore` uses the FIRST occurrence of each table)
4. Survives `strongswan-iptables-watchdog` re-applies

Verified:
- 1 `*mangle` section in `rules.v4` (was 0)
- TCPMSS rule in mangle FORWARD in memory
- Survives strongSwan container restart (watchdog test PASS)
- Per-VIP quota rules (508) still in `*filter` FORWARD

5G test symptoms this fixes:
- TCP handshake completes
- Data transfer hangs (no progress)
- ICMP "fragmentation needed" silently dropped by CGNAT
- StrongSwan app shows connected but no traffic flows

## Cut event audit trail

When quota-monitor fires a 100% cut, it does the following (in order):
1. Backs up current `rw-eap.conf` to `/home/zunaid/strongswan/swanctl/conf.d/.backups/rw-eap.conf.bak-quotamon-<epoch>`
2. Replaces the customer's EAP secret in `rw-eap.conf` with `KILLED-<16 hex chars>`
3. Calls `swanctl --load-creds` to reload charon
4. Calls `swanctl --terminate --ike-id <num> --force` to terminate the active SA
5. Sets `customers.over_quota = 1` in DB
6. Inserts row into `alerts` table (threshold=100, data_used_bytes_at_alert)
7. Inserts row into `audit_log` table (actor=quota-monitor, action=cut, target=demo-phone)

**To restore demo state after a cut:**
```bash
# On LXC 903 host
LATEST_BAK=$(ls -t /home/zunaid/strongswan/swanctl/conf.d/.backups/rw-eap.conf.bak-quotamon-* | head -1)
sudo cp "$LATEST_BAK" /home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf
sudo docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds
sqlite3 /var/lib/strongswan/ipsec.db "UPDATE customers SET data_used_bytes=0, over_quota=0 WHERE name='demo-customer';"
sudo iptables-legacy -Z FORWARD
sudo rm -f /var/run/quota-monitor.session
sudo systemctl restart quota-monitor
```

Or use the helper script: `bash /home/zunaid/strongswan/quota/reset_demo.sh`

## ⚠️ strongswan-starter.service — MUST be DISABLED (v1.2.1 fix)

The Debian strongSwan package installs a `strongswan-starter.service` that
starts a *host* charon process (`/usr/lib/ipsec/charon`). This host charon
binds to UDP 500/4500 during LXC boot, which then prevents the
**container** charon (the one we actually configure) from binding those
ports. Result: container charon boots with "no socket implementation
registered" and rejects all incoming IKE_SA_INIT with N(NO_PROP).

We never use the host charon — we run our own charon inside the
strongSwan Docker container. The fix is to disable (not mask) the host
starter so it can never start, but other `systemctl` operations are
unaffected.

```bash
sudo systemctl stop strongswan-starter
sudo systemctl disable strongswan-starter
sudo systemctl is-enabled strongswan-starter   # → disabled
sudo systemctl is-active strongswan-starter    # → inactive
```

**Verify the fix:**

```bash
ps -ef | grep -E "charon|ipsec" | grep -v grep
# should show ONLY ./charon (the container's) — NOT /usr/lib/ipsec/charon

ss -ulnp | grep -E ":500|:4500"
# should show the container charon's PID, not 252
```

**Why this is in v1.2.1:**

The first post-reboot test (2026-06-20 09:55) showed host charon (PID 252)
rejecting all connection attempts with N(NO_PROP) for 14 minutes while
the container charon couldn't bind. iPhone backed off after repeated
failures and wouldn't reconnect. Once the host charon was stopped and
the container charon rebound, the iPhone reconnected in 6 seconds.

If you ever see `unable to bind socket: Address already in use` in
`/var/log/charon-log` inside the container, check this first.
