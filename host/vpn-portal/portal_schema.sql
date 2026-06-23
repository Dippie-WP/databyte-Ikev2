-- VPN Portal — session tables (v1.3.0 + v1.3.1)
--
-- Idempotent. Safe to re-run.
-- Created by portal_auth.py / apply_portal_schema.sh on deploy.
--
-- operator_sessions   — server-side sessions for admin/operator login (replaces
--                       HMAC-signed cookie from pre-v1.3 portal). Created in v1.3.1.
-- customer_portal_sessions — server-side sessions for /portal/ customer login.
--                       Created in v1.3.0 lab; included here for fresh deploys.

-- ---------- operator_sessions ----------
CREATE TABLE IF NOT EXISTS operator_sessions (
    session_id   TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_active  INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    user_agent   TEXT,
    ip_address   TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_operator_sessions_expires ON operator_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_operator_sessions_username ON operator_sessions(username);

-- ---------- customer_portal_sessions ----------
CREATE TABLE IF NOT EXISTS customer_portal_sessions (
    session_id   TEXT PRIMARY KEY,
    customer_id  INTEGER NOT NULL,
    identity     TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_active  INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    user_agent   TEXT,
    ip_address   TEXT
);
CREATE INDEX IF NOT EXISTS idx_customer_portal_sessions_expires ON customer_portal_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_customer_portal_sessions_customer ON customer_portal_sessions(customer_id);