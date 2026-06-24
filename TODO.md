# Project TODO — databyte VPN Gateway

## 🎯 High Priority (next session)

### Testing & Alignment Plan (Zun's directive 2026-06-24 18:36)

Goal: prevent "we keep finding bugs in production" pattern. Portal_auth login bug existed 3 days before being caught.

**Layer 1 — Portal API integration tests (pytest) [START HERE — 2h]**
- [ ] `tests/test_portal_auth.py` — login w/ valid+invalid+expired+disabled+rate-limit
- [ ] `tests/test_customer_lifecycle.py` — create→archive→delete; verify cascade to rw-eap.conf
- [ ] `tests/test_installer_tokens.py` — create→fetch→re-fetch returns 404
- [ ] `tests/test_audit_log.py` — every mutation logged with correct actor
- [ ] `tests/test_strongswan_sync.py` — create customer → confirm EAP block + swanctl --list-creds
- [ ] `tests/conftest.py` — shared fixtures (test client, test customers, cleanup)
- [ ] Wire into `.github/workflows/ci.yml` — run on every push
- [ ] Catches #193 retroactively (portal_auth login SQL bug)

**Layer 2 — DB integrity check [After L1 — 1h]**
- [ ] `scripts/check_db_integrity.py` — runs in CI + on demand
  - Every `users` row has a `devices` row pointing to it
  - Every `customers` row has ≥1 device
  - Every `installer_tokens` row < 7 days old OR consumed
  - Every EAP block in rw-eap.conf has matching user in DB
  - Every active customer has matching EAP block
- Catches C-1 (orphaned devices) + B-1 (stale tokens) retroactively

**Layer 3 — Static analysis [After L1 — 30m]**
- [ ] `scripts/check_stale_refs.sh` — grep for `102.182.117.43`, `vpn.homelab.local`, `192.168.10.98`
- [ ] Pre-commit hook + CI step
- [ ] Allowlist: `*.bak`, `archive/`, `gen-certs.sh` arg name
- Catches A-1 (app.js homelab) + A-3 (template) retroactively

**Layer 4 — E2E smoke [After L1 — 1h]**
- [ ] `scripts/smoke.sh` — runs every 6h on LXC 903
- [ ] Login as each test customer
- [ ] Fetch /api/portal/me, /api/customers/13/quota
- [ ] `swanctl --list-creds` count
- [ ] Telegram alert on any failure

## 🟡 Medium Priority

- [ ] **Per-tier bandwidth limits** (replace flat 20/20 with tier-based) — 1h
- [ ] **CP7 security hardening** (6 items: fail2ban portal jail, AIDE, backups, cert expiry monitor, INPUT rule tightening, iptables-nft consolidation) — 3-4h total
- [ ] **ipBan service to VPS** (currently INACTIVE on VPS, only active on LXC 903) — 30m

## 🟢 Low Priority (polish)

- [ ] systemd `RuntimeDirectoryMode` duplicate key cleanup
- [ ] CSP `report-uri` endpoint
- [ ] logrotate config for vpn-portal
- [ ] DAT-VPN-CLIENT-WINDOWS-INSTALLER-001 SOP (formal customer-facing doc)
- [ ] nftables migration

## 🔵 Future

- [ ] 5G CGNAT for iPhone clients
- [ ] HA failover (PLAN-5H-HA-LB.md)
- [ ] Let's Encrypt DNS-01 (DONE 2026-06-24, just renewal automation)

## ⚪ Other Active Projects (NOT VPN, deferred)

- Veeam Windows backup (NFR 20 PCs)
- ERP P2V testing
- TrueNAS config backup automation
- Blue Iris NVR virtualization

## ✅ Recently Shipped

- **2026-06-24 16:55** — Code fix: app.js + templates use myvpn.databyte.co.za (homelab separated from VPS). Commit `afdd879`.
- **2026-06-24 17:35** — Per-platform test customers created (5 customers, demo_100mb tier).
- **2026-06-24 18:27** — `test-android-2-phone` customer created.
- **2026-06-24 18:34** — **Customer portal login bug FIXED** (portal_auth.py). Commit `49895dc`.
- **2026-06-24 18:36** — Testing/alignment plan logged as TODO (this file).

## 📚 Reference

- Master manual: `docs/DAT-VPN-WINDOWS-CLIENT-MASTER-001.md` (3 copies, MD5 `0555d5eaf123edb4f9557eef7bd3c71d`)
- Runbook: `reports/DAT-VPN-WINDOWS-CLIENT-001.md`
- Customer docs: `reports/DAT-VPN-{SOP,TOS,PP}-v1.0.3.docx` (Paperless 68/69/70)
- Memory: `/root/.openclaw/workspace/memory/2026-06-24.md` (full session history)
- Audit report: `reports/VPN-STACK-AUDIT-2026-06-24-16-20-UTC.md`
- Test customers: `reports/test-customers-2026-06-24.md`
