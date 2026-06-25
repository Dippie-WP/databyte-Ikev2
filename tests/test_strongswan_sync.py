"""
test_strongswan_sync.py — Tests that customer/device state syncs to strongSwan artifacts.

Every customer create MUST trigger the strongSwan side:
  - /etc/strongswan/ipsec.db INSERT INTO users (EAP identity + NTLM hash)
  - /etc/strongswan/ipsec.db INSERT INTO identities + shared_secrets (auth chain)
  - /etc/strongswan/ipsec.db INSERT INTO devices (already tested in test_portal_auth)
  - rw-eap.conf gets a new `eap-{identity}` block
  - charon --load-creds reload (so the new EAP block is active)

Customer archive / unarchive should NOT touch strongSwan artifacts (data stays
for audit; archived customers just can't login via portal_auth). The EAP block
in rw-eap.conf STAYS so charon still accepts their IKE_SA (existing connections
work; new logins via the customer portal are blocked).

Customer HARD DELETE removes EVERYTHING:
  - customers, devices, users (charon attr-sql)
  - identities, shared_secrets (charon attr-sql)
  - the eap-{identity} block in rw-eap.conf
"""
import re
import time
import pytest


class TestCreateCustomerSyncsStrongSwan:
    def test_users_row_with_ntlm_hash(self, client, operator_login, db_path):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Sync Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        eap_identity = c["eap_identity"]
        password = c["password"]
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT name, password FROM users WHERE name=?",
            (eap_identity,),
        ).fetchone()
        conn.close()
        assert row is not None, f"users row for {eap_identity} not found"
        # password column is BLOB; should contain the NTLM hash
        assert row[1] is not None
        # NTLM hash is 16 bytes for any password
        if isinstance(row[1], str):
            # hex string from fake
            assert len(row[1]) == 32
        else:
            assert len(bytes(row[1])) == 16

    def test_users_row_ntlm_hash_matches_plaintext(self, client, operator_login, db_path):
        """Verify the NTLM hash in users.password matches the plaintext via portal_auth helper."""
        import portal_auth
        c = client.post(
            "/api/customers",
            json={
                "display_name": "NTLM Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        eap_identity = c["eap_identity"]
        password = c["password"]
        expected_ntlm = portal_auth.ntlm_hash_bytes(password)
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
        assert bytes(stored).lower() == expected_ntlm.lower(), (
            f"NTLM mismatch for {eap_identity}"
        )

    def test_rw_eap_conf_block_added(self, client, operator_login, rw_eap_conf):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "EAP Conf Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        conf = rw_eap_conf.read_text()
        eap_identity = c["eap_identity"]
        password = c["password"]
        # The block should look like: eap-{identity} { id = {identity}, secret = "{password}" }
        block_pattern = re.compile(
            rf"eap-{re.escape(eap_identity)}\s*\{{[^}}]*id\s*=\s*{re.escape(eap_identity)}[^}}]*secret\s*=\s*\"{re.escape(password)}\"",
            re.DOTALL,
        )
        assert block_pattern.search(conf), (
            f"EAP block for {eap_identity} not found in rw-eap.conf:\n{conf}"
        )

    def test_swanctl_load_creds_called(self, client, operator_login, monkeypatch):
        """Reload charon creds after every customer create so the new EAP is active.
        Catches a regression where the create flow forgets to call reload_charon_creds."""
        calls = []
        # Patch the charel-rel by hooking reload_charon_creds in the app module
        import app as app_mod
        original_reload = app_mod.reload_charon_creds
        def counted_reload():
            calls.append("reload")
            return original_reload()
        monkeypatch.setattr(app_mod, "reload_charon_creds", counted_reload)
        client.post(
            "/api/customers",
            json={
                "display_name": "Reload Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        )
        assert "reload" in calls, "reload_charon_creds was not called after customer create"


class TestArchiveDoesNotTouchStrongSwan:
    """Archive is reversible, so strongSwan artifacts stay intact."""

    def test_archive_keeps_users_row(self, client, operator_login, db_path):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Archive Sync Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        eap_identity = c["eap_identity"]
        client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        # Users row still present
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT name FROM users WHERE name=?", (eap_identity,)
        ).fetchone()
        conn.close()
        assert row is not None, "users row was deleted on archive (should stay)"

    def test_archive_keeps_rw_eap_block(self, client, operator_login, rw_eap_conf):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Archive EAP Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        eap_identity = c["eap_identity"]
        client.post(f"/api/customers/{cid}/archive", cookies={"session": operator_login})
        conf = rw_eap_conf.read_text()
        assert f"eap-{eap_identity}" in conf, "EAP block was removed on archive (should stay)"


class TestDeleteCascadesStrongSwan:
    """HARD DELETE must remove ALL strongSwan artifacts."""

    def test_delete_removes_users_row(self, client, operator_login, db_path):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Delete Sync Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        cust_name = c["customer"]["name"]
        eap_identity = c["eap_identity"]
        client.delete(
            f"/api/customers/{cid}?confirm={cust_name}",
            cookies={"session": operator_login},
        )
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        # users
        assert conn.execute("SELECT COUNT(*) FROM users WHERE name=?", (eap_identity,)).fetchone()[0] == 0
        # devices
        assert conn.execute("SELECT COUNT(*) FROM devices WHERE customer_id=?", (cid,)).fetchone()[0] == 0
        conn.close()

    def test_delete_removes_rw_eap_block(self, client, operator_login, rw_eap_conf):
        c = client.post(
            "/api/customers",
            json={
                "display_name": "Delete EAP Co",
                "tier_name": "tier_5gb",
                "device_name": "laptop",
                "device_type": "Windows",
            },
            cookies={"session": operator_login},
        ).json()
        cid = c["customer"]["id"]
        cust_name = c["customer"]["name"]
        eap_identity = c["eap_identity"]
        client.delete(
            f"/api/customers/{cid}?confirm={cust_name}",
            cookies={"session": operator_login},
        )
        conf = rw_eap_conf.read_text()
        assert f"eap-{eap_identity}" not in conf, "EAP block not removed on delete"
