# strongswan-vpn-gateway

Personal strongSwan EAP VPN gateway. For per-user VIP pinning, attr-sql + SQLite, server-cert + EAP-MSCHAPv2 with PSK fallback. v1.2 lock-in, both gates green.

[![CI](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Dippie-WP/databyte-Ikev2)](https://github.com/Dippie-WP/databyte-Ikev2/releases)

## What this is

A self-hosted IKEv2 VPN gateway running in a Docker container on an LXC host. Single-tenant homelab use today; structured to make multi-tenant / commercial per-user billing straightforward later (Phase 5B–5D roadmap).

- **Image:** `zun/strongswan:6.0.7-mschapv2-attrsql` (custom build)
- **Source:** [Dippie-WP/databyte-Ikev2](https://github.com/Dippie-WP/databyte-Ikev2) — tagged `v1.0` (2026-06-19)
- **StrongSwan version:** 6.0.7 (CVE-2026-47895 patched)
- **Auth:** Server-cert (RSA-2048 + RSASSA-PSS) + EAP-MSCHAPv2 (primary) and PSK (fallback)
- **Pool:** 10.99.0.0/24 with per-user sticky VIPs via attr-sql + SQLite
- **Deployed at:** LXC 903 (192.168.10.98, on pve2 in Cape Town homelab)
- **Backed up to:** `rustfs:/open-claw-push/strongswan-{db,configs}/` (daily + ISO-week slots, 14d/8w retention)
- **Public endpoint:** 102.182.117.43, router forwards UDP 500/4500 → 192.168.10.98
- **Monitoring:** Prometheus exporter on `:9101` (`strongswan_exporter.py`), dashboard `strongswan-v1-2` in Grafana

## What's where

| Path | What's in it |
|---|---|
| `docker/` | The container: Dockerfile, docker-compose, swanctl configs, strongswan.d overrides, in-image `start.sh` |
| `host/` | The LXC host: sysctl, iptables, firewalld zone, optional nftables service |
| `scripts/` | Operate-time: cert gen, DB seed, image build, daily backup, rollback |
| `docs/` | ROADMAP, ARCHITECTURE, DEPLOYMENT, ISSUES-LOG, SESSION-HISTORY |
| `examples/` | Client profiles: Android `.sswan`, iOS `.mobileconfig` template (iOS path is broken; see issues) |

## Quick start (new host)

See `docs/DEPLOYMENT.md`. Headline:

```bash
# 1. Drop in real certs + secrets (NOT in git)
cd docker/swanctl
# Use scripts/gen-certs.sh to generate, or copy from existing host

# 2. Build the image
bash scripts/build-image.sh

# 3. Apply host config (one time)
sudo cp host/sysctl.d/99-strongswan.conf /etc/sysctl.d/
sudo cp host/firewalld/zones/trusted.xml /etc/firewalld/zones/
sudo cp host/iptables/rules.v4.template /etc/iptables/rules.v4
sudo sysctl --system
sudo firewall-cmd --reload
sudo systemctl restart netfilter-persistent

# 4. Bring up
cd docker
docker compose --profile vpn up -d

# 5. Verify
docker logs strongswan --tail 30
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas
```

## Status

| Phase | Description | Gate |
|---|---|---|
| **5A** | Foundation: conn config, user+pool+VIP pin, public-path test, reconnect, MSS clamp, server cert RSASSA-PSS, monitoring, backup | ✅ **GREEN (signed off 2026-06-19)** |

## CI

- **`.github/workflows/ci.yml`** — runs on every push to `main` and every PR. Builds the image, runs smoke tests (charon version, plugin presence, strongswan.conf structure, entrypoint perms), and lints the Dockerfile with hadolint. Bad pushes are blocked.
- **`.github/workflows/release.yml`** — runs on every `v*` tag push. Builds the image, pushes to `ghcr.io/dippie-wp/databyte-ikev2:<version>` + `:latest`, and creates a GitHub release with auto-generated notes.
| **5H** | HA + LB (2x v1.2 + keepalived VRRP, shared DB) — recovery story replaces version regression | ⏳ Queued for after 5A sign-off |
| 5B | Quota layer (nftables accounting + monitor + alerts) | ⏳ Gated on 5A sign-off |
| 5C | Surface (status FastAPI + Grafana dashboard polish) | ⏳ Gated on 5B |
| 5D | Commercial (pricing, payment-triggered reset) | 🔒 Shelved |

## Versions

- **v1.0 (this release, 2026-06-19):** EAP + attr-sql + sticky VIPs, public-path tested on 5G, monitoring via Prometheus, backup to RustFS, server cert RSASSA-PSS signed (Bleichenbacher mitigation per RFC 7427)
- **v1.2** (image tag `6.0.7-mschapv2-attrsql`): same code as v1.0
- **v1.1** (image tag `6.0.7-mschapv2`, still in registry): PSK + EAP, no VIP pinning — **not a valid fallback**, needs static pool in `strongswan.conf` to work at all

## Release notes

### v1.0 (2026-06-19) — "5A lock-in"

**Added:**
- Server cert regenerated with **RSASSA-PSS** signature (`rsassaPss + sha256 + MGF1`) — mitigates Bleichenbacher's attack per RFC 7427
- Server cert: RSA-2048, EKU `serverAuth + ipsecIKE`, SAN `DNS:vpn.homelab.local, IP:102.182.117.43`, 1-year validity
- `swanctl.conf` `secrets` block pattern for EAP users (file-based credential lookup, since `sql` plugin is not loaded)
- Combined `.mobileconfig` for iOS 18+ (CA + VPN payload in one profile; CA Trust toggle required)
- 4 client types tested end-to-end: Android EAP, iPhone PSK, iPhone EAP-MSCHAPv2, Windows EAP-MSCHAPv2
- MOBIKE proven working (LAN↔4G CGNAT migration, VIP preserved)
- Three-layer iptables persistence: `rules.v4` + watchdog service + manual recovery script
- Prometheus exporter (`strongswan_exporter.py`) on port 9101 with per-SA metrics
- Daily backup to RustFS for DB + configs + certs (with CA private key)
- Pinning: VIPs stay identity-pinned across reconnects (attr-sql lease persistence)

**Security decisions:**
- Server cert: RSA-2048 (ECDSA P-256 rejected by iOS 18+ IKEv2 — must be RSA)
- Signature: RSASSA-PSS (PKCS#1 v1.5 vulnerable to Bleichenbacher)
- EAP creds: file-based in `swanctl.conf` `secrets` block; DB column `users.password` is dead data
- iOS mobileconfig contains EAP password in plaintext (acceptable for 5A; switch to EAP-TLS in 5D)

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
