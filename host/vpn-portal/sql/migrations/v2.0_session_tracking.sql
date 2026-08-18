-- VPN Portal — TKT-011 v2.0: time-based session tracking
-- Created 2026-08-18 per Zun #37716 (Option A: cumulative columns)
-- Permission: Zun #37713 "I still want to see dat at usage" + #37714
--             "we'll need a time tracker" + #37716 "Yes option A"
--
-- Idempotent. Safe to re-run. Uses MariaDB 10.0.2+ IF NOT EXISTS syntax.
-- Tested on MariaDB 11.8.6 (LXC 909 + prod).
--
-- Adds 4 cumulative tracking columns to customers:
--   total_session_time_seconds     — sum of acctsessiontime across all sessions
--   active_days_count              — count of distinct calendar days with acctstarttime
--   last_session_at                — most recent acctstoptime
--   last_session_duration_seconds  — duration of most recent session
--
-- Update mechanism: quota-monitor.py polls radacct every 60s and updates these
-- columns on session stop. NOT used for kill logic — kill is time-based via
-- `expires_at < NOW() AND is_active=1` per (c) = #1 Hard-cut + DELETE.
--
-- Reason for cumulative (not per-day table): Zun wants "X days out of Y" +
-- total time visibility for the UI. Per-day breakdown (Option B) deferred
-- unless analytics grade reporting is needed.

-- ---------- 1. customers schema ----------
ALTER TABLE customers ADD COLUMN IF NOT EXISTS total_session_time_seconds BIGINT NOT NULL DEFAULT 0;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS active_days_count INT NOT NULL DEFAULT 0;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS last_session_at DATETIME NULL;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS last_session_duration_seconds INT NULL;

-- ---------- 2. verify ----------
SELECT '=== TKT-011 v2.0 session tracking verify ===' AS step;
SELECT 'customers new columns' AS step, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) AS columns
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = 'radius' AND TABLE_NAME = 'customers'
   AND COLUMN_NAME IN ('total_session_time_seconds', 'active_days_count', 'last_session_at', 'last_session_duration_seconds');
