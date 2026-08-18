-- VPN Portal — TKT-011 v2.0: time-based pricing migration
-- Created 2026-08-18 per Zun #37687 (5 of 6 questions answered)
-- Default (c) auto-renewal = Hard-cut + DELETE at exact timestamp (#1)
-- Permission: Zun #37699 "Let's continue vpn" 2026-08-18 18:18 UTC
--
-- Idempotent. Safe to re-run. Uses MariaDB 10.0.2+ IF NOT EXISTS syntax.
-- Tested on MariaDB 11.8.6 (LXC 909 + prod).
--
-- Changes:
--   1. tiers: ADD duration_days INT, speed_tier ENUM('10_10','20_20','unlimited')
--   2. customers: ADD expires_at DATETIME,
--                 mac_address_1 VARCHAR(17),
--                 mac_address_2 VARCHAR(17)
--   3. customers: ADD INDEX idx_customers_expires_at (for time-expiry quota scans)
--   4. tiers: RETIRE 4 old data-cap tiers (is_active=0); kept for audit/legacy
--   5. tiers: SEED 20 new time-based packages
--             (6 paid tiers × 3 speeds + 1 demo × 2 speeds = 18 + 2 = 20)
--
-- Existing data preserved:
--   - tiers.data_limit_bytes, customers.data_limit_bytes, customers.data_used_bytes,
--     customers.over_quota: kept, stop writing in app code, used only for legacy audit
--   - existing 7 customers: NOT auto-migrated (per-user, on Zun's directive per #37687)
--   - existing 4 tiers: kept but is_active=0 (will not appear in /api/tiers for purchase)

-- ---------- 1. tiers schema ----------
ALTER TABLE tiers ADD COLUMN IF NOT EXISTS duration_days INT NULL AFTER data_limit_bytes;
ALTER TABLE tiers ADD COLUMN IF NOT EXISTS speed_tier ENUM('10_10','20_20','unlimited') NULL AFTER duration_days;

-- ---------- 2. customers schema ----------
ALTER TABLE customers ADD COLUMN IF NOT EXISTS expires_at DATETIME NULL AFTER data_used_bytes;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS mac_address_1 VARCHAR(17) NULL AFTER expires_at;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS mac_address_2 VARCHAR(17) NULL AFTER mac_address_1;

-- ---------- 3. index for time-expiry scans (quota-monitor every 60s) ----------
CREATE INDEX IF NOT EXISTS idx_customers_expires_at ON customers(expires_at);

-- ---------- 4. retire old data-cap tiers ----------
-- (kept for audit/legacy; new purchases use the v2.0 packages seeded below)
UPDATE tiers SET is_active = 0 WHERE name IN ('tier_5gb','tier_10gb','tier_20gb','demo_100mb');

-- ---------- 5. seed new v2.0 packages ----------
-- 6 paid tiers (7/10/14/21/30/40 days) × 3 speeds (10/10, 20/20, unlimited) = 18
-- 1 demo tier (3 days) × 2 speeds (10/10, 20/20; no unlimited for demo) = 2
-- Total: 20 active packages
--
-- Pricing TBD — Zun to set price_zar per package. NULL for now.
-- Per-user migration is per-user (Zun #37687) — existing customers NOT auto-migrated.

INSERT IGNORE INTO tiers
    (name, display_name, data_limit_bytes, duration_days, speed_tier, price_zar, is_active, created_at, notes)
VALUES
    -- 3-day demo (2 speed options; no unlimited for demo)
    ('demo_3day_10',        '3-Day Demo',                    0,  3, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 demo — 3 days, 10/10 Mbps, free, expires_at = created_at + 3 days'),
    ('demo_3day_20',        '3-Day Demo (20/20)',            0,  3, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 demo — 3 days, 20/20 Mbps, free, expires_at = created_at + 3 days'),
    -- 7-day paid (3 speed options)
    ('paid_7day_10',        '7-Day Package',                 0,  7, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 7 days, 10/10 Mbps, price pending'),
    ('paid_7day_20',        '7-Day Package (20/20)',         0,  7, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 7 days, 20/20 Mbps, price pending'),
    ('paid_7day_unlimited', '7-Day Package (Unlimited)',     0,  7, 'unlimited', NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 7 days, unlimited Mbps, price pending'),
    -- 10-day paid
    ('paid_10day_10',       '10-Day Package',                0, 10, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 10 days, 10/10 Mbps, price pending'),
    ('paid_10day_20',       '10-Day Package (20/20)',        0, 10, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 10 days, 20/20 Mbps, price pending'),
    ('paid_10day_unlimited','10-Day Package (Unlimited)',    0, 10, 'unlimited', NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 10 days, unlimited Mbps, price pending'),
    -- 14-day paid
    ('paid_14day_10',       '14-Day Package',                0, 14, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 14 days, 10/10 Mbps, price pending'),
    ('paid_14day_20',       '14-Day Package (20/20)',        0, 14, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 14 days, 20/20 Mbps, price pending'),
    ('paid_14day_unlimited','14-Day Package (Unlimited)',    0, 14, 'unlimited', NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 14 days, unlimited Mbps, price pending'),
    -- 21-day paid
    ('paid_21day_10',       '21-Day Package',                0, 21, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 21 days, 10/10 Mbps, price pending'),
    ('paid_21day_20',       '21-Day Package (20/20)',        0, 21, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 21 days, 20/20 Mbps, price pending'),
    ('paid_21day_unlimited','21-Day Package (Unlimited)',    0, 21, 'unlimited', NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 21 days, unlimited Mbps, price pending'),
    -- 30-day paid
    ('paid_30day_10',       '30-Day Package',                0, 30, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 30 days, 10/10 Mbps, price pending'),
    ('paid_30day_20',       '30-Day Package (20/20)',        0, 30, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 30 days, 20/20 Mbps, price pending'),
    ('paid_30day_unlimited','30-Day Package (Unlimited)',    0, 30, 'unlimited', NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 30 days, unlimited Mbps, price pending'),
    -- 40-day paid
    ('paid_40day_10',       '40-Day Package',                0, 40, '10_10',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 40 days, 10/10 Mbps, price pending'),
    ('paid_40day_20',       '40-Day Package (20/20)',        0, 40, '20_20',     NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 40 days, 20/20 Mbps, price pending'),
    ('paid_40day_unlimited','40-Day Package (Unlimited)',    0, 40, 'unlimited', NULL, 1, UNIX_TIMESTAMP(), 'TKT-011 v2.0 paid — 40 days, unlimited Mbps, price pending');

-- ---------- 6. verify ----------
SELECT '=== TKT-011 v2.0 migration verify ===' AS step;
SELECT 'tiers columns' AS step, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) AS columns
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = 'radius' AND TABLE_NAME = 'tiers';
SELECT 'customers columns' AS step, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) AS columns
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = 'radius' AND TABLE_NAME = 'customers';
SELECT 'active tiers' AS step, COUNT(*) AS count FROM tiers WHERE is_active = 1;
SELECT 'retired tiers' AS step, COUNT(*) AS count FROM tiers WHERE is_active = 0;
SELECT 'tiers with duration_days' AS step, COUNT(*) AS count FROM tiers WHERE duration_days IS NOT NULL;
SELECT 'distinct durations' AS step, GROUP_CONCAT(DISTINCT duration_days ORDER BY duration_days) AS durations FROM tiers WHERE is_active = 1;
SELECT 'distinct speeds' AS step, GROUP_CONCAT(DISTINCT speed_tier ORDER BY speed_tier) AS speeds FROM tiers WHERE is_active = 1;
