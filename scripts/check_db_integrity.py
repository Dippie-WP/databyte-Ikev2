#!/usr/bin/env python3
"""
check_db_integrity.py — VPN portal DB integrity check (Layer 2 of testing plan)

Runs a battery of sanity checks against the canonical auth DB (the strongSwan
attr-sql SQLite that backs both charon auth + portal). Catches silent drift
that production users would hit but tests can't simulate.

Checks
------
1. users-orphaned     Every row in `users` has a matching row in `devices`
                      (strongswan_user_id). No orphan EAP credentials.
2. customers-orphaned Every active (is_operator=0, is_active=1) row in
                      `customers` has ≥1 row in `devices`. No customer
                      that can log in via portal but has no EAP identity.
3. tokens-stale       Every row in `installer_tokens` is either:
                        - consumed_at IS NOT NULL (already used)
                        - expires_at > now (still valid)
                      No expired+unused tokens lingering in the table.
4. eap-conf-orphan    Every `eap-<name>` block in rw-eap.conf has a matching
                      `users.name` in DB. (Optional — skipped if --eap-conf
                      not provided.)
5. eap-conf-missing   Every active customer with devices has a matching
                      `eap-<device.device_name>` block in rw-eap.conf.
                      (Optional — same.)

Usage
-----
    # Live check on VPS (canonical)
    sudo python3 scripts/check_db_integrity.py

    # Live check with custom paths
    sudo python3 scripts/check_db_integrity.py \\
        --db /var/lib/strongswan/ipsec.db \\
        --eap-conf /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf

    # CI / test (no rw-eap.conf)
    python3 scripts/check_db_integrity.py --db tests/fixtures/test.db

    # JSON output for tracking
    python3 scripts/check_db_integrity.py --json

Exit codes
----------
0   All checks passed (or only INFO-level findings)
1   One or more checks failed (drift detected — fix before deploy)
2   Setup error (DB not found, schema missing, etc.)
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# Default paths on VPS prod. Override via --db / --eap-conf for testing.
DEFAULT_DB = "/var/lib/strongswan/ipsec.db"
DEFAULT_EAP_CONF = "/opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf"

# Token expiry: 7 days (matches installer_tokens.py TOKEN_TTL_SECONDS).
TOKEN_TTL_SECONDS = 7 * 24 * 3600


def log(msg, *, json_mode=False, level="INFO"):
    """Emit a single log line (or JSON record)."""
    if json_mode:
        return  # JSON mode is end-of-run report only
    print(f"[{level}] {msg}", file=sys.stderr)


def _skip_if_table_missing(db, check_name, description, exc):
    """Return a 'skipped' result if the OperationalError was a missing table."""
    if "no such table" in str(exc):
        return {
            "check": check_name,
            "description": description,
            "ok": True,
            "count": 0,
            "findings": [],
            "skipped": True,
            "skip_reason": f"required table not in DB ({exc})",
        }
    raise


def check_users_orphaned(db, json_mode):
    """Every users row has a matching devices row."""
    try:
        rows = db.execute("""
            SELECT u.id, u.name
            FROM users u
            LEFT JOIN devices d ON d.strongswan_user_id = u.id
            WHERE d.id IS NULL
            ORDER BY u.id
        """).fetchall()
    except sqlite3.OperationalError as e:
        return _skip_if_table_missing(db, "users-orphaned",
                                       "users rows with no matching devices.strongswan_user_id", e)
    findings = [{"id": r[0], "name": r[1]} for r in rows]
    return {
        "check": "users-orphaned",
        "description": "users rows with no matching devices.strongswan_user_id",
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


def check_customers_orphaned(db, json_mode):
    """Every active customer has ≥1 device."""
    try:
        rows = db.execute("""
            SELECT c.id, c.name
            FROM customers c
            LEFT JOIN devices d ON d.customer_id = c.id
            WHERE d.id IS NULL
              AND c.is_operator = 0
              AND c.is_active = 1
            ORDER BY c.id
        """).fetchall()
    except sqlite3.OperationalError as e:
        return _skip_if_table_missing(db, "customers-orphaned",
                                       "active (non-operator) customers with no devices row", e)
    findings = [{"id": r[0], "name": r[1]} for r in rows]
    return {
        "check": "customers-orphaned",
        "description": "active (non-operator) customers with no devices row",
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


def check_tokens_stale(db, json_mode):
    """No expired+unused installer tokens."""
    now = int(time.time())
    try:
        rows = db.execute("""
            SELECT token, customer_id, created_at, expires_at, consumed_at
            FROM installer_tokens
            WHERE consumed_at IS NULL
              AND expires_at < ?
            ORDER BY expires_at
        """, (now,)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {
                "check": "tokens-stale",
                "description": "expired but never-consumed installer tokens",
                "ok": True,
                "count": 0,
                "findings": [],
                "skipped": True,
                "skip_reason": "installer_tokens table not in DB (portal not yet initialized)",
            }
        raise
    findings = [
        {
            "token": r[0][:8] + "...",  # truncate for safety
            "customer_id": r[1],
            "created_at": r[2],
            "expires_at": r[3],
            "age_seconds": now - r[3],
        }
        for r in rows
    ]
    return {
        "check": "tokens-stale",
        "description": "expired but never-consumed installer tokens",
        "ok": len(findings) == 0,
        "count": len(findings),
        "findings": findings,
    }


def parse_eap_blocks(eap_conf_path):
    """Parse rw-eap.conf and return set of eap-<name> identities found.

    The strongSwan rw-eap.conf format is:
        eap-<name> {
            secret = "..."
        }
    We extract the <name> from each block header.
    """
    text = Path(eap_conf_path).read_text()
    # Match "eap-<name> {" on a line by itself
    return set(re.findall(r"^eap-([\w.-]+)\s*\{", text, re.MULTILINE))


def check_eap_conf_orphan(db, eap_names, json_mode):
    """Every eap-<name> block in rw-eap.conf has a matching users.name."""
    if not eap_names:
        return {
            "check": "eap-conf-orphan",
            "description": "rw-eap.conf blocks with no matching users.name (no blocks found)",
            "ok": True,
            "count": 0,
            "findings": [],
            "skipped": True,
        }
    placeholders = ",".join("?" for _ in eap_names)
    rows = db.execute(
        f"SELECT name FROM users WHERE name IN ({placeholders})",
        tuple(eap_names),
    ).fetchall()
    found = {r[0] for r in rows}
    missing = sorted(eap_names - found)
    return {
        "check": "eap-conf-orphan",
        "description": "rw-eap.conf blocks with no matching users.name",
        "ok": len(missing) == 0,
        "count": len(missing),
        "findings": [{"name": n} for n in missing],
    }


def check_eap_conf_missing(db, eap_names, json_mode):
    """Every active customer's devices have a matching eap-<name> block."""
    try:
        rows = db.execute("""
            SELECT c.id AS cust_id, c.name AS cust_name,
                   d.id AS dev_id, d.device_name, u.name AS user_name
            FROM customers c
            JOIN devices d ON d.customer_id = c.id
            JOIN users u ON u.id = d.strongswan_user_id
            WHERE c.is_operator = 0
              AND c.is_active = 1
              AND d.is_active = 1
            ORDER BY c.id, d.id
        """).fetchall()
    except sqlite3.OperationalError as e:
        return _skip_if_table_missing(db, "eap-conf-missing",
                                       "active customers whose EAP user has no matching rw-eap.conf block", e)
    missing = []
    for cust_id, cust_name, dev_id, device_name, user_name in rows:
        if user_name not in eap_names:
            missing.append({
                "customer_id": cust_id,
                "customer_name": cust_name,
                "device_id": dev_id,
                "device_name": device_name,
                "user_name": user_name,
            })
    return {
        "check": "eap-conf-missing",
        "description": "active customers whose EAP user has no matching rw-eap.conf block",
        "ok": len(missing) == 0,
        "count": len(missing),
        "findings": missing,
    }


def main():
    parser = argparse.ArgumentParser(
        description="VPN portal DB integrity check (Layer 2)"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"Path to auth DB (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--eap-conf", default=None,
        help=f"Path to rw-eap.conf (default: {DEFAULT_EAP_CONF}, skip eap checks if not found)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output machine-readable JSON instead of human report",
    )
    args = parser.parse_args()

    # --- Setup ---
    db_path = Path(args.db)
    if not db_path.exists():
        if args.json:
            print(json.dumps({"error": f"DB not found: {db_path}"}))
        else:
            log(f"DB not found: {db_path}", level="ERROR")
        return 2

    eap_path = None
    if args.eap_conf:
        eap_path = Path(args.eap_conf)
        if not eap_path.exists():
            log(f"rw-eap.conf not found at {eap_path} — eap-related checks will be skipped", level="WARN")
            eap_path = None
    elif Path(DEFAULT_EAP_CONF).exists():
        # Default path exists, use it
        eap_path = Path(DEFAULT_EAP_CONF)
    # else: eap_path stays None, eap checks will be skipped

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        if args.json:
            print(json.dumps({"error": f"SQLite open failed: {e}"}))
        else:
            log(f"SQLite open failed: {e}", level="ERROR")
        return 2

    # --- Run checks ---
    results = []
    results.append(check_users_orphaned(conn, args.json))
    results.append(check_customers_orphaned(conn, args.json))
    results.append(check_tokens_stale(conn, args.json))

    if eap_path is not None:
        try:
            eap_names = parse_eap_blocks(eap_path)
            results.append(check_eap_conf_orphan(conn, eap_names, args.json))
            results.append(check_eap_conf_missing(conn, eap_names, args.json))
        except (OSError, ValueError) as e:
            log(f"rw-eap.conf parse failed: {e}", level="ERROR")
            return 2
    else:
        results.append({
            "check": "eap-conf-orphan",
            "description": "skipped (no rw-eap.conf)",
            "ok": True,
            "count": 0,
            "findings": [],
            "skipped": True,
        })
        results.append({
            "check": "eap-conf-missing",
            "description": "skipped (no rw-eap.conf)",
            "ok": True,
            "count": 0,
            "findings": [],
            "skipped": True,
        })

    conn.close()

    # --- Report ---
    failed = [r for r in results if not r["ok"]]
    summary = {
        "db_path": str(db_path),
        "eap_conf_path": str(eap_path) if eap_path else None,
        "checks_total": len(results),
        "checks_failed": len(failed),
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("=" * 70)
        print(f"DB integrity check: {db_path}")
        if eap_path:
            print(f"rw-eap.conf:        {eap_path}")
        else:
            print(f"rw-eap.conf:        (not provided — eap checks skipped)")
        print("=" * 70)
        for r in results:
            status = "✅ OK " if r["ok"] else "❌ FAIL"
            skipped = "  [SKIPPED]" if r.get("skipped") else ""
            print(f"  {status}  {r['check']:20s}  {r['description']}{skipped}")
            if r["count"] > 0:
                # Show first 5 findings, then "... and N more"
                shown = r["findings"][:5]
                for f in shown:
                    print(f"           - {f}")
                if len(r["findings"]) > 5:
                    print(f"           ... and {len(r['findings']) - 5} more")
        print("=" * 70)
        if failed:
            print(f"❌ {len(failed)} check(s) FAILED. Fix drift before deploy.")
        else:
            print(f"✅ All {len(results)} checks passed.")
        print()

    # --- Exit code ---
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())