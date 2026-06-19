#!/usr/bin/env bash
# seed_5B1.sh — Seed the operator account + demo account for Phase 5B.
#
# Idempotent. Re-running is a no-op (uses INSERT OR IGNORE + ON CONFLICT).
# Run on the LXC HOST (not inside the container).
#
# What this seeds:
#
#   TIERS:
#     - demo_100mb (104,857,600 bytes = 100 MiB) — for client demos
#
#   STRONGSWAN USERS (the upstream `users` table):
#     - demo-phone  (password=NULL) — first demo device
#     - demo-laptop (password=NULL) — second demo device
#     Note: zun/zun-iphone/zun-windows already exist from 5A tests and are NOT
#     recreated here. zun-android does NOT exist yet (deferred from 5A.3).
#
#   CUSTOMERS (the new `customers` table):
#     - zun-operator   (is_operator=1, no tier, unlimited bypass)
#     - demo-customer  (tier=demo_100mb, 100 MB allowance, 0 used)
#
#   DEVICES (the new `devices` table):
#     - zun-operator -> zun, zun-iphone, zun-windows (links to existing users)
#     - demo-customer -> demo-phone, demo-laptop (links to new users)
#
# After this script:
#   * Zun's 3 real devices are bound to zun-operator (operator bypass)
#   * demo-customer has 2 placeholder devices connectable once creds are set
#   * For 5B.5, you'll generate NTLM hashes for demo-phone/demo-laptop via
#     the admin web page (5C.3) OR manually with the seed-db.sh pattern.

set -euo pipefail

DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
TS=$(date -u +%s)

if [ ! -s "$DB_PATH" ]; then
    echo "ERROR: $DB_PATH missing or empty" >&2
    exit 1
fi

# Compute byte values
MB100=$((100 * 1024 * 1024))  # 104857600

echo "=== Seed 5B.1 (operator + demo) ==="
echo "  DB:   $DB_PATH"
echo "  Time: $(date -u +%FT%TZ)"

# We need strongSwan user IDs to populate devices.strongswan_user_id.
# Strategy: insert users (or look them up), capture their IDs, then insert devices.

# Step 1: insert new strongSwan users (demo-phone, demo-laptop) with NULL password.
#         The existing zun/zun-iphone/zun-windows are NOT touched.
# Step 2: insert demo_100mb tier.
# Step 3: insert zun-operator and demo-customer customers.
# Step 4: insert devices linking customers to strongSwan users.

sqlite3 "$DB_PATH" <<SQL
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

-- ============================================================================
-- Step 1: strongSwan `users` rows for demo devices
-- ============================================================================
INSERT OR IGNORE INTO users (name, password) VALUES
    ('demo-phone',  NULL),
    ('demo-laptop', NULL);

-- ============================================================================
-- Step 2: demo tier
-- ============================================================================
INSERT OR IGNORE INTO tiers (name, display_name, data_limit_bytes, price_zar, is_active, created_at, notes)
VALUES ('demo_100mb', 'Demo 100 MB', $MB100, NULL, 1, $TS,
        'Demo tier — 100 MiB. Persistent (for client demos). Zun resets data_used_bytes manually via reset_demo.sh.');

-- ============================================================================
-- Step 3: customers
-- ============================================================================
-- zun-operator: bypasses all quota checks. No tier.
INSERT OR IGNORE INTO customers (name, display_name, telegram_id, telegram_username, is_operator, is_active, over_quota, data_limit_bytes, data_used_bytes, tier_id, status, created_at, updated_at, notes)
VALUES ('zun-operator', 'Zun (operator)', 7748884597, 'zuzu172', 1, 1, 0, 0, 0, NULL, 'active', $TS, $TS,
        'Operator account. Bypasses ALL quota checks. Devices: zun, zun-iphone, zun-windows (5A-tested).');

-- demo-customer: on demo_100mb tier, 0 used, 100 MB limit
INSERT OR IGNORE INTO customers (name, display_name, telegram_id, telegram_username, is_operator, is_active, over_quota, data_limit_bytes, data_used_bytes, tier_id, status, created_at, updated_at, notes)
VALUES ('demo-customer', 'Demo Customer', NULL, NULL, 0, 1, 0, $MB100, 0,
        (SELECT id FROM tiers WHERE name='demo_100mb'),
        'active', $TS, $TS,
        'Persistent demo account. Reset data_used_bytes via reset_demo.sh after each client demo.');

-- ============================================================================
-- Step 4: devices (link strongSwan users to customers)
-- ============================================================================
-- zun's existing 3 devices -> zun-operator
INSERT OR IGNORE INTO devices (customer_id, strongswan_user_id, device_name, is_active, created_at, updated_at, notes)
SELECT
    (SELECT id FROM customers WHERE name='zun-operator'),
    u.id, u.name, 1, $TS, $TS,
    'Existing 5A-tested device, linked to operator account'
FROM users u
WHERE u.name IN ('zun','zun-iphone','zun-windows');

-- demo devices -> demo-customer
INSERT OR IGNORE INTO devices (customer_id, strongswan_user_id, device_name, is_active, created_at, updated_at, notes)
SELECT
    (SELECT id FROM customers WHERE name='demo-customer'),
    u.id, u.name, 1, $TS, $TS,
    'Demo device (placeholder — needs NTLM hash via admin page in 5C.3)'
FROM users u
WHERE u.name IN ('demo-phone','demo-laptop');

-- ============================================================================
-- Audit log
-- ============================================================================
INSERT INTO audit_log (actor, action, target_type, payload, created_at)
VALUES ('system', 'seed_5B1', 'system',
        json_object('tiers_added', 'demo_100mb',
                    'customers_added', 'zun-operator,demo-customer',
                    'strongswan_users_added', 'demo-phone,demo-laptop',
                    'devices_linked', '5 (zun/zun-iphone/zun-windows/demo-phone/demo-laptop)',
                    'ts', $TS),
        $TS);

COMMIT;

-- ============================================================================
-- Verification view
-- ============================================================================
SELECT '--- Tiers ---' AS section;
SELECT id, name, display_name, data_limit_bytes, is_active FROM tiers ORDER BY id;

SELECT '--- Customers ---' AS section;
SELECT id, name, is_operator, is_active, over_quota, data_limit_bytes, data_used_bytes, tier_id FROM customers ORDER BY id;

SELECT '--- Devices ---' AS section;
SELECT d.id, c.name AS customer, u.name AS strongswan_user, d.device_name, d.is_active
FROM devices d
JOIN customers c ON c.id = d.customer_id
JOIN users u ON u.id = d.strongswan_user_id
ORDER BY d.id;

SELECT '--- StrongSwan users (full list) ---' AS section;
SELECT id, name, length(password) AS pw_len FROM users ORDER BY id;
SQL

echo "  Status: OK"
