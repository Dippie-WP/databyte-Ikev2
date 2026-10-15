# Databyte VPN

Self-hosted IKEv2 VPN stack — **strongSwan** (EAP-MSCHAPv2) + **FreeRADIUS** (MariaDB identity) + **FastAPI portal** (customer/operator) + **Telegram bot** (operator admin).

[![CI](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/ci.yml) [![drift-detect](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/drift-detect.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/drift-detect.yml) [![portal-smoke](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/portal-smoke.yml/badge.svg)](https://github.com/Dippie-WP/databyte-Ikev2/actions/workflows/portal-smoke.yml) [![Release](https://img.shields.io/github/v/release/Dippie-WP/databyte-Ikev2)](https://github.com/Dippie-WP/databyte-Ikev2/releases) *(no GitHub releases — see "Versions" below)*

---

## Live state (verified 2026-10-15 06:50 UTC / 08:50 SAST)

All values in this table were verified live against `vpn-prod-01` via `ssh` + `mariadb` + `docker` + `certbot` commands in this turn. Counts grow as customers connect — treat as a snapshot, not a constant.

| Item | Value | How verified |
|---|---|---|
| **Server** | `vpn-prod-01` (Xneelo VPS, was `vps-01` — renamed per HOT-210, 2026-09-30) | `ssh root@vpn-prod-01 'hostname'` |
| **Public IPv4** | `154.65.110.44` | `curl ifconfig.me` from prod |
| **Tailscale IP** | `100.64.212.47` | `tailscale ip -4` on prod |
| **OS** | Debian 13.7 (trixie) | `/etc/debian_version` on prod |
| **Uptime** | 6 days, 19 hours | `uptime -p` on prod |
| **charon container** | `zun/strongswan:6.0.7-mschapv2-attrsql` — running 6 d 19 h (since 2026-10-08T14:06:34Z) | `docker inspect strongswan` on prod |
| **Portal** | **v2.2.1** (deployed 2026-08-10, git HEAD `8e02a6e`) | `systemctl cat vpn-portal.service` Description on prod |
| **Bot** | **v2.5.0** (HEAD `01ad543` in `vpn-admin-bot` repo) | `git tag --sort=-v:refname` in vpn-admin-bot |
| **MariaDB** | 11.8.6-MariaDB-0+deb13u1, **`radius` DB = 45 tables** | `SELECT VERSION();` + `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius'` |
| **`radcheck` rows** | 28 | `SELECT COUNT(*) FROM radcheck` |
| **`radpostauth` rows** | 2,301 | `SELECT COUNT(*) FROM radpostauth` |
| **`radacct` rows** | 1,168 | `SELECT COUNT(*) FROM radacct` |
| **`customers` rows** | 12 | `SELECT COUNT(*) FROM customers` |
| **`users` rows** | 12 | `SELECT COUNT(*) FROM users` |
| **`devices` rows** | 11 | `SELECT COUNT(*) FROM devices` |
| **`operators` rows** | 1 | `SELECT COUNT(*) FROM operators` |
| **LE cert `myvpn.databyte.co.za`** | ECDSA, expires **2026-12-02** (48 days valid) | `certbot certificates` on prod |
| **LE cert `adminer.databyte.co.za`** | ECDSA, expires **2026-12-02** (48 days valid) | `certbot certificates` on prod |
| **Backups** | kopia → `kop.databyte.co.za:443` — **502 Bad Gateway, PARKED** (Zun msg #43961) | `kopia repository status` on prod |

---

## What this is

A self-hosted IKEv2 VPN stack. Charon handles IPsec; FreeRADIUS holds customer identities (in MariaDB, not charon-local SQLite); the FastAPI `vpn-portal` provides customer self-service + operator admin; the Telegram bot provides operator admin via `/sessions`, `/customers`, `/logs` commands. Customers connect from anywhere over 5G/WiFi, authenticate via EAP-MSCHAPv2 (proxied to FreeRADIUS via `eap-radius` plugin), and get per-user sticky VIPs in the `10.99.0.0/24` pool.

| Use case | Start here |
|---|---|
| **Self-host** (you only, no customers) | [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — gateway-only path, no RADIUS |
| **Operator** — greenfield Xneelo VPS deploy | [`docs/VPS-XNEELO-DEPLOY.md`](docs/VPS-XNEELO-DEPLOY.md) — script + 16 post-script steps |
| **Operator** — DR rebuild on fresh VPS | [`docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0](docs/RUNBOOK-DR-REBUILD-AND-HA.md) — 24 steps, 75-135 min |
| **Architect** — current state + design shifts | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — live-state table 2026-10-15 |
| **Customer install** — Windows client | [`docs/DAT-VPN-INT-WIN-001.md`](docs/DAT-VPN-INT-WIN-001.md) (HARDLOCKED, production-approved) |

---

## Features (live-verified 2026-10-15)

- **strongSwan 6.0.7** EAP-MSCHAPv2 gateway — runs as a Docker container with `--network host` (NOT bridge), 7 bind mounts (charon config + log + state)
- **FreeRADIUS** identity store — MariaDB-backed, 45 tables in `radius` DB, case-insensitive collation (`utf8mb4_unicode_ci`)
- **Per-VIP byte counters** — nftables named meters in `inet quota_table` (Phase 7.5; NOT iptables-legacy)
- **Quota enforcement at 100%** — nftables counter + RADIUS identity DISABLE + RFC 5176 Disconnect-Request (UDP/3799 → charon)
- **FastAPI customer/operator portal** — v2.2.1, systemd-managed, 2,301 radpostauth + 1,168 radacct rows processed
- **Telegram bot** — v2.5.0, runs as `bot-polling.service` (sole polling source per HOT-261), chat_id 7748884597
- **daloRADIUS** admin UI on `127.0.0.1:8000` (operator-facing, loopback-only)
- **4 CI workflows** — `ci.yml` (pytest), `drift-detect.yml` (catches manual LIVE edits), `portal-smoke.yml` (headless-browser UI), `release.yml` (tag-triggered)
- **kopia backups** to `kop.databyte.co.za:443` — currently **502 Bad Gateway (PARKED per Zun msg #43961)**; last successful `.update-info.json` mtime 2026-10-10 00:00
- **pytest + MariaDB test harness** for portal code

---

## What's where

| Path | Purpose |
|---|---|
| `docker/` | strongSwan container (Dockerfile, swanctl configs, in-image `start.sh`) |
| `docs/` | Public-safe docs: architecture, runbooks, deploy, customer setup guides |
| `examples/` | Client profiles (Android `.sswan`, iOS `.mobileconfig` template) |
| `host/` | Charon-side ops: `host/vpn-portal/` (FastAPI code), `host/freeradius/` (operator overlay), `host/backup/` (rclone to RustFS), `host/scripts/` (operational), `host/iptables/`, `host/sysctl.d/`, `host/systemd/`, etc. |
| `ops/` | Operational tools |
| `quota/` | Quota subsystem (nftables monitor + bandwidth monitor) |
| `scripts/` | Top-level scripts: `bootstrap-xneelo.sh` (one-shot prod install), `gen-certs.sh`, `build-image.sh`, `strongswan-db-backup.sh` |
| `tests/` | Portal pytest suite (full integration tests against MariaDB) |
| `tools/` | CI drift-detect + live-sync scripts |
| `tracker/` | Project tracker |

**Note:** this repo previously referenced an `internal/` folder (operator docs) — that folder **does not exist** in this repo. If you find a reference to it in older docs, treat as historical.

---

## CI

Four workflows in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| **`ci.yml`** | every push + PR | pytest; spins up MariaDB service |
| **`drift-detect.yml`** | cron (4-7 h interval) + manual | SSHs to `vpn-prod-01`, MD5-checks high-risk files vs git HEAD; catches manual LIVE edits |
| **`portal-smoke.yml`** | every push + PR | headless-browser UI test against the portal |
| **`release.yml`** | tag push | Builds container image, pushes to `ghcr.io/dippie-wp/databyte-ikev2:<tag>`, creates GitHub release |

**Current status (2026-10-15):** all 4 workflows **green** on HEAD `26e46d4`. 6 commits pushed in this batch (pushed at 2026-10-15 05:34 UTC, all checks passed):

| # | Commit | What |
|---|---|---|
| 1 | `746262a` | `docs: refresh RUNBOOK-DR-REBUILD-AND-HA to v1.1.0 (2026-10-15 fact-check)` |
| 2 | `bda11ae` | `docs: refresh ARCHITECTURE verified-live table to 2026-10-15 (v2.2.1 + v2.5.0)` |
| 3 | `3337129` | `docs: add VPS-XNEELO-DEPLOY v1.0.0 (Xneelo VPS deploy procedure)` |
| 4 | `1ad0b9b` | `docs: fix DEPLOYMENT.md 'does everything' claim + off-by-one in 2 places` |
| 5 | `d295765` | `docs: fix 24 vps-01 -> vpn-prod-01 refs in 3 customer-facing Windows docs` |
| 6 | `26e46d4` | `docs: add DAT-VPN-INT-WIN-LIVE-001 v0.1.0 (parallel live-state fact-check)` |

All 6 also backed up to `rustfs:open-claw-push/homelab-vpn/docs-factcheck-2026-10-15/docs/`.

---

## Deploy / DR / Architecture pointers

| Need | Doc |
|---|---|
| Greenfield Xneelo VPS deploy (script + 16 post-script steps for prod parity) | [`docs/VPS-XNEELO-DEPLOY.md`](docs/VPS-XNEELO-DEPLOY.md) v1.0.0 (2026-10-15) |
| DR rebuild from kopia on fresh VPS (24 steps, 75-135 min) | [`docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0](docs/RUNBOOK-DR-REBUILD-AND-HA.md) |
| Current prod state + architecture shifts since v1.x | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (live-state table 2026-10-15) |
| Windows client install (HARDLOCKED, production-approved) | [`docs/DAT-VPN-INT-WIN-001.md`](docs/DAT-VPN-INT-WIN-001.md) v1.1.0 (HARDLOCKED, 2026-07-07) |
| Windows client install — **live-state parallel** (fact-check, discrepancy log) | [`docs/DAT-VPN-INT-WIN-LIVE-001.md` v0.1.0](docs/DAT-VPN-INT-WIN-LIVE-001.md) (2026-10-15) |
| Windows client install — operator guide | [`docs/DAT-VPN-INT-WIN-GUIDE-001.md`](docs/DAT-VPN-INT-WIN-GUIDE-001.md) |
| Windows client install — hostile networks (captive portals, SonicWall, FortiGate) | [`docs/DAT-VPN-INT-WIN-HOSTILE-001.md`](docs/DAT-VPN-INT-WIN-HOSTILE-001.md) |
| Cloudflare DNS records + API token scope | [`docs/CLOUDFLARE-DNS.md`](docs/CLOUDFLARE-DNS.md) |

---

## Versions

| Component | Version | Source / commit | Notes |
|---|---|---|---|
| **Portal (FastAPI)** | **v2.2.1** | systemd Description on prod; git HEAD `8e02a6e` in databyte-Ikev2 | Deployed 2026-08-10; `app.py` 3,266 lines (SHA `baca20f7...`); `bot.py` 1,988 lines (SHA `8cbda46e...`) |
| **Bot (Telegram)** | **v2.5.0** | tag `v2.5.0` in `vpn-admin-bot` repo; HEAD `01ad543` | Latest bot; previous tags: v2.4.2, v2.4.1, v2.4.0 |
| **strongSwan image** | `6.0.7-mschapv2-attrsql` | `zun/strongswan` Docker image, built from `docker/Dockerfile` | Running 6 d 19 h on prod |
| **MariaDB** | 11.8.6-MariaDB-0+deb13u1 | Debian 13 (`trixie`) package | 45 tables in `radius` DB |
| **OS** | Debian 13.7 (trixie) | `/etc/debian_version` on prod | — |

**No GitHub releases on this repo.** Versions are tracked in commit history + per-component streams:
- **Portal** version lives in this repo (`databyte-Ikev2`) — tracked in systemd Description + commit history
- **Bot** version lives in the `vpn-admin-bot` repo (separate version stream) — tracked via tags
- **strongSwan image** version is the Docker tag (built fresh per host, not pushed to a registry)

---

## License

None declared. Personal project.

---

## Maintainer

Zun — [github.com/Dippie-WP](https://github.com/Dippie-WP). Built with Misha 🐻.
