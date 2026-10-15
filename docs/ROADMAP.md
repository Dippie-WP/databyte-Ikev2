# ROADMAP

Phased execution per the two-gate rule: each phase is green only when (a) all its technical pass criteria are met AND (b) operator sign-off is given. No auto-promotion.

## Current state (live 2026-10-15 06:50 UTC / 08:50 SAST)

**Stack versions (verified live against `vpn-prod-01`):**
- **Portal:** v2.2.1 (deployed 2026-08-10, git HEAD `8e02a6e`) — systemd `vpn-portal.service`
- **Bot:** v2.5.0 (HEAD `01ad543` in `vpn-admin-bot` repo, tag `v2.5.0`) — systemd `bot-polling.service`
- **strongSwan image:** `zun/strongswan:6.0.7-mschapv2-attrsql` — running 6 d 19 h (since 2026-10-08T14:06:34Z)
- **MariaDB:** 11.8.6-MariaDB-0+deb13u1, `radius` DB = 45 tables
- **OS:** Debian 13.7 (trixie)
- **Hostname:** `vpn-prod-01` (was `vps-01`, renamed per HOT-210, 2026-09-30)
- **Public IP:** 154.65.110.44 · **Tailscale IP:** 100.64.212.47
- **LE certs:** `myvpn.databyte.co.za` + `adminer.databyte.co.za` (ECDSA, expires 2026-12-02, 48 days valid)
- **Live data:** 12 customers · 2,301 radpostauth rows · 1,168 radacct rows · 28 radcheck entries
- **Backups:** kopia → `kop.databyte.co.za:443` — **502 Bad Gateway, PARKED** (Zun msg #43961; last successful `.update-info.json` mtime 2026-10-10 00:00)
- **CI:** all 4 workflows green on HEAD `c3aa4f4` (latest push)

**Tags on `origin`:** v1.0, v1.1.0, v1.2, v1.2.1–v1.2.4, v1.2.6, v1.2.7, v1.2.7.1–v1.2.7.4, v1.2.8–v1.2.14, v1.3.0, v1.4.x through v1.9.0 (varied, including SSE-merge and destructive-replay branches), **v2.0.0**, **v2.1.0**, **v2.1.1**, **v2.2.0** (current). **v1.2.5 deleted.** ORPHANED: v1.8.0 and v1.9.0-sse preserved as historical markers but point at the destructive-replay branch (`backup-broken-v1.9.1-pre-reset`), NOT at `main`. The current `main` → `c3aa4f4` (README rewrite commit).

> **Per Zun msg #43974 (2026-10-15 06:19 UTC): "Update read me fully! Live artifacts truth and latest time stamps. No guessing or assuming. Should be clear to understand."** This ROADMAP is being updated in that same session — all live-state numbers above were re-verified via live `ssh` + `mariadb` + `docker` + `certbot` commands in this turn.

---

## Phase status

| Phase | Description | Status |
|---|---|---|
| 5A | Foundation (lock-in, certs, attr-sql, MSS clamp, exporter) | ✅ DONE — both gates green 2026-06-18 |
| 5B | Quota layer (DB + **iptables-legacy, lab only** + monitor + cut) | ✅ DONE (lab) — 2026-06-19 23:30 UTC, v1.1.0. **VPS production uses nftables** (Phase 7.5 swap, 2026-07-09) |
| 5C.1+5C.2 | Self-service portal (FastAPI + vanilla JS) | ✅ DONE — v1.2 |
| 5C.3 | Grafana `strongswan-quota` integration | ✅ DONE — v1.2.2 |
| 5C.4 | ~~RustFS daily backup verify~~ | ⛔ CANCELLED — PBS full-host replaces |
| **5C.5** | **~~Self-service device management~~** | **⛔ REVERTED (v1.2.6)** — model locked to 1 creds = 1 device. Branch + tag deleted. |
| **5C.6** | **~~Multi-device-per-customer with shared creds~~** | **🔒 SHELVED (2026-06-20)** — strongSwan's 1-identity-1-VIP design blocks per-device tracking under EAP-MSCHAPv2. If revisited, only clean path is Option 4 (per-device client certs / EAP-TLS). Do NOT restart from 5C.5/5C.6 EAP-MSCHAPv2 work. Plan retained at `docs/PLAN-5C6-MULTIDEVICE-CREDENTIALS.md` Rev 2 for historical record. |
| v1.3.0 | Customer portal at `/portal/` (lab) + diagnosis protocol + charon defaults audit | ✅ DONE — 2026-06-21 17:50 UTC |
| v1.7.x | v1.7 series (operational hardening) | ✅ DONE — through 2026-06-28 |
| v2.0.0 | **Phase 5 RADIUS cutover** (charon `eap-radius` proxies auth to FreeRADIUS, DAE/Disconnect-Request, reset bug fix, Phase 7 cleanups) | ✅ SHIPPED — 2026-07-06 (`a9d2527`) |
| v2.1.0 | Case-insensitive identity normalization + CI drift detection | ✅ SHIPPED — 2026-07-11 |
| v2.1.1 | Dead-code cleanup post-Phase-4E (CORR-035) | ✅ SHIPPED — 2026-07-12 |
| **Phase 4D** | RADIUS Phase 4A-4D: SQLAlchemy + MariaDB layer for `portal_auth.py`; reset-restore from backup hardened; unarchive BLOB→hex idempotency | ✅ SHIPPED — 2026-07-05 |
| **Phase 4E** | **Unify portal SQLite + MariaDB** (portal business data → MariaDB; 10 tables migrated; SQLite `ipsec.db` retains only charon-internal tables) | ✅ SHIPPED — 2026-07-12 (`cb9bf69`) — see `docs/PHASE-4E-DEPLOYMENT-NOTES.md`, `docs/PHASE-4E-EXECUTION-PLAN.md`. `docs/PHASE-4E-SCOPE-ASSESSMENT.md` is **SUPERSEDED** by these two. |
| **Phase 5** | **charon `eap-radius` cutover** (charon forwards EAP-MSCHAPv2 auth to FreeRADIUS over local UDP/1812; local `users`/`pools`/`leases` SQLite tables no longer authoritative) | ✅ SHIPPED — 2026-07-06 |
| **Phase 5+** | **RFC 5176 Disconnect-Request** (DAE/CoA listener on charon UDP/3799; FreeRADIUS → charon signed-by-shared-secret CoA to hard-cut at 100% quota) | ✅ SHIPPED — commit `3b00b8e` |
| **Phase 7.5** | **iptables-legacy → nftables** swap for quota counters (named counters persist across `nft flush ruleset`; eliminates the 5B.6 bug class permanently) | ✅ LIVE on VPS — 2026-07-09 (per TOOLS.md) |
| **v2.2.0** | **FreeRADIUS operator overlay + radacct chain wired end-to-end** | ✅ SHIPPED — 2026-07-13 12:35 SAST / 10:35 UTC, commit `805ea84` |
| **v2.2.1** | **Portal upgrade** (post-Phase-4E; v2.2.1 deployed 2026-08-10, git HEAD `8e02a6e`) | ✅ SHIPPED — 2026-08-10 |
| **v2.5.0** (bot) | **Telegram bot v2.5.0** (`/sessions`, `/customers`, `/logs` amendments; `setMyCommands` post_init; HOT-66 swanctl-watchdog; 3 amendments 2026-10-09 + setMyCommands 2026-10-14) | ✅ SHIPPED — 2026-10-14 in `vpn-admin-bot` repo, tag `v2.5.0` |
| **5H** | HA + LB (2x charon + keepalived VRRP + shared MariaDB) | ⏳ NOT STARTED — **last-last phase** (Zun, 2026-06-20), still ahead of 5H: Phase 4D complete, Phase 4E complete, v2.2.0 complete |

---

## Done since 2026-07-13 (L2 doc-update session, 2026-10-15)

Per Zun msg #43961 ("read the git repo. And also live artifacts from prod vpn. And go update those notes that has old information or long time ago version information. Develop a clear plan. Execute step by step. Not all at once. Layer by layer.") and msg #43970 ("Do it well. Focus. Facts. Unsure use web verify everything you writing.").

**7 commits pushed to `Dippie-WP/databyte-Ikev2:main`** (all verified via `git log` + `git ls-remote origin HEAD`):

| # | Commit | Layer | What |
|---|---|---|---|
| 1 | `746262a` | L2.1 | Refresh `docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.0.7 → **v1.1.0** (2026-10-15 fact-check). 24 sections updated, 6 new hard rules, §1.2 service/port inventory, §1.6 gap #14 bandwidth-monitor STALE warning, kopia 502 parked note, §2.5 acceptance counts, §5 hard rules #8-9, §7 changelog, §8 verification signature. |
| 2 | `bda11ae` | L2.2 | Refresh `docs/ARCHITECTURE.md` v2.2.0 → **2026-10-15 fact-check** (v2.2.1 portal + v2.5.0 bot). Verified-live table refreshed with current numbers + 7 new rows (Portal, Bot, Services, Listening ports, Quota enforcement, bandwidth-monitor, LE certs); 3 new architecture shifts (bot v2.5.0, strongswan-watchdog HOT-66, kopia 502). |
| 3 | `3337129` | L2.3 | **New doc** `docs/VPS-XNEELO-DEPLOY.md` v1.0.0 — canonical Xneelo VPS deploy procedure. Documents `scripts/bootstrap-xneelo.sh` (19 steps per header) + §3.4 "What the script does NOT do" (17-item prod-parity gap) + §4 post-script steps (§4.1-§4.12, 30-45 min) for full prod-parity deploy. Fixes 3 previously-broken `VPS-XNEELO-DEPLOY.md` references in `DEPLOYMENT.md`, `QUICKSTART.md`, `SESSION-HISTORY.md`. |
| 4 | `1ad0b9b` | L2.4 | Fix `docs/DEPLOYMENT.md` line 6 factual inaccuracy. "Does everything in 15-25 min" → accurate description of base gateway install + 17 post-script steps + cross-ref to `VPS-XNEELO-DEPLOY.md` §3.4 + §4. Off-by-one fix: "16" → "17" in 2 places (this doc + `VPS-XNEELO-DEPLOY.md` changelog). |
| 5 | `d295765` | L2.8 | Fix 24 `vps-01` → `vpn-prod-01` refs in 3 customer-facing Windows docs (`DAT-VPN-INT-WIN-001.md` 3, `-GUIDE-001.md` 15, `-HOSTILE-001.md` 6). All were operational `ssh`/`scp` commands. Verified script facts (MD5 `fc6a83d18b195bf3cbba1558f87f912a`, 23609 B) against live prod — still match v2.6.5. Flagged 2 HARDLOCKED script facts for separate investigation (git commit `bf4e4b1` not in repo; template `setup-databyte-vpn-windows.ps1` not on prod). |
| 6 | `26e46d4` | L2.8b | **New doc** `docs/DAT-VPN-INT-WIN-LIVE-001.md` v0.1.0 — **parallel companion** to the HARDLOCKED `DAT-VPN-INT-WIN-001.md`. Per Zun msg #43979 ("Ignore the headlock. This book we writing Parralel To the headlock doc so we don't touch it"). Live-state fact-check with 2026-10-15 stamps; hostname fix (all 24 vps-01→vpn-prod-01); discrepancy log (flagged for separate investigation, NOT silently fixed). |
| 7 | `c3aa4f4` | L2.6 | Rewrite `README.md` fully with live artifacts truth + 2026-10-15 time stamps. Per Zun msg #43988. "Live state" section with 16 verified facts; "What's where" table (dropped `internal/` — doesn't exist); "Versions" table (multi-component: portal v2.2.1 + bot v2.5.0 + image 6.0.7); "Deploy / DR / Architecture pointers" table. |

**CI status on all 7 commits:** **6/6 check-runs completed success** on each push (LIVE vs git HEAD drift, Lab-leakage guard, Build image + smoke test, DB integrity check, Portal API integration tests, portal smoke). All 4 workflow files (ci.yml, drift-detect.yml, portal-smoke.yml, release.yml) show "completed successfully" for the most recent run on each push.

**RustFS backup:** all 7 + the README pushed to `rustfs:open-claw-push/homelab-vpn/docs-factcheck-2026-10-15/` (8 files total: README.md + 7 docs in `docs/` subdirectory).

**L3 audit per layer:** 0 vps-01 refs remaining in any doc (except the hardlock's internal `DAT-VPN-INT-WIN-001.md`); all live-state numbers verified live against `vpn-prod-01`; all 16 README claims verified via live exec; 0 remaining stale values.

---

## Still to be done

### From the L2 doc-update plan (2.5, 2.8b, 2.9)

| Layer | Doc | Status | Notes |
|---|---|---|---|
| **2.5** | `docs/SESSION-HISTORY.md` line 197 | **DECIDED TO LEAVE** | Line 197 references `docs/VPS-XNEELO-DEPLOY.md` (now valid, created in 2.3). The doc is a historical narrative of 2026-06-18; the ref is to a state at a point in time. Modifying would change the historical record. Decision per Zun pattern of "don't touch historical docs." |
| **2.8b** | HARDLOCKED script facts investigation | **FLAGGED — NEEDS SEPARATE LAYER** | From 2.8: (a) git commit `bf4e4b1` + 8 other referenced commits NOT in databyte-Ikev2 repo; (b) template file `setup-databyte-vpn-windows.ps1` (claimed MD5 `5541343b...`) NOT on prod. These are HARDLOCKED claims in the approved doc. Per Zun msg #43979 ("Ignore the headlock"), this parallel doc does NOT modify the hardlock. The next hardlock revision should determine the correct state. **Do NOT touch without explicit review.** |
| **2.9** | new `docs/BOT.md` | **NOT STARTED** | Bot architecture doc (v2.5.0). Currently only documented in the `prod-vpn-bot-edit` skill. Would cover: components, env vars (`TELEGRAM_BOT_TOKEN`, `VPN_HOST`, etc.), deployment (bot-polling.service + vpn-portal.service), restart pattern, `setMyCommands` post_init, HOT-261 sole-polling-source constraint, 3 amendment history. Quick write if/when needed. |

### From the original ROADMAP backlog (5H, v1.3 items)

| Item | Status | Notes |
|---|---|---|
| **5H — HA + LB** | ⏳ NOT STARTED | **Last-last phase** (Zun, 2026-06-20 10:45 UTC). 2x charon + keepalived VRRP active/passive + shared MariaDB. Detailed plan in `docs/RUNBOOK-DR-REBUILD-AND-HA.md` §3 (Option B: DNS round-robin + MOBIKE + Galera). RTO 2h → 30-120s on single node failure. Cost ~R1500/mo for vps-02. |
| **v1.3.0 backlog** | ⏳ OPEN | See "v1.3 backlog" section below for full list. |
| **5C.5 (reverted)** | ⛔ REVERTED | Model locked: 1 (username, password) = 1 device. Branch + tag deleted. Reverting the revert is **explicitly discouraged** — charon's `uniqueids=yes` default enforces 1-identity-1-VIP. |
| **5C.6 (shelved)** | 🔒 SHELVED | If multi-device per customer is ever needed, plan is `docs/PLAN-5C6-MULTIDEVICE-CREDENTIALS.md` Rev 2 — **only Option 4 (per-device client certs / EAP-TLS) is clean**. Do NOT restart from EAP-MSCHAPv2 work. |

---

## Future ideas (if I want it) — facts only, web-sourced 2026-10-15

**Per Zun msg #43994: "search the web for things we can add in future such as features and cool things and security."** All items below are sourced from public web searches on 2026-10-15. No speculation, no assumptions — only what the sources actually say.

| # | Idea | Source | Status (2026-10-15) | Why it might matter |
|---|---|---|---|---|
| F1 | **strongSwan 6.1.0 upgrade** | [strongswan.org blog 2026-09-07](https://strongswan.org/blog/2026/09/07/strongswan-6.1.0-released.html) | **RELEASED 2026-09-07** (major version bump) | We're running 6.0.7 (older patch of 6.0.x). 6.1.0 includes 2 CVEs fixed in the 6.0.x line: **CVE-2026-78133** (libcharon IKEv2 rekeying collisions → use-after-free → potential RCE; affects 6.0.0+) and **CVE-2026-78134** (eap-peap / eap-ttls auth details propagation → potential auth bypass). We're on EAP-MSCHAPv2 so 78134 doesn't affect us directly, but 78133 DOES (we use charon for IKEv2). **Upgrade recommended for security.** |
| F2 | **Post-quantum IKEv2 (ML-KEM hybrid)** | [IETF draft-ietf-ipsecme-ikev2-mlkem-04](https://datatracker.ietf.org/doc/html/draft-ietf-ipsecme-ikev2-mlkem-04) (2026-02-26) | **IETF INTERNET-DRAFT** (not yet RFC) | Adds ML-KEM (NIST FIPS 203, standardized 2024) as additional key exchange in IKEv2, combined with traditional ECDH (hybrid PQC). Quantum-resistant key establishment. Academic paper (Electronics 2026-07-28, DOI 10.3390/electronics15153340) demonstrates "Hybrid Post-Quantum IKEv2 on Embedded Automotive Platforms" using ECDH-P384 + ML-KEM-768. **Not yet supported in strongSwan 6.1.0** (it's a draft). Watch for RFC + strongSwan support — likely 1-2 years out. |
| F3 | **DDoS protection (RFC 8019)** | [RFC 8019](https://datatracker.ietf.org/doc/html/rfc8019) (2016-11) | **RFC 8019 (2016)** | 4-component DDoS strategy: (1) harden half-open SA database by reducing retention time; (2) harden by rate-limiting single IPs/prefixes; (3) guidance on failed IKE_AUTH; (4) **Client Puzzles mechanism** (new in this RFC). We currently don't implement Client Puzzles. strongSwan docs [Security Recommendations](https://docs.strongswan.org/docs/latest/howtos/securityRecommendations.html) cover related hardening. Useful if we ever face volumetric IKEv2 DDoS. |
| F4 | **strongSwan 6.0.3 features** | [strongSwan blog tags/release](https://www.strongswan.org/blog/tags/release) | **RELEASED** (earlier in 6.0.x line) | (a) **New VICI event for receiving alerts** — would simplify our `bot-polling.service` alerting (currently scrapes logs). (b) **On-device certificates in managed profiles of the Android app** — useful for future Android strongSwan app integration. (c) **eap-mschapv2 plugin vulnerability fix** — security fix. |
| F5 | **strongSwan 6.0.5 features (2026-03-23)** | [strongswan.org blog 2026-03-23](https://www.strongswan.org/blog/2026/03/23/strongswan-6.0.5-released.html) | **RELEASED 2026-03-23** | charon-cmd now supports **childless IKE SA initiation** with the `--childless` option (force argument to abort if peer doesn't support). Useful for testing/debugging IKE SA establishment without configuring CHILD_SA. Minor for our prod use case. |
| F6 | **Alternative strongSwan Prometheus exporters** | Multiple community repos | Various maturity | We already have a `strongswan_exporter` (port 9101, listed in services). Community alternatives: [jlti-dev/ipsec_exporter](https://github.com/jlti-dev/ipsec_exporter) (Go), [plantatorbob/strongswan_exporter](https://github.com/plantatorbob/strongswan_exporter) (Go), [torilabs/ipsec-prometheus-exporter](https://github.com/torilabs/ipsec-prometheus-exporter) (Python), [ThaseG/strongswan-exporter](https://github.com/ThaseG/strongswan-exporter) (Go, "perfect for monitoring your VPN infrastructure"). All subscribe via VICI. Could compare metrics coverage with our existing exporter. Not urgent. |
| F7 | **strongSwan IKEv2 post-quantum roadmap** | strongSwan 6.1.0 release blog + project mailing lists | Under discussion in upstream | F2 depends on this. strongSwan project tracks PQC work; no concrete commit yet. Track [strongswan.org/blog](https://www.strongswan.org/blog/) for updates. |
| F8 | **Alternative VPN tech: WireGuard** | (not searched in this turn, but well-known) | Mature (Linux kernel since 5.6) | Faster handshake, simpler codebase, but no built-in EAP/AAA integration. Would require a different architecture. Not a drop-in for IKEv2 + FreeRADIUS. Mentioned for completeness, NOT recommended for our use case (per-user sticky VIPs + FreeRADIUS EAP integration is core to our design). |
| F9 | **strongSwan 6.0.x security recommendations** | [docs.strongswan.org Security Recommendations](https://docs.strongswan.org/docs/latest/howtos/securityRecommendations.html) | Live docs | Installing a drop policy with `local\|remote_ts = 0.0.0.0/0,::/0` blocks all un-IPsec'd traffic. We currently allow SSH + ICMP explicitly via iptables (per `bootstrap-xneelo.sh` §8). Could audit this for completeness — verify all non-IPsec bypass rules are documented. |

### From the v1.3 backlog (already in the original ROADMAP, kept here for completeness)

| Item | Status | Notes |
|---|---|---|
| iOS native IKEv2 + EAP | ⏳ OPEN | Needs Let's Encrypt cert via certbot + DNS-01 (current iOS path silently fails cert validation with self-signed). |
| Customer onboarding flow | ⏳ NOT PLANNED | Operator-only model per Zun 2026-06-19. |
| CA cert auto-bundle for Android | ⏳ OPEN | Include CA in `.sswan` profile export. |
| Phone-side UX polish | ⏳ OPEN | Shorter `rekey_time` (24h → 20-30m), `reauth_time` (24h → 2-3h), `charon.keep_alive = 20s`. |
| Server-side defaults audit | ⏳ OPEN | Every `charon.*` setting reviewed for gateway vs client default. |
| Cloudflare bot detection | ⏳ OPEN | ifconfig.me may give `ERR_CONNECTION_CLOSED` because shared MASQ IP looks bot-like. |
| 5G MTU/PMTUD | ✅ DONE | Server-side MSS clamp at 1260 fixes (5A.7). May need carrier-specific tuning. |
| 5G CGNAT stability | ⏳ OPEN | iOS SAs die in 4-30 min on cellular. Try lower fragment_size (1100), raise `ikesa_max_halfopen` to 10, `install_virtual_ip=yes` test. |
| iptables → nftables migration | ✅ DONE on VPS | Phase 7.5 (2026-07-09). LXC 903 lab may still be on iptables-legacy (low priority). |

---

## v1.3.0 customer portal (lab, 2026-06-21)

`http://192.168.10.98:8080/portal/` — customers log in with VPN credentials, see only their own tier + usage meter.

**4 rules locked (Zun, 15:22 UTC):**
1. Same credentials as VPN (NTLM hash verify, no new secrets)
2. Just tier + usage shown (no sessions/alerts/history)
3. No notifications (no email, no Telegram)
4. Strict isolation (10 guarantees, see CHANGELOG.md)

**Schema:** `customer_portal_sessions` table added (additive, no migrations).

**New files:** `portal_auth.py`, `www/portal/index.html`, `www/static/portal.js`, `tools/portal-customer-smoke.js` (10/10 green in 14.8s).

**Lab build (LAN-only).** Zun confirmed: "we still in the lab. Once we go client facing we will change all this stuff." Re-do for production when going client-facing (TLS, public DNS, CSP, etc.).

**Active development branches:** none. All merged to main.

**Live infra (verified live 2026-10-15 06:50 UTC, on `vpn-prod-01` = 154.65.110.44):**
- **vpn-prod-01 prod VPS** (`myvpn.databyte.co.za`): charon (PID 165329) in Docker container `zun/strongswan:6.0.7-mschapv2-attrsql`, bound UDP/500 + UDP/4500; 7 bind mounts from `/opt/strongswan-vpn-gateway/docker/...` (swanctl/ + strongswan.d/*.conf + charon-log-host/charon.log + /var/lib/strongswan). Charon VICI on container TCP/127.0.0.1:4502. Status: `running` (since 2026-10-08T14:06:34Z, 6d 19h uptime at audit time).
- **FreeRADIUS** (systemd on host): bound 1812/1813/18120 + CoA-relay (charon listens on 3799); `radpostauth` = 2,301 rows, `radacct` = 1,168 rows (live interim-updates firing).
- **MariaDB 11.8.6-MariaDB-0+deb13u1** (systemd): 127.0.0.1:3306, db `radius`. **45 tables**. `customers` = 12, `users` = 12, `devices` = 11, `operators` = 1, `installer_tokens` + `alerts` + `audit_log` + `purchases` + `tiers` + `radpostauth` + `radacct` + `radcheck` (28) etc.
- **vpn-portal.service** (FastAPI, gunicorn): https://vpn-portal.databyte.co.za/, returns `{"status":"ok","db_ok":true,"db_customers":12,"charon_ok":true}` (verified 2026-10-15 06:50 UTC).
- **bot-polling.service** (Telegram bot polling daemon): v2.5.0, runs `/opt/vpn-portal/bot_polling.py` + `bot.py`; chat_id 7748884597 (Zun).
- **Services running on vpn-prod-01 (24 total, incl. 7 new since 2026-07):** `apache2`, `bandwidth-monitor` ⚠ STALE PATH, `bot-polling`, `cron`, `dockhand-bridge`, `freeradius`, `mariadb`, `nginx`, `prometheus`, `prometheus-node-exporter`, `quota-exporter`, `quota-monitor`, `strongswan_exporter`, `strongswan-watchdog`, `ttyd`, `vpn-portal`, `vps-alert-relay`, `zerotier-one` + `alertmanager` (user-mode).
- **LXC 902 (myservices, 192.168.10.212)** — referenced here only for historical context; no longer hosts the VPN stack. The VPN pipeline runs on `vpn-prod-01`. LXC 902's existing services (Grafana, Prometheus, Dockhand, Paperless, node-exporter, etc.) continue to operate.
- **zun-operator:** is_operator=1, bypass on quota enforcement.
- **real-customers:** 11 customer/device records (excludes operator).

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
- Customers: 2 simultaneous connections per purchase, shared quota pool, **5GB / 10GB / 20GB** catalog at **$3 / $5 / $8 USD** for **Tier 1 / 2 / 3**
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
- `quota/seed_real_tiers.sh` — 5GB / 10GB / 20GB tiers at $3 / $5 / $8 USD (Tier 1 / 2 / 3)
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

## 5D — RADIUS migration — ✅ SHIPPED 2026-07-06 / 2026-07-13 (Phase 5 LIVE + Phase 4E + v2.2.0)

> **Repurposing note (added 2026-07-05, history preserved below):** Originally 5D was defined as multi-tenant SaaS billing (subtasks 5D.1–5D.5 below) and SHELVED 2026-06-19 because Zun is the sole operator. On 2026-07-05 the phase was repurposed to the **FreeRADIUS + daloRADIUS** migration per Zun msgs #23766 + #23783 — the underlying business need ("automate auth, leave portal in control") is the same, but the implementation is shifted away from "multi-tenant SaaS" to "single-operator + RADIUS-backed EAP". The SaaS subtasks 5D.1–5D.5 are kept below as historical context only.

**Actual delivery (verified live 2026-07-13):**
- **Phase 5 / v2.0.0** (`a9d2527`, 2026-07-06): charon `eap-radius` cutover, RFC 5176 DAE, reset bug fix, Phase 7 cleanups
- **Phase 5+** (`3b00b8e`, 2026-07-06): charon DAE/Disconnect-Request listener (UDP/3799), FreeRADIUS → charon signed CoA
- **Phase 4D** (2026-07-05): SQLAlchemy + MariaDB layer in `portal_auth.py`
- **Phase 4E** (`cb9bf69`, 2026-07-12): portal business data (customers, users, devices, installer_tokens, tiers, alerts, purchases, audit_log) moved from `ipsec.db` SQLite to MariaDB `radius` DB. See `docs/PHASE-4E-DEPLOYMENT-NOTES.md` and `docs/PHASE-4E-EXECUTION-PLAN.md`. `docs/PHASE-4E-SCOPE-ASSESSMENT.md` is **SUPERSEDED** by these two.
- **v2.1.0** (2026-07-11): case-insensitive identity normalization (CORR-026) + CI drift-detection workflow
- **v2.1.1** (2026-07-12): dead-code cleanup post-Phase-4E (CORR-035)
- **v2.2.0** (`805ea84`, 2026-07-13): FreeRADIUS operator overlay (`host/freeradius/` + `provision-freeradius.sh`) + radacct chain wired end-to-end (CORR-035)

**Origin status (Zun 2026-06-19 12:30 UTC, still valid):** "I'm the only one hosting the server." Single-operator only — no multi-tenant SaaS, no automated billing, no customer self-signup. The "buy more → DM to Zun → payment link" flow is manual by design.

**Original goal (now superseded by RADIUS migration):** Multi-tenant billing, payment-triggered reset, customer-facing messages.

| Step | What | Status |
|---|---|---|
| 5D.1 | Pricing tiers | Retained (e.g. `tier_5gb`/`tier_10gb`/`tier_20gb` columns in MariaDB `tiers`) — not a billing trigger, just an admin display |
| 5D.2 | Audit trail (who connected, when, how much) | ✅ DELIVERED — `radpostauth` table holds auth events; `radacct` (added 2026-07-13) holds session-bytes/online-time; `audit_log` in MariaDB holds admin actions |
| 5D.3 | Payment-triggered reset (Stripe / Paystack) | ⛔ NOT planned — manual top-up only per Zun 2026-06-19 |
| 5D.4 | Customer onboarding flow (signup → credentials) | ⛔ NOT planned — operator-only model |
| 5D.5 | Hard VIP pinning (released=0 enforced, no lease reuse) | ✅ DELIVERED via sticky VIP preservation across reconnects (charon pin + MariaDB `customers.framed_ip` column) |

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

> **2026-10-15 update:** kopia → `kop.databyte.co.za:443` is the current primary backup for the VPS. **Currently 502 Bad Gateway, PARKED** (Zun msg #43961); last successful `.update-info.json` mtime 2026-10-10 00:00. RPO at risk until resolved. RustFS (`open-claw-push/homelab-vpn/docs-factcheck-2026-10-15/`) is a secondary docs backup, updated on each L2 doc-update session.

---

## Change log

| Rev | Date | Author | Notes |
|---|---|---|---|
| (v1) | 2026-06-18 | Misha 🐻 | Initial 5A foundation roadmap. |
| (v2) | 2026-06-19 23:30 UTC | Misha 🐻 | 5B quota layer added. |
| (v3) | 2026-06-20 19:25 UTC | Misha 🐻 | 5C.5 reverted. Model locked to 1 creds = 1 device. |
| (v4) | 2026-06-20 10:45 UTC | Misha 🐻 | 5H HA + LB marked as last-last phase. |
| (v5) | 2026-06-21 17:50 UTC | Misha 🐻 | v1.3.0 customer portal complete. |
| (v6) | 2026-07-13 16:00 SAST | Misha 🐻 | v2.2.0 doc-sync. FreeRADIUS operator overlay + radacct chain wired end-to-end. Verified-live table 2026-07-13. |
| **(v7)** | **2026-10-15 06:50 UTC / 08:50 SAST** | **Misha 🐻** | **Per Zun msg #43994: "Yes road map next. Look at the current goals. Update what is done so far. Then looks what still to be done. Then search the web for things we can add in future such as features and cool things and security. Add it to a section of if I want it etc. facts only. No assuming no compaction. Clear written."** Full update: (1) "Current state" header refreshed to 2026-10-15 with all live-state numbers re-verified live against `vpn-prod-01` (portal v2.2.1, bot v2.5.0, 12 customers, 2,301 radpostauth, 1,168 radacct, kopia 502 PARKED). (2) Phase status table updated: v2.2.1 portal, v2.5.0 bot added. (3) NEW "Done since 2026-07-13" section listing the 7 L2 doc-update commits (`746262a`, `bda11ae`, `3337129`, `1ad0b9b`, `d295765`, `26e46d4`, `c3aa4f4`) — all pushed to `origin/main` + RustFS, CI all green. (4) "Still to be done" section: L2.5 SESSION-HISTORY (decided leave), L2.8b HARDLOCKED investigation (flagged), L2.9 BOT.md (not started), 5H HA+LB, v1.3 backlog items. (5) NEW "Future ideas (if I want it)" section with **9 web-sourced items** (F1-F9): strongSwan 6.1.0 upgrade (CVE-2026-78133, CVE-2026-78134), post-quantum IKEv2 (ML-KEM, draft-ietf-ipsecme-ikev2-mlkem-04), RFC 8019 DDoS protection, strongSwan 6.0.3 VICI alert events, strongSwan 6.0.5 childless IKE SA, alternative Prometheus exporters, strongSwan PQC roadmap, WireGuard (mentioned for completeness, not recommended), strongSwan security recommendations audit. All items cite the source URL; no speculation. All existing phase detail sections (5A, 5B, 5C, 5C.5, 5D) and v1.3 backlog retained **unchanged** per "no compaction". v1.3 backlog cross-referenced into "Future ideas" for completeness. Backup strategy section updated to add kopia 502 PARKED status. |
