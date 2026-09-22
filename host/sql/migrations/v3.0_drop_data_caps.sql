-- v3.0_drop_data_caps.sql
-- Idempotent schema cleanup: drop dead-weight data-cap columns + 8 inactive legacy tier rows.
--
-- Decision rationale (per HOT-211 + TKT-011-v2-answers.md "Layer separation" §c, msg #37713):
--   * Kill mechanism moved to `expires_at < NOW()` per HOT-214 (time-based, not data-based)
--   * `customers.data_used_bytes` RETAINED for stats observability (6/12 rows have non-zero usage today)
--   * `tiers.data_limit_bytes` (`data_limit_bytes = 0` on all 19 active rows since v2.0)
--   * `customers.data_limit_bytes` (`data_limit_bytes = 0` on 11/12 rows; only `zun` has 1 PiB which never fires)
--   * `customers.over_quota` (`over_quota = 0` on all 12 rows; kill no longer reads it)
--   * 8 inactive legacy tier rows (id 1–8): tier_5gb, tier_10gb, tier_20gb, demo_100mb,
--     custom_1048576mb_*, custom_1024mb_* — zero customers, is_active = 0, pure dead data
--
-- HOT-208 mysqldump-first gate applies (LXC 909 dev test before prod).
-- HOT-241/HOT-218: visible receipts, factual only.

START TRANSACTION;

-- Drop dead columns from tiers (always 0 on active rows)
ALTER TABLE tiers
  DROP COLUMN data_limit_bytes;

-- Drop dead columns from customers
--   data_limit_bytes: copied from tier at create, never fires as kill (only display-only)
--   over_quota: never set, never read by kill logic (which moved to expires_at)
ALTER TABLE customers
  DROP COLUMN data_limit_bytes,
  DROP COLUMN over_quota;

-- Delete 8 inactive legacy tier rows (zero customers, all is_active=0)
DELETE FROM tiers WHERE id IN (1, 2, 3, 4, 5, 6, 7, 8);

COMMIT;
