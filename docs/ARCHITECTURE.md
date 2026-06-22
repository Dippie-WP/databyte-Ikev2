# ARCHITECTURE

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

### SQLite (NOT Postgres, NOT MySQL)
The DB is small (~200KB), low-write (just lease updates), no concurrent writers (single charon). SQLite is the right tool. The daily backup is a 200KB copy to RustFS. If we ever need multiple gateway replicas, this changes.

### iptables-legacy (NOT nftables) for quota counters
We use iptables-legacy with per-VIP ACCEPT rules. **Why not nftables?** nftables has named counters that persist across rule reloads (a 5B.6-style bug couldn't happen with nftables). But nftables doesn't ship with the LXC's firewalld backend yet, and adding nftables-native as a second stack would duplicate complexity. **Tradeoff:** iptables-legacy counters get wiped on every `iptables-restore`. The 5B.6 fix (narrow watchdog case statement) prevents the wipe. For v1.3 we may migrate to nftables for permanent counter persistence.

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
