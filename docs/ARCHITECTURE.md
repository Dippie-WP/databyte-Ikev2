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

## Known limitations (v1.2)

- **iOS** — `.mobileconfig` silently fails cert validation. The strongSwan app on iOS works (uses PSK), but the native IKEv2 client is broken. v1.3 + LE cert is the fix.
- **5G IP rotation** — Vodacom 5G rotates public IP every few minutes. MOBIKE handles this in 1-3 sec, but during the gap, packets go to the dead IP. Workaround: rekey_time=24h, will shorten in 5B.
- **Cloudflare bot detection** — ifconfig.me may give `ERR_CONNECTION_CLOSED` because shared MASQ IP looks bot-like. (Actually works on 5G in our testing, but other Cloudflare-fronted sites may fail.)
- **No HA** — single charon. If the container dies, all clients disconnect. Acceptable for v1.2.
