# ARCHITECTURE

## Network topology

```
[5G phone / iPhone / friend laptop]
        │ (UDP 500, UDP 4500, ESP)
        │ over internet
        ▼
[Router 192.168.10.29]
   public IP 102.182.117.43
   port-forwards UDP 500 + 4500 → 192.168.10.212
        │
        ▼
[LXC 902 @ pve2 — 192.168.10.212]
   ├── firewalld (trusted zone, MASQ for 10.99.0.0/24)
   ├── netfilter-persistent (MSS clamp FORWARD 1260)
   ├── sysctl.d/99-strongswan.conf (ip_forward=1, redirect hardening)
   │
   └── [Docker container: strongswan (network_mode: host)]
         ├── charon daemon (swanctl, VICI on 127.0.0.1:4502)
         ├── filelog → /var/log/charon-filog.log (bind-mounted)
         └── SQLite at /var/lib/strongswan/ipsec.db (bind-mounted)
                 ↕ daily 03:00 UTC backup
                 ▼
         [RustFS bucket: open-claw-push/strongswan-db/]
              ├── daily/   (14d retention, ISO date slot)
              └── weekly/  (8w retention, ISO week slot)

[Optional sidecar: torilabs/ipsec_exporter @ :8078]
   metrics → Prometheus → Grafana
```

## What runs where

### LXC 902 host (192.168.10.212)
- **firewalld** (iptables backend) — trusted zone for 10.99.0.0/24 with `<masquerade/>` and `<forward/>`
- **netfilter-persistent** — loads `/etc/iptables/rules.v4` (includes the MSS clamp)
- **systemd sysctl** — `/etc/sysctl.d/99-strongswan.conf` enables forwarding
- **cron** — `/etc/cron.d/strongswan-db-backup` runs the daily backup
- **ipsec_exporter** (host process) — VICI → :8078 Prometheus metrics (optional)

### Docker container (strongswan, network_mode: host)
- **charon daemon** — IKEv2 + EAP-MSCHAPv2 + PSK fallback
- **swanctl** — config loader (VICI, TCP 127.0.0.1:4502)
- **attr-sql plugin** — per-user VIP pinning via SQLite
- **Filesystem:**
  - `/etc/swanctl/` (read-only bind-mount of `docker/swanctl/`)
  - `/etc/strongswan.d/*.conf` (file-level bind-mounts of `docker/strongswan.d/`)
  - `/etc/strongswan.conf` (baked into image)
  - `/var/lib/strongswan/ipsec.db` (bind-mount from LXC host)
  - `/var/log/charon-filog.log` (bind-mount to LXC host)

### Client (5G phone, iPhone, friend laptop)
- **Android strongSwan app** (works) — uses `.sswan` profile with CA cert + EAP creds
- **iOS native VPN** (broken, see ISSUES-LOG) — `.mobileconfig` silently fails cert validation
- **Windows / macOS strongSwan** (untested) — should work with same `.sswan` profile

## Data flow (single TCP connection example)

1. Phone opens `https://example.com` in Chrome
2. TCP SYN from phone (10.99.0.50) → server's strongSwan endpoint
3. SYN-ACK back, handshake done over UDP 4500 + ESP
4. Chrome sends HTTP GET (small, fits MSS)
5. example.com responds with HTML + assets
6. Server's LXC receives response, MASQ rewrites src=192.168.10.212
7. **MSS clamp (5A.7) on FORWARD ensures response packets are ≤ 1260 bytes**
8. LXC ESP-encapsulates response → UDP 4500 → phone
9. Phone's strongSwan app decapsulates → delivers to Chrome
10. Chrome renders

## Why these specific design choices

### network_mode: host (NOT bridge)
IKE (UDP 500/4500) and ESP (proto 50) require direct host access. Bridge mode would require hairpin NAT and break MOBIKE. The downside is the container shares the LXC's network namespace — acceptable for trusted LAN; if exposing beyond, add firewall rules limiting source ranges to known good clients.

### Docker image built fresh per host (NOT pushed to registry)
The build is ~5 min and the image is tied to a specific strongSwan version. Pushing to a registry adds operational overhead with no real benefit at this scale. We can add `ghcr.io` later if a 3rd host deployment makes the build time matter.

### SQLite (NOT Postgres, NOT MySQL)
The DB is small (~200KB), low-write (just lease updates), no concurrent writers (single charon). SQLite is the right tool. The daily backup is a 200KB copy to RustFS. If we ever need multiple gateway replicas, this changes.

### firewalld + iptables backend (NOT nftables-native)
The LXC has firewalld already, with the trusted zone. We use it. nftables-native would be cleaner long-term but isn't worth the migration now. We document both paths in `host/`.

### Self-signed CA (NOT Let's Encrypt)
LE is what v1.3 will use. For v1.2, the Android strongSwan app handles self-signed CAs fine when the CA is installed as a user CA. iOS is broken either way. The setup is:
1. Generate self-signed CA (10y)
2. Generate server cert signed by CA (1y, ECDSA P-256)
3. Install CA on Android via `Settings → Security → Install from storage`
4. In strongSwan app, profile uses server's `vpn.homelab.local` ID + the installed CA

### Sticky VIP via attr-sql (NOT hard pin)
Upstream attr-sql doesn't enforce hard pinning. The pattern we use: pre-insert the address row in `addresses` table with `identity=X, released=0`. charon picks it up and re-uses. But if the row gets deleted (e.g., charon crash recovery), the next user may get that VIP. v1.3 will add a custom plugin for hard pin.

## Quota layer (Phase 5B — in progress)

### Data model

The quota layer extends the strongSwan attr-sql SQLite DB with 6 new tables:

```
strongSwan upstream tables (30):
  users          ← identity + EAP password
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
nftables counter (bytes per VIP)
  → leases.address (BLOB) → leases.identity (= users.id)
    → devices.strongswan_user_id (= users.id)
      → devices.customer_id
        → customers.tier_id → tiers.data_limit_bytes
        → customers.data_limit_bytes (effective limit = tier + manual extensions)
        → customers.is_operator (bypass flag)
```

**Key design decisions (2026-06-19, locked):**
- Operator (Zun): `is_operator=1`, bypasses ALL quota checks, no tier, no cap
- Customers: 2 simultaneous connections per account (enforced at nftables layer)
- Quota is shared across all devices of one customer (combined `data_used_bytes`)
- Per-purchase model: 100% = hard cut, manual extension by operator after payment
- No calendar/rolling cycle — data_limit_bytes is manual + tier-based

### Tiers (seeded in 5B.1)

| Name | Display | data_limit_bytes | Status |
|------|---------|------------------|--------|
| tier_3gb | 3 GB | 3,221,225,472 | Active, for sale |
| tier_10gb | 10 GB | 10,737,418,240 | Active, for sale |
| tier_15gb | 15 GB | 16,106,127,360 | Active, for sale |
| demo_100mb | Demo 100 MB | 104,857,600 | Persistent demo account (Zun resets after each demo) |

**Operator:** `zun-operator` (is_operator=1, no tier, unlimited bypass)

### Components

```
[LXC 903 host]
  quota-monitor.py          ← reads nftables counters + DB → alert/cut decisions
  quota-schema.service      ← oneshot at boot, applies schema (idempotent)
  nftables-zun-vpn.service ← accounting rules per VIP (5B.2, in progress)

[strongSwan container]
  charon                    ← VICI on TCP 127.0.0.1:4502 (read by quota-monitor)
  ipsec.db                 ← bind-mount from host, shared with LXC

[Customer-facing (5C, future)]
  vpn-bot.py               ← Telegram bot: auth + buy-more relay + 80%/100% DMs
  customer web page         ← FastAPI: usage bar, device list, "buy more" button
  admin web page            ← FastAPI /admin: customer mgmt, cred gen, quota extend

[Operator monitoring]
  Grafana (existing)        ← operator-only: system health + all users overview
  vpn-quota dashboard      ← 5C.4: per-customer usage, active SAs, alert history
```

### quota-monitor.py logic (5B.3)

1. Read nftables byte counters for each active VIP in the rw-pool range
2. For each VIP with non-zero bytes:
   a. Resolve VIP → leases.identity → users.id
   b. Resolve users.id → devices.strongswan_user_id → devices.customer_id
   c. Load customers.row (is_operator? skip if yes)
   d. Load customers.data_limit_bytes vs data_used_bytes
   e. If threshold crossed (80% or 100%) and no existing alert row for this cycle:
      - Log alert to `alerts` table
      - Send Telegram DM to operator (Zun)
      - Send Telegram DM to customer (if telegram_id known)
   f. If 100% and customer.over_quota == 0:
      - Set customers.over_quota = 1
      - Terminate CHILD_SA via VICI (`swanctl --terminate --ike <conn> --ike-id <id>`)
      - Block new SA by setting customers.is_active = 0 (optional)
3. Also: check `leases` table for new VIP assignments since last run
4. Update `devices.last_seen_v4` and `devices.last_seen_at` for active SAs
5. Sleep 60s, repeat

### nftables accounting rules (5B.2)

Accounting is per VIP (not per user identity). This is simpler and avoids nftables having to track strongSwan's internal identity mapping. The `leases` table bridges VIP ↔ identity.

Rules are in `host/systemd/nftables-zun-vpn.service` (extends existing FORWARD rules).

```
table ip strongswan-quota {
  chain postrouting-quota {
    type filter hook postrouting priority 0; policy accept;
    ip saddr 10.99.0.0/24 counter comment "bytes-from-vip"
    ip daddr 10.99.0.0/24 counter comment "bytes-to-vip"
  }
}
```

Counters are read by quota-monitor.py via `nft list chain ip strongswan-quota postrouting-quota`.

## Known limitations (v1.2)

- **iOS** — `.mobileconfig` silently fails cert validation. The strongSwan app on iOS works (uses PSK), but the native IKEv2 client is broken. v1.3 + LE cert is the fix.
- **5G IP rotation** — Vodacom 5G rotates public IP every few minutes. MOBIKE handles this in 1-3 sec, but during the gap, packets go to the dead IP. Workaround: rekey_time=24h, will shorten in 5B.
- **Cloudflare bot detection** — ifconfig.me may give `ERR_CONNECTION_CLOSED` because shared MASQ IP looks bot-like. (Actually works on 5G in our testing, but other Cloudflare-fronted sites may fail.)
- **No HA** — single charon. If the container dies, all clients disconnect. Acceptable for v1.2.
