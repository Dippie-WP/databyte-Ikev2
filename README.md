# strongswan-vpn-gateway

Personal strongSwan EAP VPN gateway — IKEv2 in a Docker container on an LXC host. Per-user sticky VIPs (attr-sql + SQLite), server-cert auth + EAP-MSCHAPv2 with PSK fallback. 5A/5B/5C green; pre-launch commercial stack live.

[![CI](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Dippie-WP/databyte-Ikev2)](https://github.com/Dippie-WP/databyte-Ikev2/releases)

**Production:** `https://myvpn.databyte.co.za/` (Xneelo Johannesburg; Cloudflare DNS, grey-cloud for IKEv2)

## What this is

A self-hosted IKEv2 VPN gateway running in a Docker container on an LXC host. Personal + test devices + commercial customers connect from anywhere over 5G/WiFi. Phases 5A (foundation), 5B (quota layer) and 5C (portal surface) are GREEN; 5D is repurposed (2026-07-05) as the RADIUS migration (FreeRADIUS + daloRADIUS on the prod VPS, single MariaDB). The pre-launch commercial stack — per-customer bandwidth caps, customer portal, Windows installer, billing IDs — is **LIVE** on the Xneelo Johannesburg VPS.

## Key facts

- **Image:** `zun/strongswan:6.0.7-mschapv2-attrsql` (custom build)
- **strongSwan:** 6.0.7 (CVE-2026-47895 patched)
- **Auth:** server-cert (RSA-2048 + PKCS#1 v1.5) + EAP-MSCHAPv2 (primary), PSK (fallback)
- **Pool:** `10.99.0.0/24` — per-user sticky VIPs via attr-sql + SQLite
- **Production VPS:** Xneelo Johannesburg — `myvpn.databyte.co.za`
- **Lab:** LXC 903 (`192.168.10.98`, on pve2, Cape Town homelab)
- **Backups:** `rustfs:/open-claw-push/strongswan-{db,configs}/` (daily + ISO-week slots, 14d/8w retention)

## Repo layout

| Path | Contents |
|---|---|
| `docker/` | The container: Dockerfile, docker-compose, swanctl configs, strongswan.d overrides, in-image `start.sh` |
| `host/` | The LXC host: sysctl, iptables, firewalld zone, optional nftables service |
| `scripts/` | Operate-time: cert gen, DB seed, image build, daily backup, rollback |
| `docs/` | All documentation (index below) |
| `examples/` | Client profiles: Android `.sswan`, iOS `.mobileconfig` template |

## Documentation

- [Quick start](docs/QUICKSTART.md) — full deploy from a fresh host (gateway-only)
- [Deployment](docs/DEPLOYMENT.md) — step-by-step on a new host (full stack)
- [Architecture](docs/ARCHITECTURE.md) — design + "build from scratch" guide
- [VPS deploy](docs/VPS-XNEELO-DEPLOY.md) — production runbook
- [Roadmap](docs/ROADMAP.md) — phase status + gates
- [CI](docs/CI.md) — GitHub Actions workflows
- [Cloudflare DNS](docs/CLOUDFLARE-DNS.md)
- [DR rebuild + HA](docs/RUNBOOK-DR-REBUILD-AND-HA.md)
- [Limitations](docs/LIMITATIONS.md) — known constraints
- [Changelog](CHANGELOG.md) — version history + release notes

## Status

5A / 5B / 5C **green** · 5D (RADIUS migration) **in progress** · 5H (HA + LB) **not started**. Full phase table with gates: [docs/ROADMAP.md](docs/ROADMAP.md).

## License

None declared. Personal project.

## Maintainer

Zun ([@zuzu172](https://t.me/zuzu172) on Telegram, GitHub `Dippie-WP`). Built with Misha.
