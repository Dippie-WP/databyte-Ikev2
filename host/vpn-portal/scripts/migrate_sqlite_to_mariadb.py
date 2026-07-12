#!/usr/bin/env python3
"""Phase 4E migration script — SQLite → MariaDB portal-side tables.

Run ONCE on vps-01 as root. Idempotent — truncates target tables first.

Pre-flight: backup must exist at
    /tmp/pre_4E_2026-07-12/{pre_4E.sqlite3.sql,pre_4E.mariadb.sql}

Post-flight: prints row-count diff for each table.
"""

import sqlite3
import pymysql
import sys
import json
from datetime import datetime

SQLITE_PATH = "/var/lib/strongswan/ipsec.db"
MARIADB = dict(host="127.0.0.1", port=3306, user="root", database="radius", charset="utf8mb4", unix_socket="/run/mysqld/mysqld.sock")

# Portal-side tables in dependency order (parents before children).
TABLES = [
    "tiers",
    "users",
    "customers",
    "devices",
    "customer_portal_sessions",
    "operator_sessions",
    "installer_tokens",
    "audit_log",
    "alerts",
    "purchases",
]

# Mapping SQLite CREATE TABLE → MariaDB CREATE TABLE.
# All columns explicitly mapped. Booleans stay TINYINT(1). Timestamps stay INT.
# BLOB columns: password in users.
SCHEMA = {
    "tiers": """
        CREATE TABLE IF NOT EXISTS tiers (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(64) NOT NULL UNIQUE,
            display_name VARCHAR(128) NOT NULL,
            data_limit_bytes BIGINT NOT NULL,
            price_zar INT DEFAULT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at INT NOT NULL,
            notes TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "users": """
        CREATE TABLE IF NOT EXISTS users (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) NOT NULL UNIQUE,
            password VARBINARY(255) DEFAULT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "customers": """
        CREATE TABLE IF NOT EXISTS customers (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(128) NOT NULL UNIQUE,
            display_name VARCHAR(128) DEFAULT NULL,
            telegram_id BIGINT DEFAULT NULL,
            telegram_username VARCHAR(64) DEFAULT NULL,
            is_operator TINYINT(1) NOT NULL DEFAULT 0,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            over_quota TINYINT(1) NOT NULL DEFAULT 0,
            data_limit_bytes BIGINT NOT NULL DEFAULT 0,
            data_used_bytes BIGINT NOT NULL DEFAULT 0,
            tier_id INT DEFAULT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            max_devices INT NOT NULL DEFAULT 1,
            bandwidth_down_mbps INT NOT NULL DEFAULT 20,
            bandwidth_up_mbps INT NOT NULL DEFAULT 20,
            created_at INT NOT NULL,
            updated_at INT NOT NULL,
            notes TEXT,
            billing_id VARCHAR(64) DEFAULT NULL,
            email VARCHAR(128) DEFAULT NULL,
            eap_rotated_at INT DEFAULT NULL,
            user_id INT DEFAULT NULL,
            KEY idx_customers_telegram_id (telegram_id),
            KEY idx_customers_tier_id (tier_id),
            KEY idx_customers_over_quota (over_quota),
            KEY idx_customers_user_id (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "devices": """
        CREATE TABLE IF NOT EXISTS devices (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            customer_id INT NOT NULL,
            strongswan_user_id INT NOT NULL UNIQUE,
            device_name VARCHAR(128) NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            last_seen_v4 VARCHAR(45) DEFAULT NULL,
            last_seen_at INT DEFAULT NULL,
            created_at INT NOT NULL,
            updated_at INT NOT NULL,
            notes TEXT,
            device_type VARCHAR(32) DEFAULT NULL,
            os_version VARCHAR(64) DEFAULT NULL,
            hostname VARCHAR(128) DEFAULT NULL,
            KEY idx_devices_customer_id (customer_id),
            KEY idx_devices_last_seen_at (last_seen_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "customer_portal_sessions": """
        CREATE TABLE IF NOT EXISTS customer_portal_sessions (
            session_id VARCHAR(64) NOT NULL PRIMARY KEY,
            customer_id INT NOT NULL,
            identity VARCHAR(255) NOT NULL,
            created_at INT NOT NULL,
            last_active INT NOT NULL,
            expires_at INT NOT NULL,
            user_agent TEXT,
            ip_address VARCHAR(64),
            KEY idx_customer_portal_sessions_expires (expires_at),
            KEY idx_customer_portal_sessions_customer (customer_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "operator_sessions": """
        CREATE TABLE IF NOT EXISTS operator_sessions (
            session_id VARCHAR(64) NOT NULL PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            created_at INT NOT NULL,
            last_active INT NOT NULL,
            expires_at INT NOT NULL,
            user_agent TEXT,
            ip_address VARCHAR(64),
            revoked TINYINT(1) NOT NULL DEFAULT 0,
            KEY idx_operator_sessions_expires (expires_at),
            KEY idx_operator_sessions_username (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "installer_tokens": """
        CREATE TABLE IF NOT EXISTS installer_tokens (
            token VARCHAR(64) NOT NULL PRIMARY KEY,
            customer_id INT NOT NULL,
            device_id INT DEFAULT NULL,
            created_at INT NOT NULL,
            expires_at INT NOT NULL,
            consumed_at INT DEFAULT NULL,
            consumed_ip VARCHAR(64) DEFAULT NULL,
            created_by VARCHAR(255) DEFAULT NULL,
            KEY idx_installer_tokens_customer (customer_id),
            KEY idx_installer_tokens_expires (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "audit_log": """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            actor VARCHAR(64) NOT NULL,
            action VARCHAR(128) NOT NULL,
            target_type VARCHAR(64) DEFAULT NULL,
            target_id INT DEFAULT NULL,
            payload TEXT,
            created_at INT NOT NULL,
            KEY idx_audit_log_target (target_type, target_id),
            KEY idx_audit_log_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "alerts": """
        CREATE TABLE IF NOT EXISTS alerts (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            customer_id INT NOT NULL,
            threshold INT NOT NULL,
            sent_at INT NOT NULL,
            telegram_msg_id_zun INT DEFAULT NULL,
            telegram_msg_id_customer INT DEFAULT NULL,
            customer_notified TINYINT(1) NOT NULL DEFAULT 0,
            data_used_bytes_at_alert BIGINT NOT NULL,
            KEY idx_alerts_customer_id (customer_id),
            KEY idx_alerts_customer_threshold (customer_id, threshold)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    "purchases": """
        CREATE TABLE IF NOT EXISTS purchases (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            customer_id INT NOT NULL,
            tier_id INT DEFAULT NULL,
            data_added_bytes BIGINT NOT NULL,
            data_used_before BIGINT NOT NULL,
            data_used_reset TINYINT(1) NOT NULL DEFAULT 1,
            created_at INT NOT NULL,
            notes TEXT,
            KEY idx_purchases_customer_id (customer_id),
            KEY idx_purchases_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
}


def sqlite_cols(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def migrate_table(sqlite_conn, maria_conn, table):
    cols = sqlite_cols(sqlite_conn, table)
    if not cols:
        return 0
    cur = sqlite_conn.execute(f"SELECT {', '.join(cols)} FROM {table}")
    rows = cur.fetchall()
    if not rows:
        return 0
    col_names = ", ".join(f"`{c}`" for c in cols)
    cur = maria_conn.cursor()
    for row in rows:
        values = []
        for v in row:
            if v is None:
                values.append("NULL")
            elif isinstance(v, (int, float)):
                values.append(str(v))
            elif isinstance(v, bytes):
                values.append("0x" + v.hex())
            elif isinstance(v, str):
                # Escape single quotes and backslashes for inline SQL literal.
                escaped = v.replace("\\", "\\\\").replace("'", "\\'")
                values.append(f"'{escaped}'")
            else:
                raise ValueError(f"unhandled type {type(v)} in {table}")
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({', '.join(values)})"
        cur.execute(sql)
    maria_conn.commit()
    return len(rows)


def main():
    print(f"[{datetime.utcnow().isoformat()}Z] Phase 4E migration START")
    print("=" * 60)
    # 1. Drop + recreate portal-side tables in MariaDB
    print("Step 1: Drop+recreate MariaDB portal-side tables...")
    maria = pymysql.connect(**MARIADB)
    mc = maria.cursor()
    for t in TABLES:
        mc.execute(f"DROP TABLE IF EXISTS {t}")
        mc.execute(SCHEMA[t])
    maria.commit()
    print(f"  → {len(TABLES)} tables recreated")
    # 2. Migrate data
    print("Step 2: Migrate data SQLite → MariaDB...")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    summary = {}
    for t in TABLES:
        n = migrate_table(sqlite_conn, maria, t)
        # verify
        mc.execute(f"SELECT COUNT(*) FROM {t}")
        cnt = mc.fetchone()[0]
        sc = sqlite_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        match = "✓" if cnt == sc else "✗"
        print(f"  {match} {t:30s} sqlite={sc} mariadb={cnt} migrated={n}")
        summary[t] = (sc, cnt, sc == cnt)
    maria.close()
    sqlite_conn.close()
    all_ok = all(v[2] for v in summary.values())
    print("=" * 60)
    print(f"[{datetime.utcnow().isoformat()}Z] Phase 4E migration {'SUCCESS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()