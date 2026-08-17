-- tests/fixtures/radius-schema.sql
-- ----------------------------------------------------------------------------
-- FreeRADIUS schema (subset). Matches what app.py writes to in create_client
-- and other customer-mutation paths. Without these tables, portal integration
-- tests that exercise customer create / radcheck writes fail with
-- "no such table: radcheck".
--
-- Live portal uses MariaDB with the full FreeRADIUS schema (~3.0.17 schema
-- from /etc/freeradius/3.0/mods-config/sql/main/mysql/schema.sql). This
-- fixture is the MINIMAL subset the portal code touches:
--   - radcheck      (per-user Cleartext-Password + NT-Password rows)
--   - radusergroup  (per-user group membership + priority)
--   - reply         (per-user reply attributes, for completeness)
--   - usergroup     (legacy alias — kept for backward-compat with old code)
--
-- Added: 2026-08-17 (fix for ci workflow Portal API integration tests
-- regression on commit b1d047f which synced LIVE app.py that uses a
-- direct cursor to write radcheck, bypassing portal_auth's patched _db()).
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS radcheck (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     VARCHAR(64) NOT NULL DEFAULT '',
    attribute    VARCHAR(64) NOT NULL DEFAULT '',
    op           CHAR(2)     NOT NULL DEFAULT '==',
    value        VARCHAR(253) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS radcheck_username ON radcheck (username);

CREATE TABLE IF NOT EXISTS radreply (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     VARCHAR(64) NOT NULL DEFAULT '',
    attribute    VARCHAR(64) NOT NULL DEFAULT '',
    op           CHAR(2)     NOT NULL DEFAULT '=',
    value        VARCHAR(253) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS radreply_username ON radreply (username);

CREATE TABLE IF NOT EXISTS radusergroup (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     VARCHAR(64) NOT NULL DEFAULT '',
    groupname    VARCHAR(64) NOT NULL DEFAULT '',
    priority     INTEGER     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS radusergroup_username ON radusergroup (username);

-- Legacy alias (some old paths still reference usergroup).
CREATE TABLE IF NOT EXISTS usergroup (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    UserName     VARCHAR(64) NOT NULL DEFAULT '',
    GroupName    VARCHAR(64) NOT NULL DEFAULT '',
    priority     INTEGER     NOT NULL DEFAULT 1
);
