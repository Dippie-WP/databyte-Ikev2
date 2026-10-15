# ARCHITECTURE

> **⚠ Live deployment context (updated 2026-10-15, v2.2.1 portal + v2.5.0 bot fact-check):** Production runs on the Xneelo VPS **`vpn-prod-01`** (renamed from `vps-01` per HOT-210, 2026-09-30), 154.65.110.44, `myvpn.databyte.co.za`. The 🟢 verified-live table at the top is the canonical reference for the current prod state. **The body of this file (below the verified-live table + "Key architecture shifts" table) is the legacy v1.x LXC 903 lab architecture** — preserved for historical context; do not use it as a guide for current state. For DR/HA procedure see `docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0 (2026-10-15). The `docs/VPS-XNEELO-DEPLOY.md` reference is stale (file removed; use `scripts/bootstrap-xneelo.sh` instead). Lab (LXC 903 @ 192.168.10.98) and prod (VPS @ 154.65.110.44) are intentionally separate — no sync, never will be.

## 🟢 Verified-live 2026-10-15 (v2.2.1 portal + v2.5.0 bot production state on vpn-prod-01)

The following are live-verified facts from `ssh root@vpn-prod-01` at 06:50 UTC / 08:50 SAST, cross-checked against `git rev-parse HEAD` on origin/main = `b3cf38b9d22f87aa4f127d84b06f81e118c90bbc` (no remote tags on databyte-Ikev2; **bot** tags live in `vpn-admin-bot` repo: latest **v2.5.0**, HEAD `01ad543`). For the full service + port inventory see `docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0 §1.2 + §1.3.

| Aspect | Live state | Notes |
|---|---|---|
| **Image** | `zun/strongswan:6.0.7-mschapv2-attrsql` | Unchanged since v1.x; only operator-overlay config evolved |
| **Container ID** | `Up 6 days (healthy)` | Started **2026-10-08T14:06:34Z** (audit time); bind mounts: 7 paths from `/opt/strongswan-vpn-gateway/docker/...` |
| **charon daemon** | PID 165329, UDP/500 + UDP/4500 listening on `0.0.0.0` + `[::]` | EAP-MSCHAPv2 via FreeRADIUS (NOT local SQLite) |
| **VICI socket** | TCP `127.0.0.1:4502` (container-internal, host-readable via `docker exec`) | unchanged |
| **Quotas** | **nftables** (`nft list ruleset \| grep quota` → `table inet quota_table`) | Per-VIP packets/bytes counters in nftables (NOT iptables-legacy). Per Phase 7.5 switch |
| **Identity store** | **MariaDB 11.8.6-MariaDB-0+deb13u1** at 127.0.0.1:3306, db `radius` (post-Phase-4E cutover) | **45 tables** (was 42): 7 RADIUS-protocol + ~38 daloRADIUS-mirrored business (`customers`, `users`, `devices`, `installer_tokens`, `tiers`, etc.). **Live row counts (2026-10-15):** `radcheck=26`, `radpostauth=2,299`, `radacct=1,166`, `customers=11`, `users=11`, `devices=10`, `operators=1` |
| **FreeRADIUS** | active, listening 1812/1813/18120 + CoA at 3799 | Connected to charon via `eap-radius.conf` (binds UDP/1812+1813 to charon in container) |
| **Portal (FastAPI)** | **`v2.2.1`** (deployed 2026-08-10, git HEAD `8e02a6e`) | systemd Description verified. **NOTE:** FastAPI `app.version` string on line 141 still says `"2.1.1"` — cosmetic drift from systemd Description. Code SHAs: `app.py` 3,266 lines (`baca20f7e9c08691a06ad4c3d2e4bec92e40cbb6d6235288f51bce774318be2b`), `bot.py` 1,988 lines (`8cbda46e3aa073841dc429856488e0b5b24d21818ccb275668dcd5882c881042`) |
| **Bot (Telegram)** | **`v2.5.0`** (HEAD `01ad543`) | Runs as `bot-polling.service` (sole polling source per HOT-261); never start a second `bot.py` polling instance — HTTP 409 Conflict from Telegram. Tag `v2.5.0` is in `vpn-admin-bot` repo, not `databyte-Ikev2`. |
| **Services running** | **18 services** (incl. 7 new since 2026-07): `apache2`, `bandwidth-monitor` ⚠ STALE, `bot-polling`, `cron`, `dockhand-bridge`, `freeradius`, `mariadb`, `nginx`, `prometheus`, `prometheus-node-exporter`, `quota-exporter`, `quota-monitor`, `strongswan_exporter`, `strongswan-watchdog`, `ttyd`, `vpn-portal`, `vps-alert-relay`, `zerotier-one` + `alertmanager` (user-mode). | See RUNBOOK §1.2 for the full ExecStart + backup-coverage table. |
| **Listening ports** | TCP: 22, 80, 443, 2384, 3306, 4502, 5355, 7681, 8000, 9101, 9102, 9999. UDP: 500, 4500, 1812, 1813, 18120, 3799, 5355, 9993, 44062, 51820. | **6 new ports since 2026-07:** `4502` (VICI, was missing from prior table), `7681` (ttyd), `9999` (ops python), `9993` (ZeroTier), `44062` (ZeroTier per-iface), `51820` (kernel wg0). See RUNBOOK §1.3. |
| **radpostauth** | **2,299** rows | Live MSCHAPv2 with migrated NTLM hashes from charon SQLite. **~6× growth** since 2026-07 (was 390) |
| **radacct** | **1,166** rows | Live interim-updates from charon eap-radius `accounting = yes` (added v2.2.0, 805ea84). **~230× growth** since 2026-07 (was 5) |
| **customers** | **11** rows | Live portal business data (was 5 at Phase 4E baseline; +6 since) |
| **charon/customer identity source** | **NOW**: FreeRADIUS with `SELECT id FROM users WHERE name='%{User-Name}'` SQL module | Was: SQLite at `/var/lib/strongswan/ipsec.db` via attr-sql |
| **Quota enforcement (script location)** | `quota-monitor.py` lives at `/opt/strongswan-vpn-gateway/quota/` (35,898 B, Oct 12) | **INVERTED** from v1.x — `/home/zunaid/strongswan/quota/` is now a `.bak` graveyard. See RUNBOOK §1.6 gap #1. |
| **bandwidth-monitor.service** | `ExecStart=/usr/bin/python3 /home/zunaid/strongswan/quota/bandwidth-monitor.py` | ⚠ **STALE PATH** — running on `.bak-pre-phase8-2026-07-12` since 2026-10-08 (3-month-old code). Canonical: `/opt/.../quota/bandwidth-monitor-nft.py`. See RUNBOOK §1.6 gap #14 + §2.3 step 11a. |
| **LE certs** | `myvpn.databyte.co.za` (SAN: `myvpn` + `vpn-portal`), `adminer.databyte.co.za` | Both ECDSA, expiry **2026-12-02** (48 days valid). `certbot certonly --nginx` style. |
| **Backup** | kopia → PBS (homelab) + RustFS (docs push only) | kopia runs per-host; doc push only via rclone. **⚠ 2026-10-15: kopia 502 Bad Gateway from `kop.databyte.co.za:443`** (parked; Zun msg #43961); last successful `.update-info.json` mtime 2026-10-10 00:00. **RPO at risk** until resolved. |
| **ACCOUNTING CHAIN** | Wired end-to-end (v2.2.0) | See `host/freeradius/` overlay + `provision-freeradius.sh` |

## Key architecture shifts since the v1.x design (locked forward by v2.2.0)

| Was (v1.x) | Is now (v2.2.x) | Commit |
|---|---|---|
| SQLite at `/var/lib/strongswan/ipsec.db` (portable via bind-mount) | MariaDB `radius` database with `customers`/`users`/`devices`/`installer_tokens`/etc. tables (single source of truth) | Phase 4E `cb9bf69` (2026-07-12) |
| iptables-legacy per-VIP byte counters in FORWARD | nftables named counters in `inet quota_table` | Phase 7.5 / applied 2026-07-09 |
| charon's local `users`/`pools`/`leases` (SQLite) | FreeRADIUS `radcheck`/`radusergroup` (MariaDB), charon eap-radius plugin proxies auth | Phase 5 `a9d2527` (2026-07-06) |
| `/etc/freeradius/3.0/` with default Debian config (no operator overlay) | `host/freeradius/` overlay dir + `provision-freeradius.sh` | v2.2.0 `805ea84` (2026-07-13) |
| Quota enforcement via iptables counter + ipsec connection kill | Quota enforcement via nftables counter + RADIUS identity DISABLE + RFC 5176 Disconnect-Request (`3799/udp` → charon) | Phase 5+ `b00b8e` (2026-07-06) |
| `curl https://vpn-portal.databyte.co.za/` for portal | same URL, now powered by **portal v2.2.1** (FastAPI + MariaDB `radius` DB) | Phase 4C `09e3cfe`; v2.2.1 deployed 2026-08-10 (HEAD `8e02a6e`) |
| `/home/zunaid/strongswan/quota/quota-monitor.py` (LXC 903 lab only) | live at `/opt/strongswan-vpn-gateway/quota/quota-monitor.py` on VPS (script location **INVERTED** vs v1.0.7 docs — see RUNBOOK §1.6 gap #1) | phase-by-phase, see ROADMAP.md |
| **(new in v2.5.0)** No Telegram bot integration | **`vpn-admin-bot v2.5.0`** (HEAD `01ad543`) — `@Databytevpn_bot` runs as `bot-polling.service`, sole polling source per HOT-261; chat_id 7748884597 (Zun) | 3 amendments on 2026-10-09 (TKT-029 layered block + `/sessions` + `/customers` + `/logs`); `setMyCommands` fix 2026-10-14 (HOT-241) |
| **(new in v2.5.x)** No automatic swanctl config reload | **`strongswan-watchdog.service`** (HOT-66 fix) — polls `/opt/.../docker/swanctl/conf.d/` every 3s, runs `swanctl --load-all` on change. Renamed from `strongswan-iptables-watchdog.service`. | HOT-66 |
| **(new in v2.5.x)** kopia 4-7h cron drift | **kopia 502 Bad Gateway** from `kop.databyte.co.za:443` (2026-10-15 audit) — RPO at risk, parked (Zun msg #43961) | TBD |

The remainder of this file, below, is the **legacy v1.x lab architecture** (LXC 903 192.168.10.98). It is preserved for historical context and as the design from which the above shifts were derived. DO NOT use it as a guide for current state — every claim about "the system" should be cross-checked against the 🟢 verified-live table above.


## Network topology

```
[5G phone / iPhone / friend laptop]
        │ (UDP 500, UDP 4500, ESP)
        │ over internet
        ▼
[Router 192.168.10.29]
   public IP 102.182.117.43
   port-forwards UDP 500 + 4500 → 192.168.10.98
        │
        ▼
[LXC 903 @ pve2 — 192.168.10.98 (vpn-gateway)]
   ├── sysctl.d/99-strongswan.conf (ip_forward=1)
   ├── netfilter-persistent (loads /etc/iptables/rules.v4)
   ├── strongswan-iptables-watchdog.service (re-applies on container restart only)
   │
   ├── [iptables-legacy FORWARD chain — 508 per-VIP ACCEPT rules]
   │     ├── quota:10.99.0.5  ACCEPT out (bytes counter, customer demo-phone)
   │     ├── quota:10.99.0.5  ACCEPT in  (bytes counter, customer demo-phone)
   │     ├── quota:10.99.0.50 ACCEPT out (bytes counter, customer zun-iphone)
   │     ├── ... (254 outbound + 254 inbound rules = 508 total)
   │     └── *mangle FORWARD: TCPMSS --set-mss 1260 (5A.7 — 5G PMTUD fix)
   │
   ├── [iptables-legacy *nat POSTROUTING]
   │     └── MASQUERADE for 10.99.0.0/24
   │
   ├── [Docker container: strongswan (network_mode: host)]
   │     ├── charon daemon (swanctl, VICI on 127.0.0.1:4502)
   │     ├── filelog → /var/log/charon-log-host/charon.log (bind-mounted)
   │     └── SQLite at /var/lib/strongswan/ipsec.db (bind-mounted)
   │             ↕ daily 03:00 UTC backup
   │             ▼
   │     [RustFS bucket: open-claw-push/strongswan-db/]
   │          ├── daily/   (14d retention, ISO date slot)
   │          └── weekly/  (8w retention, ISO week slot)
   │
   ├── [quota-monitor.service (Phase 5B.4)]
   │     ├── /usr/bin/python3 /home/zunaid/strongswan/quota/quota-monitor.py
   │     ├── Polls every 60s
   │     ├── Reads iptables counters for each active VIP
   │     ├── Looks up customer in DB
   │     ├── At 80%: log alert to DB
   │     ├── At 100%: terminate SA, kill EAP secret in rw-eap.conf, reload charon
   │     └── Updates customers.data_used_bytes from counter deltas
   │
   └── [quota-schema.service (Phase 5B.1)]
         ├── /home/zunaid/strongswan/quota/apply_quota_schema.sh
         └── Runs once at host boot, idempotent

[Optional sidecar: strongswan_exporter @ :9101]
   metrics → Prometheus → Grafana (dashboard: strongswan-v1-2)
```

## What runs where

### LXC 903 host (192.168.10.98, vpn-gateway)
- **netfilter-persistent** — loads `/etc/iptables/rules.v4` (includes the per-VIP quota counters + MSS clamp)
- **strongswan-iptables-watchdog.service** — re-applies `rules.v4` on container restart (NOT on every docker exec — see 5B.6 gotcha)
- **systemd sysctl** — `/etc/sysctl.d/99-strongswan.conf` enables forwarding
- **systemd quota-monitor** — long-running quota daemon, 60s poll
- **systemd quota-schema** — oneshot at host boot, applies DB schema
- **cron** — daily 03:00 UTC DB backup to RustFS
- **strongswan_exporter** (host process) — VICI → :9101 Prometheus metrics

### Docker container (strongswan, network_mode: host)
- **charon daemon** — IKEv2 + EAP-MSCHAPv2 + PSK fallback
- **swanctl** — config loader (VICI, TCP 127.0.0.1:4502)
- **attr-sql plugin** — per-user VIP pinning via SQLite
- **Filesystem:**
  - `/etc/swanctl/` (read-only bind-mount of `docker/swanctl/`)
  - `/etc/strongswan.d/*.conf` (file-level bind-mounts of `docker/strongswan.d/`)
  - `/etc/strongswan.conf` (baked into image)
  - `/var/lib/strongswan/ipsec.db` (bind-mount from LXC host)
  - `/var/log/charon-log-host/charon.log` (bind-mount to LXC host)

### Client (5G phone, iPhone, friend laptop)
- **Android strongSwan app** (works) — uses `.sswan` profile with CA cert + EAP creds
- **iOS strongSwan app** (works) — iOS native VPN + `.mobileconfig` is broken for EAP-MSCHAPv2 on iOS 18+
- **Windows / macOS strongSwan** (untested) — should work with same `.sswan` profile

## Data flow (single TCP connection example)

1. Phone opens `https://example.com` in Chrome
2. TCP SYN from phone (10.99.0.5) → server's strongSwan endpoint
3. SYN-ACK back, handshake done over UDP 4500 + ESP
4. Chrome sends HTTP GET (small, fits MSS)
5. example.com responds with HTML + assets
6. Server's LXC receives response, MASQ rewrites src=192.168.10.98
7. **MSS clamp (5A.7) on FORWARD ensures response packets are ≤ 1260 bytes**
8. **Per-VIP counter (5B.2) increments: `quota:10.99.0.5` ACCEPT in/out bytes**
9. LXC ESP-encapsulates response → UDP 4500 → phone
10. Phone's strongSwan app decapsulates → delivers to Chrome
11. Chrome renders
12. **60s later: quota-monitor (5B.3) reads counter delta, adds to demo-customer.data_used_bytes**
13. **At 80%: daemon logs WARN to alerts table**
14. **At 100%: daemon terminates SA + kills EAP secret in rw-eap.conf + reloads charon**

## Why these specific design choices

### network_mode: host (NOT bridge)
IKE (UDP 500/4500) and ESP (proto 50) require direct host access. Bridge mode would require hairpin NAT and break MOBIKE. The downside is the container shares the LXC's network namespace — acceptable for trusted LAN; if exposing beyond, add firewall rules limiting source ranges to known good clients.

### Docker image built fresh per host (NOT pushed to registry)
The build is ~5 min and the image is tied to a specific strongSwan version. Pushing to a registry adds operational overhead with no real benefit at this scale. We can add `ghcr.io` later if a 3rd host deployment makes the build time matter.

### ~~SQLite (NOT Postgres, NOT MySQL)~~ — STALE 2026-07-12 (Phase 4E)

The 2026-06 discussion chose SQLite for the charon-stack (small, low-write, single charon writer). This was correct for **charon internal state** (addresses, ike_sas, pools — unchanged). For **portal business state** (customers, devices, users), the design shifted in Phase 4E (commit `cb9bf69`, 2026-07-12) to **MariaDB 11.x at 127.0.0.1:3306, db `radius`**. Both coexist now: MariaDB holds business + RADIUS-protocol tables; SQLite at `/var/lib/strongswan/ipsec.db` holds ONLY charon-internal tables. See "🟢 Verified-live" table at top of file.

### ~~iptables-legacy (NOT nftables) for quota counters~~ — STALE 2026-07-09 (Phase 7.5)

This section described the LXC 903 lab design (pre-July 9). For VPS production, we **did migrate** to nftables in Phase 7.5: live `nft list ruleset | grep quota` returns `table inet quota_table { ... }` with named counters that persist across `nft flush ruleset`. iptables-legacy was the right choice for the lab; nftables is the right choice for production with the 5B.6-style bug class as a permanent category. The 5B.6 fix (narrow watchdog case statement) remains as defense-in-depth even though it no longer fires on VPS.

### Self-signed CA (NOT Let's Encrypt)
LE is what v1.3 will use. For v1.0/v1.1, the Android strongSwan app handles self-signed CAs fine when the CA is installed as a user CA. iOS is broken either way. The setup is:
1. Generate self-signed CA (10y)
2. Generate server cert signed by CA (1y, RSA-2048 + PKCS#1 v1.5 for iOS compat)
3. Install CA on Android via `Settings → Security → Install from storage`
4. In strongSwan app, profile uses server's `vpn.homelab.local` ID + the installed CA

### Sticky VIP via attr-sql (NOT hard pin)
Upstream attr-sql doesn't enforce hard pinning. The pattern we use: pre-insert the address row in `addresses` table with `identity=X, released=0`. charon picks it up and re-uses. But if the row gets deleted (e.g., charon crash recovery), the next user may get that VIP. v1.3 will add a custom plugin for hard pin.

## Quota layer (Phase 5B — ✅ GREEN, v1.1.0)

### How it works (one-liner)

Every 60 seconds, a Python daemon reads the iptables-legacy per-VIP byte counter for every active VPN session, looks up the customer in the DB, increments their `data_used_bytes`. At 80% it logs a warning. At 100% it terminates the IKE_SA, kills the EAP secret in `rw-eap.conf` (replace with `KILLED-<random>`), reloads charon, and marks the customer `over_quota=1`. Re-authentication is blocked because the secret is now dead.

### Data model

The quota layer extends the strongSwan attr-sql SQLite DB with 6 new tables:

```
strongSwan upstream tables (30):
  users          ← identity + EAP password (DEAD data — secrets live in rw-eap.conf)
  pools          ← IP pool definitions (start/end BLOB)
  leases         ← active VIP assignments (address BLOB, identity → users.id)
  user_pools     ← user ↔ pool junction
  ...

Quota layer tables (6 new, additive):
  tiers          ← catalog: name, data_limit_bytes, price_zar, is_active
  customers      ← per-user: name, is_operator (bypass), tier_id, data_limit_bytes,
                  data_used_bytes, over_quota (1=hard cut), is_active
  devices        ← strongSwan user ↔ customer link (strongswan_user_id → users.id)
  purchases      ← audit log: each top-up event (customer_id, data_added_bytes)
  alerts         ← threshold events: 80%/100% fired (customer_id, threshold, ts)
  audit_log      ← admin actions: create/extend/suspend/reset
```

**VIP resolution chain** (at quota-monitor query time):
```
iptables-legacy counter (bytes per VIP, in FORWARD chain)
  → leases.address (BLOB) → leases.identity (= users.id)
    → devices.strongswan_user_id (= users.id)
      → devices.customer_id
        → customers.tier_id → tiers.data_limit_bytes
        → customers.data_limit_bytes (effective limit = tier + manual extensions)
        → customers.is_operator (bypass flag)
```

**Key design decisions (locked 2026-06-19):**
- Operator (Zun): `is_operator=1`, bypasses ALL quota checks, no tier, no cap
- Customers: 2 simultaneous connections per account (enforced at iptables layer)
- Quota is shared across all devices of one customer (combined `data_used_bytes`)
- Per-purchase model: 100% = hard cut, manual extension by operator after payment
- No calendar/rolling cycle — `data_limit_bytes` is manual + tier-based
- **Counter = iptables-legacy byte counters in FORWARD chain, NOT nftables, NOT charon `leases` table**
- **Source of truth for "who's connected" = `swanctl --list-sas`** (not charon `leases` table — stale on re-acquire)
- **Source of truth for "how much data" = iptables-legacy FORWARD counter** (per-VIP ACCEPT rules)
- **Kill credentials at 100% via `rw-eap.conf` mutation, NOT DB** (see ADR `5B-credentials-kill.md`)

### Tiers (seeded in 5B.1, **rewritten 2026-06-22** for 5D pre-commercial lineup)

| Name | Display | data_limit_bytes | Price | Status |
|------|---------|------------------|-------|--------|
| tier_5gb  | 5 GB  |  5,368,709,120 | $3 USD | Active, for sale — Tier 1 |
| tier_10gb | 10 GB | 10,737,418,240 | $5 USD | Active, for sale — Tier 2 |
| tier_20gb | 20 GB | 21,474,836,480 | $8 USD | Active, for sale — Tier 3 |
| demo_100mb | Demo 100 MB | 104,857,600 | — | Persistent demo (Zun resets after each demo) |

**Operator:** `zun-operator` (is_operator=1, no tier, unlimited bypass)
**Demo customer:** `demo-customer` (tier=4, 2 devices, hard cap at 100 MiB for testing)

### Components

```
[LXC 903 host]
  iptables-legacy FORWARD chain (508 per-VIP ACCEPT rules, byte counters)
  quota-monitor.py          ← reads counters + DB → alert/cut decisions
  quota-monitor.service     ← systemd unit, 60s poll
  quota-schema.service      ← oneshot at boot, applies schema (idempotent)
  strongswan-iptables-watchdog.service ← re-applies rules.v4 on container restart
  strongswan-iptables-watchdog.sh     ← script (FIXED 5B.6 — only on actual lifecycle events)

[strongSwan container]
  charon                    ← VICI on TCP 127.0.0.1:4502
  ipsec.db                 ← bind-mount from host, shared with LXC + quota-monitor
  rw-eap.conf              ← conf-driven EAP secrets, killed at 100% by quota-monitor

[Customer-facing (5C, future)]
  vpn-bot.py               ← Telegram bot: auth + buy-more relay + 80%/100% DMs
  customer web page         ← FastAPI: usage bar, device list, "buy more" button
  admin web page            ← FastAPI /admin: customer mgmt, cred gen, quota extend

[Operator monitoring]
  Grafana (existing)        ← operator-only: system health + all users overview
  vpn-quota dashboard      ← 5C.4: per-customer usage, active SAs, alert history
```

### quota-monitor.py logic (5B.3 — full flow)

```
every 60s:
  for each active SA in `swanctl --list-sas`:
    parse VIP (10.99.0.X) from the SA
    read iptables counter for VIP from FORWARD chain
    if counter is 0: skip (no traffic)
    resolve VIP → leases.address → leases.identity → users.id
                 → devices.strongswan_user_id → devices.customer_id
                 → customers row
    if customer.is_operator: skip (operator bypass)
    if not customer.is_active: skip (suspended)
    if customer.over_quota: skip (already cut)
    compute delta = current_counter - last_session_counter
    if delta > 0: customer.data_used_bytes += delta
    pct = 100 * customer.data_used_bytes / customer.data_limit_bytes
    if pct >= 100 and not over_quota:
      CUT: terminate SA (--terminate --ike-id <id> --force)
           replace EAP secret in rw-eap.conf with KILLED-<random>
           swanctl --load-creds
           customer.over_quota = 1
           log alert (threshold=100)
           log audit (action='cut', target=demo-phone)
    elif pct >= 80 and no prior 80% alert for this customer:
      WARN: log alert (threshold=80)
            log audit (action='warn', target=demo-phone)
  sleep 60s
```

**Session sidecar** at `/var/run/quota-monitor.session` tracks per-customer last counter value for delta computation. Cleared on daemon restart (intentional re-baseline).

### iptables-legacy accounting rules (5B.2)

Per-VIP ACCEPT rules in FORWARD chain. 254 outbound (src=VIP) + 254 inbound (dst=VIP) = 508 total rules. Each rule has a byte counter that monotonically increases (until `iptables-restore` resets it).

```
*filter
:FORWARD ACCEPT [0:0]
-A FORWARD -s 10.99.0.5 -j ACCEPT -m comment --comment "quota:10.99.0.5"
-A FORWARD -d 10.99.0.5 -j ACCEPT -m comment --comment "quota:10.99.0.5"
-A FORWARD -s 10.99.0.6 -j ACCEPT -m comment --comment "quota:10.99.0.6"
-A FORWARD -d 10.99.0.6 -j ACCEPT -m comment --comment "quota:10.99.0.6"
... (248 more VIPs)
*nat
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s 10.99.0.0/24 -j MASQUERADE
*mangle
:FORWARD ACCEPT [0:0]
-A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260  (5A.7)
```

**Why ACCEPT (not RETURN):** iptables-legacy is first-match. We want to count bytes then fall through to default. ACCEPT terminates the rule chain but matches our policy (default ACCEPT). RETURN would re-evaluate later rules.

**Why per-VIP rules (not per-customer):** iptables has no concept of "customer." The VIP IS the customer identifier from the network's perspective. We resolve VIP → customer in the daemon.

**Why 254 not 256:** the .1 and .255 are reserved (network + broadcast). The pool is 10.99.0.0/24, so usable range is 10.99.0.2 through 10.99.0.254 (253 IPs). 253 × 2 directions = 506, plus 2 reserved = 508.

### 5B.6 — the watchdog bug (FIXED 2026-06-19 19:48 UTC)

**Symptom:** iOS app showed 140 MB pushed through tunnel, but daemon's `data_used_bytes` only showed 22 MB. Zun: "You said the data cap is in place. But I'm already way over 100mb usage. Why do you lie."

**Root cause:** `strongswan-iptables-watchdog.service` re-applied `iptables-restore /etc/iptables/rules.v4` on every docker container event, including `exec_create`/`exec_start`/`health_status*`. These fired on:
- Every Prometheus scrape (30s) — `health_status*` matched
- Every quota-monitor poll (60s) — `exec_create`/`exec_start` matched
- Every `swanctl --list-sas` from the exporter (15s) — same
- Every `swanctl --load-creds` from quota-monitor cut (rare) — same

Each re-apply **reset all 508 per-VIP byte counters to 0**. The math: 60s daemon poll + 30s Prometheus scrape + various health checks = ~6 counter resets per minute. Daemon's 60s poll always read the counter within a few seconds of a reset, so it saw 0–5s of traffic accumulation, never the full 60s.

**Fix:** narrowed watchdog case statement from
```
start|restart|unpause|attach|exec_create|exec_start|health_status*
```
to
```
start|restart|unpause|die|stop|kill|oom
```

Only actual container lifecycle events trigger rule re-apply now. `docker exec` and health checks do NOT. Verified with direct test: 3 `docker exec swanctl` calls in a row left the counter alone (19292 → 19472 bytes naturally, not wiped to 0).

**Lesson:** for production iptables-counter accounting, use nftables with named counters (which persist across `nft flush ruleset` reloads) OR ensure the watchdog only re-applies on actual lifecycle events. iptables-legacy `restore` does NOT preserve counters.

See ADR `docs/decisions/5B-architecture.md` for full analysis.

### Test results (4 end-to-end runs)

| Run | Time | Trigger | Connect → cut | Peak | Final | Notes |
|---|---|---|---|---|---|---|
| #1 | 2026-06-19 17:42 UTC | Pre-set 100 MiB + 1 byte | n/a | n/a | 104.8% | First proven cut, no real client |
| #2 | 2026-06-19 19:44 UTC | Real iOS app (8 min streaming) | 8 min | 22 MB/min | 104.8% | Exposed 5B.6 bug — 140 MB in app / 22 MB in daemon |
| #3 | 2026-06-19 19:56 UTC | Real iOS app (heavy browsing) | 2:23 | 144 MB/min | 158.0% | Zun pushed hard, cap fired at 158% |
| #4 | 2026-06-19 23:26 UTC | Real iOS app (heavy browsing) | 1:06 | 140 MB/min | 158.0% | iOS app auto-logged off — Zun: "Beautiful" |

## Known limitations (v1.1.0)

- **iOS native VPN** — `.mobileconfig` silently fails cert validation. The strongSwan iOS app works (uses EAP-MSCHAPv2 reliably). v1.3 + LE cert may fix native.
- **5G IP rotation** — Vodacom 5G rotates public IP every few minutes. MOBIKE handles this in 1-3 sec, but during the gap, packets go to the dead IP. Workaround: rekey_time=24h, will shorten in 5B.
- **5G CGNAT stability** — iOS SAs die in 4-30 min on cellular. v1.3 backlog: lower fragment_size (1100), raise `ikesa_max_halfopen` to 10, test install_virtual_ip=yes.
- **Cloudflare bot detection** — ifconfig.me may give `ERR_CONNECTION_CLOSED` because shared MASQ IP looks bot-like. Actually works on 5G in our testing, but other Cloudflare-fronted sites may fail.
- **No HA** — single charon. If the container dies, all clients disconnect. 5H is the fix.
- **iptables-counter fragility** — any future re-apply of `rules.v4` will reset counters. See 5B.6 ADR for migration plan to nftables named counters.

---

## Changelog (architecture doc itself)

- **2026-10-15 06:50 UTC / 08:50 SAST (v2.2.1 portal + v2.5.0 bot fact-check):** Refreshed the 🟢 verified-live table to 2026-10-15 live state. **Drift fixed:** (1) Verified-live date 2026-07-13 → **2026-10-15**; (2) Hostname `vps-01` → **`vpn-prod-01`** (HOT-210); (3) MariaDB **42 → 45 tables** with full live row-count baseline (`radcheck=26`, `radpostauth=2,299`, `radacct=1,166`, `customers=11`, `users=11`, `devices=10`, `operators=1`); (4) `radpostauth` 390 → **2,299** (~6× growth); (5) `radacct` 5 → **1,166** (~230× growth); (6) `customers` 5 → **11** (+6 new); (7) Container status: `Up About an hour` → **`Up 6 days`** (StartedAt 2026-10-08T14:06:34Z); (8) **New rows added:** `Portal` (v2.2.1 + SHAs), `Bot` (v2.5.0), `Services running` (18 services incl. 7 new), `Listening ports` (6 new ports), `Quota enforcement (script location)` (INVERTED), `bandwidth-monitor.service` (STALE path warning), `LE certs` (adminer.databyte.co.za + 48 days); (9) `kopia 502 Bad Gateway` flagged in Backup row (parked; Zun msg #43961); (10) Architecture shifts table: `v2.2.0` → `v2.2.x`; added 3 new shifts (bot, swanctl-watchdog, kopia); portal `v2.2.0` → `v2.2.1`. **Broken reference fixed:** `docs/VPS-XNEELO-DEPLOY.md` (file doesn't exist) replaced with pointer to `docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0 + `scripts/bootstrap-xneelo.sh`. **L3 audit:** all numbers in the new verified-live table re-verified against live prod via SSH; 0 drift remaining. Cross-check with `docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0 §1.2 + §1.3 + §1.6.

- **2026-07-13 16:00 SAST / 14:00 UTC (v2.2.0 doc-sync):** Added "🟢 Verified-live 2026-07-13" table at the top with live-verified facts from `ssh root@vps-01`. Added "Key architecture shifts since v1.x" mapping each commit+date for the SQLite→MariaDB (Phase 4E), iptables→nftables (Phase 7.5), SQLite→FreeRADIUS (Phase 5) transitions. Marked two design-decision subsections as STALE with the commit that superseded each. The lab (LXC 903) architecture below is preserved as historical context — every claim MUST be cross-checked against the verified-live table. Doc-touch commit: included in v2.2.0 docs sync (this audit). Verification receipts: live VPS container image MD5, freeRADIUS listening on 1812/1813/18120, charon UDP/500+4500, MariaDB `radius` DB at 42 tables with `customers`/`users`/`devices` rows.

- **2026-07-01:** Added "live deployment context" warning (lab = LXC 903; prod = VPS via DEPLOYMENT.md).
- **Earlier:** Original v1.x architecture documentation as-built (5A → 5B → 5C sequence).
