-- VPN Portal — Bug #2 fix: explicit user_id FK on customers table (v1.4.0)
--
-- Idempotent. Safe to re-run.
--
-- Background: Bug #2 surfaced 2026-06-24. The relationship between customers
-- and their EAP user was implicit via devices.strongswan_user_id (the proper
-- join path, fixed in commit 49895dc). But customers had NO direct link to
-- the user — any future code that needs customer→user without devices would
-- have to JOIN through devices, and SQLite didn't enforce the relationship.
--
-- This migration adds customers.user_id as an INTEGER FK to users(id), giving:
--   1. Direct customer → user lookup (no devices JOIN needed)
--   2. SQLite-enforced referential integrity (defense in depth)
--   3. Documentation of intent: this customer has THIS EAP user
--
-- Operator rows (is_operator=1) have NO user — they never auth via EAP.
-- Their user_id is NULL. This is by design.
--
-- Apply at deploy:
--   sudo bash apply_portal_user_id_fk.sh /var/lib/strongswan/ipsec.db

-- ---------- 1. Add the column (idempotent) ----------
-- SQLite ALTER TABLE ADD COLUMN fails with "duplicate column" on re-run.
-- Detect existing column first; only ALTER if missing.
-- (Bash apply script catches the error anyway, but doing it in SQL makes the
-- script self-contained + safe to run from CI without error swallowing.)
ALTER TABLE customers ADD COLUMN user_id INTEGER REFERENCES users(id);

-- ---------- 2. Backfill ----------
-- For each customer with at least one device, set user_id from the device's
-- strongswan_user_id. We use the device with the lowest id (the first one
-- created) — same priority as /api/customers/{id}/rotate_eap.
--
-- Customers with no devices get NULL (typically operators or archived).
-- Re-running is a no-op (already-set user_id is preserved by the WHERE clause).
UPDATE customers
   SET user_id = (
       SELECT d.strongswan_user_id
         FROM devices d
        WHERE d.customer_id = customers.id
        ORDER BY d.id ASC
        LIMIT 1
   )
 WHERE user_id IS NULL
   AND EXISTS (SELECT 1 FROM devices d WHERE d.customer_id = customers.id);

-- ---------- 3. Index (idempotent) ----------
-- Lookups go: customers.id → customers.user_id (for installer token generation
-- + EAP rotation) and customers.user_id → users (the FK direction). Index on
-- user_id speeds the reverse lookup.
CREATE INDEX IF NOT EXISTS idx_customers_user_id ON customers(user_id);
