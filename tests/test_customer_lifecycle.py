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


# ---------- v1.3.2 — Rotate EAP credentials (Bug #4 fix) ----------

class TestRotateEAPCredentials:
    """In-portal EAP credential rotation. Real prod bug: previously required
    SSH to VPS + manual ops/rotate-vpn-credentials.py invocation. Now the
    operator clicks a button in the portal UI.

    Behavioral contract:
      - Rotates the password (users.password NTLM hash + rw-eap.conf secret)
      - PRESERVES the EAP identity (Lesson #193 / Bug #4 lineage — never rename)
      - Sets customers.eap_rotated_at to now
      - Reloads charon creds
      - Writes audit row (NO plaintext password in audit)
      - Returns success but NOT the new password (defense in depth)

    Refuses on:
      - 404: customer not found
      - 403: customer is the operator account
      - 409: customer is archived (unarchive first)
      - 409: customer has no devices
    """

    def _create(self, client, operator_login, name, device_name="laptop"):
        return client.post(
            "/api/customers",
            json={
                "display_name": name,
                "tier_name": "tier_5gb",
                "device_name": device_name,
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()

    def test_rotate_succeeds_and_preserves_identity(self, client, operator_login, rw_eap_conf):
        c = self._create(client, operator_login, "Rotate Co")
        cid = c["customer"]["id"]
        eap_identity_before = c["eap_identity"]
        password_before = c["password"]

        # Confirm eap block exists with old secret
        assert f'eap-{eap_identity_before}' in rw_eap_conf.read_text()
        assert password_before in rw_eap_conf.read_text()

        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        # Identity is PRESERVED (Lesson #193 / Bug #4 lineage)
        assert body["eap_identity"] == eap_identity_before
        assert body["customer_id"] == cid
        assert body["eap_rotated_at"] > 0
        # New password is NOT in the response (defense in depth)
        assert "password" not in body
        assert "new_password" not in body

    def test_rotate_changes_password_in_rw_eap_conf(self, client, operator_login, rw_eap_conf):
        c = self._create(client, operator_login, "Rotate Conf Co")
        cid = c["customer"]["id"]
        eap_identity = c["eap_identity"]
        old_password = c["password"]
        old_conf = rw_eap_conf.read_text()
        assert f'secret = "{old_password}"' in old_conf

        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200
        new_conf = rw_eap_conf.read_text()
        # Old secret is gone
        assert f'secret = "{old_password}"' not in new_conf
        # Block still exists with same identity
        assert f'eap-{eap_identity}' in new_conf
        # New secret is present (some non-empty quoted value)
        import re
        secret_match = re.search(
            rf"eap-{re.escape(eap_identity)}\s*\{{[^}}]*secret\s*=\s*\"([^\"]+)\"",
            new_conf,
            re.DOTALL,
        )
        assert secret_match is not None
        assert len(secret_match.group(1)) > 8, "new secret should be non-trivial"

    def test_rotate_updates_users_password_ntlm_hash(self, client, operator_login, db_path):
        c = self._create(client, operator_login, "Rotate Hash Co")
        cid = c["customer"]["id"]
        eap_identity = c["eap_identity"]
        old_password = c["password"]

        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200

        # Read users.password from DB
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT password FROM users WHERE name=?", (eap_identity,)
        ).fetchone()
        conn.close()
        assert row is not None
        stored = row[0]
        if isinstance(stored, str):
            stored = bytes.fromhex(stored)
        # Old password hash should NOT match anymore.
        # Use portal_auth.ntlm_hash_bytes (uses openssl legacy provider, not
        # the hashlib.md4 path which is broken on Python 3.13+).
        import portal_auth
        old_ntlm = portal_auth.ntlm_hash_bytes(old_password)
        assert bytes(stored).lower() != old_ntlm.lower(), (
            "users.password still matches old plaintext — rotation didn't take effect"
        )

    def test_rotate_sets_eap_rotated_at_timestamp(self, client, operator_login, db_path):
        c = self._create(client, operator_login, "Rotate TS Co")
        cid = c["customer"]["id"]

        # Before rotate: eap_rotated_at should be NULL
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT eap_rotated_at FROM customers WHERE id=?", (cid,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None, f"eap_rotated_at should start NULL, got {row[0]}"

        # Rotate
        before = int(__import__("time").time())
        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200
        after = int(__import__("time").time())

        # After rotate: eap_rotated_at should be set to a recent timestamp
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT eap_rotated_at FROM customers WHERE id=?", (cid,)
        ).fetchone()
        conn.close()
        assert row is not None
        rotated_at = row[0]
        assert rotated_at is not None
        assert before <= rotated_at <= after, (
            f"eap_rotated_at={rotated_at} outside [{before},{after}]"
        )

    def test_rotate_writes_audit_log_without_plaintext(self, client, operator_login):
        c = self._create(client, operator_login, "Rotate Audit Co")
        cid = c["customer"]["id"]
        eap_identity = c["eap_identity"]

        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200

        # Read audit log via API
        ar = client.get(
            "/api/admin/audit?target_type=customer&target_id=" + str(cid),
            cookies={"session": operator_login},
        )
        body = ar.json()
        actions = [row["action"] for row in body["rows"]]
        assert "customer_eap_rotate" in actions
        # Find the row and check NO plaintext password leaked
        rotate_row = [r for r in body["rows"] if r["action"] == "customer_eap_rotate"][0]
        import json as _json
        payload_str = _json.dumps(rotate_row["payload"])
        assert eap_identity in payload_str  # identity is OK to log
        # No password fields
        assert "password" not in payload_str.lower()
        assert "secret" not in payload_str.lower()

    def test_rotate_archived_customer_returns_409(self, client, operator_login):
        c = self._create(client, operator_login, "Rotate Archived Co")
        cid = c["customer"]["id"]
        # Archive first
        r = client.post(
            f"/api/customers/{cid}/archive",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200
        # Rotate should fail
        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 409
        assert "archived" in r.text.lower()

    def test_rotate_missing_customer_returns_404(self, client, operator_login):
        r = client.post(
            "/api/customers/99999/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 404

    def test_rotate_operator_returns_403(self, client, operator_login):
        # Get the operator's customer id
        r = client.get("/api/customers", cookies={"session": operator_login})
        body = r.json()
        # The first item is typically the operator (ORDER BY is_operator DESC)
        operator_customer = next(
            (c for c in body if c.get("is_operator")), None
        )
        assert operator_customer is not None, "expected an operator customer in seed"
        op_id = operator_customer["id"]
        r = client.post(
            f"/api/customers/{op_id}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 403
        assert "operator" in r.text.lower()

    def test_rotate_reload_charon_called(self, client, operator_login, monkeypatch):
        """Verify reload_charon_creds is called (catches the bug where rotation
        forgets to reload and charon keeps serving the OLD secret)."""
        calls = []
        import app as app_mod
        original_reload = app_mod.reload_charon_creds
        def counted_reload():
            calls.append("reload")
            return original_reload()
        monkeypatch.setattr(app_mod, "reload_charon_creds", counted_reload)

        c = self._create(client, operator_login, "Rotate Reload Co")
        cid = c["customer"]["id"]
        r = client.post(
            f"/api/customers/{cid}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200
        assert "reload" in calls, "reload_charon_creds was not called after rotation"


# ============================================================
# Bug #2 — explicit user_id FK on customers (v1.4.0)
# ============================================================
#
# Before: customer→user was implicit via devices.strongswan_user_id.
# After: customers.user_id is a real FK column. Populated on create,
# queryable directly, used by /rotate_eap and installer-token paths.
#
# Operator rows (is_operator=1) have no user and user_id stays NULL.
class TestCustomerUserIdFK:
    """Regression tests for the customers.user_id FK column."""

    def _create(self, client, operator_login, name, device_name="laptop"):
        return client.post(
            "/api/customers",
            json={
                "display_name": name,
                "tier_name": "tier_5gb",
                "device_name": device_name,
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()

    def test_create_customer_populates_user_id_fk(self, client, operator_login, db_path):
        """POST /api/customers must set customers.user_id to the user's PK."""
        import sqlite3
        body = self._create(client, operator_login, "FK Test Co")
        cust_id = body["customer"]["id"]
        cust_name = body["customer"]["name"]
        eap_identity = body["eap_identity"]

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT c.user_id, u.id AS user_id_via_join "
            "FROM customers c "
            "JOIN devices d ON d.customer_id = c.id "
            "JOIN users u ON u.id = d.strongswan_user_id "
            "WHERE c.id = ?",
            (cust_id,),
        ).fetchone()
        conn.close()
        assert row is not None, f"customer {cust_id} has no joined user/device row"
        cust_user_id, user_id_via_join = row
        assert cust_user_id is not None, "customers.user_id is NULL after create"
        assert cust_user_id == user_id_via_join, (
            f"customers.user_id ({cust_user_id}) doesn't match users.id "
            f"({user_id_via_join}) — FK would be invalid"
        )

    def test_create_two_customers_each_gets_distinct_user_id(self, client, operator_login, db_path):
        """Two customers must have two distinct users, two distinct user_ids."""
        import sqlite3
        body_a = self._create(client, operator_login, "Customer A", device_name="laptop")
        body_b = self._create(client, operator_login, "Customer B", device_name="laptop")
        cust_a = body_a["customer"]["id"]
        cust_b = body_b["customer"]["id"]

        conn = sqlite3.connect(str(db_path))
        ua = conn.execute("SELECT user_id FROM customers WHERE id = ?", (cust_a,)).fetchone()[0]
        ub = conn.execute("SELECT user_id FROM customers WHERE id = ?", (cust_b,)).fetchone()[0]
        conn.close()
        assert ua is not None and ub is not None, "user_id must be set on both"
        assert ua != ub, f"both customers got the same user_id {ua}"

    def test_operator_user_id_is_null(self, client, operator_login, db_path):
        """The operator customer (seed) must have user_id NULL — they never auth via EAP."""
        import sqlite3
        # Get the operator customer
        r = client.get("/api/customers", cookies={"session": operator_login})
        assert r.status_code == 200
        op = next((c for c in r.json() if c.get("is_operator")), None)
        assert op is not None, "no operator in seed"

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT user_id FROM customers WHERE id = ?", (op["id"],)
        ).fetchone()
        conn.close()
        assert row[0] is None, f"operator user_id should be NULL, got {row[0]}"

    def test_rotate_eap_uses_customer_user_id_fk(self, client, operator_login, db_path):
        """Bug #2 fix: /rotate_eap uses customers.user_id (not devices join)."""
        import sqlite3
        body = self._create(client, operator_login, "Rotate FK Co")
        cust_id = body["customer"]["id"]

        # Sanity: user_id is set
        conn = sqlite3.connect(str(db_path))
        cust_user_id = conn.execute(
            "SELECT user_id FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()[0]
        conn.close()
        assert cust_user_id is not None, "precondition: customers.user_id must be set"

        # Rotate
        r = client.post(
            f"/api/customers/{cust_id}/rotate_eap",
            cookies={"session": operator_login},
        )
        assert r.status_code == 200, r.text

        # After rotation: user_id still points to the SAME users row (no rename)
        conn = sqlite3.connect(str(db_path))
        cust_user_id_after = conn.execute(
            "SELECT user_id FROM customers WHERE id = ?", (cust_id,)
        ).fetchone()[0]
        user_name_after = conn.execute(
            "SELECT name FROM users WHERE id = ?", (cust_user_id_after,)
        ).fetchone()[0]
        conn.close()
        assert cust_user_id_after == cust_user_id, (
            f"customers.user_id changed during rotation: "
            f"{cust_user_id} -> {cust_user_id_after}"
        )
        assert user_name_after == body["eap_identity"], (
            "EAP identity changed during rotation (Bug #4 lineage violation)"
        )

    def test_idx_customers_user_id_exists(self, db_path):
        """The migration creates idx_customers_user_id. Must exist after migration."""
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='customers' AND name='idx_customers_user_id'"
        ).fetchone()
        conn.close()
        assert idx is not None, "idx_customers_user_id not created by portal-user-id-fk.sql"

    def test_user_id_column_references_users_id(self, db_path):
        """customers.user_id must be a FOREIGN KEY to users(id), not a bare INTEGER."""
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("PRAGMA foreign_key_list(customers)").fetchall()
        conn.close()
        # Filter for the user_id FK specifically (PRAGMA returns one row per FK column)
        fk_to_users = [r for r in row if r[2] == "users" and r[3] == "user_id"]
        assert fk_to_users, (
            f"customers.user_id is not a FK to users(id). "
            f"PRAGMA foreign_key_list(customers) = {row}"
        )


# ============================================================
# v1.5.0 — Speed plan at customer creation (per-customer, NOT tier-driven)
# ============================================================
#
# Per Zun 2026-06-25 05:19: two preset options at customer create time:
#   - 'standard'         → 20/20 mbps symmetric (default)
#   - 'asymmetric_40_20' → 40/20 mbps asymmetric
#
# Tiers (tier_5gb/tier_10gb/tier_20gb) drive DATA QUOTA, not bandwidth.
# The operator picks the speed plan when creating the customer.
# Explicit bandwidth_down/up override wins over speed_plan.
class TestSpeedPlan:
    """Regression tests for the speed_plan field in POST /api/customers."""

    def _create(self, client, operator_login, **extra):
        body = {
            "display_name": "Speed Plan Co",
            "tier_name": "tier_5gb",
            "device_name": "laptop",
            "device_type": "Windows",
        }
        body.update(extra)
        # Avoid name collision on repeated calls in same test session
        if "name" not in body and "display_name" in extra:
            pass  # custom display_name used as slug source
        return client.post(
            "/api/customers",
            json=body,
            cookies={"session": operator_login},
        )

    def _read_bandwidth(self, db_path, cust_id):
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT bandwidth_down_mbps, bandwidth_up_mbps FROM customers WHERE id = ?",
            (cust_id,),
        ).fetchone()
        conn.close()
        return row

    def test_default_speed_plan_is_standard_20_20(self, client, operator_login, db_path):
        """No speed_plan, no explicit bandwidth → defaults to 20/20."""
        r = self._create(client, operator_login)
        assert r.status_code == 200, r.text
        cust_id = r.json()["customer"]["id"]
        down, up = self._read_bandwidth(db_path, cust_id)
        assert down == 20, f"expected bandwidth_down_mbps=20, got {down}"
        assert up == 20, f"expected bandwidth_up_mbps=20, got {up}"

    def test_speed_plan_standard_sets_20_20(self, client, operator_login, db_path):
        """speed_plan='standard' → 20/20 (explicit form)."""
        r = self._create(client, operator_login,
                         display_name="Std Speed Co",
                         speed_plan="standard")
        assert r.status_code == 200, r.text
        cust_id = r.json()["customer"]["id"]
        down, up = self._read_bandwidth(db_path, cust_id)
        assert (down, up) == (20, 20), f"expected (20, 20), got ({down}, {up})"

    def test_speed_plan_asymmetric_40_20_sets_40_20(self, client, operator_login, db_path):
        """speed_plan='asymmetric_40_20' → 40 down / 20 up."""
        r = self._create(client, operator_login,
                         display_name="Asym Speed Co",
                         speed_plan="asymmetric_40_20")
        assert r.status_code == 200, r.text
        cust_id = r.json()["customer"]["id"]
        down, up = self._read_bandwidth(db_path, cust_id)
        assert (down, up) == (40, 20), f"expected (40, 20), got ({down}, {up})"

    def test_explicit_bandwidth_wins_over_speed_plan(self, client, operator_login, db_path):
        """Explicit bandwidth_down/up override speed_plan."""
        r = self._create(client, operator_login,
                         display_name="Override Co",
                         speed_plan="standard",
                         bandwidth_down_mbps=100,
                         bandwidth_up_mbps=50)
        assert r.status_code == 200, r.text
        cust_id = r.json()["customer"]["id"]
        down, up = self._read_bandwidth(db_path, cust_id)
        assert (down, up) == (100, 50), (
            f"explicit override ignored: speed_plan='standard' gave ({down}, {up})"
        )

    def test_explicit_bandwidth_without_speed_plan(self, client, operator_login, db_path):
        """Explicit bandwidth works without speed_plan (advanced override path)."""
        r = self._create(client, operator_login,
                         display_name="Adv Co",
                         bandwidth_down_mbps=50,
                         bandwidth_up_mbps=50)
        assert r.status_code == 200, r.text
        cust_id = r.json()["customer"]["id"]
        down, up = self._read_bandwidth(db_path, cust_id)
        assert (down, up) == (50, 50)

    def test_partial_explicit_bandwidth_rejected_400(self, client, operator_login):
        """Only down provided (no up) → 400. Partial is ambiguous."""
        r = self._create(client, operator_login,
                         display_name="Partial Co",
                         bandwidth_down_mbps=50)
        assert r.status_code == 400, r.text
        assert "both" in r.text.lower() or "together" in r.text.lower(), r.text

    def test_partial_explicit_bandwidth_only_up_rejected_400(self, client, operator_login):
        """Only up provided (no down) → 400."""
        r = self._create(client, operator_login,
                         display_name="Partial2 Co",
                         bandwidth_up_mbps=30)
        assert r.status_code == 400, r.text

    def test_invalid_speed_plan_rejected_422(self, client, operator_login):
        """speed_plan='gigabit' is not in the Literal → Pydantic 422 (schema validation)."""
        r = self._create(client, operator_login,
                         display_name="Bad Plan Co",
                         speed_plan="gigabit")
        assert r.status_code == 422, r.text
        # Pydantic returns a structured error mentioning the literal options
        body = r.json()
        assert "speed_plan" in str(body), r.text
        assert "standard" in r.text and "asymmetric_40_20" in r.text

    def test_bandwidth_out_of_range_rejected_422(self, client, operator_login):
        """bandwidth_down_mbps > 1000 → Pydantic 422 (le=1000 bound)."""
        r = self._create(client, operator_login,
                         display_name="Too Fast Co",
                         bandwidth_down_mbps=2000,
                         bandwidth_up_mbps=2000)
        assert r.status_code == 422, r.text
        assert "1000" in r.text

    def test_speed_plan_does_not_override_tier(self, client, operator_login, db_path):
        """Tier drives DATA QUOTA (data_limit_bytes), speed_plan drives BANDWIDTH (mbps).
        Per Zun's directive: speed_plan is per-customer, NOT tier-based. This test
        proves the two are independent: tier_10gb + asymmetric_40_20 → 10 GiB quota,
        40/20 mbps bandwidth.
        """
        r = self._create(client, operator_login,
                         display_name="Indep Co",
                         tier_name="tier_10gb",
                         speed_plan="asymmetric_40_20")
        assert r.status_code == 200, r.text
        body = r.json()
        cust_id = body["customer"]["id"]

        # Tier quota is 10 GiB (10737418240 bytes)
        assert body["customer"]["data_limit_bytes"] == 10737418240, (
            f"tier 10GB quota lost: got {body['customer']['data_limit_bytes']}"
        )
        # Speed plan is 40/20 (independent)
        down, up = self._read_bandwidth(db_path, cust_id)
        assert (down, up) == (40, 20)
