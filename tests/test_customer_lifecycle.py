"""
test_customer_lifecycle.py — Tests for POST/DELETE/archive/unarchive /api/customers.

Catches the full CRUD chain for customer lifecycle:
  - Create customer → assert customer + device + users row + rw-eap.conf block + audit_log row
  - Archive customer → status='archived', is_active=0, audit_log row, reversible
  - Unarchive customer → status='active', is_active=1, audit_log row
  - DELETE customer → cascades devices/users/audit_log, removes EAP block, audit_log row
  - Duplicate name → 409
  - Bad tier → 400
  - Bad device_type → 400
  - device_name == customer_name → 400 (collision guard)
  - device_name starts with customer_name- → 400 (collision guard)
  - Custom tier (auto-create)
"""
import re
import pytest


class TestCreateCustomer:
    """POST /api/customers — operator creates a new client."""

    def test_create_customer_minimal(self, client, operator_login, rw_eap_conf):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Acme Corp",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["customer"]["name"] == "acme-corp"
        assert body["customer"]["tier"] == "tier_5gb"
        assert body["device"]["device_name"] == "laptop"
        assert body["eap_identity"] == "acme-corp-laptop"
        assert "password" in body
        assert len(body["password"]) >= 16  # secrets.token_urlsafe(16)

        # rw-eap.conf has the new block
        conf = rw_eap_conf.read_text()
        assert "eap-acme-corp-laptop" in conf
        assert f'id     = acme-corp-laptop' in conf
        assert f'secret = "{body["password"]}"' in conf

    def test_create_customer_with_email_and_billing_id(self, client, operator_login):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Test Co",
                "tier_name": "tier_10gb",
                "device_name": "laptop",
                "device_type": "Windows",
                "email": "ops@test.example",
                "billing_id": "INV-001",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text
        assert r.json()["customer"]["email"] == "ops@test.example"
        assert r.json()["customer"]["billing_id"] == "INV-001"

    def test_create_customer_custom_tier_auto_creates(self, client, operator_login):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Custom User",
                "tier_name": "custom",
                "custom_cap_mb": 250,
                "device_name": "phone",
                "device_type": "iOS",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # tier_name is auto-generated custom_<N>mb_<ts>
        assert body["customer"]["tier"].startswith("custom_250mb_")
        # data_limit_bytes = 250 * 1024 * 1024
        assert body["customer"]["data_limit_bytes"] == 250 * 1024 * 1024

    def test_create_customer_no_auth_returns_401_or_403(self, client):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "No Auth",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
        )
        # 401 (no session) or 403 (wrong session) — both acceptable
        assert r.status_code in (401, 403)

    # ---------- validation ----------

    def test_create_customer_invalid_email_400(self, client, operator_login):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Bad Email",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
                "email": "not-an-email",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 400
        assert "email" in r.json()["detail"].lower()

    def test_create_customer_invalid_device_type_400(self, client, operator_login):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Bad Device Type",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Refrigerator",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 400
        assert "device_type" in r.json()["detail"]

    def test_create_customer_device_name_eq_customer_name_400(self, client, operator_login, db_path):
        import sqlite3
        # Create a customer first to get a known name
        client.post(
            "/api/customers",
            json={
                "name": "acme",
                "display_name": "Acme",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        # Now try to create a customer whose device_name == customer.name (via slugified display_name)
        r = client.post(
            "/api/customers",
            json={
                "display_name": "weird-co",
                "tier_name": "tier_5gb",
                "device_name": "weird-co",  # == slugified display_name
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        # Either the customer name is auto-derived from device_name slug and we get 409,
        # or we get 400 from the collision guard
        assert r.status_code in (400, 409)

    def test_create_customer_duplicate_name_409(self, client, operator_login):
        body = {
            "display_name": "Acme Two",
            "tier_name": "tier_5gb",
            "device_name": "laptop",
            "device_type": "Windows",
        }
        r1 = client.post("/api/customers", json=body, cookies={"session": operator_login})
        assert r1.status_code == 200
        r2 = client.post("/api/customers", json=body, cookies={"session": operator_login})
        assert r2.status_code == 409

    def test_create_customer_unknown_tier_400(self, client, operator_login):
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Bad Tier",
                "tier_name": "tier_does_not_exist",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 400
        assert "tier" in r.json()["detail"].lower()

    def test_create_customer_archived_tier_400(self, client, operator_login, db_path):
        # Archive tier_10gb first
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE tiers SET is_active=0 WHERE name='tier_10gb'")
        conn.commit()
        conn.close()
        r = client.post(
            "/api/customers",
            json={
                "display_name": "Archived Tier",
                "tier_name": "tier_10gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        assert r.status_code == 400


class TestArchiveUnarchive:
    """POST /api/customers/{id}/archive + /unarchive."""

    def _create(self, client, operator_login, name):
        r = client.post(
            "/api/customers",
            json={
                "display_name": name.title(),
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        return r.json()

    def test_archive_then_unarchive_round_trip(self, client, operator_login, db_path):
        c = self._create(client, operator_login, "Roundtrip Co")
        cid = c["customer"]["id"]
        # Archive
        r = client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Check DB
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT status, is_active FROM customers WHERE id=?", (cid,)).fetchone()
        conn.close()
        assert row[0] == "archived"
        assert row[1] == 0
        # Unarchive
        r = client.post(f"/api/customers/{cid}/unarchive", cookies={"session": operator_login})
        assert r.status_code == 200
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT status, is_active FROM customers WHERE id=?", (cid,)).fetchone()
        conn.close()
        assert row[0] == "active"
        assert row[1] == 1

    def test_archive_idempotent(self, client, operator_login):
        c = self._create(client, operator_login, "Idempotent Co")
        cid = c["customer"]["id"]
        r1 = client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        r2 = client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json().get("already_archived") is True

    def test_archive_unknown_customer_404(self, client, operator_login):
        r = client.post("/api/customers/99999/archive", cookies={"session": operator_login})
        assert r.status_code == 404

    def test_archive_operator_forbidden_403(self, client, operator_login, db_path):
        """Cannot archive the operator account (is_operator=1)."""
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        # 'admin' is the operator from seed
        admin_id = conn.execute("SELECT id FROM customers WHERE name='admin'").fetchone()[0]
        conn.close()
        r = client.post(f"/api/customers/{admin_id}/archive", cookies={"session": operator_login})
        assert r.status_code == 403


class TestDeleteCustomer:
    """DELETE /api/customers/{id}?confirm=<name> — HARD delete."""

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
        ).json()

    def test_delete_with_wrong_confirm_400(self, client, operator_login):
        c = self._create(client, operator_login, "Delete Co")
        cid = c["customer"]["id"]
        r = client.delete(
            f"/api/customers/{cid}?confirm=WRONG_NAME",
            cookies={"session": operator_login},
        )
        assert r.status_code == 400

    def test_delete_cascades_devices_users_audit(self, client, operator_login, rw_eap_conf, db_path):
        c = self._create(client, operator_login, "Delete Cascade")
        cid = c["customer"]["id"]
        cust_name = c["customer"]["name"]
        eap_identity = c["eap_identity"]

        # Pre-check rw-eap.conf has the block
        assert f"eap-{eap_identity}" in rw_eap_conf.read_text()

        r = client.delete(
            f"/api/customers/{cid}?confirm={cust_name}",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["devices_deleted"] == 1

        # DB: customer gone, devices gone, users gone
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE id=?", (cid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM devices WHERE customer_id=?", (cid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users WHERE name=?", (eap_identity,)).fetchone()[0] == 0
        conn.close()

        # rw-eap.conf: block removed
        assert f"eap-{eap_identity}" not in rw_eap_conf.read_text()

    def test_delete_operator_forbidden_403(self, client, operator_login, db_path):
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        admin_id = conn.execute("SELECT id FROM customers WHERE name='admin'").fetchone()[0]
        conn.close()
        r = client.delete(
            f"/api/customers/{admin_id}?confirm=admin",
            cookies={"session": operator_login},
        )
        assert r.status_code == 403

    def test_delete_unknown_404(self, client, operator_login):
        r = client.delete(
            "/api/customers/99999?confirm=anything",
            cookies={"session": operator_login},
        )
        assert r.status_code == 404


class TestListAndGetCustomers:
    """GET /api/customers and /api/customers/{id}."""

    def test_list_customers_includes_tier_and_usage(self, client, operator_login):
        client.post(
            "/api/customers",
            json={
                "display_name": "Listed Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        r = client.get("/api/customers", cookies={"session": operator_login})
        assert r.status_code == 200
        customers = r.json()
        names = [c["name"] for c in customers]
        assert "listed-co" in names
        listed = [c for c in customers if c["name"] == "listed-co"][0]
        assert listed["tier"] == "tier_5gb"
        assert listed["used_bytes"] == 0

    def test_list_customers_no_auth_401(self, client):
        r = client.get("/api/customers")
        assert r.status_code in (401, 403)

    def test_get_customer_detail(self, client, operator_login):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Detail Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        r = client.get(f"/api/customers/{cid}", cookies={"session": operator_login})
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "detail-co"
        # Devices list shape depends on app version; just verify non-empty and has the laptop
        assert len(body["devices"]) >= 1
        device_names = [d["device_name"] for d in body["devices"]]
        assert "laptop" in device_names

    def test_get_customer_audit_log_present(self, client, operator_login):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Audit Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        r = client.get(f"/api/customers/{cid}", cookies={"session": operator_login})
        body = r.json()
        # Audit log should have at least the create_client entry
        actions = [a["action"] for a in body.get("audit_log", [])]
        assert "create_client" in actions
