# Project TODO — databyte VPN Gateway

## 🎯 High Priority (next session)

### Testing & Alignment Plan (Zun's directive 2026-06-24 18:36)

Goal: prevent "we keep finding bugs in production" pattern. Portal_auth login bug existed 3 days before being caught.

**Layer 1 — Portal API integration tests (pytest) [✅ DONE 2026-06-24 — commit 7966c0b, 82 tests]**
- [x] `tests/test_portal_auth.py` — login w/ valid+invalid+expired+disabled+rate-limit (35 tests)
- [x] `tests/test_customer_lifecycle.py` — create→archive→delete; verify cascade to rw-eap.conf (18 tests)
- [x] `tests/test_installer_tokens.py` — create→fetch→re-fetch returns 404 (10 tests)
- [x] `tests/test_audit_log.py` — every mutation logged with correct actor (8 tests)
- [x] `tests/test_strongswan_sync.py` — create customer → confirm EAP block + swanctl --list-creds (9 tests)
- [x] `tests/conftest.py` — shared fixtures (test client, test customers, cleanup)
- [x] Wire into `.github/workflows/ci.yml` — run on every push (portal-tests job)
- [x] Catches #193 retroactively (portal_auth login SQL bug)

**Layer 2 — DB integrity check [✅ DONE 2026-06-25 — commit e794490]**
- [x] `scripts/check_db_integrity.py` — runs in CI + on demand
  - [x] Every `users` row has a `devices` row pointing to it
  - [x] Every `customers` row has ≥1 device
  - [x] Every `installer_tokens` row < 7 days old OR consumed
  - [x] Every EAP block in rw-eap.conf has matching user in DB
  - [x] Every active customer has matching EAP block
- Catches C-1 (orphaned devices) + B-1 (stale tokens) retroactively
- Wired into CI db-integrity job: tests drift detection + recovery

**Layer 3 — Static analysis [After L1 — 30m]**
- [ ] `scripts/check_stale_refs.sh` — grep for `102.182.117.43`, `vpn.homelab.local`, `192.168.10.98`
- [ ] Pre-commit hook + CI step
- [ ] Allowlist: `*.bak`, `archive/`, `gen-certs.sh` arg name
- Catches A-1 (app.js homelab) + A-3 (template) retroactively

**Layer 4 — E2E smoke [✅ DONE 2026-06-25 — commit c58a95a]**
- [x] `scripts/smoke.sh` — runs every 6h on LXC 903
- [x] Login as each test customer
- [x] Fetch /api/portal/me, /api/customers (with quota fields)
- [x] `swanctl --list-creds` count
- [x] Telegram alert on any failure (optional, env-gated)
- [x] systemd vpn-portal-smoke.{service,timer} — every 6h on LXC 903, Persistent=true

## 🟡 Medium Priority

- [ ] **CP7 security hardening** (6 items: fail2ban portal jail, AIDE, backups, cert expiry monitor, INPUT rule tightening, iptables-nft consolidation) — 3-4h total
- [ ] **ipBan service to VPS** (currently INACTIVE on VPS, only active on LXC 903) — 30m
- [ ] **Customer portal idle expiry split** (operator 30d OK, customer must be ≤24h idle / ≤7d absolute) — 30m. Surfaced 2026-06-24 by friend-overseas Android test
- [x] **Bug #2 customers.user_id FK** — DONE 2026-06-25 — commit a70e866 (customers.user_id INTEGER REFERENCES users(id), 6 L1 tests, idempotent migration, integrity check #6)
- [x] **Speed-plan feature** — DONE 2026-06-25 — commit 90d8c36 (v1.5.0). Per-customer speed_plan at creation: standard (20/20) or asymmetric_40_20 (40/20). Tiers drive quota only, NOT bandwidth. 10 L1 tests. Per-tier bandwidth NUKED per Zun 2026-06-25 05:33.
- [ ] **Per-tier bandwidth limits** — NUKED per Zun 2026-06-25 05:33 (speed-plan is per-customer, not tier-driven)

## 🟢 Low Priority (polish)

- [ ] systemd `RuntimeDirectoryMode` duplicate key cleanup
- [ ] CSP `report-uri` endpoint
- [ ] logrotate config for vpn-portal
- [ ] DAT-VPN-CLIENT-WINDOWS-INSTALLER-001 SOP (formal customer-facing doc)
- [ ] nftables migration
- [ ] **Tier label ↔ cap mismatch in customer portal** — `demo_100mb` shows "Demo 100MB" but per-customer override is 500 MiB. Rename tier OR drop override. — 5 min. Surfaced 2026-06-24

## 🔵 Future

- [ ] 5G CGNAT for iPhone clients
- [ ] HA failover (PLAN-5H-HA-LB.md)
- [ ] Let's Encrypt DNS-01 (DONE 2026-06-24, just renewal automation)
- [ ] **Tracker** (spreadsheet / markdown / CSV) — Zun hasn't picked A/B/C yet. Pending format decision.
- [ ] **Known limitation: Netflix (and other anti-VPN streaming services) won't work through tunnel.** Xneelo IP range (AS37153, 154.65.110.44) returns non-ZA CDN IPs (Dublin/Virginia/Oregon) from Netflix GeoDNS instead of af-south-1 Cape Town. Streaming will buffer, fail, or show "unblocker/proxy" error. Workaround: friend turns off VPN for streaming only. Add to DAT-VPN-SOP-001 v1.0.4 customer doc when next revised. Surfaced 2026-06-24 by Lagos/Starlink friend.

## ⚪ Other Active Projects (NOT VPN, deferred)

- Veeam Windows backup (NFR 20 PCs)
- ERP P2V testing
- TrueNAS config backup automation
- Blue Iris NVR virtualization

## ✅ Recently Shipped

- **2026-06-25 05:35** — **Speed-plan feature shipped** (v1.5.0, commit `90d8c36`). Per-customer `speed_plan` at creation time: `standard` (20/20 mbps symmetric) or `asymmetric_40_20` (40 down / 20 up). Tiers drive DATA QUOTA only; speed_plan drives BANDWIDTH (independent — per Zun: not tier-based). Precedence: explicit `bandwidth_down_mbps`/`bandwidth_up_mbps` (advanced override) > `speed_plan` preset > default standard. ClientCreate Pydantic model + `resolve_bandwidth()` / `validate_bandwidth()` helpers. UI: dropdown + optional 'Custom bandwidth' override fields. 10 L1 tests in `TestSpeedPlan`. Audits: bandwidth-monitor + installer_tokens already read `customers.bandwidth_*`; no downstream change. L1 101→111.
- **2026-06-25 05:25** — **Bug #2 fixed**: `customers.user_id INTEGER REFERENCES users(id)` column added (v1.4.0). Idempotent migration `host/vpn-portal/portal-user-id-fk.sql` + `apply_portal_user_id_fk.sh`. App: `/api/customers` POST populates; `/rotate_eap` + installer_tokens prefer FK. 6 L1 regression tests (TestCustomerUserIdFK class). 6th integrity check (`user-id-fk`). Fixed latent bug in CI db-integrity cleanup step. L1 95→101. Commit `a70e866`.
- **2026-06-25 04:50** — **L2 + L4 testing layers shipped**: L2 `scripts/check_db_integrity.py` (5 checks against canonical auth DB) wired into CI db-integrity job. L4 `scripts/smoke.sh` + systemd `vpn-portal-smoke.{service,timer}` — 5-check API-layer smoke running every 6h on LXC 903. Commits `e794490` + `c58a95a`.
- **2026-06-25 04:30** — **Deep housekeeping**: HARDLOCK rename (nftables-zun-vpn.service → nftables-vpn.service), rotate-vpn-credentials.py VPS path fix (/etc/swanctl/conf.d → /opt/strongswan-vpn-gateway/docker/swanctl/conf.d), archive deprecated v1.5.0 PowerShell scripts. Commits `7a0758f` + `3306551` + `f277951`.
- **2026-06-25 04:05** — **Bug #4 fixed**: POST `/api/customers/{id}/rotate_eap` endpoint — rotates EAP password in DB + rw-eap.conf while preserving EAP identity. Adds `customers.eap_rotated_at` column. 9 new L1 regression tests. Verified live on VPS. Commit `cdd93b7`.
- **2026-06-25 04:00** — **Bug #1 fixed**: PORTAL_TTL split — customer portal 30d→1h sliding window (operator kept at 30d). 4 regression tests added. Commit `64b7801`.
- **2026-06-24** — **L1 pytest 82 tests passing**: test_portal_auth 35 + test_customer_lifecycle 18 + test_installer_tokens 10 + test_audit_log 8 + test_strongswan_sync 9. Wired into CI as portal-tests job in `.github/workflows/ci.yml`. Commit `7966c0b`.
- **2026-06-24 16:55** — Code fix: app.js + templates use myvpn.databyte.co.za (homelab separated from VPS). Commit `afdd879`.
- **2026-06-24 17:35** — Per-platform test customers created (5 customers, demo_100mb tier).
- **2026-06-24 18:27** — `test-android-2-phone` customer created.
- **2026-06-24 18:34** — **Customer portal login bug FIXED** (portal_auth.py). Commit `49895dc`.
- **2026-06-24 18:36** — Testing/alignment plan logged as TODO (this file).
- **2026-06-24 19:01** — **First overseas Android client (friend) connected to production VPN** (`test-android-friend-laptop` @ 98.97.77.223 → 10.99.0.4, EAP-MSCHAPv2 + AES-256, 20/20 mbit). End-to-end confirmed working. Surfaced 3 portal bugs (idle expiry, tier label, name-based user↔customer mapping) — added to TODO.
- **2026-06-24 19:11** — Live VPN monitor (PID 52437, 30s poll) deployed during friend test. Logs `/tmp/vpn_monitor.log`.

## 🟢 Closed (false alarms / intentional separation)

- **Bug #3** Tier label vs cap mismatch — `demo_100mb` tier label "Demo 100MB" matched cap (audit 2026-06-25 confirmed no actual override)
- **Bug #6** Stale EAP key eap-demo-phone in rw-eap.conf — charon auto-loads all keys from file; "stale" key was active test customer (audit 2026-06-25 confirmed)
- **Bug #5** VPS ↔ LXC 903 DB drift — **NOT A BUG, INTENTIONAL SEPARATION**. Per Zun 2026-06-25 03:48 UTC + 04:32 UTC: "903 has nothing to do with production. We built the lab for test and build. Leave it alone now. We only focus on vps production. The lab will Keep for personal." Drift between lab portal UI and prod VPS DB is by design. DO NOT add any sync mechanism between them.

## 📚 Reference

- Master manual: `docs/DAT-VPN-WINDOWS-CLIENT-MASTER-001.md` (canonical file, 1 copy, MD5 `fc6a83d18b195bf3cbba1558f87f912a`)
- Runbook: `reports/DAT-VPN-WINDOWS-CLIENT-001.md`
- Customer docs: `reports/DAT-VPN-{SOP,TOS,PP}-v1.0.3.docx` (Paperless 68/69/70)
- Memory: `/root/.openclaw/workspace/memory/2026-06-24.md` (full session history)
- Audit report: `reports/VPN-STACK-AUDIT-2026-06-24-16-20-UTC.md`
- Test customers: `reports/test-customers-2026-06-24.md`
