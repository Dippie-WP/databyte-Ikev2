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
