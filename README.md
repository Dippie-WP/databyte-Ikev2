# strongswan-vpn-gateway

Databyte VPN stack — strongSwan 6.0.7 EAP-MSCHAPv2 gateway + FreeRADIUS/MariaDB identity store + FastAPI customer/operator portal. **Latest release `v2.1.1` (2026-07-12) — Phase 4E single-source-of-truth cutover shipped, Phase 5 eap-radius cutover live on prod, DR runbook v1.0.7 published, 10 ISO-9001 docs in Paperless.**

[![CI](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml) [![drift-detect](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/drift-detect.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/drift-detect.yml) [![portal-smoke](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/portal-smoke.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/portal-smoke.yml) [![Release](https://img.shields.io/github/v/release/Dippie-WP/databyte-Ikev2)](https://github.com/Dippie-WP/databyte-Ikev2/releases)

## What this is (v2.1.1)

A self-hosted IKEv2 VPN stack running on a single Xneelo Johannesburg VPS (`vps-01`, 154.65.110.44). StrongSwan handles the IPsec; **FreeRADIUS + MariaDB hold all customer identities**; the FastAPI `vpn-portal` does customer self-service + operator admin. Customers connect from anywhere over 5G/WiFi, authenticate via EAP-MSCHAPv2 against FreeRADIUS (which proxies the `radcheck` rows in MariaDB), and get a per-user sticky VIP out of `10.99.0.0/24`. Quota enforcement cuts at 100% by **disabling the `radcheck` row + sending a signed RFC 5176 Disconnect-Request to charon's `eap-radius.dae` socket** — hard kill, no grace period.

- **Image:** `zun/strongswan:6.0.7-mschapv2-attrsql` (custom build, CVE-2026-47895 patched)
- **Source:** [Dippie-WP/databyte-Ikev2](https://github.com/Dippie-WP/databyte-Ikev2) — `main` branch is canonical
- **strongSwan version:** 6.0.7
- **Auth:** Server-cert (RSA-2048 + PKCS#1 v1.5) + EAP-MSCHAPv2, **resolved at runtime by FreeRADIUS** (charon uses `eap-radius {}` since Phase 5 cutover 2026-07-06 — see `CHANGELOG.md`)
- **Identity store:** MariaDB `radius` database — `radcheck`/`radusergroup` (RADIUS) + portal business tables `customers`/`users`/`devices`/`installer_tokens`/`audit_log`/`tiers`/`operator_sessions`/`customer_portal_sessions`/`alerts`/`purchases` (unified in Phase 4E commit `cb9bf69`)
- **Pool:** 10.99.0.0/24 with per-user sticky VIPs via `attr-sql` + `charon.ipsec.sqlite`
- **Lab LXC:** 903 (`vpn-gateway`, 192.168.10.98, on pve2 in Cape Town homelab) — Zun's personal/dev stack, not customer-facing
- **Production VPS:** Xneelo Johannesburg, `vps-01` (154.65.110.44)
  - VPN endpoint: `myvpn.databyte.co.za` (IKEv2, Cloudflare DNS grey cloud)
  - Portal: `vpn-portal.databyte.co.za` (customer self-service + operator admin, nginx + certbot)
- **Backups:** kopia → `kop.databyte.co.za`; daily + ISO-week slots; **DR runbook v1.0.7 verified** at [`docs/RUNBOOK-DR-REBUILD-AND-HA.md`](docs/RUNBOOK-DR-REBUILD-AND-HA.md)

## Production state (verified 2026-07-12, v2.1.1)

| Item | Verified value | Source |
|---|---|---|
| Portal `/api/health` | `{"status":"ok","db_ok":true,"db_customers":5,"charon_ok":true}` | `https://vpn-portal.databyte.co.za/api/health` |
| StrongSwan container | `healthy \| running` | `docker ps` on vps-01 |
| radpostauth growth post-4E | 323 → 324 with new Access-Accept at 07:22:58 SAST | `SELECT COUNT(*) FROM radpostauth` (proof: migrated NTLM hashes still authenticate) |
| Test suite | 162 passed, 1 skipped, 0 failed | `pytest` locally + `ci.yml` |
| CI workflows | 3/3 green (`ci`, `drift-detect`, `portal-smoke`); 4th (`release`) tag-triggered | GitHub Actions |
| Live customers | 3 (zun-operator, zun-100mb-test DISABLED, zun-customer-demo at 101.6%) | `SELECT username FROM radcheck WHERE value IS NOT NULL` |
| HEAD on `origin/main` | `a3c1adc` (DR runbook v1.0.7) | `git ls-remote origin` |
| Latest tag | `v2.1.1` | `git tag -l` |
| Last deploy | 2026-07-12 08:17 SAST / 06:17 UTC | `docs/PHASE-4E-DEPLOYMENT-NOTES.md` |

## Phase status

| Phase | Description | Status |
|---|---|---|
| **5A** | Foundation: conn config, user+pool+VIP pin, public-path test, MSS clamp, server cert, monitoring, backup | ✅ **GREEN** (signed off 2026-06-18) |
| **5B** | Quota layer (iptables-legacy per-VIP byte counters + 60s monitor daemon + 80% warn + 100% hard cut) | ✅ **GREEN** (v1.1.0, 2026-06-19) |
| **5C.1+5C.2** | Self-service portal (FastAPI + vanilla JS) — customer + operator | ✅ **GREEN** (v1.2 → v1.3.0) |
| **5C.3** | Grafana `strongswan-quota` dashboard | ✅ **GREEN** (v1.2.2) |
| **5C.4** | ~~RustFS daily backup verify~~ | ⛔ **CANCELLED** — replaced by PBS full-LXC |
| **5C.5/5C.6** | ~~Self-service / multi-device~~ | ⛔ **SHELVED** — strongSwan 1-identity-1-VIP blocks per-device under EAP-MSCHAPv2; per-device would require EAP-TLS |
| **5D / Phase 4** | Portal ↔ MariaDB split-brain unification (portal business data in MariaDB `radius`) | ✅ **GREEN** (Phase 4E `cb9bf69`, 2026-07-12) |
| **5D / Phase 5** | Charon `eap-radius` cutover (charon proxies auth to FreeRADIUS) | ✅ **GREEN / LIVE** (v2.0.0, 2026-07-06) |
| **5D / Phase 5+** | RFC 5176 DAE Disconnect-Request hard-cut at 100% quota | ✅ **GREEN** (`3b00b8e`) |
| **5D / Phase 6** | Customer re-registration via portal (radcheck is now the source of truth) | ✅ **GREEN** (v2.0.0) |
| **5D / Phase 7** | Cleanup: vestigial eap-* blocks removed, FR IPv6 secret realigned, dead SQLite refs deleted | ✅ **GREEN** (v2.0.0 + v2.1.1) |
| **5H** | HA + LB (2x VPS + keepalived VRRP + shared DB) | ⏳ **NOT STARTED** — plan at `docs/PLAN-5H-HA-LB.md`; RTO/RPO numbers in DR runbook |

## Documentation set (validated, ISO 9001:2015 compliant)

All docs are stored in Paperless NGX (doc IDs 79–88) and mirrored to `rustfs:/open-claw-push/Validated docs/`. The DR runbook covers end-to-end rebuild on a fresh VPS from off-server secrets + kopia backups + this repo.

| Doc ID | Title | Audience |
|---|---|---|
| `DAT-OPS-DR-RUNBOOK-001` v1.0.7 | VPN Stack DR + HA — fact-grounded rebuild procedure | Internal ops |
| `DAT-VPN-INT-ARCH-001` v1.0.1 | Internal Architecture (this stack) | Internal |
| `DAT-VPN-INT-SEC-001` v1.0.0 | Internal Security Stack | Internal |
| `DAT-VPN-INT-SOP-001` v1.0.0 | Internal SOP (operator runbook) | Internal |
| `DAT-VPN-INT-WIN-001` v1.1.0 | Windows Client Setup — operators | Internal |
| `DAT-VPN-EXT-ARCH-001` v1.0.0 | External Architecture Overview | Customer-facing |
| `DAT-VPN-EXT-PP-001` v1.0.0 | Privacy Policy | Customer-facing |
| `DAT-VPN-EXT-SOP-001` v1.0.0 | Client Onboarding & Offboarding SOP | Customer-facing |
| `DAT-VPN-EXT-TOS-001` v1.0.0 | Terms of Service | Customer-facing |
| `DAT-VPN-EXT-WIN-001` v1.0.0 | Windows Client Setup — customers | Customer-facing |

DR runbook source of truth: [`docs/RUNBOOK-DR-REBUILD-AND-HA.md`](docs/RUNBOOK-DR-REBUILD-AND-HA.md).

## What's where

| Path | What's in it |
|---|---|
| `docker/` | The strongSwan container: Dockerfile, docker-compose, swanctl configs, `10-eap-radius.conf` (Phase 5), in-image `start.sh` |
| `host/strongswan/` | Charon-side ops: `swanctl/conf.d/10-eap-radius.conf`, `swanctl/conf.d/rw-eap.conf` (EAP fallback only), iptables-legacy per-VIP rules, `quota-monitor.py` + systemd unit |
| `host/vpn-portal/` | FastAPI customer/operator portal: `app.py` (v2.1.1), `portal_auth.py`, `installer_tokens.py`, `tests/`, `www/` |
| `host/scripts/` | Operate-time: `deploy-portal-vps.sh`, `vpn-disconnect.py` (RFC 5176 DAE sender), cert gen, DB seed, image build, daily backup, `reset_quota` |
| `tools/` | `ci-drift-detect.sh`, `sync-from-live.sh`, `check-portal-deployed.sh`, `check_github_parity.sh` |
| `docs/` | All `DAT-*` validated docs, `ROADMAP`, `ARCHITECTURE`, `DEPLOYMENT`, `ISSUES-LOG`, `SESSION-HISTORY`, `VPS-XNEELO-DEPLOY`, `CLOUDFLARE-DNS`, **`RUNBOOK-DR-REBUILD-AND-HA`**, `PHASE-4E-*` |
| `examples/` | Client profiles: Android `.sswan`, iOS `.mobileconfig` template (iOS path broken; use strongSwan app for EAP-MSCHAPv2) |

## CI

Four workflows in `.github/workflows/`:

- **`ci.yml`** — runs on every push to `main` and every PR. Spins up a MariaDB service, runs `pytest` (162 tests), smoke-tests the portal. Bumps every GitHub Action to current Node.js (no deprecation warnings).
- **`drift-detect.yml`** — runs on push + every 6h. SSHs to `vps-01` and MD5-checks four high-risk files (portal code + swanctl config) against the repo HEAD. Catches manual LIVE edits before they cause deploy drift. Companion: `tools/ci-drift-detect.sh`, `tools/sync-from-live.sh`. Requires GitHub secret `VPSSSH` (private key authorized on VPS) + var `VPS_HOST=154.65.110.44`.
- **`portal-smoke.yml`** — runs `node tools/portal-smoke.js` (headless-browser UI test, 8 checks). Screenshots upload as artifacts (3-day retention).
- **`release.yml`** — tag-triggered. Builds the strongSwan image, pushes to `ghcr.io/dippie-wp/databyte-ikev2:<tag>` + `:latest`, creates GitHub release with auto-generated notes.

## Versions

Latest release line is **v2.x** (Phase 5 eap-radius cutover). v1.x is historical — kept for reference only; do not build v1.x tags into prod images.

### v2.1.1 (2026-07-12) — "dead-code cleanup post-Phase-4E"

Phase 4E moved portal business data from SQLite to MariaDB but left behind two portal-side SQLite references as dead code. Removed:
- `host/vpn-portal/portal_auth.py`: `_sqlite_query()` + 4 env vars + dead comment block + `import json` (45 lines, zero callers post-4E)
- `tests/conftest.py`: `patch_portal_auth_db` no longer intercepts `subprocess.run` (70 lines)
- `host/vpn-portal/installer_tokens.py`: stale "vps-01 portal runs SQLite" comment (1 line)

Kept (intentionally): strongSwan's `/var/lib/strongswan/ipsec.db` (charon's VICI config DB, not portal data); `bulk_action.py` (ssh-to-LXC-903 ops, out of scope). 162 tests pass.

### v2.1.0 (2026-07-11) — "case-insensitive identity normalization + CI drift detection"

Three sister files did `WHERE u.name = ?` against `users.name` in charon SQLite — case-**sensitive**. FreeRADIUS MariaDB is case-insensitive. So VPN connected fine but downstream services silently got wrong defaults (e.g. Siraaj hit a 20/20 mbit fallback that happened to match her requested plan — invisible bug). Fixed at all 3 call sites with `.strip().lower()` normalization.

Also added: GitHub Actions `drift-detect.yml` workflow + `tools/ci-drift-detect.sh` + `tools/sync-from-live.sh`. Models after HOOP.dev "IaC Drift Detection in GitHub CI/CD".

### v2.0.0 (2026-07-06) — "Phase 5 cutover: charon → FreeRADIUS"

**Architectural boundary.** First v2 baseline. Customer EAP identities now live in MariaDB `radcheck`/`radusergroup`, not in `rw-eap.conf`. Quota hard-cut = disable radcheck + send signed Disconnect-Request, not rewrite local EAP secret.

Includes:
- **Phase 5 cutover** — `rw-eap` connection uses `eap-radius {}` (`5891d45`)
- **RFC 5176 DAE** — `host/scripts/vpn-disconnect.py` opens UDP 3799, sends signed Disconnect-Request to `charon eap-radius.dae` (`3b00b8e`)
- **Reset bug fix** — `reset_quota` restores radcheck from pre-cut backup (`fe60527`)
- **Pool-LEASE attribution sync** — quota-monitor reads `swanctl --list-pools --leases` for live VIP→identity mapping (`9a93832`)
- **Portal SQLite/MariaDB split-brain fix** — `lookup_user_and_customer` + 2 siblings now read portal-local SQLite (later unified into MariaDB in Phase 4E)
- **DAE unit + integration tests** — 5 packet-shape + 1 live-charon test (`ffd6c5d`)
- **30s dashboard auto-refresh** — operator no longer misses quota cuts (`d6bd0e2`)
- **Phase 7 cleanup 1** — vestigial `eap-*` blocks removed (`rw-eap.conf` 71 → 59 lines)
- **Phase 7 cleanup 3** — FR `clients.conf` IPv6 secret realigned (root cause of 15+ "Invalid Message-Authenticator" bursts across charon reloads)

### v2.0.0 → v2.1.1 highlights

| Version | Date | What |
|---|---|---|
| v2.1.1 | 2026-07-12 | Dead-code cleanup post-Phase-4E |
| v2.1.0 | 2026-07-11 | Case-insensitive identity normalization (3 sites) + CI drift detection |
| v2.0.0 | 2026-07-06 | Phase 5 eap-radius cutover + DAE + reset bug fix + Phase 7 cleanups |

### v1.x — historical (do not build into prod)

- **v1.0 (2026-06-18):** EAP + attr-sql + sticky VIPs, public-path tested on 5G, monitoring via Prometheus, backup to RustFS
- **v1.1.0 (2026-06-19):** Quota layer (5B). 3 end-to-end runs with real iOS traffic, all cut correctly.
- **v1.2.x → v1.2.14 (2026-06-20 → 21):** Device-info UI, VICI parser hardening, reboot fixes, self-service portal polish, operator client onboarding, customer portal at `/portal/`
- **v1.3.0 → v1.4.6 (2026-06-21 → 23):** Production portal at `vpn-portal.databyte.co.za`, `customers.user_id` FK, strict-CSP refactor
- **v1.5.0 → v1.7.5 (2026-06-23 → 28):** `speed_plan` per-customer, Windows PowerShell auto-installer (HARDLOCKED at v2.6.5 — see CHANGELOG note on v2.x numbering), SSE-replace-polling for live data. Release tag for this baseline: **`v1.7.0-recovered`** (recovery from 2026-06-27 incident).

**Note on v2.x numbering:** The `v2.3.0`/`v2.6.0`/`v2.7.x` references in `tracker/generate_tracker.py` are **Windows installer version labels** for `setup-databyte-vpn.ps1` (HARDLOCKED at v2.6.5), not git tags. The `v2.0.0` git tag (2026-07-06) is the **first v2 baseline** — the architectural boundary at Phase 5 eap-radius cutover.

## Quick start (per-host / customer self-host)

A complete end-to-end deploy from a fresh Linux box. Single-operator setup — you host the server, you use the server, you administer it. Onboarding: per-customer baked installer (`setup-databyte-vpn-windows.ps1` template, see `scripts/README-windows-vpn.md`).

> **For the Xneelo VPS production deployment (`vps-01`, `myvpn.databyte.co.za`):** see [`docs/VPS-XNEELO-DEPLOY.md`](docs/VPS-XNEELO-DEPLOY.md) and the DR runbook at [`docs/RUNBOOK-DR-REBUILD-AND-HA.md`](docs/RUNBOOK-DR-REBUILD-AND-HA.md). The production stack uses FreeRADIUS + MariaDB (the architecture this repo is currently on) — the per-host setup below is the **gateway-only** path without RADIUS.

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

> **Note:** The script default uses RSA-2048 for the server cert (changed 2026-06-19 — ECDSA P-256 was rejected by iOS 18+ IKEv2). Signature is PKCS#1 v1.5 with sha256 (RSASSA-PSS was tried but iOS 18 silently rejected it — rolled back to PKCS#1 v1.5). Cert is 1y expiry; rotate manually.

### 3. Set your admin password

The default `rw-eap.conf.template` has an `eap-zun` user. Edit it to your identity and set a password:

```bash
cp docker/swanctl/conf.d/rw-eap.conf.template docker/swanctl/conf.d/rw-eap.conf
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
   - Username: `yourname` (lowercase — see CORR-2026-07-11-026 in CHANGELOG)
   - Password: `YourStrongPassword2026!`
4. Connect — should get VIP 10.99.0.50

#### iPhone / iPad (iOS 18+)

**Use the strongSwan iOS app** ([App Store link](https://apps.apple.com/app/strongswan-vpn-client/id1453698374)) — iOS native VPN Settings + `.mobileconfig` is **fundamentally broken for EAP-MSCHAPv2** on iOS 18+ (iOS sends EAP identity, server sends MSCHAPV2 challenge, iOS never responds). The strongSwan app is the official client and has a working EAP-MSCHAPv2 implementation.

Setup:
1. Install **strongSwan VPN Client** from the App Store (free)
2. Open the app → tap **+** to add a profile
3. **Server:** your public IP or hostname (e.g. `vpn.example.com` or `102.182.117.43`)
4. **Username:** the strongSwan user you seeded (lowercase — e.g. `yourname` or `demo-phone`)
5. **Password:** the secret you set in `docker/swanctl/conf.d/rw-eap.conf`
6. **CA certificate:** import the `strongswan-ca.crt.pem` (e.g. air-drop it, or download from a URL you host)
7. **Server identity (advanced / settings cog):** must match the server cert CN/SAN, e.g. `vpn.example.com`. If the app auto-fills the IP, **change it** — charon matches on IDr.
8. Tap **Save** → flip the toggle

If the app says "trust this CA": enable it. If "no proposal chosen": the server expects AES-256/SHA2-256/DH14 (default in the strongSwan app — should just work).

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

> **For Databyte customers:** the canonical Windows installer is `setup-databyte-vpn-<customer>-<device>.ps1` (baked by operator from the customer portal, see `DAT-VPN-EXT-WIN-001`). Hardlocked at v2.6.5; mirrors at `vpn-portal.databyte.co.za/static/baked/`.

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

For HA rollback (multiple instances + LB), see `docs/PLAN-5H-HA-LB.md` and the DR runbook.

## Critical known limitations

1. **charon-cmd 5.9.5** in test environment incompatible with 6.0.7 server's EAP-Identity flow. Real load test deferred.
2. **5G IP rotation** can cause brief IKE_SA re-auth on iOS (not pure MOBIKE; functionally equivalent).
3. **MSS clamp at 1260** required for 5G carriers. Lives in `host/iptables/rules.v4`. Forgetting this → iana.org-style timeouts.
4. **EAP creds in plaintext** in `swanctl.conf` `secrets` block (per-host quick-start only; production uses FreeRADIUS `radcheck` which is also plaintext in MariaDB — TLS-side hardening deferred).
5. **No CRL/OCSP.** Server cert has 1-year validity, manual rotation. Bleichenbacher mitigation deferred.
6. **charon-log** lives inside container — must bind-mount to host for log shipping.
7. **DAE secret** must be copied off-server for DR rebuild — see `RUNBOOK-DR-REBUILD-AND-HA.md` §0.5 secret checklist (item 5).
8. **Multi-device per customer** is blocked by strongSwan's 1-identity-1-VIP design under EAP-MSCHAPv2. Per-device would require EAP-TLS (5C.5/5C.6 SHELVED — see `docs/PLAN-5C6-MULTIDEVICE-CREDENTIALS.md`).
9. **Case sensitivity of customer identity** — `users.name` lookups in charon's VICI sqlite are case-sensitive. The portal normalizes on input (`.strip().lower()`), but if you bypass the portal you MUST send lowercase identities (CORR-2026-07-11-026).

## License

None declared. Personal project.

## Maintainer

Zun (@zuzu172 on Telegram, github Dippie-WP). Built with Misha 🐻.