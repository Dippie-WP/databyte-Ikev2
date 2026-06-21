#!/usr/bin/env python3
"""v1.2.13 — Bulk customer action runner.

Reads JSON action spec from stdin:
  {"action": "archive"|"unarchive"|"change_tier"|"delete",
   "ids": [int, ...],
   "tier_id": int (only for change_tier)}

Runs atomically: BEGIN IMMEDIATE -> loop customers -> COMMIT.
On any error, ROLLBACK (the python exception causes sys.exit(1), but BEGIN was issued).
Caller (portal) interprets the JSON output to know what happened.

Output JSON:
  {"affected": [{"id": int, "name": str}, ...],
   "skipped":  [{"id": int, "name": str?, "reason": str}, ...],
   "devices_deleted": int,
   "eap_targets": [str, ...]}
or:
  {"error": "..."}
"""
import json
import sqlite3
import sys
import time


def main():
    payload = json.loads(sys.stdin.read())
    action = payload["action"]
    ids = payload["ids"]
    tier_id = payload.get("tier_id")

    db = sqlite3.connect("/var/lib/strongswan/ipsec.db")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("BEGIN IMMEDIATE")
    try:
        res = {
            "affected": [],
            "skipped": [],
            "devices_deleted": 0,
            "eap_targets": [],
        }

        ph = ",".join(["?"] * len(ids))
        cur = db.execute(
            f"SELECT id, name, is_operator, status, tier_id FROM customers WHERE id IN ({ph})",
            ids,
        )
        rows = cur.fetchall()
        cur.close()
        by_id = {r[0]: r for r in rows}

        ts = int(time.time())
        for cid in ids:
            if cid not in by_id:
                res["skipped"].append({"id": cid, "reason": "not found"})
                continue
            r = by_id[cid]
            if r[2] and action in ("delete", "change_tier"):
                res["skipped"].append({"id": cid, "name": r[1], "reason": "is_operator"})
                continue
            if action == "archive" and r[3] == "archived":
                res["skipped"].append({"id": cid, "name": r[1], "reason": "already_archived"})
                continue
            if action == "unarchive" and r[3] != "archived":
                res["skipped"].append({"id": cid, "name": r[1], "reason": "not_archived"})
                continue
            if action == "change_tier" and r[4] == tier_id:
                res["skipped"].append({"id": cid, "name": r[1], "reason": "already_on_tier"})
                continue
            if action == "archive":
                db.execute(
                    "UPDATE customers SET status='archived', is_active=0, updated_at=? WHERE id=?",
                    (ts, cid),
                )
            elif action == "unarchive":
                db.execute(
                    "UPDATE customers SET status='active', is_active=1, updated_at=? WHERE id=?",
                    (ts, cid),
                )
            elif action == "change_tier":
                cur2 = db.execute("SELECT data_limit_bytes FROM tiers WHERE id=?", (tier_id,))
                tier_limit = cur2.fetchone()[0]
                cur2.close()
                db.execute(
                    "UPDATE customers SET tier_id=?, data_limit_bytes=?, updated_at=? WHERE id=?",
                    (tier_id, tier_limit, ts, cid),
                )
            elif action == "delete":
                cur2 = db.execute(
                    "SELECT id, device_name FROM devices WHERE customer_id=?", (cid,)
                )
                devs = cur2.fetchall()
                cur2.close()
                # Collect user identities (eap-{customer}-{device}) for both
                # rw-eap.conf (handled by portal) and the strongSwan users table.
                for d in devs:
                    res["eap_targets"].append(f"{r[1]}-{d[1]}")
                # Delete strongSwan attr-sql pool entries (users table) FIRST
                # so the customer delete doesn't leave orphan identities.
                if res["eap_targets"]:
                    ph2 = ",".join(["?"] * len(res["eap_targets"]))
                    db.execute(
                        f"DELETE FROM users WHERE name IN ({ph2})",
                        res["eap_targets"],
                    )
                db.execute("DELETE FROM devices WHERE customer_id=?", (cid,))
                db.execute("DELETE FROM alerts WHERE customer_id=?", (cid,))
                db.execute("DELETE FROM purchases WHERE customer_id=?", (cid,))
                db.execute(
                    "DELETE FROM audit_log WHERE target_type='customer' AND target_id=?",
                    (cid,),
                )
                db.execute("DELETE FROM customers WHERE id=?", (cid,))
                res["devices_deleted"] += len(devs)
            res["affected"].append({"id": cid, "name": r[1]})

        db.commit()
        print(json.dumps(res))
    except Exception as ex:
        try:
            db.rollback()
        except Exception:
            pass
        print(json.dumps({"error": str(ex)}))
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
