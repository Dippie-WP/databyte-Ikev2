# strongswan-vpn-gateway

Self-hosted IKEv2 VPN stack — strongSwan EAP-MSCHAPv2 gateway + FreeRADIUS identity store + FastAPI customer/operator portal. Latest release `v2.2.1`.

[![CI](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml) [![drift-detect](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/drift-detect.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/drift-detect.yml) [![portal-smoke](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/portal-smoke.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/portal-smoke.yml) [![Release](https://img.shields.io/github/v/release/Dippie-WP/databyte-Ikev2)](https://github.com/Dippie-WP/databyte-Ikev2/releases)

## What this is

A self-hosted IKEv2 VPN stack. StrongSwan handles IPsec; FreeRADIUS holds customer identities; the FastAPI `vpn-portal` provides customer self-service + operator admin. Customers connect from anywhere over 5G/WiFi, authenticate via EAP-MSCHAPv2, and get per-user sticky VIPs.

**For self-hosters:** see [`docs/QUICKSTART.md`](docs/QUICKSTART.md) for the per-host install walkthrough (cert gen, docker compose, client setup for Android/iOS/Windows/Linux).

**For operators / collaborators:** production deploy details, runbooks, and internal artefacts live under [`internal/`](internal/) (this repo).

## Features

- **strongSwan 6.0.7** EAP-MSCHAPv2 gateway with custom Dockerfile (CVE-patched)
- **FreeRADIUS** identity store (case-insensitive MariaDB collation)
- **Per-user sticky VIPs** via `attr-sql` + `charon.ipsec.sqlite`
- **Quota enforcement** at 100% — disables radcheck + sends RFC 5176 Disconnect-Request (hard kill, no grace period)
- **FastAPI portal** for customer self-service + operator admin
- **4 CI workflows**: pytest, drift-detect (catches manual LIVE edits), portal-smoke, release
- **pytest + MariaDB test harness** for portal code

## What's where

| Path | Purpose |
|---|---|
| `docker/` | strongSwan container (Dockerfile, docker-compose, swanctl configs, in-image start.sh) |
| `host/` | Charon-side ops + FastAPI portal (app.py, auth helpers, installer tokens, tests, web assets) |
| `host/strongswan/` | swanctl config, iptables per-VIP rules, quota monitor + systemd unit |
| `host/vpn-portal/` | FastAPI customer/operator portal |
| `host/scripts/` | Operate-time: deploy, cert gen, DB seed, image build, daily backup, quota reset, DAE sender |
| `tools/` | CI drift-detect + live-sync scripts |
| `docs/` | Public-safe docs: architecture, deployment, runbooks, customer setup guides, quickstart |
| `examples/` | Client profiles (Android `.sswan`, iOS `.mobileconfig` template) |
| `tests/` | Portal pytest suite (full integration tests) |
| `internal/` | ⚠️ Internal operator docs: production deploy, changelog, TODO, token revocations |

## CI

Four workflows in `.github/workflows/`:

- **`ci.yml`** — pytest on every push + PR. Spins up MariaDB service.
- **`drift-detect.yml`** — SSHs to production VPS, MD5-checks high-risk files vs git HEAD. Catches manual LIVE edits.
- **`portal-smoke.yml`** — headless-browser UI test against the portal.
- **`release.yml`** — tag-triggered. Builds container image, pushes to `ghcr.io/dippie-wp/databyte-ikev2:<tag>`, creates GitHub release.

## Versions

Latest release line is `v2.x`. See [releases](https://github.com/Dippie-WP/databyte-Ikev2/releases) for changelog. Full change history (with CORR-IDs, deployment notes, post-mortems) is in [`internal/CHANGELOG.md`](internal/CHANGELOG.md).

## License

None declared. Personal project.

## Maintainer

Zun — [github.com/Dippie-WP](https://github.com/Dippie-WP). Built with Misha 🐻.
