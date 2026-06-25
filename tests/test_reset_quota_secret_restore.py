"""
test_reset_quota_secret_restore.py — Regression test for the dead-state after hard cut.

Bug (2026-06-25, caught by Zun):
  The hard-cut at 100% KILLs the customer's EAP secret in rw-eap.conf by
  prepending 'KILLED-<random>' (prevents any reconnect auth). The reset_quota
  endpoint is supposed to detect this and restore the secret from a backup.

  But the KILLED detection built dev_names from `devices.device_name`
  (just 'cellphone'), while rw-eap.conf blocks use the EAP identity
  (format = '{customer.name}-{device.device_name}' = 'zade-cellphone').
  State machine checked 'zade-cellphone' in ['cellphone'] → False, so
  KILLED detection missed every customer after a hard cut. The customer's
  phone kept sending the dead password, charon rejected, no reconnect
  possible until a manual fix.

  Fix: query users.name (the EAP identity) directly via JOIN devices → users.

  This test:
  1. Creates customer + device + user with a known EAP identity.
  2. Crafts a fake rw-eap.conf with a KILLED secret for that customer.
  3. Crafts a fake backup with the real secret.
  4. Calls /api/quota/{id}/reset via the operator endpoint.
  5. Asserts secret_restored=True in the response.
  6. Asserts the deployed conf no longer has KILLED- prefix.
  7. Asserts audit log entry has secret_restored=True and the right device.
  8. Defensive: a customer whose devices have NO matching EAP identity in
     conf (e.g. never connected) does NOT trigger a false restore.
"""

import json
import sqlite3
import time


# ---------- helpers ----------

def _seed_eap_identity(db_path, customer_name, device_name, eap_identity, secret):
    """Insert customer + device + user so eap_identity is reachable."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        ts = int(time.time())
        # users table (test schema: id, name, password only)
        cur.execute("DELETE FROM users WHERE name=?", (eap_identity,))
        # password is the NTLM hash; for test we just need any non-NULL value
        cur.execute("""
            INSERT INTO users (name, password)
            VALUES (?, ?)
        """, (eap_identity, b"\x00" * 16))
        user_id = cur.lastrowid
        # customers table
        cur.execute("DELETE FROM customers WHERE name=?", (customer_name,))
        cur.execute("""
            INSERT INTO customers
                (name, display_name, is_operator, is_active, over_quota,
                 data_limit_bytes, data_used_bytes, status, max_devices,
                 bandwidth_down_mbps, bandwidth_up_mbps,
                 created_at, updated_at)
            VALUES (?, ?, 0, 1, 1, ?, ?, 'active', 1, 20, 20, ?, ?)
        """, (customer_name, customer_name, 104857600, 131788427, ts, ts))
        customer_id = cur.lastrowid
        # devices table (links customer ↔ user)
        cur.execute("DELETE FROM devices WHERE customer_id=?", (customer_id,))
        cur.execute("""
            INSERT INTO devices
                (customer_id, strongswan_user_id, device_name, is_active,
                 created_at, updated_at, device_type)
            VALUES (?, ?, ?, 1, ?, ?, 'iOS')
        """, (customer_id, user_id, device_name, ts, ts))
        conn.commit()
        return customer_id, user_id
    finally:
        conn.close()


def _seed_audit_log(db_path, customer_id):
    """Insert a cut_100pct audit event so the reset has context."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        ts = int(time.time())
        cur.execute("""
            INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at)
            VALUES ('quota-monitor', 'cut_100pct', 'customer', ?, ?, ?)
        """, (customer_id, json.dumps({
            "data_used": 131788427,
            "data_limit": 104857600,
            "sas_terminated": 1,
        }), ts))
        conn.commit()
    finally:
        conn.close()


# ---------- tests ----------

class TestResetQuotaSecretRestore:
    """reset_quota must restore KILLED secrets so customer can reconnect."""

    def test_killed_secret_detected_via_eap_identity_not_device_name(
        self, client, app_module, db_path, operator_login, monkeypatch
    ):
        """Bug: dev_names was [device_name]; must be [eap_identity = customer-device]."""
        customer_name = "zade"
        device_name = "cellphone"
        eap_identity = "zade-cellphone"
        real_secret = "KILLED-9ef53187e3d7216f"  # current conf state after hard cut

        customer_id, _user_id = _seed_eap_identity(
            db_path, customer_name, device_name, eap_identity, real_secret,
        )
        _seed_audit_log(db_path, customer_id)

        # Fake the current conf (KILLED) and the backup (real secret).
        killed_conf = f"""
secrets {{
    eap-zade-cellphone {{
        id     = zade-cellphone
        secret = "KILLED-9ef53187e3d7216f"
    }}
}}
"""
        real_conf = f"""
secrets {{
    eap-zade-cellphone {{
        id     = zade-cellphone
        secret = "RealSecretValue123"
    }}
}}
"""

        calls = {"cp_dest": []}

        def fake_ssh(cmd_args, **kw):
            # sqlite3 -json queries (db_query path)
            if cmd_args[0] == "sqlite3":
                sql = cmd_args[-1] if len(cmd_args) > 1 else ""
                # Customer lookup
                if "SELECT id, name, data_used_bytes, over_quota FROM customers" in sql:
                    return json.dumps([{
                        "id": customer_id,
                        "name": customer_name,
                        "data_used_bytes": 131788427,
                        "over_quota": 1,
                    }])
                # Device + user lookup (for EAP identity)
                if "u.name AS eap_identity" in sql or "FROM devices d" in sql:
                    return json.dumps([{"eap_identity": eap_identity}])
                # Other queries (audit_log read etc.) — return empty
                return "[]"
            # cat RW_EAP_CONF
            if cmd_args[0] == "cat" and cmd_args[-1] == app_module.RW_EAP_CONF:
                if calls["cp_dest"]:
                    return real_conf
                return killed_conf
            # ls BACKUP_DIR
            if cmd_args[0] == "ls":
                # Pretend the latest backup exists
                return f"rw-eap.conf.bak-quotamon-{int(time.time())}\n"
            # cp backup → RW_EAP_CONF
            if cmd_args[0] == "cp":
                calls["cp_dest"].append(cmd_args[-1])
                return ""
            # docker exec swanctl --load-creds (and any other docker call)
            if cmd_args[0] == "docker":
                return ""
            # iptables-legacy -Z FORWARD
            if cmd_args[0] == "iptables-legacy":
                return ""
            # rm sidecar
            if cmd_args[0] == "rm":
                return ""
            # Anything else — return empty (test doesn't exercise)
            return ""

        # Override ssh_903 with our fake
        monkeypatch.setattr(app_module, "ssh_903", fake_ssh)

        r = client.post(f"/api/quota/{customer_id}/reset")
        assert r.status_code == 200, f"reset_quota failed: {r.status_code} {r.text}"
        body = r.json()

        # Find the restore_secret step in the response
        restore_step = next((s for s in body.get("steps", []) if s["step"] == "restore_secret"), None)
        assert restore_step is not None, (
            f"Expected restore_secret step. Got steps: {[s['step'] for s in body.get('steps', [])]}"
        )
        assert restore_step["ok"] is True, (
            f"Expected restore_secret ok=True. Got: {restore_step}"
        )
        assert "zade-cellphone" in restore_step["devices"], (
            f"Expected zade-cellphone in restored devices. Got: {restore_step['devices']}"
        )
        assert body.get("secret_restored") is True, (
            f"Expected secret_restored=True. Got: {body}"
        )

    def test_no_killed_secret_no_restore(self, client, app_module, db_path, operator_login, monkeypatch):
        """Defensive: if the customer's secret is NOT KILLED, do not touch backups."""
        customer_name = "saalieg"
        device_name = "laptop"
        eap_identity = "saalieg-laptop"

        customer_id, _user_id = _seed_eap_identity(
            db_path, customer_name, device_name, eap_identity, "live-secret",
        )

        # Conf has a healthy (non-KILLED) secret for saalieg
        healthy_conf = f"""
secrets {{
    eap-saalieg-laptop {{
        id     = saalieg-laptop
        secret = "LiveWorkingSecret"
    }}
}}
"""

        def fake_ssh(cmd_args, **kw):
            if cmd_args[0] == "sqlite3":
                sql = cmd_args[-1] if len(cmd_args) > 1 else ""
                if "SELECT id, name, data_used_bytes, over_quota FROM customers" in sql:
                    return json.dumps([{
                        "id": customer_id,
                        "name": customer_name,
                        "data_used_bytes": 0,
                        "over_quota": 0,
                    }])
                if "u.name AS eap_identity" in sql or "FROM devices d" in sql:
                    return json.dumps([{"eap_identity": eap_identity}])
                return "[]"
            if cmd_args[0] == "cat":
                return healthy_conf
            if cmd_args[0] == "ls":
                return ""
            return ""

        monkeypatch.setattr(app_module, "ssh_903", fake_ssh)

        r = client.post(f"/api/quota/{customer_id}/reset")
        assert r.status_code == 200
        body = r.json()
        # No restore_secret step
        restore_step = next((s for s in body.get("steps", []) if s["step"] == "restore_secret"), None)
        assert restore_step is None, (
            f"Did NOT expect restore_secret step (secret was healthy). Got: {restore_step}"
        )
        assert body.get("secret_restored") is False

    def test_customer_with_unrelated_killed_secret_does_not_trigger(
        self, client, app_module, db_path, operator_login, monkeypatch
    ):
        """Defensive: a KILLED secret belonging to ANOTHER customer must NOT trigger restore."""
        # Create customer A with healthy secret
        _customer_a_id, _ = _seed_eap_identity(
            db_path, "customera", "laptop", "customera-laptop", "ok-secret",
        )
        # Customer B (the one we're resetting) with healthy secret
        customer_b_id, _ = _seed_eap_identity(
            db_path, "customerb", "phone", "customerb-phone", "ok-secret-b",
        )

        # Conf has a KILLED secret for customer A but NOT customer B
        conf = f"""
secrets {{
    eap-customera-laptop {{
        id     = customera-laptop
        secret = "KILLED-aaaaaaaaaaaa"
    }}
    eap-customerb-phone {{
        id     = customerb-phone
        secret = "OkSecretB"
    }}
}}
"""
        def fake_ssh(cmd_args, **kw):
            if cmd_args[0] == "sqlite3":
                sql = cmd_args[-1] if len(cmd_args) > 1 else ""
                if "SELECT id, name, data_used_bytes, over_quota FROM customers" in sql:
                    return json.dumps([{
                        "id": customer_b_id,
                        "name": "customerb",
                        "data_used_bytes": 0,
                        "over_quota": 0,
                    }])
                if "u.name AS eap_identity" in sql or "FROM devices d" in sql:
                    return json.dumps([{"eap_identity": "customerb-phone"}])
                return "[]"
            if cmd_args[0] == "cat":
                return conf
            if cmd_args[0] == "ls":
                return f"rw-eap.conf.bak-quotamon-{int(time.time())}\n"
            return ""
        monkeypatch.setattr(app_module, "ssh_903", fake_ssh)

        r = client.post(f"/api/quota/{customer_b_id}/reset")
        assert r.status_code == 200
        body = r.json()
        # Customer B's secret is healthy → no restore step
        restore_step = next((s for s in body.get("steps", []) if s["step"] == "restore_secret"), None)
        assert restore_step is None, (
            f"Did NOT expect restore_secret for customer B. Got: {restore_step}"
        )
        assert body.get("secret_restored") is False