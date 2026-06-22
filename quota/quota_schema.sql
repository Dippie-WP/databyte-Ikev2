-- quota_schema.sql — Quota engine tables (additive to upstream strongSwan schema)
-- ============================================================================
-- Project:   databyte-Ikev2 (Phase 5B — Quota Layer)
-- Author:    Misha (operator-initiated, Zun-approved 2026-06-19 13:17 UTC)
-- DB target: /var/lib/strongswan/ipsec.db (shared with strongSwan attr-sql)
--
-- Apply:
--   sqlite3 /var/lib/strongswan/ipsec.db < quota/quota_schema.sql
--
-- Idempotent: all CREATE statements use IF NOT EXISTS. Safe to re-run.
--
-- Design notes:
--   * 6 new tables. No name collisions with upstream strongSwan (those are
--     addresses/attribute_pools/attributes/certificate_*.../users, etc.)
--   * FKs to upstream `users` table are documented but NOT enforced
--     (PRAGMA foreign_keys=0 in this DB; matches existing pattern in seed-db.sh)
--   * All timestamps are Unix epoch seconds INTEGER (consistent with
--     leases.acquired / leases.released)
--   * VIP resolution chain at query time:
--       nftables counter (bytes) -> leases.address (BLOB) -> leases.identity
--         -> strongSwan users.id -> devices.strongswan_user_id
--           -> devices.customer_id -> customers.tier_id -> tiers.data_limit_bytes
--   * The `is_operator` flag on customers short-circuits ALL quota checks
--     in quota-monitor.py (no nftables read, no 80%/100% triggers)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- tiers: catalog of quota tiers
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tiers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,        -- e.g. "tier_5gb", "tier_10gb", "tier_20gb", "demo_100mb"
    display_name     TEXT    NOT NULL,               -- e.g. "5 GB", "10 GB", "20 GB", "Demo 100MB"
    data_limit_bytes INTEGER NOT NULL,                -- tier allowance in bytes
    price_zar        INTEGER,                        -- price in ZAR cents (NULL = not for sale)
    is_active        INTEGER NOT NULL DEFAULT 1,     -- 1=available, 0=archived
    created_at       INTEGER NOT NULL,                -- Unix epoch seconds
    notes            TEXT
);

-- ----------------------------------------------------------------------------
-- customers: end users (incl. operator + demo)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,         -- e.g. "zun-operator", "demo-customer", "acme-corp"
    display_name     TEXT,                            -- human-readable
    telegram_id      INTEGER,                         -- Telegram user ID for DM alerts (NULL=no DM)
    telegram_username TEXT,                           -- @handle, display only
    is_operator      INTEGER NOT NULL DEFAULT 0,      -- 1 = bypass ALL quota checks (Zun only)
    is_active        INTEGER NOT NULL DEFAULT 1,      -- 0 = admin-suspended, all devices refused
    over_quota       INTEGER NOT NULL DEFAULT 0,      -- 1 = hit 100%, hard cut in effect
    data_limit_bytes INTEGER NOT NULL DEFAULT 0,      -- current allowance (tier + manual extensions)
    data_used_bytes  INTEGER NOT NULL DEFAULT 0,      -- cumulative used since last reset
    tier_id          INTEGER,                         -- FK tiers.id (NULL for operator)
    status           TEXT    NOT NULL DEFAULT 'active', -- 'active'|'suspended'|'expired'
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    notes            TEXT
);

-- ----------------------------------------------------------------------------
-- devices: strongSwan identity <-> customer (1 customer has 1+ devices)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id         INTEGER NOT NULL,             -- FK customers.id
    strongswan_user_id  INTEGER NOT NULL UNIQUE,      -- FK strongSwan users.id
    device_name         TEXT    NOT NULL,             -- e.g. "zun-android", "demo-phone"
    is_active           INTEGER NOT NULL DEFAULT 1,   -- admin can disable single device
    last_seen_v4        TEXT,                         -- last VIP assigned (text "10.99.0.3")
    last_seen_at        INTEGER,                      -- Unix epoch of last IKE_SA establish
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    notes               TEXT
);

-- ----------------------------------------------------------------------------
-- purchases: track each top-up / quota extension (audit + dedup)
-- One row per "buy more" event. When admin extends quota, a purchases row
-- is created AND customers.data_limit_bytes is incremented AND
-- customers.data_used_bytes is reset to 0 (or kept — see notes).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id       INTEGER NOT NULL,               -- FK customers.id
    tier_id           INTEGER,                        -- FK tiers.id (NULL if custom)
    data_added_bytes  INTEGER NOT NULL,               -- amount added to data_limit_bytes
    data_used_before  INTEGER NOT NULL,               -- snapshot for audit
    data_used_reset   INTEGER NOT NULL DEFAULT 1,     -- 1 = reset data_used_bytes to 0; 0 = keep
    created_at        INTEGER NOT NULL,
    notes             TEXT                            -- e.g. "Paid via EFT, ref ABC123"
);

-- ----------------------------------------------------------------------------
-- alerts: 80%/100% threshold events (dedup + audit)
-- One row per (customer, threshold) per cycle. quota-monitor.py checks
-- MAX(sent_at) for a customer+threshold before re-firing (within a cycle).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id              INTEGER NOT NULL,        -- FK customers.id
    threshold                INTEGER NOT NULL,         -- 80 or 100 (percent)
    sent_at                  INTEGER NOT NULL,         -- Unix epoch
    telegram_msg_id_zun      INTEGER,                  -- msg id of Zun's alert (NULL if not sent)
    telegram_msg_id_customer INTEGER,                  -- msg id of customer's alert (NULL if not sent)
    customer_notified        INTEGER NOT NULL DEFAULT 0, -- 1 if customer DM was sent
    data_used_bytes_at_alert INTEGER NOT NULL          -- snapshot for audit
);

-- ----------------------------------------------------------------------------
-- audit_log: admin actions (create/extend/suspend/reset/...)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT    NOT NULL,                     -- 'zun' | 'system' | 'quota-monitor'
    action      TEXT    NOT NULL,                     -- 'create_customer' | 'extend_quota' | ...
    target_type TEXT,                                 -- 'customer' | 'tier' | 'device' | 'system'
    target_id   INTEGER,                              -- target row id
    payload     TEXT,                                 -- JSON of action-specific fields
    created_at  INTEGER NOT NULL
);

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_customers_telegram_id        ON customers(telegram_id);
CREATE INDEX IF NOT EXISTS idx_customers_tier_id            ON customers(tier_id);
CREATE INDEX IF NOT EXISTS idx_customers_over_quota         ON customers(over_quota);
CREATE INDEX IF NOT EXISTS idx_devices_customer_id          ON devices(customer_id);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen_at         ON devices(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_purchases_customer_id        ON purchases(customer_id);
CREATE INDEX IF NOT EXISTS idx_purchases_created_at         ON purchases(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_customer_id           ON alerts(customer_id);
CREATE INDEX IF NOT EXISTS idx_alerts_customer_threshold    ON alerts(customer_id, threshold);
CREATE INDEX IF NOT EXISTS idx_audit_log_target             ON audit_log(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at         ON audit_log(created_at);
