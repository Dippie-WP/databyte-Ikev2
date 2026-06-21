# ROADMAP

Phased execution per the two-gate rule: each phase is green only when (a) all its technical pass criteria are met AND (b) operator sign-off is given. No auto-promotion.

## Current state (live 2026-06-20 19:30 UTC)

**Latest tag: v1.2.8** — headless-browser smoke test for the portal (8 checks).

**Phase status:**
| Phase | Description | Status |
|---|---|---|
| 5A | Foundation (lock-in, certs, attr-sql, MSS clamp, exporter) | ✅ DONE — both gates green 2026-06-18 |
| 5B | Quota layer (DB + iptables + monitor + cut) | ✅ DONE — both gates green 2026-06-19 23:30 UTC, v1.1.0 |
| 5C.1+5C.2 | Self-service portal (FastAPI + vanilla JS) | ✅ DONE — v1.2 |
| 5C.3 | Grafana `strongswan-quota` integration | ✅ DONE — v1.2.2 |
| 5C.4 | ~~RustFS daily backup verify~~ | ⛔ CANCELLED — PBS full-LXC replaces |
| **5C.5** | **~~Self-service device management~~** | **⛔ REVERTED (v1.2.6)** — model locked to 1 creds = 1 device. Branch + tag deleted. |
| **5C.6** | **~~Multi-device-per-customer with shared creds~~** | **🔒 SHELVED (2026-06-20)** — strongSwan's 1-identity-1-VIP design blocks per-device tracking under EAP-MSCHAPv2. If revisited, only clean path is Option 4 (per-device client certs / EAP-TLS). Do NOT restart from 5C.5/5C.6 EAP-MSCHAPv2 work. Plan retained at `docs/PLAN-5C6-MULTIDEVICE-CREDENTIALS.md` Rev 2 for historical record. |
| v1.2.1 | Reboot fixes (two-charons, docker cold-boot) | ✅ DONE |
| v1.2.3 | VICI parser hardening | ✅ DONE |
| v1.2.4 | Device info UI + CHANGELOG.md | ✅ DONE |
| **v1.2.6** | **5C.5 revert + 1-device-per-customer model lock** | **✅ DONE — 2026-06-20 19:30 UTC** |
| **v1.2.7** | **Operator client onboarding (POST /api/customers + portal form + current_session + billing/email)** | **✅ DONE — 2026-06-20 20:36 UTC** |
| **v1.2.7.1** | **Critical UI fix: `el()` flattens array children (portal unusable since 5C.2)** | **✅ DONE — 2026-06-21 03:50 UTC** |
| **v1.2.7.2** | **Device-name collision guard (server + browser) + Web Share API button on one-shot panel** | **✅ DONE — 2026-06-21 04:13 UTC** |
| **v1.2.7.3** | **Operator usage visibility (real bytes + 'no cap' label, no hidden numbers)** | **✅ DONE — 2026-06-21 06:00 UTC** |
| **v1.2.7.4** | **Operator visibility follow-up — customers-list row uses usageBar()** | **✅ DONE — 2026-06-21 06:30 UTC** |
| **v1.2.8** | **Headless-browser smoke test (8 checks, locks in v1.2.7.1-class regression coverage)** | **✅ DONE — 2026-06-21 06:45 UTC** |
| **5H** | HA + LB (2x v1.2.x + keepalived VRRP + shared DB on NFS from TrueNAS, ~5s failover) | ⏳ NOT STARTED — **last-last phase** (Zun, 2026-06-20) |
| 5D | Commercial (multi-tenant SaaS, billing, customer signup) | 🔒 SHELVED — single-operator only (Zun, 2026-06-19) |
| v1.3 | iOS native EAP fixes, cert rotation, MTU/PMTUD, nftables migration | 🔒 SHELVED — backlog, no scheduled work |

**Tags on origin:** v1.0, v1.1.0, v1.2, v1.2.1, v1.2.2, v1.2.3, v1.2.4, v1.2.6, v1.2.7, v1.2.7.1, v1.2.7.2, v1.2.7.3, v1.2.7.4, v1.2.8 (14 total). **v1.2.5 deleted.**

**Active development branches:** none. All merged to main.

**Live infra (verified 12:35 UTC):**
- LXC 902 (myservices, 192.168.10.212): 12 Docker containers running (grafana, prometheus, dockhand, paperless, node-exporter, truenas-exporter, snmp-exporter, ipsec-exporter, strongswan-exporter, vpn-quota-exporter, etc.)
- LXC 903 (vpn-gateway, 192.168.10.98): charon PID 668 bound UDP 500/4500, 508 iptables per-VIP rules, all systemd services enabled + active
- Active SAs: 0 (Zun's connection ended 12:30 UTC)
- demo-customer: 23.4 MB / 100 MB used, over_quota=0
- zun-operator: 0 bytes, operator bypass

## 5A — Foundation (lock-in) — ✅ GREEN (both gates, 2026-06-18)

**Goal:** Self-hosted IKEv2 EAP-MSCHAPv2 + per-user sticky VIP. Public-path tested.

| Step | What | Status |
|---|---|---|
| 5A.1 | `rw-eap` conn config + self-signed CA + server cert (RSA-2048) | ✅ |
| 5A.2 | DB: `rw-pool` (10.99.0.0/24) + `zun` user + sticky VIP pin (10.99.0.50) | ✅ |
| 5A.3 | End-to-end client test (Android strongSwan app, 5G public path) | ✅ |
| 5A.4 | Reconnect test — same VIP returned | ✅ |
| 5A.5 | ~~Rollback rehearsal~~ OBSOLETE — replaced by 5H (HA + LB) | ⛔ |
| 5A.6 | `install_virtual_ip = no` fix (gateway mode) | ✅ |
| 5A.7 | Server-side MSS clamp at 1260 (5G PMTUD) | ✅ |
| 5A.8 | Daily backup to RustFS (DB + configs + certs) | ✅ |
| 5A.9 | Prometheus exporter + Grafana `strongswan-v1-2` dashboard | ✅ |
| 5A.10 | Server cert regen to PKCS#1 v1.5 (iOS 18 compat — RSASSA-PSS rejected by iOS) | ✅ |
| 5A.11 | ~~Load test~~ partial — PSS cert validated, 8-client sim blocked (charon-cmd 5.9.5 too old) | ⚠️ |
| 5A.12 | CI pipeline (`.github/workflows/ci.yml` + `release.yml`) | ✅ |
| 5A.13 | PR template + full end-to-end deploy guide in README | ✅ |
| 5A.14 | SCOPE LOCKED: single-operator only (no "for a friend" / multi-tenant framing) | ✅ |

## 5B — Quota layer — ✅ GREEN (both gates, 2026-06-19 23:30 UTC, **v1.1.0 tagged**)

**Goal (revised 2026-06-19 13:17 UTC — single-operator + paying customers):**
- Operator account: unlimited, no data cap, bypasses all quota checks
- Customers: 2 simultaneous connections per purchase, shared quota pool, 3GB/10GB/15GB catalog
- 100% = hard cut, manual extension by operator after payment (no calendar cycle)
- Customer-facing web page: read + "buy more" CTA → DM to operator → operator sends payment link
- Customer auth: username + password (bcrypt)
- Customer notifications: Telegram DM at 80% warn + 100% cut
- Grafana: operator-only, system + all users monitoring
- Admin web page: operator manages customers/tiers/devices/quota extensions

| Step | What | Status |
|---|---|---|
| 5B.1 | DB schema — 6 new tables (customers, tiers, devices, purchases, alerts, audit_log) + 10 indexes + seeds + systemd unit | ✅ DONE 2026-06-19 13:30 UTC |
| 5B.2 | iptables-legacy per-VIP byte counters (508 rules, 254 outbound + 254 inbound) in FORWARD chain | ✅ DONE 2026-06-19 13:49 UTC |
| 5B.3 | `quota-monitor.py` — per-VIP counter sampling, 80% warn, 100% hard cut (terminate SA + kill EAP secret + reload charon) | ✅ DONE 2026-06-19 17:42 UTC |
| 5B.4 | systemd unit (`quota-monitor.service`, Type=simple, restart=on-failure, SIGTERM-clean) | ✅ DONE 2026-06-19 17:53 UTC |
| 5B.5 | End-to-end test with demo-customer — **3 clean runs**, real iOS app traffic, 100% cut fires, secret killed, re-auth blocked | ✅ DONE 2026-06-19 23:30 UTC |
| 5B.6 | iptables-legacy watchdog bug fix — only re-apply rules.v4 on actual container lifecycle events, NOT on every docker exec | ✅ DONE 2026-06-19 19:48 UTC |
| 5C.1 | Customer web page (FastAPI + bcrypt) | ⏳ Gated on 5B green |
| 5C.2 | Admin web page (`/admin`, customer mgmt + credential gen + quota extension) | ⏳ |
| 5C.3 | Telegram bot (vpn-bot.py — auth + buy-more relay + outbound alerts) | ⏳ |
| 5C.4 | Grafana `vpn-quota` dashboard (active SAs per customer, usage, alerts) | ⏳ |

**5B deliverables (tagged v1.1.0):**
- `quota/quota_schema.sql` — 6 tables, 10 indexes, idempotent `IF NOT EXISTS`
- `quota/apply_quota_schema.sh` — host-side applier, idempotent, pre/post check
- `quota/seed_real_tiers.sh` — 3GB/10GB/15GB tiers
- `quota/seed_5B1.sh` — demo_100mb tier + zun-operator + demo-customer + 5 device links
- `quota/seed_demo_creds.sh` — conf-driven EAP creds (avoids hard-coding secrets in DB)
- `quota/reset_demo.sh` — resets demo customer's `data_used_bytes` to 0
- `quota/install_quota_rules.sh` — installs 508 per-VIP ACCEPT counters + watchdog persistence
- `quota/install_mss_clamp.sh` — installs `*mangle` TCPMSS rule (5A.7 fix)
- `quota/update_rw_eap_conf.py` — kills EAP secret at 100% (used by quota-monitor.py)
- `quota/quota-monitor.py` — main daemon (21KB, 60s poll)
- `host/systemd/quota-schema.service` — oneshot at host boot, applies schema
- `host/systemd/quota-monitor.service` — long-running daemon, restart=on-failure
- `host/systemd/strongswan-iptables-watchdog.service` — re-applies rules.v4 on container restart (FIXED 5B.6)
- `host/systemd/strongswan-iptables-watchdog.sh` — script (FIXED 5B.6)
- `host/systemd/README.md` — install instructions + 5B.6 gotcha
- `docs/ARCHITECTURE.md` — 5B section with data flow
- `docs/decisions/5B-architecture.md` — design ADR (iptables-legacy vs nftables, kill-conf vs DB, etc.)
- `docs/decisions/5B-credentials-kill.md` — why we kill conf secret, not DB

**Test results (4 end-to-end runs, real iOS app traffic where possible):**

| Run | Time | Connect → cut | Peak throughput | Final DB | Notes |
|---|---|---|---|---|---|
| #1 | 2026-06-19 17:42 UTC | n/a (synthetic pre-set 100 MiB + 1 byte) | n/a | 104.8% | First proven 100% cut, no real client |
| #2 | 2026-06-19 19:44 UTC | 8 min | 22 MB/min | 104.8% | First REAL-traffic cut (iOS app: 140 MB used in app / 100 MB cap in daemon — exposed 5B.6 watchdog bug) |
| #3 | 2026-06-19 19:56 UTC | 2 min 23 sec | 144 MB/min | 158.0% | Zun pushed hard, cap fired at 158% |
| #4 | 2026-06-19 23:26 UTC | 1 min 6 sec | 140 MB/min | 158.0% | iOS app auto-logged off — Zun confirmed "Beautiful" |

**5B.6 (watchdog bug):** the `strongswan-iptables-watchdog.service` originally re-applied `iptables-restore` on EVERY docker container event including `exec_create`/`exec_start`/`health_status*` — which fired on every Prometheus scrape (30s) and daemon poll (60s), **resetting all 508 per-VIP byte counters to 0**. Zun's "you lie" screenshot (140 MB in iOS app vs 22 MB in daemon) was the diagnostic clue. The fix: case statement narrowed to `start|restart|unpause|die|stop|kill|oom` only. See ADR `5B-architecture.md` for full root cause + math.

**Backups:** `ipsec.db.bak-5B1-20260619-132059` retained on LXC 903. Kill-conf backups at `/home/zunaid/strongswan/swanctl/conf.d/.backups/rw-eap.conf.bak-quotamon-*` (one per cut event).

## 5C — Surface — ✅ DONE (v1.2.4 latest, 5C.1/5C.2 v1.2, 5C.3 v1.2.2, 5C.4 CANCELLED)

**Goal:** Operator dashboard + monitoring integration.

| Step | What | Status |
|---|---|---|
| 5C.1 | Customer/operator web page (FastAPI + bcrypt + rate-limit) | ✅ DONE — v1.2 (5C.1+5C.2 combined) |
| 5C.2 | Admin web page (`/admin`, customer mgmt + credential gen + quota extension) | ✅ DONE — v1.2 |
| 5C.3 | Grafana `vpn-quota` dashboard (per-customer view, audit, alerts) | ✅ DONE — v1.2.2 |
| 5C.4 | ~~Backup verify (RustFS)~~ — **CANCELLED 2026-06-20** | ⛔ Replaced by PBS full-LXC backup (Zun direction) |

**5C.3 deliverable details (tagged v1.2.2):**
- `quota/quota-exporter.py` (421 lines) — Prometheus exporter on LXC 903:9102
- `host/systemd/quota-exporter.service` — long-running daemon
- `host/grafana/dashboards/strongswan-quota.json` — 11-panel dashboard
- `host/grafana/README.md` — folder-level docs

**v1.2.1 (reboot fixes):** `host/docker/daemon.json` + disable `strongswan-starter`. Two-charons bug.

**v1.2.3 (VICI parser hardening):** Recursive descent parser replaces regex in `quota-exporter.py`. 8 fixtures pass.

**v1.2.4 (device info UI + CHANGELOG):**
- `CHANGELOG.md` (new) — Keep-a-Changelog 1.1.0 format. Tracks all 7 versions.
- Sessions page lease table: + Type (fingerprint-inferred or manual badge), OS, Hostname, Public IP columns.
- Customer detail Devices table: + Type, OS, Hostname columns, inline ✎ modal editor.
- New endpoints: `GET /api/vpn/sessions/parsed`, `GET /api/devices`, `GET /api/devices/{id}`, `PUT /api/devices/{id}`.
- `swanctl_parse_sas()` — structured parse (replaces raw text). Bug fix: regex matched SPI role markers (`_i`, `_r*`).
- `fingerprint_device(algo_str)` — heuristic OS detection from IKE proposal (10 patterns).
- Schema migration: `devices` table adds `device_type/os_version/hostname TEXT`.

## 5C.5 — ~~Self-service device management~~ — ⛔ REVERTED (v1.2.6, 2026-06-20)

**Reverted by operator decision (Zun, 2026-06-20 19:25 UTC).** Model lock: **1 (username, password) = 1 device**. Same creds on a 2nd device will be kicked by charon default `uniqueids=yes` (2nd takes over, 1st drops). To get strict "reject the 2nd" semantics, a runtime SA-cap monitor is needed (backlog, not started).

**Why reverted:** the work allowed multiple device slots per customer (1:N customer→devices), which was over-scope for the 1:1 model the operator actually wants. The 5C.5 branch + tag have been deleted; the live LXC 902 portal has been reverted to the v1.2.4-base code that predates 5C.5. The schema migration `migrate_5C5_add_max_devices.sh` has been superseded by `migrate_v126_max_devices_one.sh`.

**Historical 5C.5 sub-step record (not executed in main, retained for context):**

**Goal:** Operator adds, removes, and rotates device credentials for any customer via the portal, end-to-end (UI → API → DB → charon creds reload → audit log). Today it's a 4-step manual bash + SQL operation; this phase makes it a button.

**Why:** Friend just hit this gap manually. Each new device takes ~2 min of bash + SQL + charon reload, with no audit trail beyond a hand-written `audit_log` row. Wrong layer for a homelab MSP.

**Locked decisions (Zun, 2026-06-20 15:15 UTC):**
- **Operator-only portal** — no Telegram DM to customer, no customer self-add. Operator (Zun) is the only person who creates accounts and issues credentials.
- **Max 2 devices per customer** (NOT the default 5). Enforced server-side, 409 on third device add. Configurable per-customer via new `customers.max_devices` column.
- **No actual work started** — phase is planned and documented. Zun will say "go" when ready.

| Step | What | Status |
|---|---|---|
| 5C.5.1 | Backend `POST /api/customers/{id}/devices` — accepts `device_name`, `device_type`, `os_version`, `notes`. Auto-generates password (`secrets.token_urlsafe(16)`), NTLM-hashes it, inserts `users` + `devices` rows, writes `audit_log`, returns the password **once** in the response. 409 on duplicate name. Enforce `max_devices` cap with 409. | ⏳ |
| 5C.5.2 | Backend `DELETE /api/devices/{id}` — soft delete (`is_active=0`). Keeps the EAP block in `rw-eap.conf` (audit + post-mortem; no charon disruption). Writes `audit_log`. | ⏳ |
| 5C.5.3 | Backend `POST /api/devices/{id}/rotate` — generates new password, regex-replaces the EAP block in `rw-eap.conf` (idempotent, matches by `id = <name>`), reloads charon creds via `swanctl --load-creds` over VICI URI, returns new password once. | ⏳ |
| 5C.5.4 | Backend `GET /api/customers/{id}/devices` — list with last-seen VIP + is_active. Already partially exists; wire 5C.5.1/2/3 results into it. | ⏳ |
| 5C.5.5 | Frontend: "+ Add device" button on customer detail page. Modal: friendly name (alphanumeric + dash, max 32, reject `..` / `/` / leading dash) + type select (iOS/macOS/Android/Windows/Linux/Other) + optional OS version + notes. On submit: show new password in copy-to-clipboard panel with "this is shown once" warning. | ⏳ |
| 5C.5.6 | Frontend: per-row ↻ (rotate) and ⊘ (deactivate) buttons. Rotate → same one-shot password panel. Deactivate → confirm dialog. | ⏳ |
| 5C.5.7 | Schema migration: `ALTER TABLE customers ADD COLUMN max_devices INTEGER NOT NULL DEFAULT 2`. | ⏳ |
| 5C.5.8 | End-to-end live test: create `friend-laptop2` via portal → charon picks up new creds → live SA appears in Sessions → 5C.5.2 deactivate → SA terminated on next reconnect, EAP block retained. | ⏳ |

**Out of scope (explicit):**
- Customer self-add (operator-only model)
- Telegram DM delivery of new passwords (operator hands off manually)
- Bulk device add (5D commercial scope)
- EAP block wipe on deactivation (soft delete only, retain block for audit)
- Per-customer rate-limiting on add (operator is the only caller)

**Current manual operation (the gap):** 3-step bash + SQL sequence — (1) insert EAP block in `rw-eap.conf`, (2) insert `users` row with NTLM hash, (3) insert `devices` row, (4) `swanctl --load-creds`. ~2 min per device. Last performed live for `friend-phone` (id=20) and `friend-laptop` (id=21) on 2026-06-20 14:09 + 14:59 UTC respectively.

## 5D — Commercial — 🔒 Shelved (out of scope, customer-facing bits moved to 5C)

**Status:** Zun confirmed 2026-06-19 12:30 UTC: "I'm the only one hosting the server." Single-operator only — no multi-tenant SaaS, no automated billing, no customer self-signup. The "buy more → DM to Zun → payment link" flow is manual by design.

**Original goal (if scope ever changes):** Multi-tenant billing, payment-triggered reset, customer-facing messages.

| Step | What |
|---|---|
| 5D.1 | Pricing tiers |
| 5D.2 | Audit trail (who connected, when, how much) |
| 5D.3 | Payment-triggered reset (Stripe / Paystack) |
| 5D.4 | Customer onboarding flow (signup → credentials) |
| 5D.5 | Hard VIP pinning (released=0 enforced, no lease reuse) |

## v1.3 backlog (revisit later)

- **iOS native IKEv2 + EAP** — needs Let's Encrypt cert via certbot + DNS-01 (current iOS path silently fails cert validation)
- **Customer onboarding flow** — auto-generate username + password per signup
- **CA cert auto-bundle** for Android — include CA in `.sswan` profile export
- **Phone-side UX polish** — shorter `rekey_time` (24h → 20-30m), `reauth_time` (24h → 2-3h), `charon.keep_alive = 20s`
- **Server-side defaults audit** — every `charon.*` setting reviewed for gateway vs client default
- **Cloudflare bot detection** — ifconfig.me may give `ERR_CONNECTION_CLOSED` because shared MASQ IP looks bot-like
- **5G MTU/PMTUD** — server-side MSS clamp at 1260 fixes (5A.7). May need carrier-specific tuning
- **5G CGNAT stability** — iOS SAs die in 4-30 min on cellular. Try lower fragment_size (1100), raise `ikesa_max_halfopen` to 10, install_virtual_ip=yes test
- **5H — HA + LB** — 2x v1.2 + keepalived VRRP active/passive, shared DB, ~5s failover. Tier 1 for homelab SLA. **Last-last phase (Zun, 2026-06-20 10:45 UTC)**.
- **iptables → nftables migration** — nftables named counters persist across rule reloads. Would prevent future 5B.6-style bugs. ~2-3h work, low priority since 5B.6 fix is in place.

## Backup strategy (Zun 2026-06-20 10:48 UTC)

- **5C.4 (RustFS daily DB+configs backup verify) — CANCELLED**
- **Replacement:** PBS (Proxmox Backup Server, already running at 192.168.10.84) backs up the entire LXC 903 + LXC 902 containers. No need for separate RustFS target.
- The daily `/usr/local/bin/strongswan-configs-backup.sh` job (5A.8) can remain as a quick-restore convenience but is no longer the primary backup.
