#!/opt/vpn-portal/.venv/bin/python3
"""v1.4.0 — Bulk customer action runner (MariaDB).

Phase 4E cleanup (v1.3.0): migrated from sqlite3 to MariaDB `radius` DB.
TKT-015 (v1.4.0): atomic RADIUS-audit-data purge on nuke.

Reads JSON action spec from stdin:
  {"action": "archive"|"unarchive"|"change_tier"|"delete",
   "ids": [int, ...],
   "tier_id": int (only for change_tier)}

Runs atomically: START TRANSACTION -> loop customers -> COMMIT.
On any error, ROLLBACK (the python exception causes sys.exit(1), but BEGIN was issued).

Caller (portal) interprets the JSON output to know what happened.

Output JSON:
  {"affected": [{"id": int, "name": str}, ...],
   "skipped":  [{"id": int, "name": str?, "reason": str}, ...],
   "devices_deleted": int,
   "eap_targets": [str, ...],
   "radcheck_purged": int,
   "radusergroup_purged": int,
   "radreply_purged": int,
   "radpostauth_purged": int,
   "radacct_purged": int}
or:
  {"error": "..."}
"""
import json
import os
import re
import sys
import time

import pymysql


def parse_db_url(url):
    """Parse mysql+pymysql://user:pass@host:port/db. Returns (host, port, user, password, db)."""
    m = re.match(
        r"mysql(?:\+pymysql)?://([^:]+):([^@]+)@([^:/]+):?(\d*)/([^/?#]+)",
        url,
    )
    if not m:
        raise ValueError(f"unparseable DB_URL: {url}")
    user, password, host, port, db = m.groups()
    return host, int(port) if port else 3306, user, password, db


def get_db_url():
    """Read DB_URL from env first, then fall back to /etc/vpn-portal.env."""
    url = os.environ.get("DB_URL")
    if url:
        return url
    env_path = "/etc/vpn-portal.env"
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("DB_URL="):
                return line.split("=", 1)[1]
    raise RuntimeError(f"DB_URL not found in env or {env_path}")


def main():
    # Parse + validate payload (graceful: KeyError-safe)
    payload = json.loads(sys.stdin.read())
    action = payload.get("action")
    ids = payload.get("ids") or []
    tier_id = payload.get("tier_id")

    if action not in ("archive", "unarchive", "change_tier", "delete"):
        print(json.dumps({"error": f"unknown action '{action}'"}))
        sys.exit(1)
    if not ids:
        print(json.dumps({"error": "ids is required"}))
        sys.exit(1)
    if not all(isinstance(i, int) for i in ids):
        print(json.dumps({"error": "ids must be a list of integers"}))
        sys.exit(1)
    if action == "change_tier" and tier_id is None:
        print(json.dumps({"error": "tier_id required for change_tier"}))
        sys.exit(1)

    db_url = get_db_url()
    host, port, user, password, database = parse_db_url(db_url)

    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        autocommit=False,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("START TRANSACTION")
            try:
                res = {
                    "affected": [],
                    "skipped": [],
                    "devices_deleted": 0,
                    "eap_targets": [],
                    "radcheck_purged": 0,
                    "radusergroup_purged": 0,
                    "radreply_purged": 0,
                    "radpostauth_purged": 0,
                    "radacct_purged": 0,
                }

                # Fetch all matching customers once
                ph = ",".join(["%s"] * len(ids))
                cur.execute(
                    f"SELECT id, name, is_operator, status, tier_id "
                    f"FROM customers WHERE id IN ({ph})",
                    ids,
                )
                rows = cur.fetchall()
                by_id = {r[0]: r for r in rows}

                ts = int(time.time())
                for cid in ids:
                    if cid not in by_id:
                        res["skipped"].append({"id": cid, "reason": "not found"})
                        continue
                    r = by_id[cid]
                    # r = (id, name, is_operator, status, tier_id)
                    if r[2] and action in ("delete", "change_tier"):
                        res["skipped"].append(
                            {"id": cid, "name": r[1], "reason": "is_operator"}
                        )
                        continue
                    if action == "archive" and r[3] == "archived":
                        res["skipped"].append(
                            {"id": cid, "name": r[1], "reason": "already_archived"}
                        )
                        continue
                    if action == "unarchive" and r[3] != "archived":
                        res["skipped"].append(
                            {"id": cid, "name": r[1], "reason": "not_archived"}
                        )
                        continue
                    if action == "change_tier" and r[4] == tier_id:
                        res["skipped"].append(
                            {"id": cid, "name": r[1], "reason": "already_on_tier"}
                        )
                        continue
                    if action == "archive":
                        cur.execute(
                            "UPDATE customers SET status='archived', is_active=0, "
                            "updated_at=%s WHERE id=%s",
                            (ts, cid),
                        )
                    elif action == "unarchive":
                        cur.execute(
                            "UPDATE customers SET status='active', is_active=1, "
                            "updated_at=%s WHERE id=%s",
                            (ts, cid),
                        )
                    elif action == "change_tier":
                        cur.execute(
                            "SELECT data_limit_bytes FROM tiers WHERE id=%s",
                            (tier_id,),
                        )
                        row = cur.fetchone()
                        if not row:
                            raise ValueError(f"tier_id={tier_id} not found")
                        tier_limit = row[0]
                        cur.execute(
                            "UPDATE customers SET tier_id=%s, data_limit_bytes=%s, "
                            "updated_at=%s WHERE id=%s",
                            (tier_id, tier_limit, ts, cid),
                        )
                    elif action == "delete":
                        cur.execute(
                            "SELECT id, device_name FROM devices WHERE customer_id=%s",
                            (cid,),
                        )
                        devs = cur.fetchall()
                        # Compute EAP identities for this customer (per-iteration, not global)
                        eap_ids = [f"{r[1]}-{d[1]}" for d in devs]
                        # Append to global for portal-side processing (rw-eap.conf etc.)
                        res["eap_targets"].extend(eap_ids)

                        # ----------------------------------------------------------
                        # TKT-015: Atomic RADIUS-audit-data purge (path C atomicity)
                        # All DELETEs run inside the existing START TRANSACTION.
                        # If any RADIUS row reference fails to delete (FK issue,
                        # permission error, etc.) the whole transaction ROLLS BACK —
                        # including the customer+devices+users deletes below.
                        # Result: nuke is atomic — either everything goes or nothing does.
                        # ----------------------------------------------------------
                        if eap_ids:
                            ph2 = ",".join(["%s"] * len(eap_ids))
                            # Order: stateful data first, attempt log last, sessions last.
                            # All FK-free; order is purely for hygiene + predictable logs.
                            cur.execute(
                                f"DELETE FROM radcheck WHERE UserName IN ({ph2})",
                                eap_ids,
                            )
                            res["radcheck_purged"] += cur.rowcount
                            cur.execute(
                                f"DELETE FROM radusergroup WHERE UserName IN ({ph2})",
                                eap_ids,
                            )
                            res["radusergroup_purged"] += cur.rowcount
                            cur.execute(
                                f"DELETE FROM radreply WHERE UserName IN ({ph2})",
                                eap_ids,
                            )
                            res["radreply_purged"] += cur.rowcount
                            cur.execute(
                                f"DELETE FROM radpostauth WHERE UserName IN ({ph2})",
                                eap_ids,
                            )
                            res["radpostauth_purged"] += cur.rowcount
                            cur.execute(
                                f"DELETE FROM radacct WHERE UserName IN ({ph2})",
                                eap_ids,
                            )
                            res["radacct_purged"] += cur.rowcount
                            # strongSwan attr-sql pool (the actual auth identities)
                            cur.execute(
                                f"DELETE FROM users WHERE name IN ({ph2})",
                                eap_ids,
                            )
                        # Cascade-delete the customer-scoped rows
                        cur.execute(
                            "DELETE FROM devices WHERE customer_id=%s", (cid,)
                        )
                        cur.execute(
                            "DELETE FROM alerts WHERE customer_id=%s", (cid,)
                        )
                        cur.execute(
                            "DELETE FROM purchases WHERE customer_id=%s", (cid,)
                        )
                        cur.execute(
                            "DELETE FROM audit_log WHERE target_type='customer' "
                            "AND target_id=%s",
                            (cid,),
                        )
                        cur.execute(
                            "DELETE FROM customers WHERE id=%s", (cid,)
                        )
                        res["devices_deleted"] += len(devs)
                    res["affected"].append({"id": cid, "name": r[1]})

                conn.commit()
                print(json.dumps(res))
            except Exception as ex:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(json.dumps({"error": str(ex)}))
                sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
