# Changelog

All notable changes to databyte-Ikev2 are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

(nothing in flight — all changes captured in next released version)

### v1.2.6 — 2026-06-20

Revert 5C.5 self-service device management, lock model to **1 (creds pair) =
1 device**. 5C.6 multi-device-per-customer SHELVED (strongSwan's 1-identity-1-VIP
design blocks per-device tracking with shared creds under EAP-MSCHAPv2).

**Changed**

- `customers.max_devices` default changed from `2` to `1` (schema migration +
  all existing customer rows updated).
- Branches `v1.2.5-self-service-devices` and tag `v1.2.5` **deleted** (local
  + remote). The 5C.5 work is dead code under the new model.
- Live data cleanup: deactivated all but 1 active device per customer (canonical
  = lowest device id). Audit logged.
- 2-demo-account topology: `demo-customer` (100 MiB, tier `demo_100mb`) +
  `friend-customer` (500 MB, tier `friend_500mb`) are the 2 demo accounts;
  `zun-operator` is the single operator account (unlimited).

**Removed**

- `+ Add device` UI affordance and per-row device management actions (rotate,
  deactivate) are no longer present in the dashboard (LXC 902 reverted to the
  v1.2.4 code that predates 5C.5).
- `POST /api/customers/{id}/devices`, `DELETE /api/devices/{id}`,
  `POST /api/devices/{id}/rotate`, `GET /api/customers/{id}/devices` —
  endpoints from 5C.5 that have no caller under the 1-device-per-customer
  model.
- `quota/migrate_5C5_add_max_devices.sh` (column default 2; superseded by
  `quota/migrate_v126_max_devices_one.sh`).

**Added**

- `quota/migrate_v126_max_devices_one.sh` — idempotent migration: schema
  change (max_devices DEFAULT 1) + data cleanup (deactivate extras) + audit log
  entry per deactivation. Safe to re-run.
- `docs/PLAN-5C6-MULTIDEVICE-CREDENTIALS.md` (Rev 2) — research + decision log
  for the shelved 5C.6 phase. Retained as historical record.
- Note in `docs/ROADMAP.md` flagging 5C.5 as **REVERTED** and 5C.6 as
  **SHELVED** with rationale.

**Fixed**

- (No code fix in this release — `audit_log` `at` vs `created_at` bug from
  5C.5 still exists in 5C.5-era code, but that code is reverted. Live
  v1.2.6 = v1.2.4 + schema tweak, so the bug is not present in the running
  portal.)

**Operator action items (run once after upgrade)**

- Run `quota/migrate_v126_max_devices_one.sh` on LXC 903.
- Verify `rw-eap.conf` on LXC 903 — deactivated devices still have EAP
  blocks loaded. To make auth fail at EAP rather than just succeed-and-replace,
  set their `secret = "BLOCKED-<hex>"` (do this via a follow-up `update_rw_eap_conf.py`
  pass if needed).
- Reissue credentials for `friend-customer` (500 MB demo) if you want to test
  it — the canonical device for that customer is the lowest-id active device
  (see migration output).

**Backlog (NOT auto-promoted)**

- 5C.6 (multi-device-per-customer) — SHELVED. If revisited, the only clean
  path is **Option 4** (per-device client certs / EAP-TLS) — much bigger
  build (CA, cert generation, cert distribution, iOS/Android cert install UX).
  Do NOT restart from the 5C.5 / 5C.6 EAP-MSCHAPv2 work.
- Runtime SA-cap monitor — would let us *reject* (instead of *replace*) the
  2nd device using the same creds. Today charon's default `uniqueids=yes`
  makes the 2nd device take over. Different semantics from "reject."

**Migration from v1.2.5 (if you were on the 5C.5 branch)**

- DO NOT deploy the v1.2.5 code on a fresh install. The 5C.5 work has been
  thrown away. Deploy v1.2.6 (= v1.2.4 + schema migration) instead.
- If you already ran the 5C.5 migration (`migrate_5C5_add_max_devices.sh`),
  the v1.2.6 migration handles the column change (DEFAULT 2 → DEFAULT 1)
  and the data cleanup. Safe to run on top of a 5C.5-already-applied DB.

## [Released]

### v1.2.4 — 2026-06-20

Active session device info on UI + CHANGELOG.md.

Sessions page and customer detail UI now show real-time device + connection
metadata for each active IKE_SA.

**Added**

- `GET /api/vpn/sessions/parsed` — structured parse of `swanctl --list-sas`.
  Returns `{uniqueid, conn, state, version, local_id/ip/port, remote_id/ip/port,
  vip, algo, established_secs, bytes_in/out, pkts_in/out}`.
- `GET /api/devices` — list all devices with customer + metadata.
- `GET /api/devices/{id}` — single device with metadata.
- `PUT /api/devices/{id}` — partial-update metadata (device_type,
  os_version, hostname, notes, is_active). Empty string clears a field.
  Writes `audit_log` entry (actor=portal, action=device_update).
- `devices` table migration: ADD COLUMN `device_type TEXT`,
  `os_version TEXT`, `hostname TEXT` (all nullable).
- IKE proposal fingerprinting helper `fingerprint_device(algo_str)` —
  heuristic OS detection with confidence score. 10 known patterns
  (iOS/macOS / Windows 10-11 / strongSwan Android / strongSwan desktop /
  NetworkManager). UI shows amber "inferred" badge or cyan "manual" badge.

**Changed**

- Sessions page lease table: added columns Type, OS, Hostname, Public IP.
- Customer detail Devices table: added columns Type, OS, Hostname,
  inline ✎ edit button (modal editor).
- `/api/vpn/leases` enriched with `public_ip`, `remote_port`,
  `ike_proposal`, `sa_state`, `sa_established_secs`, `sa_bytes_in`,
  `sa_bytes_out`, `sa_uniqueid` from live swanctl SA join.
- `/api/customers/{id}` devices[] now includes `device_type`,
  `os_version`, `hostname`, `notes`.

**Fixed**

- swanctl header regex: SPIs end with `_i` / `_r` role markers and the
  responder SPI carries a trailing `*`. Old regex `[0-9a-f]+\s+[0-9a-f]+`
  failed on these suffixes, so `/api/vpn/sessions/parsed` returned `[]`
  even when SAs existed. New regex: `[0-9a-f]+_i [0-9a-f]+_r\*?`.

**Known limits**

- iOS / macOS native IKEv2 does NOT transmit device hostname or OS
  version over IKE. Cells stay "—" until set manually via the portal
  edit modal.
- Type/OS detection is "inferred" only — never trust for auth.

---

## Released

### v1.2.3 — 2026-06-20

VICI envelope parser hardening for the quota exporter.

**Changed**

- `quota/quota-exporter.py` — replaced regex parser for
  `swanctl --list-pools --raw` with a proper recursive-descent parser
  (~60 LOC, no deps). Handles compact `--raw`, pretty `-P`, multi-pool,
  empty leases, nested leases, list syntax.
- Authoritative source: `charon/src/libcharon/plugins/vici/vici_message.c:556`
  (`METHOD(vici_message_t, dump, ...)`).
- 8 fixture tests pass; live verified `/metrics` returns 30 metric
  families with `vpn_exporter_up=1` and `vpn_pool_size=254`.

---

### v1.2.2 — 2026-06-20

5C.3 — quota layer Grafana integration.

**Added**

- `quota/quota-exporter.py` — Prometheus exporter on LXC 903:9102.
  Polls SQLite + iptables FORWARD + swanctl every 30s. 17 vpn_* metrics
  across customer / lease / pool / alert / audit / exporter_health
  namespaces.
- `host/systemd/quota-exporter.service` — systemd unit (enabled, after
  docker.service, restart=always).
- `host/grafana/dashboards/strongswan-quota.json` — 11-panel Grafana
  dashboard (Active Leases, Active Customers, Over Quota, Scrape Errors,
  Per-Customer Used, % Utilization, Customer Roster, Active Leases
  table, Live Traffic 5m, Alerts Recorded, Audit Log Activity).
- `host/grafana/README.md` — install + import instructions.
- Prometheus scrape job `vpn-quota-exporter` added to
  `/home/zunaid/monitoring/prometheus.yml`, hot-reloaded via
  `docker kill -s HUP prometheus`.

**Changed**

- ROADMAP.md: 5C.3 marked done, 5C.4 cancelled (PBS full-LXC backup
  replaces RustFS daily job), 5H noted as last-last phase.

---

### v1.2.1 — 2026-06-20

Reboot fixes + portal polish.

**Added**

- `host/docker/daemon.json` — disables docker iptables/bridge/ip-forward
  (safe because strongSwan uses `network_mode: host`). Fixes 4-min
  docker cold-boot race against firewalld+nftables.
- `host/docker/README.md` — install instructions for fresh LXC 903.

**Changed**

- `host/systemd/README.md` — documents that `strongswan-starter.service`
  MUST be `disabled` on LXC 903. Host charon was binding UDP 500/4500
  and rejecting IKE_SA_INIT with N(NO_PROP); container charon couldn't
  bind the ports.
- 5C.1+5C.2 portal patches folded in (audit log section in customer
  detail, loading + empty states across all pages, GET /api/vpn/leases
  endpoint with VIP→identity→device→customer resolution, POST
  /api/quota/{id}/reset full 4-step flow, mobile responsive pass with
  `data-label` attributes, Sessions auto-refresh, per-row ↺ reset button).

**Fixed**

- docker up at +10s (was 4 min).
- iPhone reconnect in 6s (was 14 min) after cold reboot.
- All 12 portal API endpoints respond 200 post-reboot.

---

### v1.2 — 2026-06-20

5C portal — self-service VPN management UI.

**Added**

- `host/vpn-portal/app.py` — FastAPI backend, single file (~490 LOC).
  15 endpoints: health, login (bcrypt + HMAC cookie, 5/IP/min rate
  limit), logout, customers list/detail, tiers, quota live/reset,
  vpn sessions/pools/leases, security bans/whitelist/unban/deadman.
- `host/vpn-portal/www/index.html` — minimal shell, no external deps,
  no Google Fonts.
- `host/vpn-portal/www/static/app.js` — vanilla JS SPA, single IIFE
  (~626 LOC), `vp-*` class prefix, dark/light theme via CSS vars +
  `body[data-theme]`, localStorage persistence.
- `host/vpn-portal/www/static/app.css` — themed CSS (~255 LOC).
- `host/vpn-portal/systemd/vpn-portal.service` — systemd unit on LXC 902.
- `host/vpn-portal/requirements.txt` — fastapi, uvicorn, bcrypt, pydantic.

**Known limits**

- `init()` uses `/api/customers` as session probe (will migrate to
  `/api/me` once service account auth lands).
- Single admin user (admin/totalconnect). Multi-admin on roadmap.

---

### v1.1.0 — 2026-06-19

5B quota layer — per-customer data cap with hard cut.

**Added**

- `quota/` package — quota monitor + attr-sql SQLite schema.
- `quota/quota-monitor.py` — daemon polling every 60s. Reads
  per-VIP iptables FORWARD byte counters (254 outbound + 254 inbound
  ACCEPT rules with `quota:VIP` comments). Resolves VIP → customer via
  `swanctl --list-sas` (NOT leases table — stale on re-acquire).
  80% warn (alerts + audit_log once per customer). 100% hard cut
  (terminate SAs via `--terminate --ike-id N --force`, replace EAP
  secret in rw-eap.conf with `KILLED-{random}`, reload charon, set
  over_quota=1).
- 6 quota tables: `customers`, `tiers`, `devices`, `customer_devices`,
  `alerts`, `audit_log` (+ supporting indexes).
- Tiers: tier_3gb / tier_10gb / tier_15gb / demo_100mb (100 MiB
  persistent demo).
- `host/systemd/quota-monitor.service` — systemd unit on LXC 903.
- `host/systemd/strongswan-iptables-watchdog.service` — re-applies
  `/etc/iptables/rules.v4` on strongSwan container lifecycle events.
- `host/scripts/install_quota_rules.sh` — idempotent iptables rule
  installer.

**Fixed**

- 5B.6 watchdog bug: original `strongswan-iptables-watchdog.sh`
  re-applied `iptables-restore` on every docker container event
  (incl. exec_create / exec_start / health_status*) which wiped byte
  counters on every Prometheus scrape. Narrowed case statement to
  `start|restart|unpause|die|stop|kill|oom`.

**Verified**

- 4 end-to-end demo runs with real iOS app traffic, all cut correctly.

---

### v1.0 — 2026-06-XX

Initial release. Bare IKEv2 + EAP-MSCHAPv2 server.

- strongSwan 6.0.7 in Docker (Debian trixie base, custom build with
  `mschapv2` + `attr-sql` plugins).
- `rw-eap` connection (server-cert + EAP-MSCHAPv2) for iOS native
  IKEv2 client.
- `rw-psk` connection (pre-shared key) as fallback.
- IP pool `rw-pool` (10.99.0.0/24, VIP=10.99.0.1 gateway, 254 leases).
- DNS pushed: 1.1.1.1, 8.8.8.8.
- Certs persisted on LVM rootfs (`/home/zunaid/strongswan/swanctl/`).
- iptables MASQUERADE for 10.99.0.0/24, FORWARD+DOCKER-USER ACCEPT.