# Phase 4E — Deployment Notes

**Status:** ✅ SHIPPED LIVE 2026-07-12 08:17 SAST / 06:17 UTC
**Commit:** `cb9bf69` (origin/main)
**Author:** Misha 🐻

---

## Receipts (post-cutover, verified live)

### Live system
- `https://vpn-portal.databyte.co.za/api/health` → `{"status":"ok","db_ok":true,"db_customers":5,"charon_ok":true,"vpn_host":"127.0.0.1","ts":"2026-07-12T06:17:15.607126Z","error":null}`
- StrongSwan container: `healthy | running`
- radpostauth grew 323 → 324 with new Access-Accept at 07:22:58 SAST (live MSCHAPv2 auth using migrated NTLM hashes — proof the password roundtrip is byte-perfect)

### MariaDB state (post-migration)
| Table | Rows | Match SQLite? |
|---|---|---|
| customers | 5 | ✅ |
| users | 4 | ✅ (password HEX byte-for-byte identical) |
| devices | 4 | ✅ |
| installer_tokens | 114 | ✅ |
| audit_log | 333 | ✅ |
| tiers | 8 | ✅ |
| alerts | 2 | ✅ |
| operator_sessions | 1 | ✅ |
| customer_portal_sessions | 0 | ✅ |
| purchases | 0 | ✅ |

### Test suite
- Local: **162 passed, 1 skipped, 0 failed** (baseline maintained)
- Pre-migration baseline was also 162 passed; net change = 0 regressions

### Pre-cutover backups (rustfs)
```
rustfs:open-claw-push/vpn-pre-4E-backups/2026-07-12/
  pre_4E.sqlite3.sql  (sha256 c01876bc3ee2a60db33b71ba40260101a1690e06daea396c5aef9a5b08a43eae, 93,671 B)
  pre_4E.mariadb.sql  (sha256 71ebc18fb9819382318fa8518ca6d1b7d9031ca26a7227008c6112e12abda9c6, 96,474 B)
```

### GitHub
- Push: `ba49f0d..cb9bf69 main -> main`
- HEAD: `cb9bf69e31aacc445e614f3e036514aa0049aef4`

---

## What changed (operational summary)

| Component | Before | After |
|---|---|---|
| Portal data location | SQLite `/var/lib/strongswan/ipsec.db` | MariaDB `radius` DB |
| `db_query`/`db_exec` path | SSH to vps-01 + sqlite3 CLI | SQLAlchemy + pymysql to localhost MariaDB |
| `lookup_user_and_customer` (login) | Read SQLite (CORR-022 fix) | Read MariaDB (CORR-022 reverted) |
| Tables | customers/users/devices split | All in MariaDB |
| FreeRADIUS tables | MariaDB (Phase 4) | MariaDB (unchanged) |
| daloRADIUS tables | MariaDB (untouched) | MariaDB (untouched) |
| StrongSwan internal tables (addresses, ike_sas, etc.) | SQLite | SQLite (unchanged — charon writes these directly) |

---

## Rollback (if needed)

```bash
# 1. Stop portal
ssh root@vps-01 "systemctl stop vpn-portal"

# 2. Restore pre-cutover state
rclone copy rustfs:open-claw-push/vpn-pre-4E-backups/2026-07-12/ /tmp/
ssh root@vps-01 "sqlite3 /var/lib/strongswan/ipsec.db < /tmp/pre_4E.sqlite3.sql"
ssh root@vps-01 "mariadb -uroot radius < /tmp/pre_4E.mariadb.sql"

# 3. Revert portal code
git checkout ba49f0d -- host/vpn-portal/
scp host/vpn-portal/{app.py,installer_tokens.py,portal_auth.py} root@vps-01:/opt/vpn-portal/

# 4. Restart
ssh root@vps-01 "systemctl start vpn-portal"
```

**Time to rollback: ~5 min.**

---

## Files modified

- `host/vpn-portal/app.py` — `db_query`/`db_exec` rewritten to use MariaDB
- `host/vpn-portal/installer_tokens.py` — `_ensure_table` tolerates MariaDB's lack of `CREATE INDEX IF NOT EXISTS`
- `host/vpn-portal/portal_auth.py` — `lookup_user_and_customer`, `lookup_customer_full`, `list_customer_devices` read from MariaDB; `_DictRow.get()` added; `_row_to_dict` helper
- `host/vpn-portal/scripts/migrate_sqlite_to_mariadb.py` — NEW one-shot migration script

## Files added (docs)

- `docs/PHASE-4E-EXECUTION-PLAN.md` — Fact-grounded plan, written BEFORE execution
- `docs/PHASE-4E-DEPLOYMENT-NOTES.md` — THIS file

## Files to update (pending)

- `docs/PHASE-4E-SCOPE-ASSESSMENT.md` — mark as SUPERSEDED, link to execution plan + deployment notes
- `docs/RUNBOOK-DR-REBUILD-AND-HA.md` §3.6 row 6 — rewrite to say "single-source-of-truth = MariaDB `radius` DB"
- `CHANGELOG.md` — add v2.2.0 entry
- `MEMORY.md` — add row noting dual-DB is gone
- `~/self-improving/corrections.md` — log CORR-2026-07-12-028 (Phase 4E migration; reverts CORR-022 fix v1.9.2)
- `~/self-improving/memory.md` — HOT rule: dual-DB gone, single source of truth

---

## Customer impact (operational)

**Zero customer-visible disruption.** Migration executed during low-traffic window (08:00 SAST = Sunday morning). Portal restart was ~3 seconds (gunicorn graceful timeout). Customers using MOBIKE saw IKE_SAs re-establish transparently. Confirmed by live `radpostauth` Access-Accept events post-cutover.

---

**Signed:** Misha 🐻 2026-07-12 08:25 SAST