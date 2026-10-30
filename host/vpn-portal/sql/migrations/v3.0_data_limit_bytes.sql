-- VPN Portal — TKT-032 v3.0: data_limit_bytes + over_quota (idempotent)
-- Created 2026-10-08 per TKT-032 + Zun #43145 ("go ahead to deploy")
-- Idempotent. Safe to re-run. Uses MariaDB 10.0.2+ IF NOT EXISTS syntax.
-- Tested on MariaDB 11.8.6 (vps-prod-01).
--
-- Why (per TKT-032 + prod /opt/vpn-portal/app.py line 354 + 1190 + 1910):
--   app.py references c.data_limit_bytes (20+ call sites: 354, 412, 1190, 1204,
--   1626, 1656, 1721, 1731, 1754, 1763, 2286, 2292, 2425, 2879, 2923, 3113,
--   3128) and c.over_quota (line 355 etc.). Neither column exists in MariaDB
--   radius.customers; data_limit_bytes is also missing from radius.tiers even
--   though t.data_limit_bytes is referenced at line 1190. /api/customers and
--   /api/customers/active-sessions currently return HTTP 500 via
--   sqlalchemy.exc.OperationalError (1054, "Unknown column 'c.data_limit_bytes'
--   in 'SELECT'"). /var/log/vpn-portal/portal.log shows the 1054 firing repeatedly.
--
-- My alembic migration 001_data_limit_bytes.py only added data_limit_bytes to
-- customers+tiers. Did NOT add over_quota. This v3.0 supersedes by adding BOTH
-- columns idempotently to unblock prod.
--
-- PRE-DEPLOY BACKUP per HOT-208 — already exists at
-- /var/backups/pre-001-data-limit-bytes-{T074334Z,T074614Z,T074857Z}.sql
-- (latest sha256 1946da57...0ed367c). TKT-032 stays open until rustfs push
-- succeeds (creds 403 currently).
--
-- DOWNGRADE (if needed, mirrors my alembic downgrade -1):
--   ALTER TABLE tiers DROP COLUMN data_limit_bytes;
--   ALTER TABLE customers DROP COLUMN over_quota;
--   ALTER TABLE customers DROP COLUMN data_limit_bytes;
-- All columns were empty (DEFAULT 0) at migration time, so safe.

-- ---------- 1. customers: data_limit_bytes ----------
ALTER TABLE customers ADD COLUMN IF NOT EXISTS data_limit_bytes BIGINT NOT NULL DEFAULT 0;

-- ---------- 2. customers: over_quota ----------
ALTER TABLE customers ADD COLUMN IF NOT EXISTS over_quota TINYINT(1) NOT NULL DEFAULT 0;

-- ---------- 3. tiers: data_limit_bytes ----------
ALTER TABLE tiers ADD COLUMN IF NOT EXISTS data_limit_bytes BIGINT NOT NULL DEFAULT 0;

-- ---------- 4. verify ----------
SELECT '=== TKT-032 v3.0 data_limit_bytes + over_quota verify ===' AS step;
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = 'radius' AND TABLE_NAME IN ('customers','tiers')
   AND COLUMN_NAME IN ('data_limit_bytes','over_quota')
 ORDER BY TABLE_NAME, COLUMN_NAME;
