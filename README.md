# Databyte VPN

Self-hosted IKEv2 VPN for customer + operator use — **strongSwan 6.0.7** (EAP-MSCHAPv2) + **FreeRADIUS** (MariaDB identity) + **FastAPI portal** (customer self-service) + **Telegram bot** (operator admin).

Production: `<your-hostname>` (cloud VPS, `<your-public-ip>`), Debian 13 trixie. Portal v2.2.1, Bot v2.5.0. Last live fact-check: 2026-10-15.

## If you are...

| You want to... | Start here |
|---|---|
| **Use the VPN** (customer) | `vpn.example.com` — Windows/iOS/Android client setup in [`docs/DAT-VPN-INT-WIN-001.md`](docs/DAT-VPN-INT-WIN-001.md) |
| **Self-host for yourself** (gateway only, no RADIUS) | [`docs/QUICKSTART.md`](docs/QUICKSTART.md) |
| **Deploy on a fresh Debian 13 VPS** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — "Build from scratch" section is the canonical guide |
| **Rebuild from backup (DR)** | [`docs/RUNBOOK-DR-REBUILD-AND-HA.md`](docs/RUNBOOK-DR-REBUILD-AND-HA.md) v1.1.0 — 24 steps, 75-135 min |
| **Understand the architecture** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — what + why + how to build |
| **Check live prod state** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §"Reference deployment" — verified-live values |
| **Read the operator guides** | [`docs/`](docs/) |

Historical/superseded docs (e.g. older deploy guides, Phase 4E migration notes, LXC lab design) are in [`docs/archive/`](docs/archive/) for reference.

## Versions

| Component | Version | Where it lives |
|---|---|---|
| Portal (FastAPI) | v2.2.1 | this repo, HEAD `8e02a6e` |
| Bot (Telegram) | v2.5.0 | `vpn-admin-bot` repo, HEAD `01ad543` |
| strongSwan image | `6.0.7-mschapv2-attrsql` | built fresh per host, not registry-pushed |

This repo uses commit-history versioning, not GitHub releases.

## Maintainer

Maintained by the operator. "Built with Misha 🐻" — the assistant that wrote most of these docs.

Fork-friendly — replace the product name, domain, VPS provider, and any contact details to match your deployment.
