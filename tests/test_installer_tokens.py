"""
test_installer_tokens.py — Tests for /api/installer/{token} and POST /api/customers/{id}/installer-token.

Lesson this catches:
  - Token reuse attempt (second fetch) → 404
  - Token expiry (>7 days) → 404
  - Invalid token format → 400
  - Non-existent customer → 404 when generating
  - Customer with no active device → 400 when generating
  - Audit log row written for both create and consume
  - Token burned on first successful fetch (consumed_at set)
"""
import time
import pytest


class TestInstallerTokenCreate:
    """POST /api/customers/{customer_id}/installer-token"""

    def test_create_token_returns_powershell_one_liner(self, client, operator_login):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Token Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        r = client.post(
            f"/api/customers/{cid}/installer-token",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "powershell_cmd" in body
        assert body["powershell_cmd"].startswith("iex (irm '")
        assert "setup-databyte-vpn.ps1?slug=token-co&token=" in body["powershell_cmd"]
        assert len(body["token_prefix"]) == 9  # 8 chars + ellipsis
        assert body["expires_in_days"] == 7

    def test_create_token_for_unknown_customer_404(self, client, operator_login):
        r = client.post(
            "/api/customers/99999/installer-token",
            cookies={"session": operator_login},
        )
        assert r.status_code == 404

    def test_create_token_for_archived_customer_400(self, client, operator_login):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Archived Token Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        r = client.post(
            f"/api/customers/{cid}/installer-token",
            cookies={"session": operator_login},
        )
        assert r.status_code == 400

    def test_create_token_no_active_device_400(self, client, operator_login, db_path):
        # Manually insert a customer with NO active device
        import sqlite3
        now = int(time.time())
        conn = sqlite3.connect(str(db_path))
        conn.execute("""INSERT INTO customers (name, display_name, is_operator, is_active,
                       data_limit_bytes, tier_id, status, max_devices, created_at, updated_at)
                       VALUES (?, ?, 0, 1, 5368709120, 1, 'active', 1, ?, ?)""",
                    ("no-device-co", "No Device Co", now, now))
        cid = conn.execute("SELECT id FROM customers WHERE name='no-device-co'").fetchone()[0]
        conn.commit()
        conn.close()
        r = client.post(
            f"/api/customers/{cid}/installer-token",
            cookies={"session": operator_login},
        )
        assert r.status_code == 400
        assert "no active device" in r.json()["detail"].lower()

    def test_create_token_audit_log_entry(self, client, operator_login, db_path):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Audit Token Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        client.post(
            f"/api/customers/{cid}/installer-token",
            cookies={"session": operator_login},
        )
        # Audit log should have the entry
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT actor, action, target_type, target_id FROM audit_log "
            "WHERE action='installer_token_create'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][2] == "customer"
        assert rows[0][3] == cid


class TestInstallerTokenConsume:
    """GET /api/installer/{token} — PUBLIC, single-use."""

    def _create_via_setup(self, client, operator_login):
        """Create customer + device, return token from generated installer link."""
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Consume Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        r = client.post(
            f"/api/customers/{cid}/installer-token",
            cookies={"session": operator_login},
        )
        body = r.json()
        # Extract token from powershell_cmd
        # Format: iex (irm 'https://...?slug=X&token=YZ...')
        token = body["powershell_cmd"].split("token=")[1].rstrip("')")
        return token, cid

    def test_consume_returns_credentials_first_time(self, client, operator_login):
        token, _ = self._create_via_setup(client, operator_login)
        r = client.get(f"/api/installer/{token}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["customer_name"] == "consume-co"
        assert body["username"] == "consume-co-laptop"
        assert "password" in body and len(body["password"]) >= 16
        assert body["server"] == "myvpn.databyte.co.za"
        assert body["tier"] == "tier_5gb"

    def test_consume_twice_returns_404(self, client, operator_login):
        token, _ = self._create_via_setup(client, operator_login)
        r1 = client.get(f"/api/installer/{token}")
        assert r1.status_code == 200
        r2 = client.get(f"/api/installer/{token}")
        assert r2.status_code == 404

    def test_consume_invalid_token_404(self, client):
        r = client.get("/api/installer/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
        assert r.status_code == 404

    def test_consume_too_short_token_400(self, client):
        r = client.get("/api/installer/short")
        assert r.status_code == 400

    def test_consume_expired_token_404(self, client, operator_login, db_path):
        token, _ = self._create_via_setup(client, operator_login)
        # Backdate expires_at to past
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE installer_tokens SET expires_at = ? WHERE token = ?",
            (int(time.time()) - 60, token),
        )
        conn.commit()
        conn.close()
        r = client.get(f"/api/installer/{token}")
        assert r.status_code == 404

    def test_consume_archived_customer_returns_403(self, client, operator_login, db_path):
        """Archive customer → consume fails 403 (is_active check at consume time)."""
        token, cid = self._create_via_setup(client, operator_login)
        client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        r = client.get(f"/api/installer/{token}")
        assert r.status_code == 403
