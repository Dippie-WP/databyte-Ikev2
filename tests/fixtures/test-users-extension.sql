-- Synthetic users table for tests. The real table is created by charon's
-- attr-sql plugin at first run (see src/pool/sqlite.sql in upstream strongSwan).
-- Schema inferred from app.py usage: name TEXT PK, password BLOB (NTLM hash).
CREATE TABLE IF NOT EXISTS users (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    password BLOB
);
CREATE INDEX IF NOT EXISTS idx_users_name ON users(name);
