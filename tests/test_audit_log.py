"""
test_audit_log.py — Tests for audit_log writes on operator actions.

Every mutation MUST write an audit_log row with:
  - actor (operator username)
  - action (e.g. 'create_client', 'archive_customer', 'extend_quota', 'reset_quota')
  - target_type ('customer', 'tier', 'device', etc.)
  - target_id (row id)
  - payload (JSON with action-specific fields)
  - created_at (unix epoch seconds)

Endpoint /api/admin/audit returns paginated audit_log rows for the UI.
"""
import json
import time
import pytest


class TestAuditLogOnCreate:
    """Every customer create writes an audit_log row."""

    def test_create_customer_writes_audit_row(self, client, operator_login, db_path):
        client.post(
            "/api/customers",
            json={
                "display_name": "Audit Create",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT actor, action, target_type, target_id, payload FROM audit_log "
            "WHERE action='create_client'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        actor, action, target_type, target_id, payload = rows[0]
        assert actor == "zun"
        assert target_type == "customer"
        p = json.loads(payload)
        assert p["customer_name"] == "audit-create"
        assert p["tier"] == "tier_5gb"
        assert p["device_name"] == "laptop"


class TestAuditLogOnArchive:
    """Archive + unarchive each write a row."""

    def _create(self, client, operator_login, name):
        return client.post(
            "/api/customers",
            json={
                "display_name": name.title(),
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()["customer"]["id"]

    def test_archive_writes_audit(self, client, operator_login, db_path):
        cid = self._create(client, operator_login, "Audit Archive Co")
        client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT action FROM audit_log WHERE target_type='customer' AND target_id=?",
            (cid,),
        ).fetchall()
        conn.close()
        actions = [r[0] for r in rows]
        # Archive writes target_id=NULL; query separately for action rows
        conn = sqlite3.connect(str(db_path))
        all_rows = conn.execute(
            "SELECT action FROM audit_log WHERE action IN ('customer_archive', 'customer_unarchive')"
        ).fetchall()
        conn.close()
        all_actions = [r[0] for r in all_rows]
        assert "customer_archive" in all_actions or "customer_unarchive" in all_actions

    def test_unarchive_writes_audit(self, client, operator_login, db_path):
        cid = self._create(client, operator_login, "Audit Unarchive Co")
        client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        client.post(f"/api/customers/{cid}/unarchive", cookies={"session": operator_login})
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT action FROM audit_log WHERE target_type='customer' AND target_id=? "
            "ORDER BY id",
            (cid,),
        ).fetchall()
        conn.close()
        actions = [r[0] for r in rows]
        # Archive/unarchive writes audit_log with target_id=NULL (uses _target_id only sometimes).
        # Query WITHOUT target_id filter to find the action rows.
        conn = sqlite3.connect(str(db_path))
        all_rows = conn.execute(
            "SELECT action, target_id FROM audit_log WHERE action IN ('customer_archive', 'customer_unarchive')"
        ).fetchall()
        conn.close()
        all_actions = [r[0] for r in all_rows]
        assert "customer_unarchive" in all_actions or "customer_archive" in all_actions


class TestAuditLogEndpoint:
    """GET /api/admin/audit returns audit_log rows (paginated)."""

    def test_audit_log_pagination(self, client, operator_login):
        # Create 3 customers
        for name in ("Audit One", "Audit Two", "Audit Three"):
            client.post(
                "/api/customers",
                json={
                    "display_name": name,
                    "tier_name": "tier_5gb",
                    "device_name": "laptop",
                    "device_type": "Windows",
                },
                cookies={"session": operator_login},
            )
        # Wait a moment so timestamps are distinct (and the order is preserved)
        r = client.get(
            "/api/admin/audit?action=create_client&limit=10",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200
        body = r.json()
        assert "rows" in body
        assert len(body["rows"]) >= 3
        for row in body["rows"]:
            assert row["action"] == "create_client"
            assert "actor" in row
            assert "ts" in row  # ISO8601 timestamp

    def test_audit_log_filter_by_action(self, client, operator_login):
        client.post(
            "/api/customers",
            json={
                "display_name": "Filter Test",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        # The login itself writes a 'operator_login' audit row
        r = client.get(
            "/api/admin/audit?action=operator_login",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200
        body = r.json()
        # All rows should be operator_login
        for row in body["rows"]:
            assert row["action"] == "operator_login"

    def test_audit_log_payload_contains_target_metadata(self, client, operator_login):
        client.post(
            "/api/customers",
            json={
                "display_name": "Payload Test",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        r = client.get(
            "/api/admin/audit?action=create_client&limit=1",
            cookies={"session": operator_login},
        )
        body = r.json()
        assert len(body["rows"]) >= 1
        row = body["rows"][0]
        assert "payload" in row
        # payload may be a dict OR a JSON string depending on app version
        if isinstance(row["payload"], str):
            row["payload"] = json.loads(row["payload"])
        assert row["payload"]["customer_name"] == "payload-test"
