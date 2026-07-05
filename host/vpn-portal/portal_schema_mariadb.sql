-- VPN Portal — MariaDB schema (Phase 4 of RADIUS migration)
-- Created 2026-07-05 by install-radius-daloradius.md Phase 4
--
-- Replaces portal_schema.sql (SQLite) + portal_customers_extensions.sql + portal-user-id-fk.sql
-- All portal tables now live in `radius` DB alongside FreeRADIUS + daloRADIUS tables.
-- Idempotent. Safe to re-run.
--
-- Tables:
--   users                       — EAP identity store (was in charon ipsec.db, now here)
--   customers                   — portal-managed customer rows
--   devices                     — per-device rows, joined to users via strongswan_user_id
--   tiers                       — service tier definitions
--   operator_sessions           — DB-backed operator (admin) sessions
--   customer_portal_sessions    — DB-backed customer portal sessions
--   audit_log                   — admin/operator action log (was charon ipsec.db)
--
-- FreeRADIUS tables (radcheck, radreply, radusergroup, etc.) are created by daloRADIUS
-- schema contrib/db/mariadb-daloradius.sql (Phase 3). Portal reads + writes radcheck
-- and radusergroup on customer lifecycle events.

-- ---------- users ----------
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER      NOT NULL AUTO_INCREMENT,
    name     VARCHAR(255) NOT NULL,
    password VARBINARY(255) DEFAULT NULL,    -- charon stores NTLM hash as BLOB (16 bytes MSCHAPv2)
    PRIMARY KEY (id),
    UNIQUE KEY users_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- tiers ----------
CREATE TABLE IF NOT EXISTS tiers (
    id               INTEGER      NOT NULL AUTO_INCREMENT,
    name             VARCHAR(64)  NOT NULL,
    display_name     VARCHAR(128) NOT NULL,
    data_limit_bytes BIGINT       NOT NULL,
    price_zar        INTEGER           DEFAULT NULL,
    is_active        TINYINT(1)   NOT NULL DEFAULT 1,
    created_at       INTEGER      NOT NULL,
    notes            TEXT              DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY tiers_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- customers ----------
CREATE TABLE IF NOT EXISTS customers (
    id                  INTEGER      NOT NULL AUTO_INCREMENT,
    name                VARCHAR(128) NOT NULL,
    display_name        VARCHAR(128)     DEFAULT NULL,
    telegram_id         BIGINT           DEFAULT NULL,
    telegram_username   VARCHAR(64)      DEFAULT NULL,
    is_operator         TINYINT(1)   NOT NULL DEFAULT 0,
    is_active           TINYINT(1)   NOT NULL DEFAULT 1,
    over_quota          TINYINT(1)   NOT NULL DEFAULT 0,
    data_limit_bytes    BIGINT       NOT NULL DEFAULT 0,
    data_used_bytes     BIGINT       NOT NULL DEFAULT 0,
    tier_id             INTEGER           DEFAULT NULL,
    status              VARCHAR(16)  NOT NULL DEFAULT 'active',
    max_devices         INTEGER      NOT NULL DEFAULT 1,
    bandwidth_down_mbps INTEGER      NOT NULL DEFAULT 20,
    bandwidth_up_mbps   INTEGER      NOT NULL DEFAULT 20,
    created_at          INTEGER      NOT NULL,
    updated_at          INTEGER      NOT NULL,
    notes               TEXT              DEFAULT NULL,
    -- v1.3.1+ extensions
    billing_id          VARCHAR(64)       DEFAULT NULL,
    email               VARCHAR(128)      DEFAULT NULL,
    -- v1.3.2 EAP rotation
    eap_rotated_at      INTEGER           DEFAULT NULL,
    -- v1.4.0 Bug #2 fix: explicit user FK
    user_id             INTEGER           DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY customers_name (name),
    KEY idx_customers_telegram_id (telegram_id),
    KEY idx_customers_tier_id (tier_id),
    KEY idx_customers_over_quota (over_quota),
    KEY idx_customers_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- devices ----------
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER      NOT NULL AUTO_INCREMENT,
    customer_id         INTEGER      NOT NULL,
    strongswan_user_id  INTEGER      NOT NULL,
    device_name         VARCHAR(128) NOT NULL,
    is_active           TINYINT(1)   NOT NULL DEFAULT 1,
    last_seen_v4        VARCHAR(45)       DEFAULT NULL,
    last_seen_at        INTEGER           DEFAULT NULL,
    created_at          INTEGER      NOT NULL,
    updated_at          INTEGER      NOT NULL,
    notes               TEXT              DEFAULT NULL,
    device_type         VARCHAR(32)       DEFAULT NULL,
    os_version          VARCHAR(64)       DEFAULT NULL,
    hostname            VARCHAR(128)      DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY devices_strongswan_user_id (strongswan_user_id),
    KEY idx_devices_customer_id (customer_id),
    KEY idx_devices_last_seen_at (last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- operator_sessions ----------
-- Operator (admin) login sessions. Server-side, DB-backed (v1.3.1+).
-- Replaces the HMAC-signed cookie scheme used in pre-v1.3.
CREATE TABLE IF NOT EXISTS operator_sessions (
    session_id   VARCHAR(64)  NOT NULL,
    username     VARCHAR(64)  NOT NULL,
    created_at   INTEGER      NOT NULL,
    last_active  INTEGER      NOT NULL,
    expires_at   INTEGER      NOT NULL,
    user_agent   VARCHAR(256)     DEFAULT NULL,
    ip_address   VARCHAR(64)      DEFAULT NULL,
    revoked      TINYINT(1)   NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id),
    KEY idx_operator_sessions_expires (expires_at),
    KEY idx_operator_sessions_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- customer_portal_sessions ----------
-- Customer /portal/ login sessions (v1.3.0+).
-- Scoped via cookie Path=/portal/ — never visible to operator endpoints.
CREATE TABLE IF NOT EXISTS customer_portal_sessions (
    session_id   VARCHAR(64)  NOT NULL,
    customer_id  INTEGER      NOT NULL,
    identity     VARCHAR(255) NOT NULL,
    created_at   INTEGER      NOT NULL,
    last_active  INTEGER      NOT NULL,
    expires_at   INTEGER      NOT NULL,
    user_agent   VARCHAR(256)     DEFAULT NULL,
    ip_address   VARCHAR(64)      DEFAULT NULL,
    PRIMARY KEY (session_id),
    KEY idx_customer_portal_sessions_expires (expires_at),
    KEY idx_customer_portal_sessions_customer (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- audit_log ----------
-- Portal admin action log. Append-only.
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    created_at  INTEGER      NOT NULL,
    actor       VARCHAR(64)      DEFAULT NULL,
    action      VARCHAR(64)  NOT NULL,
    target      VARCHAR(128)     DEFAULT NULL,
    detail      TEXT              DEFAULT NULL,
    PRIMARY KEY (id),
    KEY idx_audit_log_created_at (created_at),
    KEY idx_audit_log_actor (actor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- seed tiers ----------
-- v1.0.0 initial tiers (5GB/10GB/20GB/demo_100MB) — same data as SQLite seed
INSERT IGNORE INTO tiers (id, name, display_name, data_limit_bytes, price_zar, is_active, created_at, notes) VALUES
    (1, 'tier_5gb',     '5 GB',           5368709120,  300, 1, UNIX_TIMESTAMP(), 'Tier 1: 5GB — ZAR R3'),
    (2, 'tier_10gb',    '10 GB',         10737418240,  500, 1, UNIX_TIMESTAMP(), 'Tier 2: 10GB — ZAR R5'),
    (3, 'tier_20gb',    '20 GB',         21474836480,  800, 1, UNIX_TIMESTAMP(), 'Tier 3: 20GB — ZAR R8'),
    (4, 'demo_100mb',   'Demo 100MB',     104857600, NULL, 1, UNIX_TIMESTAMP(), 'Demo tier — free, 100MB cap');

-- ---------- verify ----------
SELECT 'tiers seed' AS check_name, COUNT(*) AS count FROM tiers;