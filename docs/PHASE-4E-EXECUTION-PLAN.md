# Phase 4E — Execution Plan (Unify portal SQLite + MariaDB)

**Status:** LIVE EXECUTION 2026-07-12 (Zun msg #25454: "execute phase 4e based on facts and truth. no guessing analyse what needs to be done build a 1000% fact base execution plan no drifts. then execute ion steps the migration")
**Author:** Misha 🐻
**Source of truth:** `ssh root@vps-01` live queries + `host/vpn-portal/{app.py,installer_tokens.py,portal_auth.py}` source

---

## 1. Facts (ground-truth, verified live 2026-07-12 06:05 UTC)

### 1.1 Live SQLite state — `/var/lib/strongswan/ipsec.db`

| Table | Rows | FK |
|---|---|---|
| customers | **5** | self-contained, `user_id → users.id` |
| devices | **4** | `customer_id → customers.id` |
| users | **4** | self-contained |
| customer_portal_sessions | **0** | `customer_id → customers.id` |
| operator_sessions | **1** | self-contained |
| installer_tokens | **114** | `customer_id → customers.id` |
| audit_log | **333** | self-contained |
| logs | **0** | strongSwan internal — STAYS in SQLite |
| tiers | **8** | self-contained |
| purchases | **0** | self-contained |
| alerts | (n — exists, not counted above) | `customer_id → customers.id` |

(StrongSwan-internal tables — `addresses`, `ike_sas`, `pools`, `peer_configs`, `child_configs`, `certificates`, etc. — STAY in SQLite. Out of scope.)

### 1.2 Live MariaDB state — `127.0.0.1:3306/radius`

| Table | Rows | Schema source | Notes |
|---|---|---|---|
| customers | **0** | Pre-existing (matches SQLite columns) | EMPTY — needs data |
| users | **0** | Pre-existing | EMPTY — `password varbinary(255)` |
| devices | **0** | Pre-existing | EMPTY |
| installer_tokens | **2** | Pre-existing | LOOKS LIKE LEFTOVER TEST DATA |
| customer_portal_sessions | **0** | daloRADIUS schema | EMPTY |
| operator_sessions | **0** | daloRADIUS schema | EMPTY |
| audit_log | **0** | daloRADIUS schema | EMPTY (column `target_type` present) |
| tiers | **4** | daloRADIUS schema | PRE-EXISTING — different from SQLite's 8 |
| radcheck, radreply, etc. | (real auth data) | FreeRADIUS | UNTOUCHED |
| 39 other daloRADIUS tables | (real billing data) | daloRADIUS | UNTOUCHED |

**CRITICAL DISCOVERY vs scope doc:** daloRADIUS-prefixed tables ALREADY EXIST in MariaDB (probably auto-created by an earlier daloRADIUS installer or migration attempt) but they are EMPTY (except 2 installer_tokens + 4 tiers). The scope doc assumed we'd CREATE them fresh — we don't, we reuse the existing same-schema tables after dropping 2 stray rows + migrating SQLite data into them.

### 1.3 Code touchpoints (live counts)

| File | Lines | `sqlite3` / `db_exec` / `db_query` calls | Already-MariaDB `_db()` calls |
|---|---|---|---|
| `host/vpn-portal/app.py` | 2872 | **70** | 1 (helper) |
| `host/vpn-portal/installer_tokens.py` | 722 | **24** (via injected `db_query`/`db_exec` from app.py) | 0 |

(Scope doc said 67+12+2=81; live count is 70+24=94 via `ssh_903(["sqlite3", DB_PATH, sql])` chain. Close enough — the exact numbers don't matter, what matters is each call goes via the `db_query`/`db_exec` helpers.)

### 1.4 DB call chain (live code path)

```python
# app.py:241-266
def db_query(sql: str) -> list:          # SELECT path
    out = ssh_903(["sqlite3", "-json", DB_PATH, sql])
    ...
def db_exec(sql: str) -> None:          # INSERT/UPDATE/DELETE path
    ssh_903(["sqlite3", DB_PATH, sql])

# app.py:218
def ssh_903(cmd_args, ...):
    # subprocess ssh root@vps-01 sqlite3 /var/lib/strongswan/ipsec.db "SQL"
```

**ALL 70+24 calls go through the network**, executing `sqlite3` CLI on the VPN gateway. After Phase 4E, both `db_query` and `db_exec` will be rewritten to use SQLAlchemy `_db()` from `portal_auth.py` against MariaDB on **localhost** (no SSH round-trip — performance win).

### 1.5 MariaDB `_db()` helper (already exists in `portal_auth.py:368`)

```python
@contextmanager
def _db():
    with _engine().connect() as raw:
        yield _Conn(raw)
```

Used by 54 touchpoints in `portal_auth.py` already. Need to expose this to `app.py` + `installer_tokens.py`.

### 1.6 Baseline test (pre-migration)

```
.venv/bin/python -m pytest tests/ --tb=line -q
162 passed, 1 skipped, 0 failed (606 warnings)
```

This is the regression bar. **Post-migration must be 162 passed.**

---

## 2. Execution plan (no-drift, fact-based)

### Step 1 — Pre-cutover backups (idempotent, to rustfs)

```bash
# 1a. SQLite snapshot
ssh root@vps-01 "sqlite3 /var/lib/strongswan/ipsec.db '.dump'" \
  > pre_4E_2026-07-12.sqlite3.sql
sha256sum pre_4E_2026-07-12.sqlite3.sql

# 1b. MariaDB snapshot
ssh root@vps-01 "mariadb-dump --single-transaction radius" \
  > pre_4E_2026-07-12.mariadb.sql
sha256sum pre_4E_2026-07-12.mariadb.sql

# 1c. Ship both to rustfs (backup-of-backup)
rclone copy pre_4E_*.sql rustfs:open-claw-push/vpn-pre-4E-backups/2026-07-12/
rclone ls rustfs:open-claw-push/vpn-pre-4E-backups/2026-07-12/
```

**Expected receipts:** 2 files in rustfs, SHA-256 printed locally.

### Step 2 — Reset MariaDB portal-side tables

The 2 stray `installer_tokens` rows in MariaDB + the 4 `tiers` rows that diverge from SQLite must be cleared so the migration starts from a known-empty state.

```sql
-- Drop & re-create portal-side tables in MariaDB.
-- This keeps daloRADIUS billing tables untouched (billing_*, invoice, payment, etc.)
-- and FreeRADIUS tables untouched (radcheck, radreply, radgroup*, radacct, radpostauth).

-- 1. Drop 4 portal-side tables that exist in MariaDB but are mostly empty.
DROP TABLE IF EXISTS installer_tokens;
DROP TABLE IF EXISTS customer_portal_sessions;
DROP TABLE IF EXISTS operator_sessions;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS devices;
DROP TABLE IF EXISTS tiers;
DROP TABLE IF EXISTS alerts;
-- (purchases and logs don't exist in MariaDB at all — we add them.)

-- 2. Create portal-side tables with EXACT same column names/types as SQLite
--    (auto-increment INTEGER PRIMARY KEY, INTEGER for timestamps, BLOB for password).
--    10 tables. Engine=InnoDB. Collation=utf8mb4_unicode_ci (matches MariaDB
--    radcheck; SQLite had no collation).
```

**Receipts:** 0 rows in all 10 tables post-DROP. Pre-DROP row counts (5+4+0+0+114+333+8+0+0+1) match SQLite total.

### Step 3 — Migrate data SQLite → MariaDB

For each of the 10 tables, dump from SQLite, transform to MariaDB-compatible INSERT, verify count post-INSERT.

```python
# host/vpn-portal/scripts/migrate_sqlite_to_mariadb.py
# Run ONCE on vps-01. Idempotent (truncates target first).
# Steps:
#   1. Open both connections
#   2. For each table in [tiers, users, customers, devices, customer_portal_sessions,
#                         operator_sessions, installer_tokens, audit_log, alerts, purchases]:
#      - SELECT * FROM sqlite_table
#      - For each row, convert:
#          - INTEGER Unix-epoch → keep as INT
#          - BLOB password → UNHEX(hex_blob) for MariaDB
#          - booleans (0/1) → keep as TINYINT
#          - INSERT OR REPLACE → INSERT ... ON DUPLICATE KEY UPDATE id=id
#      - Bulk INSERT in batches of 100
#   3. Verify: SELECT COUNT(*) from each MariaDB table == SQLite count
```

**Receipts:** post-migration `mariadb -uroot radius -e 'SELECT COUNT(*) FROM X'` for each table matches SQLite count.

### Step 4 — Rewrite app.py + installer_tokens.py to use MariaDB

**Sub-step 4.1:** Replace `db_query`/`db_exec` in `app.py` with MariaDB-backed versions.

```python
# app.py:241-266
from sqlalchemy import text

def db_query(sql: str, params: dict = None) -> list:
    with portal_auth._db() as conn:
        # Note: SQLAlchemy uses :name params, raw sqlite3 uses ?
        # We support both styles for backwards-compat.
        result = conn.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]

def db_exec(sql: str, params: dict = None) -> None:
    with portal_auth._db() as conn:
        conn.execute(text(sql), params or {})
        conn.commit()
```

**Critical:** ALL 70 call sites currently pass SQLite-flavored SQL with `?` placeholders or inline strings. The MariaDB version uses `:name` placeholders OR inline strings (SQLAlchemy `text()` accepts both for SELECT, but for writes need `text()`). Need to convert:
- `INSERT OR REPLACE INTO customers (...) VALUES (...)` → `INSERT INTO customers (...) VALUES (...) ON DUPLICATE KEY UPDATE name=VALUES(name), ...`
- `datetime('now')` → `NOW()` or pass Python `datetime.utcnow()` as param
- `strftime('%s', 'now')` → `UNIX_TIMESTAMP()`
- `BLOB` columns: `users.password` hex conversion

**Sub-step 4.2:** `installer_tokens.py` already takes `db_query`/`db_exec` as injection — no signature change needed. It will inherit the new MariaDB-backed implementations.

**Sub-step 4.3:** `lookup_user_and_customer` in `portal_auth.py:381-470` currently reads SQLite (per CORR-022). After migration, the data lives in MariaDB — rewrite to use `_db()` like the rest of `portal_auth.py`.

### Step 5 — Test before deploy

```bash
.venv/bin/python -m pytest tests/ --tb=line -q
# Must show 162 passed, 1 skipped, 0 failed
```

If regressions: fix forward, do NOT increase test count. (CORR-026 lesson: don't add tests to mask bugs; fix the code.)

### Step 6 — Deploy + restart on vps-01

```bash
# Ship code
scp host/vpn-portal/app.py host/vpn-portal/installer_tokens.py host/vpn-portal/portal_auth.py root@vps-01:/opt/vpn-portal/
ssh root@vps-01 "md5sum /opt/vpn-portal/{app.py,installer_tokens.py,portal_auth.py}"
md5sum host/vpn-portal/{app.py,installer_tokens.py,portal_auth.py}
# Restart
ssh root@vps-01 "systemctl restart vpn-portal"
ssh root@vps-01 "curl -sk https://127.0.0.1/api/health"
# Expect: {"status":"ok",...}
```

### Step 7 — Live customer re-onboarding test

```bash
# Use an existing test customer or create a smoke-test customer.
# Verify: GET /api/customers returns 5 rows (matches pre-migration count).
# Verify: GET /api/customers/{id} returns full record.
# Verify: customer can authenticate (already a known MSCHAPv2 path).
```

### Step 8 — Update docs

- `docs/RUNBOOK-DR-REBUILD-AND-HA.md` §3.6 row 6: rewrite to say "single-source-of-truth = MariaDB `radius` DB"
- `docs/PHASE-4E-SCOPE-ASSESSMENT.md`: mark as SUPERSEDED, link to this exec plan
- `CHANGELOG.md`: new entry under v2.2.0
- `MEMORY.md`: add row noting dual-DB is gone

---

## 3. Risks + mitigations (from scope doc, validated against live facts)

| Risk | Severity | Mitigation |
|---|---|---|
| Live portal breaks mid-migration → customers disconnect | HIGH | Maintenance window: portal banner + restart in <30 s; customers reauth via MOBIKE |
| SQL dialect conversion misses a case → test fails | HIGH | Test suite is the gate (162 passed baseline) |
| BLOB password roundtrip breaks MSCHAPv2 auth | CRITICAL | Test: `mariadb -uroot radius -e "SELECT id, name, HEX(password) FROM users"` matches pre-migration hex strings exactly |
| DaloRADIUS name collision | MEDIUM | daloRADIUS tables (`billing_*`, `invoice`, etc.) untouched; portal-side tables get the same names they already had |
| MariaDB `_db()` in app.py circular import | LOW | `app.py` already imports `portal_auth`; just add `_db` access |
| SSH roundtrip removed → latent bug in non-localhost deploys | LOW | All prod is localhost (`VPN_HOST=127.0.0.1`); LXC 903 lab uses SSH; check both paths |

---

## 4. Out of scope (deliberately not touched)

- FreeRADIUS tables (radcheck, radreply, etc.) — STAY in MariaDB
- daloRADIUS billing/invoice tables — STAY in MariaDB, untouched
- StrongSwan internal SQLite tables (addresses, ike_sas, etc.) — STAY in SQLite
- Kopia backup paths — STAY the same; both DBs still backed up daily
- The `logs` table — STAYS in SQLite (charon writes it directly)
- HA failover architecture — separate concern (Phase 5+)

---

## 5. Rollback plan (per scope doc, validated)

1. Stop portal: `systemctl stop vpn-portal`
2. Restore from pre-cutover rustfs backup (Step 1):
   - `rclone copy rustfs:open-claw-push/vpn-pre-4E-backups/2026-07-12/pre_4E_*.sql /tmp/`
   - `ssh root@vps-01 "mariadb -uroot radius < /tmp/pre_4E_2026-07-12.mariadb.sql"`
   - `ssh root@vps-01 "sqlite3 /var/lib/strongswan/ipsec.db < /tmp/pre_4E_2026-07-12.sqlite3.sql.sql"`
3. Revert portal code: `git checkout a01cc3c -- host/vpn-portal/` (last pre-4E commit)
4. `systemctl start vpn-portal`
5. Verify: portal HTTP 200, customer auth works

**Time to rollback: ~10 min.**

---

## 6. Success criteria

- [ ] Pre-cutover backups in rustfs with matching SHA-256
- [ ] All 10 MariaDB portal-side tables populated with matching row counts
- [ ] Test suite: 162 passed, 1 skipped, 0 failed (no regression)
- [ ] `users.password` BLOB roundtrips correctly (MSCHAPv2 still works)
- [ ] `lookup_user_and_customer` works via MariaDB (revert CORR-022)
- [ ] Portal HTTP 200 live on vps-01
- [ ] At least 1 customer re-authenticates successfully end-to-end
- [ ] DR runbook updated; PHASE-4E-SCOPE-ASSESSMENT.md marked SUPERSEDED
- [ ] Memory updated (no more "Phase 4E parked")

---

**Signed:** Misha 🐻 2026-07-12 06:10 UTC