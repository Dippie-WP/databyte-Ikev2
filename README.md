# strongswan-vpn-gateway

Personal strongSwan EAP VPN gateway. For per-user VIP pinning, attr-sql + SQLite, server-cert + EAP-MSCHAPv2 with PSK fallback. Latest release tag `v1.7.0-recovered` (2026-06-26 baseline). Active development on `main` (post-v1.9.0 SSE merge). 5A / 5B / 5C green; pre-launch commercial stack live on VPS.

[![CI](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Dippie-WP/databyte-Ikev2)](https://github.com/Dippie-WP/databyte-Ikev2/releases)

## What this is

A self-hosted IKEv2 VPN gateway running in a Docker container on an LXC host. **Pre-commercial testing** (Databyte Global Solutions, Zun). Personal + test devices + commercial customers connect from anywhere over 5G/WiFi. Phases 5A (foundation), 5B (quota layer), and 5C (portal surface) are GREEN. **5D has been repurposed (2026-07-05)** as the RADIUS migration (FreeRADIUS + daloRADIUS on prod VPS, single MariaDB, portal keeps management) — 🟡 In progress; full plan in `install-radius-daloradius.md` and tracker row 5D. The pre-launch commercial stack (per-customer bandwidth caps, customer portal, Windows installer, billing IDs) is **LIVE** on the Xneelo Johannesburg VPS at `https://myvpn.databyte.co.za/` — see [docs/VPS-XNEELO-DEPLOY.md](docs/VPS-XNEELO-DEPLOY.md).

- **Image:** `zun/strongswan:6.0.7-mschapv2-attrsql` (custom build)
- **Source:** [Dippie-WP/databyte-Ikev2](https://github.com/Dippie-WP/databyte-Ikev2) — `main` branch is canonical (currently at v1.7.5 SHA-robustness + v1.9.0 SSE merge deployed to VPS). Note: tags `archive-v1.8.0-removed-2026-07-01` and `archive-v1.9.0-sse-removed-2026-07-01` are ORPHANED historical markers (point at the destructive-replay branch, not `main`). See `CHANGELOG.md` for v1.3.0 → v1.9.0 SSE history.
- **StrongSwan version:** 6.0.7 (CVE-2026-47895 patched)
- **Auth:** Server-cert (RSA-2048 + RSASSA-PSS) + EAP-MSCHAPv2 (primary) and PSK (fallback)
- **Pool:** 10.99.0.0/24 with per-user sticky VIPs via attr-sql + SQLite
- **Lab deployed at:** LXC 903 (192.168.10.98, on pve2 in Cape Town homelab)
- **Production VPS:** Xneelo Johannesburg, `myvpn.databyte.co.za` — see [docs/VPS-XNEELO-DEPLOY.md](docs/VPS-XNEELO-DEPLOY.md) for the deployment runbook.
- **Backed up to:** `rustfs:/open-claw-push/strongswan-{db,configs}/` (daily + ISO-week slots, 14d/8w retention)
- **Lab public endpoint:** 102.182.117.43, router forwards UDP 500/4500 → 192.168.10.98 (homelab only, not production)
- **Production:** `myvpn.databyte.co.za` (Cloudflare DNS, grey cloud for IKEv2)

## What's where

| Path | What's in it |
|---|---|
| `docker/` | The container: Dockerfile, docker-compose, swanctl configs, strongswan.d overrides, in-image `start.sh` |
| `host/` | The LXC host: sysctl, iptables, firewalld zone, optional nftables service |
| `scripts/` | Operate-time: cert gen, DB seed, image build, daily backup, rollback |
| `docs/` | ROADMAP, ARCHITECTURE, DEPLOYMENT, ISSUES-LOG, SESSION-HISTORY, **VPS-XNEELO-DEPLOY**, **CLOUDFLARE-DNS** |
| `examples/` | Client profiles: Android `.sswan`, iOS `.mobileconfig` template (iOS path is broken; see issues) |

## Quick start (new host — or recovery rebuild)

A complete end-to-end deploy from a fresh Linux box. Single-operator setup — you host the server, you use the server, you administer it. No tenants, no billing, no onboarding.

Assumes you have:
- A Linux server (Debian/Ubuntu) with Docker installed
- A public IP (static, or dynamic with DDNS)
- Access to your router to forward UDP 500 + UDP 4500
- A DNS name pointing to your public IP (e.g., `vpn.example.com`)
- Root on the server

**Total time:** ~30 min on a fresh host, ~10 min if you've done it before.

### 1. Clone the repo

```bash
git clone https://github.com/Dippie-WP/databyte-Ikev2.git
cd databyte-Ikev2
```

### 2. Generate your certs (5 sec)

The image doesn't ship certs — you generate your own CA and server cert.

```bash
# Replace vpn.example.com with your actual hostname
SERVER_ID=vpn.example.com bash scripts/gen-certs.sh
```

This creates:
- `docker/swanctl/x509ca/strongswan-ca.crt.pem` (CA cert — **give this to your clients**)
- `docker/swanctl/x509/server.crt.pem` (server cert, 1y validity)
- `docker/swanctl/private/server-key.pem` (server private key, mode 600)
- `docker/swanctl/private/strongswan-ca-key.pem` (CA private key, mode 600)

> **Note:** The script default uses RSA-2048 for the server cert (changed 2026-06-19 — ECDSA P-256 was rejected by iOS 18+ IKEv2). Signature is PKCS#1 v1.5 with sha256 (RSASSA-PSS was tried in 5A.10 but iOS 18 silently rejected the cert — rolled back to PKCS#1 v1.5). Cert is 1y expiry; rotate manually. Bleichenbacher mitigation deferred to v1.3 with certbot + DNS-01 for `vpn.homelab.local`.

### 3. Set your admin password

The default `rw-eap.conf.template` has an `eap-zun` user. Edit it to your identity and set a password:

```bash
# Copy template to live config
cp docker/swanctl/conf.d/rw-eap.conf.template docker/swanctl/conf.d/rw-eap.conf

# Edit the secrets block — set your username + password
$EDITOR docker/swanctl/conf.d/rw-eap.conf
```

Example secrets block at the bottom of the file:
```ini
secrets {
  eap-yourname {
    id = yourname
    secret = "YourStrongPassword2026!"
  }
  ike-psk {
    id = vpn.example.com
    secret = "jzm+7IIsL+8lXwktTn8M5+kV4VTM2L1KjAotUQtKMyc="  # generate your own
  }
}
```

You also need to set the **server identity** in `rw-eap.conf` to match your cert:
```ini
connections {
  rw-eap {
    local {
      id = vpn.example.com  # must match your cert's CN/SAN
      cert = /etc/swanctl/x509/server.crt.pem
    }
    ...
  }
}
```

### 4. Build the image (~5 min on first build)

```bash
bash scripts/build-image.sh
# Optional: tag a specific version
# bash scripts/build-image.sh zun/strongswan:6.0.7-mschapv2-attrsql
```

### 5. Apply host network config (one-time, needs root)

```bash
# IP forwarding
sudo cp host/sysctl.d/99-strongswan.conf /etc/sysctl.d/
sudo sysctl --system

# iptables (MASQUERADE + FORWARD for 10.99.0.0/24)
sudo cp host/iptables/rules.v4.template /etc/iptables/rules.v4
sudo systemctl restart netfilter-persistent

# Docker iptables persistence (watchdog — auto-recovers if rules drop)
sudo cp host/systemd/strongswan-iptables-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strongswan-iptables-watchdog.service
```

**On your router:** forward UDP 500 and UDP 4500 from your public IP to the Docker host's LAN IP.

### 6. Start the container

```bash
cd docker
docker compose --profile vpn up -d
```

Watch the logs:
```bash
docker logs -f strongswan
```

You should see: `loaded plugins: ... attr-sql ... sqlite ... eap-mschapv2 ...` and `charon (16) started`.

### 7. Seed the DB with your first user

After charon has initialized the schema (on first run, ~5 sec):

```bash
# Compute NTLM hash of your password
PASSWORD='YourStrongPassword2026!'
HASH=$(echo -n "$PASSWORD" | iconv -t UTF-16LE | openssl dgst -md4 -provider legacy -provider default -hex | awk '{print toupper($NF)}')

# Seed the DB (VIP 10.99.0.50 — first IP in the pool)
USERNAME=yourname VIP=10.99.0.50 NTLM_HASH=$HASH bash scripts/seed-db.sh
```

### 8. Reload secrets in charon (no restart needed)

```bash
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds
```

### 9. Connect from a client

#### Android
1. Install **strongSwan VPN Client** from Play Store
2. Import `docker/swanctl/x509ca/strongswan-ca.crt.pem` (email it to yourself, tap to install)
3. Add VPN profile:
   - Gateway: `vpn.example.com`
   - Type: IKEv2 EAP (Username/Password)
   - Username: `yourname`
   - Password: `YourStrongPassword2026!`
4. Connect — should get VIP 10.99.0.50

#### iPhone / iPad (iOS 18+)

**Use the strongSwan iOS app** ([App Store link](https://apps.apple.com/app/strongswan-vpn-client/id1453698374)) — iOS native VPN Settings + `.mobileconfig` is **fundamentally broken for EAP-MSCHAPv2** on iOS 18+ (iOS sends EAP identity, server sends MSCHAPV2 challenge, iOS never responds, even with correct `AuthenticationMethod: None` + `ExtendedAuthEnabled: 1` + `AuthName` + `AuthPassword` baked into the profile). The strongSwan app is the official strongSwan client and has a working EAP-MSCHAPv2 implementation.

Setup:
1. Install **strongSwan VPN Client** from the App Store (free)
2. Open the app → tap **+** to add a profile
3. **Server:** your public IP or hostname (e.g. `vpn.example.com` or `102.182.117.43`)
4. **Username:** the strongSwan user you seeded (e.g. `zun` or `demo-phone`)
5. **Password:** the secret you set in `docker/swanctl/conf.d/rw-eap.conf`
6. **CA certificate:** import the `strongswan-ca.crt.pem` (e.g. air-drop it, or download from a URL you host)
7. **Server identity (advanced / settings cog):** must match the server cert CN/SAN, e.g. `vpn.example.com`. If the app auto-fills the IP, **change it** — charon matches on IDr.
8. Tap **Save** → flip the toggle

If the app says "trust this CA": enable it. If "no proposal chosen": the server expects AES-256/SHA2-256/DH14 (default in the strongSwan app — should just work).

**For EAP-TLS (per-device client certs, 5D path):** you can use the iOS native VPN + `.mobileconfig` flow. iOS native handles EAP-TLS reliably because the cert is the auth, no EAP-MSCHAPv2 dialog is needed. The mobileconfig approach documented in the v1.0 commit history works for that path.

#### Windows
1. **PowerShell as Admin:**
   ```powershell
   Add-VpnConnection -Name "MyVPN" -ServerAddress "vpn.example.com" `
     -TunnelType IKEv2 -AuthenticationMethod EAP `
     -RememberCredential
   Set-VpnConnectionIPsecConfiguration -Name "MyVPN" `
     -DHGroup Group14 -PfsGroup PFS2048 `
     -IntegrityCheckMethod SHA256 -EncryptionMethod AES256
   ```
2. **Install CA to LocalMachine Trusted Root** (NOT CurrentUser):
   ```powershell
   Import-Certificate -FilePath "strongswan-ca.crt.pem" `
     -CertStoreLocation "Cert:\LocalMachine\Root"
   ```
3. Network & Internet settings → VPN → MyVPN → Connect

#### Linux
Use NetworkManager-strongswan-gnome, or charon-cmd for CLI testing.

### 10. Verify it works

```bash
# On the Docker host, check active SAs
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas

# Check your client has the right VIP
ip addr show  # on the client, look for 10.99.0.50 on the tun0/ppp0 interface

# Test connectivity
ping 1.1.1.1     # should work
curl https://icanhazip.com  # should show your SERVER's public IP, not your client's
```

### Adding more users (after initial deploy)

```bash
# Generate NTLM hash for new password
HASH=$(echo -n 'NewUserPassword' | iconv -t UTF-16LE | openssl dgst -md4 -provider legacy -provider default -hex | awk '{print toupper($NF)}')

# Seed DB
USERNAME=alice VIP=10.99.0.51 NTLM_HASH=$HASH bash scripts/seed-db.sh

# Add to secrets block in docker/swanctl/conf.d/rw-eap.conf:
#   eap-alice { id = alice, secret = "NewUserPassword" }

# Reload (no restart)
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds
```

### Updating the image (after a new release)

```bash
git pull
bash scripts/build-image.sh
cd docker && docker compose --profile vpn up -d --force-recreate
```

### Rollback

```bash
# Roll back to previous image tag
docker tag zun/strongswan:6.0.7-mschapv2-attrsql zun/strongswan:6.0.7-mschapv2-attrsql.bak
docker tag zun/strongswan:6.0.7-mschapv2-attrsql.previous zun/strongswan:6.0.7-mschapv2-attrsql
cd docker && docker compose --profile vpn up -d --force-recreate
```

For HA rollback (multiple instances + LB), see Phase 5H.

## Status

| Phase | Description | Gate |
|---|---|---|
| **5A** | Foundation: conn config, user+pool+VIP pin, public-path test, reconnect, MSS clamp, server cert, monitoring, backup | ✅ **GREEN (signed off 2026-06-18)** |
| **5B** | Quota layer (iptables-legacy per-VIP byte counters + 60s monitor daemon + 80% warn + 100% hard cut) | ✅ **GREEN (signed off 2026-06-19, v1.1.0 tagged)** |
| **5C.1+5C.2** | Self-service portal (FastAPI + vanilla JS) — customer + operator | ✅ **GREEN (v1.2, then v1.3.0 customer portal at `/portal/`)** |
| **5C.3** | Grafana `strongswan-quota` dashboard | ✅ **GREEN (v1.2.2)** |
| **5C.4** | ~~RustFS daily backup verify~~ | ⛔ **CANCELLED (2026-06-20)** — replaced by PBS full-LXC backup |
| **5C.5/5C.6** | ~~Self-service / multi-device~~ | ⛔ **REVERTED / SHELVED (v1.2.6)** — strongSwan 1-identity-1-VIP blocks per-device under EAP-MSCHAPv2 |
| **5D (RADIUS migration)** | FreeRADIUS + daloRADIUS on prod VPS, single MariaDB, portal keeps management. Direct-to-prod, nuke + start fresh (per Zun #23766 + #23783). | 🟡 **IN PROGRESS 2026-07-05** (was SHELVED SaaS billing since 2026-06-19 — repurposed). Plan: `install-radius-daloradius.md` (7 phases); see tracker row 5D. |
| **5D (pre-launch)** | Per-customer bandwidth, customer portal, Windows installer, billing IDs | ✅ **LIVE on VPS** (rolled up under v1.4.0 → v1.7.0-recovered, deployed 2026-06-22 → 2026-06-26) |
| **5H** | HA + LB (2x v1.9 + keepalived VRRP + shared DB) | ⏳ **NOT STARTED** — plan at `docs/PLAN-5H-HA-LB.md` |

## CI

- **`.github/workflows/ci.yml`** — runs on every push to `main` and every PR. Builds the image, runs smoke tests (charon version, plugin presence, strongswan.conf structure, entrypoint perms), and lints the Dockerfile with hadolint. Bad pushes are blocked.
- **`.github/workflows/release.yml`** — runs on every `v*` tag push. Builds the image, pushes to `ghcr.io/dippie-wp/databyte-ikev2:<version>` + `:latest`, and creates a GitHub release with auto-generated notes.

## Versions

- **v1.0 (2026-06-18):** EAP + attr-sql + sticky VIPs, public-path tested on 5G, monitoring via Prometheus, backup to RustFS
- **v1.1.0 (2026-06-19):** Quota layer (5B) — iptables-legacy per-VIP byte counters, 60s monitor daemon, 80% warn, 100% hard cut. **Proven with 3 end-to-end runs using real iOS app traffic, all cut correctly**
- **v1.2** (image tag `6.0.7-mschapv2-attrsql`): same code as v1.1, locked image
- **v1.1** (image tag `6.0.7-mschapv2`, still in registry): PSK + EAP, no VIP pinning — **not a valid fallback**, needs static pool in `strongswan.conf` to work at all
- **v1.2.x (2026-06-20 → 2026-06-21):** device-info UI, VICI parser hardening, reboot fixes, self-service portal polish. See `CHANGELOG.md`.
- **v1.3.0 (2026-06-21):** Customer portal at `/portal/` (lab), operator dashboard polish v1.2.11-v1.2.14 rolled up. 10-isolation-guarantee cookie separation.
- **v1.4.0 → v1.4.6 (2026-06-22 → 2026-06-23):** Bug #2 explicit `customers.user_id` FK, homelab/VPS separation, audit fixes (CP4-CP6), strict-CSP refactor, security headers, production portal live at `myvpn.databyte.co.za`.
- **v1.5.0 → v1.5.2 (2026-06-23 → 2026-06-24):** `speed_plan` at customer creation (per-customer, not tier-driven), vp-s1 CSS variable fix, deploy-script upgrades.
- **v1.6.0 → v1.6.7 (2026-06-24 → 2026-06-25):** Windows PowerShell auto-installer (HARDLOCK canonical 3-line block), online-only lease filter, dashboard auto-refresh, customer-detail bandwidth display, `None`-string bug fix, Refresh-button move, KILLED-secret restore on reset.
- **v1.7.0 (2026-06-26):** `speed_plan` in PATCH + Edit modal dropdown (per Zun #22367). Deployed to `vpn-prod-01`. **Release tag:** `v1.7.0-recovered` (baseline after 2026-06-27 recovery).
- **v1.8.0 → v1.8.3 (2026-06-27):** quota-monitor pool-LEASE attribution, offline-lease UI, regenerate-password button, focus-refresh via Page Visibility API, customer-detail auto-refresh.
- **v1.9.0-sse (2026-06-27):** Server-Sent Events replace `setInterval` polling for live data.

**Note:** v2.3.0 / v2.6.0 / v2.7.0-v2.7.2 tags exist on origin but pre-date the 2026-06-26 recovery baseline (point to older commits). Treat as orphaned — do not build from them.

## Release notes

### v1.1.0 (2026-06-19) — "5B lock-in: data cap layer"

**What it does:** hard-cut data cap per customer. Every 60 seconds, a Python daemon reads iptables-legacy per-VIP byte counters, looks up the customer, increments their `data_used_bytes`. At 80% it logs a warning. At 100% it terminates the IKE_SA, kills the EAP secret in `rw-eap.conf` (replace with `KILLED-<random>`), reloads charon, and marks the customer `over_quota=1`. Re-authentication is blocked because the secret is now dead.

**Added:**
- 6 new DB tables: `tiers`, `customers`, `devices`, `purchases`, `alerts`, `audit_log` (idempotent schema, applied at boot via `quota-schema.service`)
- 254 outbound + 254 inbound iptables-legacy per-VIP ACCEPT rules in FORWARD chain = 508 rules total
- `quota-monitor.py` (21KB) — long-running daemon, polls every 60s
- `quota-monitor.service` — systemd unit, restart=on-failure, SIGTERM-clean
- `quota/install_quota_rules.sh` — installs the 508 per-VIP rules + watchdog persistence
- `quota/update_rw_eap_conf.py` — kills EAP secret at 100% (used by quota-monitor)
- `quota/seed_*.sh` — tier + customer + device seed scripts
- `quota/reset_demo.sh` — resets demo state
- `docs/decisions/5B-architecture.md` — design ADR (iptables-legacy vs nftables, kill-conf vs DB, etc.)
- `docs/decisions/5B-credentials-kill.md` — why we kill conf secret, not DB

**Fixed (5B.6):** `strongswan-iptables-watchdog.service` was re-applying `iptables-restore` on every docker container event (including `exec_create`/`exec_start`/`health_status*`) — which fired on every Prometheus scrape and daemon poll, **resetting all 508 per-VIP byte counters to 0**. Zun's "you lie" screenshot (140 MB in iOS app vs 22 MB in daemon) was the diagnostic clue. Fix: case statement narrowed to `start|restart|unpause|die|stop|kill|oom` only.

**Test results (3 runs with real iOS app traffic):**

| Run | Connect → cut | Peak | Final | Notes |
|---|---|---|---|---|
| #1 | 8 min | 22 MB/min | 104.8% | First REAL cut (also exposed 5B.6 bug) |
| #2 | 2:23 | 144 MB/min | 158.0% | Zun pushed hard |
| #3 | 1:06 | 140 MB/min | 158.0% | Zun: "Beautiful the app automatically logged me off" |

**Proven design choices (locked 2026-06-19):**
- Operator bypass via `is_operator=1` flag
- 2 simultaneous connections per customer, shared quota pool
- Per-purchase cycle, hard cut at 100%, manual extension (no calendar)
- No "unlimited" tier in catalog — Zun's account has operator bypass instead
- Notifications: customer portal at `/portal/` (no Telegram bot — customers see live quota bar)
- Tier storage = DB-only

**5C work (customer web page, admin web page, Grafana `vpn-quota` dashboard) — all DONE (v1.2 → v1.3.0).** Backup verify (5C.4) CANCELLED 2026-06-20 — replaced by PBS full-LXC backup. Telegram bot (5C.3-notification) SHELVED — customers are notified via the customer portal itself.

### v1.0 (2026-06-19) — "5A lock-in"

**Added (final v1.0 state — supersedes earlier "RSASSA-PSS" notes):**
- Server cert: **RSA-2048 + PKCS#1 v1.5** (sha256WithRSAEncryption), EKU `serverAuth + ipsecIKE`, SAN `DNS:vpn.homelab.local, IP:102.182.117.43`, 1-year validity. RSASSA-PSS was tried first but iOS 18 silently rejected it — rolled back to PKCS#1 v1.5 in 5A.10
- `swanctl.conf` `secrets` block pattern for EAP users (file-based credential lookup, since `sql` plugin is not loaded)
- strongSwan iOS app (NOT iOS native VPN + mobileconfig — broken for EAP-MSCHAPv2 on iOS 18+)
- 4 client types tested end-to-end: Android EAP, iPhone PSK (via strongSwan app), iPhone EAP-MSCHAPv2 (via strongSwan app), Windows EAP-MSCHAPv2
- MOBIKE proven working (LAN↔4G CGNAT migration, VIP preserved)
- Three-layer iptables persistence: `rules.v4` + watchdog service + manual recovery script
- Prometheus exporter (`strongswan_exporter.py`) on port 9101 with per-SA metrics
- Daily backup to RustFS for DB + configs + certs (with CA private key)
- Pinning: VIPs stay identity-pinned across reconnects (attr-sql lease persistence)

**Security decisions:**
- Server cert: RSA-2048 (ECDSA P-256 rejected by iOS 18+ IKEv2 — must be RSA)
- Signature: PKCS#1 v1.5 (RSASSA-PSS rejected by iOS 18 — lost Bleichenbacher mitigation, defer to v1.3 with certbot)
- EAP creds: file-based in `swanctl.conf` `secrets` block; DB column `users.password` is dead data
- iOS strongSwan app: EAP password stored in app's keychain (not in mobileconfig) — cleaner than the v3-v10 mobileconfig experiments

**Tested clients:**
- Android: strongSwan VPN Client + CA import + EAP-MSCHAPv2 (zun / VIP 10.99.0.50)
- iPhone PSK: combined mobileconfig (zun-iphone / VIP 10.99.0.3)
- iPhone EAP: combined mobileconfig + Certificate Trust toggle (zun-iphone / VIP 10.99.0.3)
- Windows: PowerShell `Add-VpnConnection` + `Set-VpnConnectionIPsecConfiguration` + LocalMachine CA store (zun-windows / VIP 10.99.0.4)

## Critical known limitations

1. **charon-cmd 5.9.5** in test environment incompatible with 6.0.7 server's EAP-Identity flow. Real load test deferred (requires all 3 real clients online simultaneously OR a Python VICI client).
2. **5G IP rotation** can cause brief IKE_SA re-auth on iOS (not pure MOBIKE; functionally equivalent).
3. **MSS clamp at 1260** required for 5G carriers. Lives in `host/iptables/rules.v4`. Forgetting this → iana.org-style timeouts.
4. **EAP creds in plaintext** in `swanctl.conf` `secrets` block. Acceptable for personal use; commercial needs EAP-TLS.
5. **No CRL/OCSP.** Server cert has 1-year validity, manual rotation.
6. **charon-log** lives inside container — must bind-mount to host for log shipping (pattern in `host/strongswan/strongswan.d/debug.conf`).

## License

None declared. Personal project.

## Maintainer

Zun (@zuzu172 on Telegram, github Dippie-WP). Built with Misha.
