# VPN Portal — Known State Reference

> **Purpose:** Live, receipt-grounded reference for the Databyte VPN portal (vps-01, `154.65.104.57`).
> Read this FIRST in any session that touches the VPN portal. Don't claim bugs without checking here.
> If a fact changes, update this file + re-ship to all 3 locations (workspace + VPS + rustfs).

**Document ID:** DAT-VPN-STATE-001
**Version:** 1.1
**Last verified:** 2026-11-06 10:01 UTC (live re-verify of `vpn-prod-01`)
**Maintainer:** Misha (this file is workspace-canonical, VPS + rustfs mirrors)

---

## §0 ANTI-HALLUCINATION CHECKLIST (read before claiming ANY VPN portal bug)

Per `SOUL.md` Pre-Response Gate + Zun msg #34558 "don't hallucinate finding fake bugs":

| ❌ Hallucination pattern | ✅ What to do instead |
|---|---|
| "X is broken" from memory | Pull live artifact (curl/ssh/grep) and cite the actual output in first 3 lines |
| "I think this was fixed in Y" from memory | `grep` the change in MEMORY.md or git log first |
| "Y used to be a problem" without checking | Verify the fix is still live with a live command (sha256 match, version check, etc.) |
| "There's a bug in code I haven't read this session" | `read` the actual file before claiming a bug |
| "This is similar to a past bug" | Verify the new bug has the same root cause + same trigger — don't pattern-match loosely |

**Rule:** If you can't produce the actual command + output as evidence, **WITHDRAW the claim** and say "I cannot verify — [reason]." See `SOUL.md` Addendum: Deterministic Verification.

---

## §1 LIVE STATE (verified 2026-11-06 10:01 UTC)

### 1.1 Portal health

```
$ curl -s --unix-socket /run/vpn-portal/gunicorn.sock http://localhost/api/health
{"status":"ok","db_ok":true,"db_customers":13,"charon_ok":true,"vpn_host":"127.0.0.1","ts":"2026-11-06T10:01:13Z","error":null}
```

- **Portal workers:** 5 (`ps` count) — `ExecStart=/opt/vpn-portal/.venv/bin/gunicorn app:app`
- **Services active:** `vpn-portal` · `bot-polling` · `quota-monitor` · `strongswan-watchdog` — `DEPLOYED_SHA = 8f72739`
- **DB layer:** MariaDB `radius` DB on `127.0.0.1:3306` (single source of truth for portal business data)
- **charon:** running in Docker container, `vpn_host=127.0.0.1` (VPS-01 prod), NOT LXC 903

### 1.2 Database state

| DB | Holds | Tables |
|---|---|---|
| **MariaDB `radius`** (live) | Portal business data + FreeRADIUS auth + daloRADIUS billing | 32+ tables including: `customers` (13 rows), `users`, `devices` (12), `alerts`, `purchases`, `audit_log` (533), `tiers`, `installer_tokens`, `operator_sessions`, `customer_portal_sessions`, `radcheck` (25 rows), `radreply`, `radacct` (1,763 auth events), `radpostauth` (2,840), `nas`, `billing_*` (daloRADIUS) |
| **SQLite `/var/lib/strongswan/ipsec.db`** (charon's config DB) | StrongSwan internal config ONLY | `addresses`, `attribute_pools`, `attributes`, `certificates`, `certificate_*`, `child_config_*`, `identities`, `ike_config_*`, `ike_sas`, `leases`, `logs`, `peer_config_*`, `pools`, `private_key_*`, `proposals`, `shared_secret_*`, `traffic_selectors` |
| **❌ WRONG:** "SQLite has customer data" | **FALSE** — customer data is in MariaDB, NOT sqlite | (post-Phase-4E since 2026-07-12) |

### 1.3 Customer roster (13 active — 2026-11-06)

```
id   name          is_operator  status  tier_id
1    zun-operator  1            active  NULL
104  zunaid        1            active  NULL
126  zun-android   1            active  NULL
156  zun-iphone    1            active  NULL
157  zidaan        0            active  28
158  zun-samsung   0            active  28
289  zayd          0            active  19
293  mesquita      0            active  52
294  test1         0            active  50
295  test-2        0            active  50
296  test-3        0            active  50
297  test-4        0            active  50
298  test-5        0            active  50
```
> Note: 4 `test*` customers present (ids 294–298) — **test data**; latest real add = `mesquita` (293).

Verify with:
```bash
mariadb radius -e "SELECT id, name, is_operator, status, tier_id FROM customers ORDER BY id;"
```

### 1.4 Filesystem state (VPS-01)

| Path | Perms | Owner | sha256 (last verified 2026-08-12) |
|---|---|---|---|
| `/opt/vpn-portal/scripts/bulk_action.py` | `0755` | `vpn-portal:vpn-portal` | `085c0da7a1d4c2c2263e0c9c3936a9a69d27e0be192751e95b3176be980b0df1` |
| `/opt/vpn-portal/scripts/bulk_action.py.v1.2.13.sqlite.bak` | `0755` | `vpn-portal:vpn-portal` | `9550de46e7de0e88b442a851d9aff3ec78b5ed8cdd2b83b9a896fe4b09d9a550` (rollback available) |
| `/etc/sudoers.d/vpn-portal` | `0440` | `root:root` | 2 entries: `NOPASSWD: /usr/bin/radclient` + `NOPASSWD: /opt/vpn-portal/scripts/bulk_action.py` |
| `/etc/vpn-portal.env` | `0640` | `root:vpn-portal` | Contains `DB_URL=mysql+pymysql://portal:<pw>@127.0.0.1:3306/radius`, `OPERATORS_JSON` (admin/misha/zarah Argon2id hashes), `SSH_KEY`, etc. |
| `/opt/vpn-portal/app.py` | `0644` | `vpn-portal:vpn-portal` | FastAPI app, **3,311 lines**; sha256 `4acf9672efde2c917adf01d4117550b778ffd6934db81def9ad9bb971e6bbf6e`; git/depsha `8f72739` |
| `/opt/vpn-portal/bot.py` | `0640` | `vpn-portal:vpn-portal` | Bot handlers, **3,002 lines**; sha256 `2f4eda96649aba17a2a972233d874b2bfb3330ce130da4ad8affec50695d85bc` |
| `/opt/vpn-portal/portal_auth.py` | `0644` | `vpn-portal:vpn-portal` | sha256 `29d8f6741602e6982f551e7734860f48a7b6a3b7a1160758ff1933f769409edd`; `_db()`, `enable/disable_customer_radcheck` |
| `/opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf` | `0644` | `root:root` | Atomic writes per TKT-002 helper |

---

## §2 RECENT FIXES (don't claim these are still broken)

### 2.1 Phase 4E cleanup — bulk_action.py migrated to MariaDB

### 2.1a Phase 4B RADIUS cleanup NOT called by bulk path — REAL BUG (TKT-014)
### 2.1b Path C executed — Grafana panels #64/65/66/73 clean of nuked identities (2026-08-12)

**Closed:** 2026-08-12 11:00 UTC, per Zun msg #34581. ~30 minutes total (web search + pre-flight + execution + audit).

**What was done:**
1. Web search confirmed `POST /api/v1/admin/tsdb/delete_series?match[]=...` requires `--web.enable-admin-api` flag (Debian manpages — OFF by default in hardened deployments)
2. Plan adjusted: admin API disabled in production, fall back to MariaDB deletes + quota-exporter restart + Prom staleness-delta (default 5min)
3. Pre-flight on VPS: Prom returned HTTP 500 with body `{"error":"admin APIs disabled"}` — confirmed flag is OFF
4. Backed up: `/tmp/pre-nuke-2026-08-12-radius.sql` (849,547 B, sha256 `2605f0d43fa...`) confirmed intact on VPS, untouched
5. Restarted `quota-exporter` (PID 1663 → 2409970 at 10:53 UTC) — clears its in-memory `username` label set
6. INSERT audit_log row **id=1506**: action=`purged_radacct_radpostauth_post_nuke`, actor=`operator`, trigger_msg_id=34581, payload includes `customers_json=["anthony","jono","paulbernardo","siraaj"]` + pre_nuke_backup SHA
7. Ran DELETE statements against radacct, radpostauth, radusergroup, radreply for the 4 identities (idempotent — BEFORE counts already 0 from 07:04 UTC manual cleanup). Exporter scrape next cycle produced no new samples for those series.
8. Confirmed via Grafana `/api/ds/query` at 11:00 UTC: **all 4 panels returned `labels_with_nuked_username=False`**:
   - Panel 64 "Active Customers (radacct)": frames=1, samples=0, no nuked
   - Panel 65 "7-day Session Outcomes": frames=3, samples=186, no nuked
   - Panel 66 "Cumulative Data per User": frames=1, samples=0, no nuked
   - Panel 73 "Sessions Started per Hour (24h)": frames=15, samples=906, no nuked

**What the fix changed:**
- radacct/radpostauth rows for the 4 (519 sessions + 1685 auth events) — already cleaned at 07:04 UTC (TKT-014 fix); documented again in audit_log 1506 for traceability
- Prom TSDB time-series with `{username="anthony-android"}` etc. — exporter stopped emitting after restart at 10:53; series naturally expire after `staleness-delta` (5min default = ~10:58 UTC) — verified clean by 11:00 UTC

**Documented receipts:**
- audit_log id=1506, ts=2026-08-12 10:54:49 UTC
- Pre-nuke backup unchanged: `/tmp/pre-nuke-2026-08-12-radius.sql` sha256 `2605f0d43fa...`
- quota-exporter PID 2409970 (restarted 10:53 UTC)
- Portal `/api/health`: `{"db_ok":true,"db_customers":8,"charon_ok":true}` — no regression

**❌ DO NOT claim "nuked customers will appear in Grafana dashboard" without first checking:**
- radcheck/radusergroup/radreply tables have 0 rows for nuked identities ✓
- radacct/radpostauth tables cleaned for nuked identities ✓
- audit_log row 1506 present with the purge-reason context ✓
- Grafana panels #64/65/66/73 return no `username=<nuked>` labels ✓

**Open ticket for systemic fix (NOT marked closed):** TKT-015 (new — see TOOLS.md `/root/.openclaw/workspace/projects/tickets/tickets-active.yaml`): make the bulk-delete handler (in `bulk_action.py` + `app.py:bulk_customer_action`) auto-trigger the purge from radacct/radpostauth for nuked customers — so this becomes atomic instead of manual.


**Discovered:** 2026-08-12 07:02 UTC, during first real production bulk delete (Anthony, Jono, Paul, Siraaj)

**Symptom:** Bulk delete leaves `radcheck` + `radusergroup` rows orphaned for nuked customers. (MariaDB `customers` / `devices` / `users` / rw-eap.conf all clean — those are handled. But FreeRADIUS-side data is leaked.)

**Root cause:** `app.py:bulk_customer_action` (around line 2710-2740) iterates `eap_targets` and calls `_remove_eap_block(identity)` for each (to clean `rw-eap.conf`), but does NOT call `portal_auth.remove_customer_radcheck_and_usergroup(identity)` — the Phase 4B RADIUS cleanup that the SINGLE delete path DOES call (in `app.py:delete_customer`).

**Verified via live artifacts (2026-08-12 07:04 UTC):**
- Bulk delete of 4 customers → HTTP 200, all 4 affected
- Post-flight: radcheck count was unchanged (17 before, 17 after) — 6 of those 17 were for nuked customers (paulbernardo-win/siraaj-iphone had Cleartext-Password + NT-Password rows = 4; anthony-android/jono-iphone had DISABLED- markers from prior archive = 2)
- radusergroup had 4 orphans (1 per identity in 'default' group)
- Manually cleaned 6+4 orphans with `DELETE FROM radcheck/radusergroup`

**Manual cleanup for THIS incident (2026-08-12 07:04 UTC):**
```sql
DELETE FROM radcheck WHERE UserName IN ('paulbernardo-win', 'siraaj-iphone', 'anthony-android', 'jono-iphone');
DELETE FROM radusergroup WHERE UserName IN ('paulbernardo-win', 'siraaj-iphone', 'anthony-android', 'jono-iphone');
```

**Fix tracked in:** TKT-014 (open as of 2026-08-12 07:04 UTC, P3 — production impact 3/5, urgency 3/5, risk_of_recurrence 5/5)

**❌ DO NOT claim "bulk delete cleans all RADIUS data" without verifying radcheck/radusergroup/radreply for the nuked identities.** Was an oversimplification in the procedure doc §3 until this finding. The bulk path handles DB + rw-eap.conf + charon creds but NOT FreeRADIUS-side tables.

**Closed:** TKT-013 P0 on 2026-08-12 06:33 UTC
**Time to resolve:** 12 minutes (06:21 → 06:33)
**Cleanup plan:** `/root/.openclaw/workspace/projects/phase-4e-cleanup-bulk-action.md` (sha256 `b926041b96973c64279067b4015f951d9086a9b4eb7146129075eff0867183de`)

**Was a real bug:** the bulk-action endpoint returned HTTP 502 because:
1. `vpn-portal` user's sudoers didn't include `/opt/vpn-portal/scripts/bulk_action.py` (only `/usr/bin/radclient`)
2. Script was `0644` (not executable)
3. Script was hardcoded to `sqlite3.connect("/var/lib/strongswan/ipsec.db")` — which post-Phase-4E only holds strongswan's internal schema, NOT customer data

**Fix shipped:**
- (a) Sudoers entry added: `vpn-portal ALL=(root) NOPASSWD: /opt/vpn-portal/scripts/bulk_action.py`
- (b) `chmod +x /opt/vpn-portal/scripts/bulk_action.py` (now 0755)
- (c) Migrated script to MariaDB (pymysql 1.1.1) — v1.3.0, 214 lines
  - Reads `DB_URL` from `/etc/vpn-portal.env` (self-contained, no portal imports)
  - Uses `%s` placeholders (was `?`), `START TRANSACTION` (was `BEGIN IMMEDIATE`)
  - Graceful payload handling: `payload.get("ids") or []`

**Receipts (re-verify any time):**
```bash
# 1. Sudoers has both entries
sudo cat /etc/sudoers.d/vpn-portal
# 2. Script is executable + new sha
ls -la /opt/vpn-portal/scripts/bulk_action.py
sha256sum /opt/vpn-portal/scripts/bulk_action.py  # live 2026-11-06: 085c0da7... (was 9c3068a5... on 2026-08-12)
# 3. Portal endpoint returns 200 (not 502)
curl -s -w 'HTTP=%{http_code}\n' --unix-socket /run/vpn-portal/gunicorn.sock \
  --cookie /tmp/zarah-cookie.txt -X POST http://localhost/api/customers/bulk-action \
  -H 'Content-Type: application/json' \
  -d '{"action":"delete","customer_ids":[1,999998],"confirm":"DELETE 2 CUSTOMERS"}'
# expect: HTTP 200 with {"affected":[],"skipped":[{"id":1,"name":"zun-operator","reason":"is_operator"},{"id":999998,"reason":"not found"}]}
```

**❌ DO NOT claim "bulk-action endpoint broken" without re-running the verification above.** Was true pre-2026-08-12 06:33 UTC; false now.


### 2.1c Phase 4B RADIUS cleanup NOW atomic via bulk_action.py v1.4.0 (TKT-015 CLOSED 2026-08-12)

**Closed:** 2026-08-12 11:05 UTC, per Zun msg #34585 "Ok go ahead. Use facts."

**Root cause:** `bulk_action.py` v1.3.0 (Phase 4E-migrated) cleaned MariaDB business tables (customers/devices/users/alerts/purchases/audit_log) but did NOT touch RADIUS auth-data tables (radcheck/radusergroup/radreply/radpostauth/radacct). Portal `bulk_customer_action` then handled `rw-eap.conf` but skipped the RADIUS cleanup. Result: every bulk delete left 4 orphans per nuked customer.

**Fix shipped (v1.3.0 → v1.4.0):**
- Added 5 new DELETE statements inside the existing `START TRANSACTION` block in `bulk_action.py` `elif action == "delete":` branch:
  1. `DELETE FROM radcheck WHERE UserName IN (eap_ids)` — auth credentials (was always gone via single-delete path; now via bulk too)
  2. `DELETE FROM radusergroup WHERE UserName IN (eap_ids)` — group memberships
  3. `DELETE FROM radreply WHERE UserName IN (eap_ids)` — per-user reply attributes (usually 0 rows)
  4. `DELETE FROM radpostauth WHERE UserName IN (eap_ids)` — auth attempt log
  5. `DELETE FROM radacct WHERE UserName IN (eap_ids)` — session history (largest volume)
- Added per-table rowcount counter returned in JSON response: `radcheck_purged`, `radusergroup_purged`, `radreply_purged`, `radpostauth_purged`, `radacct_purged`
- All DELETEs run inside the same `START TRANSACTION` as the customer delete → atomic rollback on any failure

**Atomicity test (real customer, real rad* rows):**
- Created test customer `tkt015test2` (id=124) + 1 user `tkt015test2-android` + 1 device + rad* rows:
  - radcheck: 1 row (`Cleartext-Password`), radpostauth: 2 rows (Accept + Reject), radreply: 1 row, radusergroup: 1 row, users: 1 row
- Bulk-delete via portal API: `POST /api/customers/bulk-action {"action":"delete","customer_ids":[124],"confirm":"DELETE 1 CUSTOMERS"}` → HTTP 200, `affected:[{"id":124,"name":"tkt015test2"}], devices_deleted:1`
- AFTER purge: **all 5 RADIUS tables = 0** for `tkt015test2-android`
- `customers WHERE id=124`: 0 (cascaded)
- audit_log row written (id=1510, action=`customer_bulk_delete`, ts=2026-08-12 11:05:14)

**Receipts:**
- `bulk_action.py` sha256 `9be1d5f0179edd3fcd10ca0155218535c514734eb388036e23f0da2bb48a8b9f`, 0755 vpn-portal:vpn-portal
- Backup at `/opt/vpn-portal/scripts/bulk_action.v1.3.0.bak.20260812-090355`
- Deployed atomically per TKT-002 pattern (write → sha256 verify → cp → chown → chmod)

**Future nukes now atomic by default.** No more manual Path C workarounds.

**❌ DO NOT claim "bulk-delete fully purges customer data" without verifying radcheck=0 + radpostauth=0 + radacct=0 + radusergroup=0 + radreply=0 + customers=0 + devices=0 + users=0 in MariaDB.** All atomic in one transaction per TKT-015.

### 2.2 Phase 4E migration — portal code (NOT bulk_action.py)

**Shipped:** 2026-07-12 06:20 UTC, commit `cb9bf69` on origin/main
**Source:** `/root/.openclaw/workspace/_projects/003_Databyte-Prod-VPN/vpn-portal/docs/PHASE-4E-DEPLOYMENT-NOTES.md`

**Migrated from SQLite → MariaDB:** `customers`, `users`, `devices`, `installer_tokens`, `audit_log`, `tiers`, `alerts`, `purchases`, `operator_sessions`, `customer_portal_sessions`

**Reverted:** CORR-022 v1.9.2 SQLite-read workaround for `lookup_user_and_customer` (no longer needed — both writer and reader are MariaDB now)

**❌ DO NOT claim "split-brain between SQLite and MariaDB" without re-verifying.** Was true pre-2026-07-12 06:20 UTC; false now.

### 2.3 TKT-002 — atomic rw-eap.conf writes

**Closed:** 2026-08-11 16:14 UTC

Shared atomic-write helper used by all VPN conf writers. Eliminates partial-write / truncation class of bugs.

**❌ DO NOT claim "rw-eap.conf could be truncated" without verifying the helper is in use.**

---

## §3 KNOWN GOTCHAS / FALSE-POSITIVE TRAPS

### 3.1 "bulk-action endpoint returns 502" — VERIFY FIRST (was real, now fixed)

**Trap:** The portal returns 502 with empty detail `"VPN gateway error: "` (no stderr content).

**Why:** The script `bulk_action.py` writes errors to **stdout** (`print(json.dumps({"error": ...}))`), not stderr. The portal's `_run_remote()` raises `HTTPException(502, f"VPN gateway error: {r.stderr[:200]}")` — but `r.stderr` is empty, so the detail is empty.

**Verify before claiming "still broken":** Re-run the receipt test in §2.1 above.

### 3.2 "SQLite has no customers table" — BY DESIGN post-Phase-4E

**Trap:** `sqlite3 /var/lib/strongswan/ipsec.db "SELECT * FROM customers"` returns "no such table".

**Why:** That sqlite file is now strongswan's config DB ONLY. Customer data lives in MariaDB `radius.customers`.

**Verify with:**
```bash
# MariaDB is the right place
mariadb radius -e "SELECT id, name FROM customers;"
# SQLite is for charon internals
sudo sqlite3 /var/lib/strongswan/ipsec.db ".tables"
```

### 3.3 "Customer not found" for an ID that exists in another DB

**Trap:** "I just created customer X (got id=120) but `/api/customers/120` returns 404."

**Verify with:**
```bash
mariadb radius -e "SELECT id, name FROM customers WHERE id=120;"
# If 0 rows → the create didn't actually persist (DB transaction rolled back)
# If 1 row → it's there, check that the portal is reading MariaDB (not stale cache)
```

### 3.4 Operator protection in bulk-action

**Behavior:** Operators (`is_operator=1`) are **silently skipped** by bulk-action, NOT failed. The `skipped` list contains `{id, name, reason: "is_operator"}`.

**Verify:** See §2.1 receipt test — operator id=1 returns `skipped` not 403.

### 3.5 Sudoers allowlist is exactly 2 paths

`/etc/sudoers.d/vpn-portal` allows ONLY:
- `/usr/bin/radclient` (CoA disconnect kicks)
- `/opt/vpn-portal/scripts/bulk_action.py` (bulk customer actions)

**Trap:** Adding a new sudo-needed script? Must update this file. Validate with `sudo visudo -c -f` before deploying.

---

## §4 MARIADB SCHEMA QUICK-REF (verified 2026-08-12)

### 4.1 `customers` (13 rows live — verified 2026-11-06)

| Column | Type | Notes |
|---|---|---|
| `id` | `int(11) AUTO_INCREMENT` | PK |
| `name` | `varchar(128)` | UNIQUE — this is the confirm-string for delete |
| `display_name` | `varchar(128)` | nullable |
| `telegram_id`, `telegram_username` | `bigint(20)`, `varchar(64)` | nullable |
| `is_operator` | `tinyint(1)` NOT NULL DEFAULT 0 | the 403-protection flag |
| `is_active` | `tinyint(1)` NOT NULL DEFAULT 1 | archive flips to 0 |
| `over_quota` | `tinyint(1)` NOT NULL DEFAULT 0 | |
| `data_limit_bytes` | `bigint(20)` | set by tier |
| `data_used_bytes` | `bigint(20)` | |
| `tier_id` | `int(11)` | FK to `tiers.id` |
| `status` | `varchar(16)` DEFAULT 'active' | archive: 'archived' |
| `max_devices`, `bandwidth_down_mbps`, `bandwidth_up_mbps` | `int(11)` | |
| `created_at`, `updated_at` | `int(11)` | Unix epoch seconds |
| `notes`, `email`, `eap_rotated_at`, `user_id`, `billing_id` | various | nullable |

### 4.2 `devices` (per customer)

| Column | Type |
|---|---|
| `id` | `int(11) AUTO_INCREMENT` PK |
| `customer_id` | `int(11)` |
| `strongswan_user_id` | `int(11)` UNIQUE — FK to `users.id` |
| `device_name`, `device_type`, `os_version`, `hostname` | `varchar` |
| `is_active` | `tinyint(1)` |
| `last_seen_v4` | `varchar(45)` |
| `last_seen_at`, `created_at`, `updated_at` | `int(11)` |

### 4.3 `users` (attr-sql pool identities — note: NOT customer table)

| Column | Type |
|---|---|
| `id` | `int(11) AUTO_INCREMENT` PK |
| `name` | `varchar(255)` UNIQUE — EAP identity e.g. `eap-zun-iphone` |
| `password` | `varbinary(255)` nullable |

### 4.4 Other tables the bulk script touches

- `alerts` — `id, customer_id, threshold, sent_at, ...`
- `purchases` — `id, customer_id, tier_id, data_added_bytes, ...`
- `audit_log` — `id, actor, action, target_type, target_id, payload (TEXT), created_at`
- `tiers` — `id, name, data_limit_bytes`

---

## §5 PRE-RESPONSE GATE — VPN-specific grounding

From `SOUL.md`. For VPN portal questions:

1. **Cite source first 3 lines.** Format: `Source: <path or endpoint> → <field/section>`.
2. **Verification pass before claim.** If memory-only, go read now or strike the claim.
3. **Receipts required for high-stakes claims.** See `SOUL.md` Addendum: Deterministic Verification — table of claim types → required artifact.

**For the VPN portal specifically:**
| Claim | Required artifact |
|---|---|
| "Customer X is active/archived" | `mariadb radius -e "SELECT id, name, status FROM customers WHERE id=X"` → row with status field |
| "Bulk endpoint works" | Re-run §2.1 receipt test → HTTP 200 with skipped reasons |
| "charon is up" | `docker ps \| grep strongswan` → container Up + `swanctl --list-sas` non-empty |
| "FreeRADIUS is healthy" | `systemctl status freeradius` + `tail -20 /var/log/freeradius/radius.log` (no errors) |
| "DB row exists for user Y" | `mariadb radius -e "SELECT ... FROM radcheck WHERE UserName='Y'"` → row count ≥ 1 |
| "Sudoers is correct" | `sudo cat /etc/sudoers.d/vpn-portal` → both expected entries |

---

## §6 TRIPLE-COPY RECEIPTS

This file lives in 3 places. After any edit, all 3 sha256 must match:

| Location | Path |
|---|---|
| Workspace | `/root/.openclaw/workspace/_projects/003_Databyte-Prod-VPN/vpn-known-state.md` |
| VPS-01 | `/opt/vpn-portal/docs/vpn-known-state.md` |
| RustFS | `rustfs:open-claw-push/projects/vpn-known-state/vpn-known-state.md` |

**Last verified:** 2026-11-06 10:01 UTC

---



---

## §7 CURRENT DEPLOYED IMAGE (verified 2026-11-06 10:01 UTC)

- **Running strongswan container image:** `zun/strongswan:6.0.7-mschapv2-attrsql`
- **Digest:** `sha256:7489a471a129716cba81c501c2dd3645d9cb74588c01257ddab7fc76eae46c0a`
- **Portal app:** not containerised — `gunicorn app:app` from `/opt/vpn-portal/.venv`; `DEPLOYED_SHA 8f72739`
- **Last verified:** 2026-11-06 10:01 UTC (`docker ps` + `docker inspect strongswan`)

> Superseded (the 2026-10-14 release state §7 previously recorded): `ghcr.io/dippie-wp/databyte-ikev2:2.7.4`, digest `sha256:289aa3b2…`, git `1d50ce9`, release.yml run `38512965699`.

### 3.6 Grafana `vpn-prod` dashboard — customer-level panels filter nuked + franna (added 2026-08-12 11:00 UTC per Zun msg #34602)

**Fix:** Added `$excluded_customers` dashboard variable = `franna|anthony|jono|paulbernardo|siraaj` to the `vpn-prod` dashboard. All 10 customer-level panels (panels 6, 41, 42, 64, 65, 66, 71, 72, 73, 200) now have an `,customer!~"$excluded_customers"` or `,username!~"$excluded_customers"` filter appended, so the 4 nuked customers (Anthony, Jono, Paul, Siraaj) AND franna are hidden from monitoring.

**Files modified:** Grafana dashboard `vpn-prod` (versions 23→25, dashboard uid `vpn-prod`)

**The 4 nuked customers are NOT in any Grafana panel because:** they were deleted from `customers` table at 2026-08-12 07:02:13 UTC (msg #34563 — bulk-delete via `app.py:bulk_customer_action`). `quota-exporter.py` reads from `customers` table — since those customers don't exist, it doesn't emit `vpn_customer_*` metrics for them. The `vpn_radacct_*` metrics could potentially show them if radacct rows existed, but Path C (msg #34581) at 2026-08-12 10:54:49 UTC deleted all radacct/radpostauth/radusergroup/radreply rows for the 4 identities. So they're clean across all data sources.

**franna is excluded defensively** even though she's alive (id=108) — Zun said in msg #34602 "Yes frana and all nuked users I don't want to see in grafana monitoring."

**To add more excluded customers later:** edit the `$excluded_customers` dashboard variable in Grafana UI (Dashboard settings → Variables → $excluded_customers) — no need to edit panel queries.

---

**END OF DOCUMENT v1.1**
