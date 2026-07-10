# Changelog

All notable changes to databyte-Ikev2 are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### v2.0.0 — 2026-07-06

**Architectural boundary: charon authentication now flows through FreeRADIUS (`eap-radius`). DAE/Disconnect-Request (RFC 5176) wired for hard-cut enforcement. Reset flow restores radcheck from pre-cut backup. Phase 7 cleanup: vestigial eap-radius blocks removed, FR IPv6 secret realigned.**

This is the **first v2 baseline**. The system at HEAD (`caf7265`) is no longer the same architecture as `v1.7.0-recovered` (`01a475f`): customer EAP identities live in MariaDB `radcheck`/`radusergroup`, not in `rw-eap.conf`. Quota enforcement cuts by killing `radcheck` + sending a signed Disconnect-Request, not by rewriting the local EAP secret.

#### 1. Phase 5 cutover (charon → FreeRADIUS)

- `host/strongswan/swanctl/conf.d/10-eap-radius.conf` — `rw-eap` connection now uses `eap-radius {}` instead of inline `eap-mschapv2 { secret = ... }`. EAP identities resolved by FreeRADIUS at runtime.
- `10-eap-radius.conf` bind-mounted into the strongSwan container (`2527dcb`).
- Dockerfile EXPOSE comment documents RADIUS ports (1812/1813 auth+acct, 3799 DAE) (`953f06b`).
- Operator README + customer re-onboarding broadcast template (`5891d45`).
- Cutover-active marker in code (`5891d45`).
- `radcheck` disable is the **primary** kill (`649918f`) — written BEFORE DAE attempt, so even if DAE fails the next re-auth is denied.

#### 2. Phase 5 follow-ups (the real v2 hardening)

- **RFC 5176 DAE Disconnect-Request (`3b00b8e`)** — `host/scripts/vpn-disconnect.py` opens UDP 3799 and sends a signed Disconnect-Request to `charon eap-radius.dae`. quota-monitor now kills active SAs at hard cut instead of waiting for the next re-auth.
- **Unit + integration tests for the DAE sender (`ffd6c5d`)** — 5 unit tests for the RFC 5176 packet shape + 1 integration test against live charon.
- **Reset bug fix (`fe60527`)** — `reset_quota` now restores the customer's `radcheck` row from the `rw-eap.conf.bak-quotamon-<epoch>` backup (created pre-cut), instead of leaving the customer permanently disabled after a 100% cut. UI toast shows the restore step explicitly (`726eb1f`).
- **Pool-LEASE attribution sync (`9a93832`)** — quota-monitor reads `swanctl --list-pools --leases` for live VIP→identity mapping (drift-proof). Was reading stale `devices.last_seen_vip`.
- **Portal SQLite/MariaDB split-brain fix** (`29a96cf`, `b6caa6c`, `c63eae9`) — `lookup_user_and_customer`, `lookup_customer_full`, `list_customer_devices` now read portal-local SQLite (the actual data store for customers/users/devices) instead of MariaDB (which only holds RADIUS rows).
- **`verify_operator_session` race fix (`6f62d98`, `73affaf`)** — UPDATE-first to eliminate the MariaDB "1020 Error writing file" (HTTP 500) on dashboard.
- **30s auto-refresh on customer dashboard (`d6bd0e2`)** — operator no longer misses quota cuts while watching stale data.
- **STEP 8 deploy marker check broadened (`8f85bf2`)** — now greps `portal/index.html` + `portal.js` in addition to `app.js` + `app.css`, so portal-only deploys don't false-fail the marker grep.

#### 3. Phase 7 cleanup (deployed-only, applied to `vpn-prod-01` 2026-07-06)

- **Cleanup 1: vestigial `eap-radius` blocks removed** — `rw-eap.conf` 71 → 59 lines. Removed 6 zombie `eap-{customer}-*` blocks that Phase 4 left behind when migrating customers to RADIUS. charon reload verified clean.
- **Cleanup 3: FreeRADIUS `clients.conf` IPv6 secret realigned** — `client localhost_ipv6 { secret = ... }` was using `testing123` (FR default). Replaced with the real 64-hex-char secret matching the local-IPv4 block. **Root cause** of the 15+ "Invalid Message-Authenticator" bursts across charon reload windows (long-standing, non-impacting until Phase 5 DAE made the IPv6 path live).
- **Cleanup 2: docs only** — `install-radius-daloradius.md` expanded 339 → 506 lines with the lessons learned from Phases 4/5. No code change.

#### 4. Verification

- 3 customers in DB: `zun-operator` (1), `zun-100mb-test` (87, DISABLED), `zun-customer-demo` (89, at 101.6% on `demo_100mb` tier).
- Last cut drill at 17:39:23 SAST (customer 93): DAE ack received, radcheck DISABLED, reset through portal UI restored radcheck, customer re-authenticated.
- `check_github_parity.sh` PASS.
- `check-portal-deployed.sh --strict` PASS (pre-commit: source = deployed on `vpn-prod-01`).
- FreeRADIUS restarted cleanly, no "Invalid Message-Authenticator" in last 30s log window.
- charon loaded 2 connections (`rw-eap` + `rw-psk`) cleanly, no "unknown option: id" log noise.

#### 5. Files changed (this commit)

- `CHANGELOG.md` — this entry + updated v2.x note (old orphans were never on origin).
- `host/vpn-portal/www/static/app.js` — `SCRIPT_VERSION = 'v2.0.0'` (was `'v1.9.0-sse'`).
- `host/vpn-portal/app.py` — `FastAPI(title="databyte vpn-portal", version="2.0.0")` (was `"0.1.0"`, OpenAPI metadata consistency).

#### 6. Deferred (NOT in v2.0.0)

- **Phase 4E: SQLite → MariaDB unify** — `customers`, `users`, `devices`, `tiers`, `audit_log` still in portal-local SQLite. Split-brain by design for now. Scope at `docs/PHASE-4E-SCOPE-ASSESSMENT.md`. Target: Friday 18:00 SAST or Saturday morning (Zun's call).
- **Customer re-onboarding broadcast** — 40 customers need to re-register through the new portal-install flow. Template at `docs/design/vpn-credentials-reset-comms.md`.
- **Collation drift fix** — 23 MariaDB tables with `utf8mb4_uca1400_ai_ci` as table default. 30-sec ALTER in next maintenance.
- **Docker image prune** — 3 unused images, 118.7MB reclaimable.

#### 7. Known anomalies (deferred, non-blocking)

- charon log "loading X failed: cert/key data missing" — cosmetic, dirs don't exist.
- SQLite/MariaDB split-brain — Phase 4E will fix.

### v1.2.15 — 2026-06-21

**Edit Customer modal: fix PATCH not firing (Zun reported)** — reported via Telegram that editing customer "saalieg" was a no-op.

**Root cause:** The Save button's click handler called `$('#ed-disp')` (the IIFE's `$` helper) to fetch form fields. In this specific modal context, `$()` returned `null` even though `document.getElementById('ed-disp')` returned the input. Same `$` source (`id => document.getElementById(id)`), same call shape, different result — a closure-capture quirk we couldn't fully explain.

**Fix:** Define a local `$` inside the Save handler. Local `$` works correctly; the IIFE-level `$` didn't.

#### 1. Three earlier modal bugs also fixed in this version

Found while diagnosing the Save bug (5-question gate applied):

- **Bug A: `labeledField()` undefined** — `openEditCustomerModal` called `labeledField(...)` 8 times but the function was never defined. Caused `ReferenceError` and prevented the modal from opening at all.
- **Bug B: `openModal(modal)` undefined** — Called at end of `openEditCustomerModal` but never defined. `closeModal` existed, `openModal` did not.
- **Bug C: Tier dropdown showed wrong tier** — `el()` helper set `setAttribute('selected', 'false')` on non-matching `<option>` elements. But `selected` is a boolean HTML attribute — **presence alone selects the option**, regardless of value. Last option with `selected="false"` won.

**Fixes (all in `host/vpn-portal/www/static/app.js`):**
- `el()` helper rewritten: skip `null/undefined/false`; for boolean attributes (`selected`, `disabled`, `checked`, `readonly`, `required`, `multiple`, `hidden`, `autofocus`) set both the IDL property AND the attribute as `''`.
- `labeledField` defined inline in `openEditCustomerModal` (only used there).
- `openModal(modalEl)` added next to `closeModal` (paired helper).
- Edit modal Save handler uses local `const $ = id => document.getElementById(id)`.

#### 2. Verification

- Operator smoke: **15/15 passed in 78s** (`tools/portal-smoke.js`)
- Customer smoke: **10/10 passed in 14.8s** (`tools/portal-customer-smoke.js`)
- End-to-end PATCH via UI: confirmed `display_name` and `notes` persist
- saalieg (id=66) restored to original state after testing

#### 3. Files changed

- `host/vpn-portal/www/static/app.js` — el() helper, labeledField, openModal, Save handler
- `tools/portal-smoke.js` — added check #15 (modal opens with populated fields)
- `tools/debug_final_verify.js` — E2E PATCH test (kept for regression)

### v1.3.0 — 2026-06-21

**Customer portal (lab). Operator dashboard polish + tooling.**

#### 1. Customer portal at `/portal/` — web UI for clients to see their own tier + usage

A new lightweight web UI at `http://192.168.10.98:8080/portal/`. Customers
log in with their VPN credentials (EAP identity + password). Same NTLM
hash that charon uses for MSCHAPv2 — **no new secrets stored**.

**What it shows (and only this):**
- Tier name (e.g. "Demo 100 MB", "Tier 10 GB")
- Progress bar with used / remaining / cap
- Over-quota indicator (red bar + warning)

**What it does NOT do (Zun's rules):**
- No notifications (no email, no Telegram)
- No session history, no alerts, no other features
- No password reset flow (use the operator)

**Isolation guarantees (defense in depth):**

1. Cookie name `portal_session` (different from operator `session` cookie)
2. Cookie `Path=/api/portal/` — browser doesn't send it to operator endpoints
3. New `require_portal_session` dep — only accepts `portal_session` cookies
4. Operator `require_session` dep explicitly REJECTS `portal_session` cookies
5. All SQL scoped to `session["customer_id"]` — never takes a customer_id from input
6. Constant-time NTLM hash compare
7. Login rate-limited 5/IP/min
8. HttpOnly + SameSite=Strict cookies
9. No file I/O, no shell exec in `/api/portal/*` routes
10. Audit log: every login (success + fail) and every portal API call logged

**Schema change:** new table `customer_portal_sessions` (additive, no
migrations to existing tables). Created via direct `CREATE TABLE IF NOT
EXISTS` on `/var/lib/strongswan/ipsec.db`.

**New files:**
- `host/vpn-portal/portal_auth.py` — NTLM verify, session helpers, FastAPI deps
- `host/vpn-portal/www/portal/index.html` — single-page mobile-first UI
- `host/vpn-portal/www/static/portal.js` — vanilla JS, no framework
- `tools/portal-customer-smoke.js` — 10-check headless-browser smoke test

**New API endpoints:**
- `POST /api/portal/login` — `{identity, password}` → cookie + customer info
- `POST /api/portal/logout` — clear cookie + delete session
- `GET /api/portal/usage` — `{tier, used, limit, pct, no_cap, over_quota}` (scoped to own customer)
- `GET /api/portal/me` — `{name, email, logged_in_as}`

**Tested:** 10/10 headless-browser checks passing in 14.8s. Operator
dashboard still 14/14 green (no regression). Live test with `demo-phone`
account.

**Lab build (LAN-only).** No HTTPS, no public exposure. Zun confirmed
lab mode — re-do for production when going client-facing (TLS, public
DNS, CSP headers, audit log shipping, etc.).

#### 2. Operator dashboard polish (v1.2.11 → v1.2.14)

Rolled up under v1.3.0 per Zun's "stop micro-tagging" rule. The four
intermediate v1.2.11 / v1.2.12 / v1.2.13 / v1.2.14 tags are kept on
origin (history) but not in CHANGELOG as separate entries:

- **v1.2.11** — Self-hosted GitHub Actions runner on LXC 903
  (`actions-runner-linux-x64-2.319.1.tar.gz`, system Chromium runtime deps,
  runs-on `[self-hosted, lxc-903, vpn]`)
- **v1.2.12** — Customer management (edit / archive / delete, search, filter,
  layout polish, toast notifications)
- **v1.2.13** — Bulk operations (POST /api/customers/bulk-action,
  atomic SQLite + EAP cleanup, checkbox UI, 2-step delete confirm)
- **v1.2.14** — Column sort (whitelisted ORDER BY, operator-pinned first) +
  active-sessions indicator (green dot, 30s polling)

All four ship-tested at 14/14 green on self-hosted CI. v1.2.12's
`status='archived'` schema choice (no migration) carried forward — the
portal DB now has `status` semantics: `active` (default) or `archived`.

#### 3. Diagnosis protocol skill baked

After 5 false-positive bug diagnoses in one day (CGNAT, iptables, Ruijie
IKE ×2, bulk script escape), Zun called for a structured gate before
any "I found a bug" report. The 5-gate protocol is now a live skill
(`openclaw skills workshop apply diagnosis-protocol-20260621-744eb4ed1e`).

#### 4. Charon defaults audit

`docs/charon-defaults-audit.md` — 28 tunables reviewed, 0 hard bugs.
Documentation-only. Audit complete.

---

### v1.2.10 — 2026-06-21

**Two bug fixes from operator feedback + the failed CI run.**

#### 1. `quota/reset_demo.sh` now detects KILLED/BLOCKED EAP secrets

The demo-account reset script used to exit early ("Already at pristine
state. No action needed") whenever the DB rows for `demo-customer` were
clean (`data_used_bytes=0, over_quota=0`). It **didn't check**
`rw-eap.conf` for the `eap-demo-phone` block — so if quota-monitor had
replaced the secret with `KILLED-...` or `BLOCKED-...` during a 100% cut
run, and the DB happened to be reset but the conf wasn't, the iPhone
demo account would silently fail to authenticate.

This was the exact failure mode Zun hit on 2026-06-21 at 07:33 UTC.
Fix: extract the `eap-demo-phone` block from `rw-eap.conf` with awk,
check the secret value against `^(KILLED|BLOCKED)-`, and if matched,
restore from the latest pre-cut backup + reload charon creds — even if
the DB is already pristine.

**Added**

- AWK extraction of `eap-demo-phone.secret` from `rw-eap.conf`
- `KILLED_BLOCKED` detection flag (treated as "needs work" alongside
  `data_used_bytes != 0 || over_quota != 0`)
- Auto-restore from latest `rw-eap.conf.bak-quotamon-*` backup when
  matched, then `docker exec strongswan swanctl --load-creds`
- 4 env-var overrides for non-standard deploy paths (`DB_PATH`,
  `CONF_FILE`, `CONF_BACKUP_DIR`, `DOCKER_CONTAINER`, `CHARON_URI`)
- Status message: distinguishes "OK — reset" from "no-op — already
  pristine"

**Verified**

- Test 1: fake KILLED secret → detected + reported ✅
- Test 2: fake BLOCKED secret → detected + reported ✅
- Test 3: live conf (clean `E6fkfBK6DvUHkG1jcipJrQ`) → no false
  positive ✅
- Test 4: conf without `eap-demo-phone` block → empty extraction,
  treated as "no work needed" ✅
- End-to-end: ran reset on live LXC 903 with secret just restored →
  correctly detected "already pristine" + no-op exit 0 ✅

#### 2. `.github/workflows/portal-smoke.yml` no longer fails on push

The v1.2.9 workflow failed at step 4 "Install Chromium" in 6 seconds
because `apt-get install chromium chromium-driver ...` doesn't work on
`ubuntu-latest` (Ubuntu 24.04 ships a snap stub for `chromium` that
pulls nothing). Failed-run emails went to Zun. Two fixes:

**Fix A — Switched to full `puppeteer` package for CI**

`tools/package.json` adds `puppeteer` (full) as a devDependency. The
workflow now does `npm install` + `npx puppeteer browsers install
chrome`, which downloads a pinned Chromium into the cache. ~280MB
first run, cached thereafter. Local dev still uses `puppeteer-core` +
system `/usr/bin/chromium` (no download), via the `tools/package.json`
`dependencies` (not `devDependencies`).

**Fix B — Skip cleanly when PORTAL_URL secret isn't set**

Added a step that checks if `PORTAL_URL` secret is empty. If so, sets
`skip=true` and all subsequent steps are gated on `steps.check.outputs.skip
!= 'true'`. The job completes with exit 0 and logs a clear warning
("PORTAL_URL secret is not set. Skipping portal smoke. See docs/CI.md
for instructions."). No more failed-run emails.

**Added**

- `tools/package.json` `devDependencies.puppeteer` ^23.10.4
- `tools/package.json` `scripts.install:chromium` for local testing
- `.github/workflows/portal-smoke.yml`:
  - Skip step at top of job (checks `PORTAL_URL` env, sets
    `skip=true` if empty)
  - `if: steps.check.outputs.skip != 'true'` gates on all install /
    test steps
  - Switched "Install Chromium" step to `npm install` + `npx puppeteer
    browsers install chrome`
  - Cleanup step (`rm -rf ~/.cache/puppeteer`) saves runner disk
- Branch trigger list updated: now triggers on `v1.2.10`, `v1.3.*`

**Verified**

- YAML parses (Python `yaml.safe_load` ok)
- Local smoke test: 8/8 pass in 14.5s against live LXC 903
- The skip-guard logic: if `PORTAL_URL` is unset, the workflow exits
  in ~2s with a warning annotation, no failed-run email

### v1.2.9 — 2026-06-21

**CI hook for the portal smoke test.** New `.github/workflows/portal-smoke.yml`
runs the v1.2.8 headless-browser test on every push to `main` (and on PRs),
gates future portal releases. The workflow is **not yet enabled** — it
needs a staging portal URL the runner can reach (see `docs/CI.md` for
three options: public staging tunnel, self-hosted runner on LXC 902, or
WireGuard tunnel). For now, manual local runs against LXC 903
(`PORTAL_URL=http://192.168.10.98:8080 node tools/portal-smoke.js`) are
the verification path before tagging.

**Also fixed (caught by v1.2.8 smoke test + manual investigation)**

- `host/vpn-portal/www/static/app.js` `renderCustomers()` (~line 574):
  v1.2.7.3 only fixed `usageBar()` and the customer detail `Quota` card
  but missed the customers-list `Usage` cell, which still rendered
  `fmtBytes(used) + ' / ' + fmtBytes(quota)` directly. For operator
  accounts the row still showed `"0 B / 0 B"` and `"0.0%"` — the
  opposite of the v1.2.7.3 fix's intent. Now the row uses `usageBar()`
  (consistent with sessions table + detail), so operators get
  `<bytes> · [NO CAP]` and the `%` column shows `—` instead of `0.0%`.
  Tagged as v1.2.7.4.

**LXC 903 network correction**

- Reverted LXC 903 from static (`192.168.10.98/24`) back to DHCP.
  Zun's rule: "Keep it dhcp. If you put static it will loose network."
  Verified post-reboot: same IP (`192.168.10.98`), `scope global
  dynamic eth0`, portal health `{"status":"ok"}`. Logged in
  `~/self-improving/corrections.md`.

**Demo-phone secret reset**

- `eap-demo-phone` secret was `BLOCKED-6100b6929f585411` (KILLED during
  a quota test) — iPhone demo-customer auth was failing because of
  this. Restored original secret `E6fkfBK6DvUHkG1jcipJrQ` from latest
  pre-cut backup. Charon reloaded. DB was already pristine
  (`data_used_bytes=0`, `over_quota=0`). Zun's iPhone should now
  reconnect cleanly.

**Added**

- `.github/workflows/portal-smoke.yml` (~150 LOC) — runs on push to
  main / v1.2.7.x / v1.2.8 / v1.2.9 / master + PRs + manual
  dispatch. Installs Chromium + puppeteer-core, patches config with
  GitHub secrets (`PORTAL_URL`, `PORTAL_ADMIN_USER`, `PORTAL_ADMIN_PASS`),
  runs the smoke test, uploads screenshots as artifacts (3-day
  retention on success, 14-day on failure). Concurrency: same-branch
  runs cancel in-flight.
- `docs/CI.md` — what the workflow does, how to wire up the staging
  portal (3 options), required secrets, local-usage parallel.

**Bugs caught by the smoke test (and now fixed in v1.2.7.4)**

- v1.2.7.3 incomplete: `usageBar()` was patched but the customers-list
  row didn't use it. The smoke test's check #7
  (`Operator row shows "no cap" pill`) caught it on the very first
  run.

**Verified**

- Smoke test: 8/8 passing in 14.4s against live LXC 903
  (`http://192.168.10.98:8080`).
- LXC 903 reachable on `192.168.10.98` after revert + reboot
  (07:35 UTC).
- demo-phone secret restored, charon loaded, no active SAs (waiting
  for iPhone reconnect).

### v1.2.8 — 2026-06-21

**Headless-browser smoke test for the portal.** New `tools/portal-smoke.js`
drives a real Chromium against the portal UI and verifies 8 DOM-layer
invariants that API-only testing misses. Specifically designed to catch
the failure mode that bit v1.2.7.1 (the `el()` flatten bug — API worked,
UI rendered empty DOM, sat broken for ~36h).

**Added**

- `tools/portal-smoke.js` (260 LOC) — Puppeteer-core driver running 8 checks
  with screenshot capture on every step (success + failure)
- `tools/portal-smoke.config.json` — base URL, credentials, Chromium path,
  timeouts. Admin password is plaintext because the portal's admin
  account is an internal test credential (see `TOOLS.md`).
- `tools/package.json` + `tools/.gitignore` — `npm install` → `npm run smoke`
  workflow. Uses puppeteer-core (no Chromium download — uses system
  `/usr/bin/chromium`)
- `tools/README.md` — usage docs + how to add a new check
- `tools/example-screenshots/` — 4 reference screenshots from a real
  passing run (login, dashboard, modal, operator "no cap" row)

**The 8 checks**

1. `/` renders login form (username + password inputs present)
2. POST `/api/login` → 200 + dashboard cards appear
3. Dashboard metric cards have non-empty values (not just skeletons)
4. Customers page has ≥1 row in table
5. `+ New client` modal opens with ≥11 fields
6. Collision warning fires for `Zayd` / `Zayd-iphone` + submit disabled
7. Operator row shows "no cap" pill (v1.2.7.3 + v1.2.7.4 regression guard)
8. Operator customer detail `Used` card shows byte string + "no cap / tracking" subtitle

**Run**

```bash
cd tools && npm install                                  # ~30s, downloads puppeteer-core only
PORTAL_URL=http://192.168.10.98:8080 node portal-smoke.js
# or: npm run smoke:live --prefix tools
```

**Verified**

- 8/8 passing in 14.4s against live `http://192.168.10.98:8080` (LXC 903,
  v1.2.7.4 deployed)
- Catches the v1.2.7.1 class of bug (DOM render regression that API
  tests can't see)
- Exit code 0 = all pass, 1 = at least one fail, 2 = fatal

**Bugs found + fixed while building this**

- v1.2.7.4 — customers-list row used `fmtBytes()` directly instead of
  `usageBar()`, so operators saw `"0 B / 0 B"` and `"0.0%"` instead of
  the v1.2.7.3-intented `<bytes> · [NO CAP]` pill. Fixed + tagged as
  v1.2.7.4.
- Config-file password masking issue (smoke test sent literal `***` as
  the password, got 401). Lesson: don't write masked values to config
  files expecting the test to magically resolve them.

### v1.2.7.4 — 2026-06-21

**Operator usage visibility — list row follow-up.** v1.2.7.3 fixed the
Usage column in `usageBar()` (sessions table + detail Quota card) but
**missed the customers-list row** (~line 574 of `app.js`), which rendered
`fmtBytes(c.used_bytes) + ' / ' + fmtBytes(c.quota_bytes)` directly. For
operator accounts the row still showed `"0 B / 0 B"` and `"0.0%"` — the
opposite of the v1.2.7.3 fix's intent. Discovered while writing the
v1.2.8 headless-browser smoke test (check #7) which specifically asserts
that operator rows show the "no cap" pill.

**Fixed**

- `host/vpn-portal/www/static/app.js` `renderCustomers()` (~line 574):
  the Usage cell now calls `usageBar(used, quota, pct, over_quota,
  is_operator)` (consistent with sessions table + detail), so operators
  get `<bytes> · [NO CAP]` and the `%` column shows `—` instead of `0.0%`.

### v1.2.7.3 — 2026-06-21

**Operator usage visibility.** The portal was hiding all data usage
numbers for operator accounts — `usageBar()` returned the literal
text `"unlimited"` and the customer detail `Quota` card subtitle read
`"operator (bypass)"` with no number. Operator bypasses still apply
(no cut, no over_quota), but visibility is independent of that.

**Changed**

- `host/vpn-portal/www/static/app.js` `usageBar()`: for `is_operator=1`
  OR `!limit`, now renders `<bytes> · <no cap|no quota>` instead of just
  `"unlimited"`. Tooltip explains "Operator account — bypasses quota
  (no cap, but usage is still tracked)".
- `host/vpn-portal/www/static/app.js` `renderCustomerDetail()`:
  when no cap applies, the progress bar is hidden entirely (no 0% green
  bar clutter), the `Used` card subtitle reads `"no cap · usage tracked"`,
  and the `Quota` card shows `"no cap"` with `"bypass"` subtitle. Color
  is neutral (`dim`).
- `host/vpn-portal/www/static/app.css`: added `.vp-usage-tag` (small
  uppercase pill) and `.vp-bar-dim` (neutral bar background).

### v1.2.7.2 — 2026-06-21

**Bug fix + share controls.** Two improvements driven by operator feedback:
(a) the `Zayd-Zayd-iphone` EAP-identity collision is now blocked at both
server and browser layer, and (b) the one-shot password panel gets a
Web Share API button so operators can hand the config to a client via
WhatsApp / Telegram / SMS / email with one tap.

**Added**

- `host/vpn-portal/www/static/app.js` — `buildShareText()` builds a
  multi-line plain-text config (server, remote ID, local ID, username,
  password, per-OS setup steps, "save this — shown once" warning).
- `renderShareControls()` on the one-shot panel:
  - Mobile (Android Chrome, iOS Safari, …): Web Share API button
    "↗ Share to WhatsApp / Telegram / etc." opens the native share sheet.
    `navigator.canShare({text})` is checked first (iOS Safari requires
    it; without it, share can throw).
  - Desktop + iOS < 13 fallback: "⧉ Copy all" copies the same text via
    Clipboard API, with `document.execCommand('copy')` last-resort for
    very old browsers.
- `host/vpn-portal/www/static/app.css` — `.vp-field-warn` (red text +
  red-tinted border) and `.vp-inp-bad` (red input border) styles for
  the collision warning.
- Live device-name collision warning in the new-client form: as the
  operator types the customer slug + device name, a red warning appears
  below the device field if either condition is detected. Submit button
  is disabled while the warning is shown.

**Fixed**

- `host/vpn-portal/app.py` `POST /api/customers` — collision guard:
  rejects `device_name` that **equals** `customer.name` (case-insensitive)
  OR **starts with** `{customer.name}-` (case-insensitive). Both cases
  produce ugly / unusable EAP identities like `Zayd-Zayd-iphone`. Error
  message tells the operator exactly what to rename it to (e.g. "use
  'iphone' instead of 'Zayd-iphone'"). 400 with a precise message
  (no DB write, no EAP block created).
- The hint text under the device name field now reads "Friendly name.
  EAP identity will be '{customer-name}-{device-name}'." (was: "Friendly
  name. EAP identity = client-name-device-name" — cryptic).

**Live data cleanup (one-shot, not in code)**

- Customer `Zayd` (id=10) device `Zayd-iphone` (id=22) — created today
  with device_name `Zayd-iphone` while customer slug was `Zayd`, producing
  EAP identity `Zayd-Zayd-iphone`. **Never used**: `last_seen_at=NULL`,
  no live SA, only audit entry was the original `create_client`. Safe
  to rename in place.
  - Renamed device → `iphone` (id=22, active=1)
  - Renamed user row → `Zayd-iphone` (id=31)
  - Renamed EAP block → `eap-Zayd-iphone { id = Zayd-iphone; secret = … }`
  - Rotated password (computed via `openssl dgst -md4 -provider legacy`
    on LXC 903 — Python `hashlib.md4` is unavailable on this OpenSSL
    build, portal uses `openssl` subprocess for the same reason)
  - Audit log: `zayd_device_rename` + `zayd_device_secret_fix` entries
- New password delivered to operator via Telegram.

**Verified**

- POST /api/customers with `name=Zayd, device_name=Zayd` → 400 "device_name
  duplicates the customer name 'Zayd' (would yield EAP identity 'Zayd-Zayd').
  Use a different device name (e.g. 'iphone', 'laptop', 'pixel9')."
- POST /api/customers with `name=Zayd, device_name=Zayd-iphone` → 400
  "device_name 'Zayd-iphone' starts with the customer name 'Zayd-' …"
- POST /api/customers with `name=acme-corp, device_name=ACME-CORP-laptop`
  → 400 (case-insensitive)
- POST /api/customers with `name=test-collision-1272, device_name=pixel9`
  → 200 (legit; created + cleaned up)
- Browser form: typing "Zayd" into customer slug then "Zayd" or "Zayd-iphone"
  into device name triggers the red warning + disables submit
- `charon --load-creds` picks up the renamed `eap-Zayd-iphone` block
  (was `eap-Zayd-Zayd-iphone`)
- Portal `/api/customers/10` shows device id=22, name="iphone", active=1

**Out of scope (still backlog)**

- Per-customer portal (clients see their own usage)
- PATCH / DELETE customer endpoints
- Mobileconfig generation (planned for v1.3 — operator types the URL into
  iOS profiles via the share-text instructions)
- Email integration (SMTP)
- Tier management UI
- POST /api/customers/{id}/devices (add another device to an existing
  customer; v1.2.6 1-device-per-customer model keeps this gated)

### v1.2.7.1 — 2026-06-21

**Critical UI fix.** Pre-existing bug since 5C.2 — the portal's `el()`
helper silently skipped arrays passed as children, dropping every
array-passed DOM element. The portal UI has been UNUSABLE since 5C.2
(login form, modals, customer detail rows, devices table all rendered
with empty inner DOM); only the curl/API surface worked. Discovered
while taking v1.2.7 release screenshots.

**Fixed**

- `host/vpn-portal/www/static/app.js` `function el()`: replaced
  `for (const c of children) { if (Array.isArray(c)) continue; ... }`
  with `const flat = children.flat(Infinity); for (const c of flat) { ... }`.
  Now `el('div', {}, [a, b])` renders the same as `el('div', {}, a, b)`,
  and `el('div', {}, sub ? [a, b] : null)` works in both branches.

**Verified**

- Login form: username + password inputs render
- Dashboard: stat cards render with values (Service OK, Database
  connected, charon reachable, ipBan active, 6 customers, 0 over
  quota, etc.)
- Customers list: 6 rows render with tier badges + usage + state pills
- New client modal: 11 fields render with placeholders, hints, dropdowns
- One-shot panel: copy-to-clipboard fields + per-OS setup cards render
- Sessions page: active leases table + swanctl raw output render

**Operator action**

- Hard-refresh your browser (Ctrl+Shift+R) to bypass any cached
  app.js from the broken version. Portal is now usable.

**Why this didn't ship tests**

- All UI work since 5C.2 was tested via `curl` against the JSON API,
  not through the browser. The bug was invisible to API-only testing.
  Going forward: every portal release should include a headless-browser
  smoke test that loads `/`, fills the login form, and verifies the
  dashboard renders. See `tools/portal-smoke.js` (to be added).

**Out of scope (still backlog)**

- Per-customer portal (clients see their own usage)
- PATCH / DELETE customer endpoints
- Mobileconfig generation
- Email integration (SMTP)
- Tier management UI

(nothing in flight — all changes captured in next released version)

### v1.2.7 — 2026-06-20

Operator client onboarding — first-class UI + endpoint for creating a new
client (customer + their single device + credentials) from the portal.
Allows both existing-tier pick AND custom-cap tier auto-creation.

**Added**

- Backend `POST /api/customers` — single transaction: validates, resolves
  tier (existing OR auto-creates `custom_<N>mb_<ts>`), generates 16-byte
  URL-safe password, computes NTLM hash, inserts `customers` + `users` +
  `devices` rows, appends EAP block to `rw-eap.conf`, reloads charon,
  audit logs, returns the password ONE-SHOT in the response.
- `quota/migrate_v127_billing_email.sh` — idempotent migration adding
  `billing_id TEXT` (nullable) + `email TEXT` (nullable) to `customers`.
- `GET /api/customers` and `GET /api/customers/{id}` extended to include
  `billing_id`, `email`, and a `current_session` block
  (public_ip, remote_port, vip, device, since, IKE proposal, bytes_in,
  bytes_out, sa_state, established_secs) joined server-side from
  active leases.
- Frontend `+ New client` button on the Customers page head.
- Frontend modal form with 11 fields: client name (slug), display name,
  billing ID (optional), email (optional), Telegram (optional), notes
  (optional), tier dropdown (existing tiers + "Custom (MiB)…"), custom
  cap input, device name, device type (6 options), OS version (optional).
  Live preview of the would-be new tier name when "Custom" is picked.
  Auto-derives the client name slug from the display name if blank.
- Frontend one-shot panel after successful create: copy-to-clipboard for
  Server / Remote ID / Local ID / Username / Password, plus per-OS setup
  step-by-step cards (iOS, Android, Windows, macOS, Linux NetworkManager).
- Frontend customer detail view shows `billing_id` + `email` rows + a
  "Current session" block (with 30s auto-refresh while detail is open).
  Active session shows public IP, VIP, device, connection time, IKE
  proposal, this-session bytes in/out. No-session state shows a dim
  "no active session" line.
- Fixed: `_audit()` was writing to `at` (the column was renamed to
  `created_at` in the 5B era). Audit log has been silently failing since
  v1.2. Also added `target_type` + `target_id` columns that were unused
  but present in the schema — now properly populated.

**Changed**

- `GET /api/customers` SELECT extended with `billing_id, email`.
- `GET /api/customers/{id}` SELECT extended; `current_session` block
  added to response (None when no active SA).
- `app.py` grew +363 LOC (helpers + ClientCreate model + POST endpoint
  + extended GETs + audit fix).
- `app.js` grew +625 LOC (modal form, one-shot panel, per-OS cards,
  current_session block, live refresh).
- `app.css` grew +178 LOC (vp-page-head, vp-modal-lg, vp-form-grid,
  vp-oneshot-warn, vp-os-card, vp-cs-grid + responsive).

**Operator flow (one client at a time)**

1. Portal → Customers → `+ New client`
2. Fill 8 required + 3 optional fields. Tier: pick existing OR "Custom
   (MiB)…" and type a cap (e.g. 1500 for 1.5 GiB).
3. Click `Create client`. Server creates customer + device + EAP block
   in charon, reloads charon, audit-logs.
4. Modal flips to a one-shot panel: copy the 5 fields (Server, Remote ID,
   Local ID, Username, Password) or send the client the per-OS setup
   card that matches their device.
5. Close the modal. The customer is now in the list, with the password
   only ever visible in that one-shot panel.

**Behavior reminders**

- 1 customer = 1 device (per v1.2.6). Adding a second device for the
  same customer requires the (shelved) 5C.6 work — not in this PR.
- 5C.6 is still SHELVED. Do NOT auto-resurrect.
- The operator is the only credential issuer. No client self-service,
  no email/SMTP integration, no Telegram bot.
- If the operator needs to edit `billing_id` or `email` after the fact,
  raw SQL is the only path in this PR (no PATCH endpoint).

**Out of scope (backlog, not in this PR)**

- PATCH /api/customers/{id} — edit billing_id/email/notes
- DELETE /api/customers/{id} — soft/hard offboard via portal
- Mobileconfig generation (.mobileconfig) for one-tap iOS install
- Email integration (SMTP) for sending creds automatically
- Tier management UI (today: SQLite-only)
- Bulk import / CSV upload of customers

**Test coverage**

- POST /api/customers: existing tier (test-co) — 200
- POST /api/customers: custom cap 1500 MiB (acme-demo) — 200,
  tier `custom_1500mb_<ts>` auto-created
- POST /api/customers: existing tier with full data (beta-test-client) — 200
- POST /api/customers: bad email — 400
- POST /api/customers: bad device_type — 400
- POST /api/customers: missing custom_cap_mb when tier=custom — 400
- POST /api/customers: duplicate name — 409
- POST /api/customers: not authenticated — 401
- GET /api/customers: includes billing_id + email for all rows
- GET /api/customers/6 (test-co): includes devices[], current_session
  (None — test-co hasn't connected)
- Migration: idempotent re-run on LXC 903 (no-op)
- charon reloaded after each create (EAP block visible in `swanctl
  --load-creds` output)

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

---

### v1.3.1 — 2026-06-21

**Operator session cookie hardening + customer-portal rate-limit tighten.**

- `portal_auth.py`: operator sessions now use a 1-hour sliding-window TTL (`PORTAL_TTL=3600s`) instead of 30-day fixed. Bug #1 from friend-test session.
- Customer portal login: lockout after 5 fails / IP / minute (was 5 / IP / 5 min). Brute-force hardening for `/api/portal/login`.

### v1.3.2 — 2026-06-21

**In-portal EAP credential rotation (Bug #4).**

- `POST /api/portal/rotate` — customer can rotate their own EAP secret from `/portal/` settings. Argon2-style password validation, `swanctl --load-creds` via VICI URI, audit-logged.
- Old secret remains valid for 60s grace window (for in-flight connections), then charon reloads creds.
- Smoke test added (operator + customer). Bug #4 from friend-test session.

### v1.4.0 — 2026-06-22

**Production portal live at `myvpn.databyte.co.za`. Audit-driven hardening (CP1-CP5).**

1. **Cloudflare Origin Cert + nginx + gunicorn** in front of FastAPI on the Xneelo VPS (`vpn-prod-01`, 154.65.110.44). No public TLS termination on the VPS — CF edge terminates, Origin Cert authenticates.
2. **Strict-CSP refactor** — removed all `unsafe-inline` style/script. Uses CSSOM `setProperty('--pct', ...)` for progress bars. 0 CSP violations on operator + customer portals.
3. **No-cache HTML** on `/` and `/portal/` — prevents stale-after-deploy cache chain (CP6).
4. **Bug #2** — explicit `customers.user_id` FK to `users.id` (was ambiguous join via `device_name`). Commit `a70e866`. Fixes the customer-modal save bug.
5. **7 audit fixes shipped:** firewall INPUT TCP 80/443, customers schema (billing_id+email columns), SSH known_hosts drop-in for `ProtectSystem=strict`, dashboard `active_bans: -1` sentinel, hardcoded `.98` removal, Security tab hidden on VPS, cache-bust on app.js.
6. **Homelab/VPS separation:** app.js + templates now reference `myvpn.databyte.co.za` (not hardcoded LAN IP). Lab portal (LXC 903) stays on `192.168.10.98`; prod portal (VPS) on `myvpn.databyte.co.za`. INTENTIONALLY SEPARATE — no sync.

### v1.4.5 — 2026-06-23

**STEP 8.5 customer-facing audit tool + structural anti-lie fix.**

- `tools/customer-facing-audit.js` (commit `b9dd9b0`) — headless-browser run that creates a customer, generates an installer link, validates it, deletes the customer, and verifies the deletion. Anti-lie: catches drift between admin UI and what a real customer would see.
- STEP 1.5 in `tools/deploy-portal-vps.sh` — `node --check` on `app.js` and `portal.js` BEFORE deploy (catches syntax errors that would crash the production portal).
- Tracker entry added: "never claim shipped without actual end-to-end test" (lesson #166).

### v1.4.6 — 2026-06-23

**Live pool-lease source + EAP-aware SA parser.**

- Sessions tab now reads `swanctl --list-pools --leases` (live) instead of stale DB `devices.last_seen_vip`. Drift-proof.
- SA parser now joins via `users.name` (EAP identity) instead of `device_name`. Was breaking for Windows clients where IKE identity and device_name diverge.
- Operator dashboard shows real-time VIP per active SA.

### v1.5.0 — 2026-06-23

**`speed_plan` at customer creation (per-customer, NOT tier-driven).**

- New `SPEED_PLANS` constant in `app.py`: `standard` (20/20 Mbps) and `asymmetric_40_20` (40 down / 20 up). Per-customer at creation time.
- Create-customer modal adds speed_plan dropdown.
- Tiers drive **data quota only** — NOT bandwidth. Per-tier bandwidth mapping was nuked (Zun, 2026-06-25 05:33 UTC).
- Precedence: explicit `bandwidth_down_mbps/up_mbps` > `speed_plan` preset > default `standard`.
- `tests/test_customer_lifecycle.py` — 10 new tests covering speed_plan handling.

### v1.5.1 — 2026-06-23

**`--vp-s1` CSS variable fix (modal background bug).**

- `www/static/app.css` was missing `--vp-s1` variable. Modal backgrounds were transparent.
- Found via 5-question gate when friend reported "modal looks weird."

### v1.5.2 — 2026-06-23

**Deploy script upgrade: tar+ssh fallback, --dry-run, STEP 8 app.css check.**

- `tools/deploy-portal-vps.sh` — tar+ssh fallback when rsync unavailable.
- `--dry-run` mode shows file diff before deploy.
- STEP 8 now also greps `app.css` for CSS-only fixes (track CSS SHA in `.last_deployed`).
- `tools/check-portal-deployed.sh` — anti-lie verifier: compares deployed file SHAs to git SHAs.

### v1.6.0 — 2026-06-24

**Windows PowerShell auto-installer (HARDLOCK canonical 3-line block).**

- Customer create modal → "Generate Installer Link" button → base64-packed URL with slug + token → customer pastes 3 lines into PowerShell.
- Installer connects to `https://myvpn.databyte.co.za/`, validates token, downloads canonical `setup-databyte-vpn.ps1`, runs it. CA cert imported to `LocalMachine\Root`. EAP-MSCHAPv2 pre-configured in profile XML — no dialog.
- Bug #2 (missing `customers` DB migration on deploy) fixed — schema scripts now in `host/vpn-portal/portal_customers_extensions.sql` + idempotent `apply_portal_schema.sh`.
- Canonical `setup-databyte-vpn.ps1` v2.0.0 (commit `72e9bef`). All deprecated versions (`setup-windows-vpn.ps1`, `connect-databyte-vpn.ps1`) archived.
- Lesson #169: HARDLOCK on canonical installer scripts — no `-zun`/`-v2`/`-fix` suffixes.

### v1.6.1 — 2026-06-24

**Installer: ship the canonical 3-line block instead of iex (irm URL).**

- Was using `iex (irm https://...)` which leaked the install URL. Now ships the 3-line block: `[Net.ServicePointManager]::SecurityProtocol=...; Invoke-WebRequest ...; powershell -ExecutionPolicy Bypass -File ...`.
- All Windows client scripts use `vpn-portal.databyte.co.za` for installer download (v2.4.0+).

### v1.6.2 — 2026-06-24

**Online-only lease filter on Sessions tab.**

- Was showing all `devices.last_seen_vip` rows including offline ones. Now joins via live `swanctl --list-pools --leases`. Operator sees actual active SAs only.

### v1.6.3 — 2026-06-25

**Dashboard auto-refresh every 30s.**

- Was manual-refresh-only. Now polls `/api/health` + `/api/customers` every 30s, re-renders counts and quota bars without page reload. Bug found when operator missed a quota cut because they were looking at stale data.

### v1.6.4 — 2026-06-25

**Show allocated bandwidth in customer detail modal.**

- Modal now shows `bandwidth_down_mbps` / `bandwidth_up_mbps` from the customer row, not just the speed_plan name. Closes the "what did the operator actually set?" gap.

### v1.6.5 — 2026-06-25

**`None`-string bug fix in `db_query` + modal defense.**

- SQLite returned `'None'` (Python str) instead of NULL when a column was NULL. Caused "show bandwidth as 'None Mbit/s'" bug on customer modal. `db_query` now coerces str `'None'` → None → proper rendering.

### v1.6.6 — 2026-06-25

**Move Customers page Refresh button to top.**

- Was at bottom of the page — operator kept missing it. Now top-right next to search box.

### v1.6.7 — 2026-06-25

**`reset_quota` now restores KILLED secret after hard cut.**

- Bug: when a customer hit 100% cut, the EAP secret in `rw-eap.conf` was replaced with `KILLED-<random>`. Operator reset DB quota, but KILLED secret remained — customer couldn't reconnect.
- Fix: `reset_quota` now reads the customer's `eap_identity` from the DB (via JOIN devices → users), looks up the matching `eap-<id>` block in `rw-eap.conf`, and restores the secret from pre-cut backup before reload.
- 3 regression tests in `tests/test_reset_quota_secret_restore.py`. Bug-catching test FAILS without fix, PASSES with.

### v1.7.0 — 2026-06-26

**`speed_plan` in PATCH + Edit modal dropdown (per Zun #22367).**

- `PATCH /api/customers/{id}` now accepts `speed_plan` and re-derives `bandwidth_down_mbps`/`bandwidth_up_mbps` from the plan (unless explicit values provided).
- Edit modal adds speed_plan dropdown — operator can change plan without re-creating customer.
- Deployed to `vpn-prod-01` 2026-06-26.

### v1.7.0-recovered — 2026-06-26

**Recovery baseline after 2026-06-27 main-branch corruption incident.**

- See `docs/INCIDENT-2026-06-27.md`. `main` branch was force-pushed from `main-zun-v1.4.0` (broken historical state) back to working state at `01a475f`. Tag `v1.7.0-recovered` cut at the working commit.
- Security review summary at `docs/SECURITY-REVIEW-SUMMARY.md`: 3 CRITICAL findings all fixed.

### v1.8.0 — 2026-06-27

**Quota-monitor pool-LEASE attribution + offline-lease UI + regenerate-password button.**

1. **Pool-LEASE attribution:** quota-monitor now reads `swanctl --list-pools --leases` for live VIP→identity mapping. Was reading stale `devices.last_seen_vip`. Drift-proof.
2. **Offline lease UI:** Customers page now shows customers with stale VIPs in a separate "offline" section with last-seen timestamp.
3. **Regenerate password button:** Operator can regenerate customer EAP password from Edit modal. Argon2-style validation, charon reload, audit log.

### v1.8.1 — 2026-06-27

**Customer-detail auto-refresh now re-renders Usage card + progress bar.**

- Was refreshing the table but not the detail modal. Operator opened detail, data went stale.

### v1.8.2 — 2026-06-27

**Force refresh on tab focus via Page Visibility API.**

- When operator tabs back to the portal, data is re-fetched. Avoids stale views after long absence.

### v1.8.3 — 2026-06-27

**Shorter polling intervals + bfcache pageshow + dev console markers.**

- Polling reduced from 30s → 15s on dashboard, 60s → 30s on customers list.
- `pageshow` event from bfcache also triggers refresh (Safari/iOS).
- Dev console markers (`[v1.8.3]`) for quick version verification in browser console.

### v1.9.0-sse — 2026-06-27

**Server-Sent Events replace `setInterval` polling for live data.**

- Single SSE connection from browser → portal. Server pushes events for: active SA changes, quota updates, customer count, charon health.
- Eliminates N concurrent `setInterval` polls (was wasteful — 30s × 5 tabs × 3 endpoints).
- Live bandwidth meter now updates in <1s of actual change (was up to 30s lag).
- Fallback to `setInterval(60s)` if SSE fails.

---

**Note on v2.x version numbering:** The `v2.3.0` / `v2.6.0` / `v2.7.0` / `v2.7.1` / `v2.7.2` references in `tracker/generate_tracker.py` are **Windows installer version labels** for `setup-databyte-vpn.ps1` (HARDLOCKED at v2.6.5 in `MEMORY.md`), **not** git tags — they were never pushed to origin. The `v2.0.0` git tag (above, 2026-07-06) is the **first v2 baseline** — the architectural boundary at Phase 5 eap-radius cutover. `v1.7.0-recovered` remains the recovery baseline prior to that work.

---

### v1.9.0 (SSE merge onto `main`) — 2026-06-28

Replayed v1.9.0-sse SSE backend onto the recovery baseline branch (`c6b29b1`).
Production deployment captured at this SHA. The `v1.9.0-sse` tag remains
attached to the original preservation branch.

### v1.8.0 (ARIA sweep) — 2026-06-28

**Caveat:** the tag `v1.8.0` on origin points at the preservation branch
(quota-monitor pool-LEASE attribution + offline-lease UI + regenerate-password
button — `7a55d15`). The same logical work was merged onto `main` as part of
the `v1.7.0-recovered` recovery line. The tag is ORPHANED but the feature is
live.

### v1.7.5 (deploy SHA robustness) — 2026-06-28

`deploy-portal-vps.sh` Step 6 SHA verification now uses `sudo -n sha256sum`
because `vpn-portal` source files may install with mode 0640 (cannot be read
by the `vpn-portal` user that previously ran the SHA check).

### v1.7.4 (notification reconcile) — 2026-06-27

Portal no longer double-notifies. `showBanner` and the toast notification
system are now reconciled into a single source of truth so the operator
sees each event exactly once.

### v1.7.3 (cache-bust standardisation) — 2026-06-27

`deploy-portal-vps.sh` Step 7 cache-bust now uses a single source of truth
on `cache_bust_version` so `index.html` and static assets always agree.

### v1.7.2 (modal lifecycle) — 2026-06-26

Modal stacking, focus trap, ESC close, focus restore. Replaces broken
v1.7.0 modal build that got lost in a dashboard deploy.

### v1.7.1 (portal.css trailing HTML) — 2026-06-26

`portal.css` had trailing HTML after the closing `</style>` — removed.

### v1.6.7 (KILLED-secret restore on hard cut) — 2026-06-26

`reset_quota` now restores the customer secret to a non-KILLED state if the
hard cut left it KILLED — so the customer can reconnect immediately
instead of needing operator intervention.

### v1.6.6 (Refresh button position) — 2026-06-25

Customers page Refresh button moved to top of the list so operators see it
before scrolling.

### v1.6.5 (sqlite3 'None' string bug) — 2026-06-25

`db_query` previously passed `None` (Python) to `sqlite3`, which silently
stored the literal string `"None"`. Fixed: SQLite NULLs now stored
correctly; modal defends against the legacy `"None"` string for existing
rows.

### v1.6.4 (allocated bandwidth in detail modal) — 2026-06-25

The customer detail modal now shows allocated bandwidth alongside quota.

### v1.6.3 (dashboard auto-refresh) — 2026-06-25

Operator dashboard auto-refreshes every 30s. Live counters no longer stale
on operator view.

### v1.6.2 (Sessions tab online-only) — 2026-06-24

Sessions tab filtered to online-only leases. KILLED / OFFLINE entries no
longer clutter the operator view.

### v1.6.0 / v1.5.0 (live cutover to VPS) — 2026-06-22 / 2026-06-25

Portal cut from LXC 903 lab to VPS (`vpn-prod-01`, 154.65.110.44) on
2026-06-22. Customer docs (SOP/TOS/PP v1.0.3) filled-in-portal-URL version
deployed 2026-06-24. Speed-plan tiers (standard + asymmetric_40_20) baked
into the customer creation form on 2026-06-25.

### v1.4.6 (live pool-leases + EAP-aware SA parser) — 2026-06-26

The portal now reads live `pool-leases` from charon and parses SAs with
EAP identity (not `device_name`). First time the operator UI has shown
real connection identities (not just device strings).

### v1.4.5 (per-VIP ACCEPT iptables/iptables-nft) — 2026-06-26

Per-VIP ACCEPT rules consolidated from 508 rules → 508 entries in the
single `quota:` chain under `iptables-nft` (no breaking change).

### v1.4.x (nft METER quota + swanctl EAP regex fix) — 2026-06-26 (replay)

Zun's `f4ea70c` v1.4.0 work was replayed onto `main` as `e4a4673` for
fast-forward cleanliness. Then a destructive-replay bug wiped 117 files,
which were restored. The replay commit was preserved on a separate branch
to keep history accessible (`backup-broken-v1.9.1-pre-reset`). The actual
`main` tree at the equivalent state is downstream of `21a8ae7` and
forward.

### v1.3.0 (operator dashboard polish) — 2026-06-21

Rolled-up v1.2.11 → v1.2.14 changes (modals, filters, banner placements)
under v1.3.0 per Zun's "stop micro-tagging" rule.

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
---

### v1.0.0 (windows installer template) — 2026-07-10

**Per-customer baked Windows installer template** (`setup-databyte-vpn-windows.ps1`).

Co-exists with the v2.6.5 generic canonical installer (`setup-databyte-vpn.ps1`).
Both share Steps 2–7 (profile + IPsec + Registry + RasSetCredentials + rasdial);
the baked template differs in Steps 0–1 and in how credentials arrive on the
client (baked at operator edit time, not prompted).

**Added**

- `scripts/setup-databyte-vpn-windows.ps1` — operator template, MD5
  `5541343b9c5efe3b3b9257dbd3332805`, 22639 B / 476 lines.
- `BAKED-IN CONFIG` block at the top of the template (operator edits
  `$Username`, `$Password`, `$ServerCertSha256` per customer).
- Step 0 self-bootstrap of **ISRG Root X2** via
  `certutil.exe -addstore -f Root`. Handles Windows 10 <1903 + Win 11
  builds where X2 isn't yet trusted.
- Optional SHA-256 fingerprint pin in Step 1 (strict cert validation
  when operator bakes `$ServerCertSha256` to a real value).
- Deployed artifacts (public, kept on VPS):
  - `https://vpn-portal.databyte.co.za/static/certs/isrg-root-x2.pem`
  - `https://vpn-portal.databyte.co.za/static/certs/root-ye.pem`

**Changed**

- Canonical delivery path: **`vpn-portal.databyte.co.za`** for ALL
  customer-facing installer downloads. Reason: `myvpn.databyte.co.za`
  is flagged on Cloudflare's badware list for some customer network
  paths (verified 2026-07-10 with verbatim StopBadware template HTML
  in response body). The LE cert SAN already covers both hostnames,
  so the VPN connection target stays `myvpn.databyte.co.za`.
- All installer code paths now use `curl.exe`, not `Invoke-WebRequest`.
  Reason: PS 5.1 `Invoke-WebRequest` has known TLS 1.3 + ISRG Root X2
  chain issues. `curl.exe` (Win 10 1803+) handles it correctly.

**Verified**

- Live connection 2026-07-10 13:05 UTC, Zun's Windows 11 24H2 build 26200:
  - strongSwan SA `rw-eap #22 ESTABLISHED, IKEv2,
    AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048`,
    remote `102.182.117.43[4500]`, EAP `zun-iphone`, virtual IP `10.99.0.2`
  - 143,102 packets out, 177 MB transferred in 168 seconds
  - All 8 script steps green (cert chain bootstrap, cert verify, profile
    create, IPsec config, registry tweaks, RasSetCredentials, rasdial,
    poll-to-connected verification)

**Commits**

- `070f59e` — feat(win-installer): baked-credential IKEv2+EAP-MSCHAPv2 (initial commit)
- `a4ada5d` — feat(win-installer): re-commit under new canonical name
- `1dea754` — refactor(win-installer): rename to `setup-databyte-vpn-windows.ps1`

**Not changed**

- `setup-databyte-vpn.ps1` v2.6.5 (MD5 `fc6a83d18b195bf3cbba1558f87f912a`) —
  HARDLOCKED, no filename/URL/method changes.
- VPS-side `swanctl` / `charon.conf` / `ipsec.secrets` / FreeRADIUS configs —
  no changes.
- `docs/DAT-VPN-INT-WIN-001.md` v1.1.0 + `DAT-VPN-INT-WIN-001-v1.1.0.docx` —
  in-place doc refresh (no version bump), new § 12 added.

