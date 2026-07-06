# Phase 4E — Scope Assessment: Unify portal SQLite + MariaDB

**Status:** DEFERRED — scope assessment only (Zun to approve)
**Date:** 2026-07-06 17:55 SAST
**Author:** Misha (on request, post-Phase-7-cleanups)

---

## Current state

The vpn-portal uses TWO databases simultaneously:

| DB | Where | Tables | Touchpoints |
|---|---|---|---|
| **Portal-local SQLite** | VPS `/var/lib/strongswan/ipsec.db` | customers, devices, users, customer_portal_sessions, operator_sessions, installer_tokens, audit_log, logs, tiers, purchases | 81 (67 in app.py, 12 in installer_tokens.py, 2 in portal_auth.py) |
| **MariaDB** | VPS `127.0.0.1:3306/radius` | radcheck, radusergroup, radreply (Phase 4+) + ALL daloRADIUS admin tables (39 total) | 54 in portal_auth.py (already MariaDB-aware) |

**Why two DBs:**
- Phase 4 migration only moved RADIUS-protocol tables (`radcheck`, `radusergroup`, `radreply`) to MariaDB
- Portal-local rows (customers, devices, users, audit_log, etc.) stayed in SQLite for backward compatibility
- `lookup_user_and_customer` was rewritten in `c63eae9` + `b6caa6c` to read portal-local SQLite (after I caught a bug where it was reading MariaDB and not finding rows)

**Why unify:**
- Two DBs = two sources of truth. Schema drift risk. Backup complexity (2 dumps instead of 1).
- Some operations require a JOIN across both (e.g., "customer with their radcheck status" — joins customers.id to radcheck.username, awkward)
- Hard to enforce referential integrity (customers ↔ devices is in SQLite, radcheck ↔ customer is in MariaDB, no FK enforcement)
- Maintenance pain: any new column needs to be added in two places

## What needs to move

| Table | From | To | Rows | FK constraints |
|---|---|---|---|---|
| `customers` | SQLite | MariaDB | ~3 active | None (portal data) |
| `devices` | SQLite | MariaDB | ~3 active | FK → customers.id |
| `users` | SQLite | MariaDB | ~3 active | FK → customers.id (v1.2.6+) |
| `customer_portal_sessions` | SQLite | MariaDB | rolling | FK → customers.id |
| `operator_sessions` | SQLite | MariaDB | rolling | FK → operators.username |
| `installer_tokens` | SQLite | MariaDB | rolling | FK → customers.id |
| `audit_log` | SQLite (298 rows) | MariaDB (0 rows) | 298 to migrate | None |
| `logs` | SQLite | MariaDB | bulk | None |
| `tiers` | SQLite | MariaDB | 5 rows | None |
| `purchases` | SQLite | MariaDB | rolling | FK → customers.id |

StrongSwan-internal tables (pools, ike_sas, peer_configs, etc.) STAY in SQLite — they're not portal-managed.

## SQL touchpoints to convert

| File | db_exec/db_query calls | MariaDB-aware (`_db()`) calls |
|---|---|---|
| `host/vpn-portal/app.py` | 67 | 0 |
| `host/vpn-portal/installer_tokens.py` | 12 | 0 |
| `host/vpn-portal/portal_auth.py` | 2 | 54 |
| **Total** | **81** | **54** |

Conversion approach: replace `db_exec("SQL")` → `with portal_auth._db() as conn: conn.execute(sql, params).fetchall()`. Most are simple rewrites; some need parameterization changes (sqlite3 `:p1` style vs SQLAlchemy `:name`).

## Effort estimate

| Phase | Time | Risk |
|---|---|---|
| 1. Schema dump + CREATE TABLE in MariaDB (10 tables, matching collation utf8mb4_unicode_ci) | 1 hour | LOW — copy schema, no data |
| 2. Data migration script (SQLite → MariaDB, FK preservation) | 1 hour | MEDIUM — must handle FKs and avoid daloRADIUS name collisions |
| 3. Convert 81 SQL touchpoints in app.py + installer_tokens.py | 4-6 hours | MEDIUM — easy to break edge cases |
| 4. Convert 2 db_exec touchpoints in portal_auth.py | 30 min | LOW |
| 5. Unit + integration tests | 2 hours | LOW |
| 6. End-to-end test (create/rotate/archive/cut/reset customer, audit log integrity) | 2 hours | MEDIUM |
| 7. Documentation + dry-run | 1 hour | LOW |
| **TOTAL** | **~12 hours** | |

Plus: needs a 4-hour maintenance window for the cutover (data migration + restart + verify).

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| daloRADIUS name collision (`customers`, `users` tables exist in both DBs) | HIGH | Rename SQLite tables to portal-prefixed: `portal_customers`, `portal_users`, `portal_devices`, etc. Touchpoint rewrite mandatory. |
| Data loss during migration | HIGH | `sqlite3 ipsec.db .dump > pre_4E.sql` + `mariadb-dump --single-transaction radius > pre_4E_mariadb.sql` BEFORE migration. Backup to rustfs. |
| Customer can't log in during cutover | HIGH | Maintenance window. Old SQLite kept as fallback for 7 days. |
| Different SQL dialects (sqlite3 vs MariaDB) | MEDIUM | Test parameterization early. Some SQL like `INSERT OR REPLACE` doesn't exist in MariaDB — rewrite as `DELETE + INSERT` or `INSERT … ON DUPLICATE KEY UPDATE`. |
| BLOB handling for `users.password` (NTLM hash) | MEDIUM | SQLite stores as `X'<hex>'` literal, MariaDB as `UNHEX('<hex>')` or `0x<hex>` literal. Use SQLAlchemy `Binary` type for transparent conversion. |
| Foreign key enforcement differences | LOW | MariaDB doesn't enforce FKs on InnoDB by default; matches SQLite default. Leave as-is. |
| audit_log migration — 298 rows | LOW | Bulk INSERT in batches of 100. Verify count after. |

## Rollback plan

1. Pre-cutover: backup SQLite to `pre_4E_<date>.sqlite3` on rustfs
2. Pre-cutover: backup MariaDB to `pre_4E_mariadb_<date>.sql` on rustfs
3. Keep portal-side SQLite files for 7 days post-cutover (do not delete)
4. If cutover breaks: revert portal code to commit before 4E, restart vpn-portal. SQLite untouched.
5. Drop the new MariaDB tables if rollback required (no harm, fresh state)

## My recommendation

**Defer Phase 4E to a separate work session.** This is migration, not cleanup. Zun's "cleanup" prompt covered items 1-3 (rot removal, docs, security hardening). Item 4 is a 12-hour migration that deserves its own window.

**Triggers for "do it now":**
- A bug surfaces that requires JOINing customers + radcheck
- A new feature requires referential integrity (e.g., billing)
- Zun wants to add a 2nd operator who needs admin UI access to portal-side tables

**Until then:** the 2-DB setup works. Both DBs are backed up daily to rustfs. Schema drift hasn't bitten us yet.

## What I would do if Zun says "go"

1. **Window:** Friday 18:00 SAST (4-hour block, low traffic)
2. **Pre-flight:** SQLite dump + MariaDB dump to rustfs (30 min)
3. **Schema:** Create `portal_*` prefixed tables in MariaDB (1 hour)
4. **Data:** Migrate rows with batch INSERT, verify counts (1 hour)
5. **Code:** Convert 81 touchpoints in app.py + installer_tokens.py (4 hours) — separate commits per file for easy review
6. **Test:** Run L1 + L2 test suites, customer-flow smoke test (2 hours)
7. **Cutover:** Maintenance mode banner, restart portal, smoke test (30 min)
8. **Verify:** 24h of fresh audit_log entries, monitor for any FK violations (ongoing)
9. **Cleanup:** Remove portal-side SQLite tables after 7 days (separate ticket)

## Status

- ✅ Scope assessment written
- ⏸️ Migration NOT started — awaiting Zun's explicit go-ahead + maintenance window
- 📝 If approved: ~12 hours over Friday 18:00 + Saturday morning window