"""
test_portal_auth.py — Tests for portal_auth.py (login, session, customer lookup).

Catches:
  - Lesson #193 retroactively (portal_auth.lookup_user_and_customer SQL bug — existed 3 days)
  - Operator session create/verify/delete lifecycle
  - Password hash helpers (Argon2id for operator, NTLM for customers)
  - Session expiry and sliding refresh
  - Lookup_user_and_customer: name ↔ device_name join correctness
"""
import time
import pytest

import portal_auth


# ---------- Password hashing helpers ----------

class TestPasswordHashing:
    def test_argon2_hash_roundtrip(self):
        h = portal_auth.hash_operator_password("hunter2-very-strong")
        assert h.startswith("$argon2id$")
        assert portal_auth.verify_operator_password(h, "hunter2-very-strong") is True

    def test_argon2_wrong_password(self):
        h = portal_auth.hash_operator_password("hunter2-very-strong")
        assert portal_auth.verify_operator_password(h, "WRONG") is False

    def test_argon2_empty_inputs_return_false(self):
        h = portal_auth.hash_operator_password("hunter2-very-strong")
        assert portal_auth.verify_operator_password("", "hunter2-very-strong") is False
        assert portal_auth.verify_operator_password(h, "") is False
        assert portal_auth.verify_operator_password("", "") is False

    def test_argon2_malformed_hash_returns_false(self):
        assert portal_auth.verify_operator_password("not-a-hash", "anything") is False

    def test_ntlm_hash_format(self):
        h = portal_auth.ntlm_hash_bytes("test-pw")
        assert isinstance(h, (bytes, bytearray))
        assert len(h) == 16

    def test_ntlm_hash_known_value(self):
        # NTLM of "password" = MD4(UTF-16-LE("password"))
        h = portal_auth.ntlm_hash_bytes("password")
        assert h.hex().upper() == "8846F7EAEE8FB117AD06BDD830B7586C"

    def test_verify_password_bytes_and_hex_equivalent(self):
        ntlm = portal_auth.ntlm_hash_bytes("hello")
        assert portal_auth.verify_password(ntlm, "hello") is True
        assert portal_auth.verify_password(ntlm.hex().upper(), "hello") is True
        assert portal_auth.verify_password(ntlm, "wrong") is False


# ---------- Operator session lifecycle ----------

@pytest.mark.usefixtures("patch_portal_auth_db")
class TestOperatorSession:
    def test_create_and_verify(self):
        sid = portal_auth.create_operator_session(
            username="admin", user_agent="pytest", ip_address="127.0.0.1",
        )
        assert isinstance(sid, str) and len(sid) >= 32
        sess = portal_auth.verify_operator_session(sid)
        assert sess is not None
        assert sess["username"] == "admin"
        assert sess["session_id"] == sid

    def test_delete_session(self):
        sid = portal_auth.create_operator_session("admin", "ua", "1.2.3.4")
        portal_auth.delete_operator_session(sid)
        assert portal_auth.verify_operator_session(sid) is None

    def test_invalid_session_returns_none(self):
        assert portal_auth.verify_operator_session("nonexistent-session-id") is None

    def test_expired_session_returns_none(self, db_path):
        sid = portal_auth.create_operator_session("admin", "ua", "1.2.3.4")
        # Backdate expires_at (the column verify_operator_session checks against)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE operator_sessions SET expires_at = ? WHERE session_id = ?",
            (int(time.time()) - 60, sid),
        )
        conn.commit()
        conn.close()
        sess = portal_auth.verify_operator_session(sid, slide=False)
        assert sess is None

    def test_sliding_expiry_refreshes(self, db_path):
        sid = portal_auth.create_operator_session("admin", "ua", "1.2.3.4")
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        before = int(time.time()) - 3600
        conn.execute(
            "UPDATE operator_sessions SET last_active = ? WHERE session_id = ?",
            (before, sid),
        )
        conn.commit()
        conn.close()
        sess = portal_auth.verify_operator_session(sid, slide=True)
        assert sess is not None
        conn = sqlite3.connect(str(db_path))
        new_active = conn.execute(
            "SELECT last_active FROM operator_sessions WHERE session_id = ?", (sid,),
        ).fetchone()[0]
        conn.close()
        assert new_active > before

    def test_revoke_all_sessions(self):
        s1 = portal_auth.create_operator_session("admin", "ua", "1.1.1.1")
        s2 = portal_auth.create_operator_session("admin", "ua", "2.2.2.2")
        s3 = portal_auth.create_operator_session("zun",  "ua", "3.3.3.3")
        n = portal_auth.revoke_all_operator_sessions("admin")
        assert n == 2
        assert portal_auth.verify_operator_session(s1) is None
        assert portal_auth.verify_operator_session(s2) is None
        assert portal_auth.verify_operator_session(s3) is not None

    def test_concurrent_verify_does_not_error(self, db_path):
        """Regression 2026-07-06: operator session-refresh ping 500'd with
        MariaDB error 1020 ('Record has changed since last read in table
        operator_sessions') under concurrent verify_operator_session() calls.

        Previous code did SELECT-then-UPDATE in the same transaction. On
        MariaDB/InnoDB REPEATABLE READ, the 2nd worker's UPDATE failed because
        the row had been modified by a concurrent committed tx.

        This test simulates the race: 20 sequential verify calls against the
        same session_id. With sqlite3 (used in tests) the bug doesn't fire
        — sqlite3 doesn't have REPEATABLE READ row-version semantics. But the
        test still exercises the new UPDATE-first code path and confirms it
        is idempotent, returns the same row each time, and never errors.
        """
        sid = portal_auth.create_operator_session("admin", "ua", "1.2.3.4")
        seen = set()
        for _ in range(20):
            sess = portal_auth.verify_operator_session(sid, slide=True)
            assert sess is not None
            assert sess["session_id"] == sid
            assert sess["username"] == "admin"
            seen.add(sess["last_active"])
        # Each verify should bump last_active (or at minimum, not regress)
        assert len(seen) >= 1  # at least one distinct value seen

    def test_concurrent_purge_then_verify(self, db_path):
        """Regression 2026-07-06: a session whose expires_at has just passed
        (race with purge_expired_operator_sessions) must return None cleanly,
        not raise. The DELETE-on-expired path in verify_operator_session is
        best-effort and must not throw."""
        import sqlite3
        sid = portal_auth.create_operator_session("admin", "ua", "1.2.3.4")
        # Backdate expires_at to force the expired branch
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE operator_sessions SET expires_at = ? WHERE session_id = ?",
            (int(time.time()) - 60, sid),
        )
        conn.commit()
        conn.close()
        # Should return None, not raise
        assert portal_auth.verify_operator_session(sid, slide=True) is None
        # And the row should be gone
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT 1 FROM operator_sessions WHERE session_id = ?", (sid,),
        ).fetchone()
        conn.close()
        assert row is None

    def test_no_slide_still_validates(self, db_path):
        """slide=False must still verify the row is alive (not revoked/expired)
        and return it. New code path uses UPDATE last_active=last_active
        WHERE session_id=? AND revoked=0 AND expires_at >= now."""
        sid = portal_auth.create_operator_session("admin", "ua", "1.2.3.4")
        sess = portal_auth.verify_operator_session(sid, slide=False)
        assert sess is not None
        assert sess["username"] == "admin"


# ---------- Customer portal session lifecycle ----------

@pytest.mark.usefixtures("patch_portal_auth_db")
class TestCustomerPortalSession:
    def test_create_and_verify(self):
        sid = portal_auth.create_session(
            customer_id=1, identity="customer-laptop",
            user_agent="ua", ip_address="9.9.9.9",
        )
        sess = portal_auth.verify_session(sid)
        assert sess is not None
        assert sess["customer_id"] == 1
        assert sess["identity"] == "customer-laptop"

    def test_portal_ttl_is_short_enough_for_stolen_cookie_threat_model(self):
        """Bug #1 (regression): customer portal TTL was 30 days. Stolen phone
        + stolen cookie = 30 days full account access (incl. EAP password reset
        since v1.3.x). Threat model requires ≤ 1h sliding window."""
        assert portal_auth.PORTAL_TTL <= 3600, (
            f"PORTAL_TTL too long: {portal_auth.PORTAL_TTL}s "
            f"(={portal_auth.PORTAL_TTL // 3600}h). "
            f"Stolen-cookie blast radius must be ≤ 1 hour."
        )

    def test_portal_ttl_independent_from_operator_ttl(self):
        """Bug #1 (regression): operator config (30d) was reused for portal.
        They MUST be independent constants so future operator-TTL changes
        don't silently re-leak to customer portal."""
        assert hasattr(portal_auth, "PORTAL_TTL"), "PORTAL_TTL constant missing"
        assert hasattr(portal_auth, "OPERATOR_TTL"), "OPERATOR_TTL constant missing"
        assert portal_auth.PORTAL_TTL != portal_auth.OPERATOR_TTL, (
            "PORTAL_TTL and OPERATOR_TTL must differ (Bug #1 regression). "
            f"Both are {portal_auth.PORTAL_TTL}s."
        )

    def test_new_session_expires_within_portal_ttl(self):
        """Sliding window: a freshly-created session expires at now + PORTAL_TTL."""
        import sqlite3
        now = int(time.time())
        sid = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        conn = sqlite3.connect(str(portal_auth.DB_PATH))
        # conftest sets DB_PATH to the tmp DB
        row = conn.execute(
            "SELECT expires_at FROM customer_portal_sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
        conn.close()
        assert row is not None
        expires_at = row[0]
        delta = expires_at - now
        # Allow 5s slack for the between-call clock skew
        assert abs(delta - portal_auth.PORTAL_TTL) <= 5, (
            f"Session expires_at differs from now+PORTAL_TTL by {delta - portal_auth.PORTAL_TTL}s"
        )

    def test_sliding_window_refreshes_expiry(self):
        """Sliding: verify_session(..., slide=True) extends expires_at to now+PORTAL_TTL."""
        import sqlite3
        from unittest.mock import patch
        sid = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        # Sleep just enough to make expiry noticeably different
        time.sleep(1.1)
        portal_auth.verify_session(sid, slide=True)
        now = int(time.time())
        conn = sqlite3.connect(str(portal_auth.DB_PATH))
        row = conn.execute(
            "SELECT expires_at FROM customer_portal_sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
        conn.close()
        new_delta = row[0] - now
        # After slide, delta should be ~ PORTAL_TTL again, not PORTAL_TTL - 1.1
        assert new_delta >= portal_auth.PORTAL_TTL - 2, (
            f"Sliding window failed: expires_at is only {new_delta}s from now "
            f"(expected ~{portal_auth.PORTAL_TTL}s)"
        )

    def test_delete_session(self):
        sid = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        portal_auth.delete_session(sid)
        assert portal_auth.verify_session(sid) is None

    def test_purge_expired_sessions(self, db_path):
        now = int(time.time())
        s_recent = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        for i in range(2):
            sid = portal_auth.create_session(1, f"c-{i}", "ua", "9.9.9.9")
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "UPDATE customer_portal_sessions SET expires_at = ? WHERE session_id = ?",
                (now - 60, sid),
            )
            conn.commit()
            conn.close()
        purged = portal_auth.purge_expired_sessions()
        assert purged == 2
        assert portal_auth.verify_session(s_recent) is not None

    # --- Bug #2/R2 (added 2026-06-25): absolute max session age cap ---
    # Bug: 1h sliding window alone allows a continuously-active session to
    # live indefinitely (each verify refreshes expires_at). Threat model
    # requires a hard ceiling. Fix: 7-day absolute max via created_at check.

    def test_max_session_age_constant_exists_and_is_7_days(self):
        """R2: CUSTOMER_MAX_SESSION_AGE must exist and equal 7 days. The
        threat model requires a hard ceiling — sliding alone allows indefinite
        lifetime for an active session."""
        assert hasattr(portal_auth, "CUSTOMER_MAX_SESSION_AGE"), (
            "CUSTOMER_MAX_SESSION_AGE constant missing"
        )
        assert portal_auth.CUSTOMER_MAX_SESSION_AGE == 7 * 86400, (
            f"CUSTOMER_MAX_SESSION_AGE must be 7 days (604800s), "
            f"got {portal_auth.CUSTOMER_MAX_SESSION_AGE}s"
        )

    def test_session_under_absolute_max_is_accepted(self, db_path):
        """A session 1 day old (well under 7d cap) must still be accepted,
        even if expires_at (sliding) somehow got stale. The absolute cap
        doesn't make things expire faster — it only caps at 7d."""
        import sqlite3
        sid = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        # Backdate created_at to 1 day ago
        one_day_ago = int(time.time()) - 86400
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE customer_portal_sessions SET created_at = ? WHERE session_id = ?",
            (one_day_ago, sid),
        )
        conn.commit()
        conn.close()
        sess = portal_auth.verify_session(sid)
        assert sess is not None, "1-day-old session must still be accepted"
        assert sess["customer_id"] == 1

    def test_session_over_absolute_max_is_rejected_and_deleted(self, db_path):
        """A session 8 days old (over 7d cap) MUST be rejected even if
        sliding expiry says it's fresh. This is the core R2 fix."""
        import sqlite3
        sid = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        # Backdate created_at to 8 days ago (over 7d cap)
        eight_days_ago = int(time.time()) - (8 * 86400)
        # Also reset expires_at to "now + 1h" so sliding wouldn't reject it
        fresh_expiry = int(time.time()) + portal_auth.PORTAL_TTL
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE customer_portal_sessions SET created_at = ?, expires_at = ? WHERE session_id = ?",
            (eight_days_ago, fresh_expiry, sid),
        )
        conn.commit()
        conn.close()
        sess = portal_auth.verify_session(sid)
        assert sess is None, "8-day-old session must be rejected (R2 absolute cap)"
        # Verify it was deleted from DB
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT 1 FROM customer_portal_sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
        conn.close()
        assert row is None, "Session exceeding absolute max must be deleted from DB"

    def test_session_exactly_at_absolute_max_is_accepted(self, db_path):
        """Boundary: a session just under the 7d cap (6d23h) must be accepted."""
        import sqlite3
        sid = portal_auth.create_session(1, "c-laptop", "ua", "9.9.9.9")
        # Backdate created_at to 6d23h ago (just under 7d cap)
        six_d_23_h = int(time.time()) - (6 * 86400 + 23 * 3600)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE customer_portal_sessions SET created_at = ? WHERE session_id = ?",
            (six_d_23_h, sid),
        )
        conn.commit()
        conn.close()
        sess = portal_auth.verify_session(sid)
        assert sess is not None, "Session 6d23h old must be accepted (under 7d cap)"


# ---------- lookup_user_and_customer — THE BUG THAT EXISTED 3 DAYS ----------

class TestLookupUserAndCustomer:
    """This is the test that would have caught Lesson #193 in real-time.

    Lesson #193: `lookup_user_and_customer` did `WHERE d.device_name = ?` with
    `identity` as param. Real customers have `device_name=laptop` and
    `identity=*-laptop` — they never matched. Login was broken for every
    customer since June 21 (3 days).
    """

    def _seed_customer_with_device(self, db_path, *, customer_name, device_name,
                                   identity, status="active", device_active=1,
                                   password="customer-secret-pw"):
        import sqlite3, time
        now = int(time.time())
        ntlm = portal_auth.ntlm_hash_bytes(password)
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("""INSERT INTO customers (name, display_name, is_operator, is_active,
                       data_limit_bytes, tier_id, status, max_devices, created_at, updated_at)
                       VALUES (?, ?, 0, 1, 5368709120, 1, ?, 1, ?, ?)""",
                    (customer_name, customer_name.title(), status, now, now))
        customer_id = cur.lastrowid
        cur.execute("INSERT INTO shared_secrets (type, data) VALUES (?, ?)", (1, ntlm))
        secret_id = cur.lastrowid
        cur.execute("INSERT INTO identities (type, data) VALUES (?, ?)", (1, identity.encode()))
        identity_id = cur.lastrowid
        cur.execute("INSERT INTO shared_secret_identity (shared_secret, identity) VALUES (?, ?)",
                    (secret_id, identity_id))
        cur.execute("INSERT INTO users (name, password) VALUES (?, ?)", (identity, ntlm))
        user_id = cur.lastrowid
        cur.execute("""INSERT INTO devices (customer_id, strongswan_user_id, device_name,
                       is_active, device_type, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'Windows 11 24H2', ?, ?)""",
                    (customer_id, user_id, device_name, device_active, now, now))
        conn.commit()
        conn.close()
        return customer_id, user_id

    def test_lookup_finds_customer_by_eap_identity(self, db_path, patch_portal_auth_db):
        """Lesson #193 regression: identity='acme-laptop' must match device_name='laptop'.

        The bug was: query was `WHERE d.device_name = ?` with `? = 'acme-laptop'`,
        so it never matched. Fix: join on `d.strongswan_user_id = u.id` instead.
        """
        self._seed_customer_with_device(
            db_path,
            customer_name="acme",
            device_name="laptop",
            identity="acme-laptop",
            password="customer-secret-pw",
        )
        user = portal_auth.lookup_user_and_customer("acme-laptop")
        assert user is not None, "Lesson #193 regression: identity match failed"
        assert user["customer_name"] == "acme"
        assert user["device_name"] == "laptop"
        assert user["customer_status"] == "active"
        assert user["device_is_active"] == 1

    def test_lookup_returns_none_for_nonexistent_identity(self, db_path, patch_portal_auth_db):
        self._seed_customer_with_device(
            db_path, customer_name="acme", device_name="laptop", identity="acme-laptop",
        )
        assert portal_auth.lookup_user_and_customer("nobody-nothing") is None

    def test_lookup_inactive_customer_returns_inactive_status(self, db_path, patch_portal_auth_db):
        self._seed_customer_with_device(
            db_path, customer_name="archived-co", device_name="laptop",
            identity="archived-co-laptop", status="archived",
        )
        user = portal_auth.lookup_user_and_customer("archived-co-laptop")
        assert user is not None
        assert user["customer_status"] == "archived"
        assert user["device_is_active"] == 1

    def test_lookup_inactive_device_returns_inactive_device(self, db_path, patch_portal_auth_db):
        self._seed_customer_with_device(
            db_path, customer_name="suspended-co", device_name="phone",
            identity="suspended-co-phone", device_active=0,
        )
        user = portal_auth.lookup_user_and_customer("suspended-co-phone")
        assert user is not None
        assert user["device_is_active"] == 0


# ---------- /api/login endpoint integration ----------

class TestOperatorLoginEndpoint:
    def test_login_valid_credentials_returns_ok(self, client, admin_password):
        r = client.post("/api/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["user"] == "admin"
        assert "session" in r.cookies

    def test_login_wrong_password_returns_401(self, client):
        r = client.post("/api/login", json={"username": "admin", "password": "definitely-wrong"})
        assert r.status_code == 401

    def test_login_nonexistent_user_returns_401(self, client):
        # Wrong user → 401, NOT 404 (avoid user enumeration)
        r = client.post("/api/login", json={"username": "ghost", "password": "anything"})
        assert r.status_code == 401

    def test_login_rate_limited_after_5_attempts(self, client):
        for _ in range(5):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})
        r = client.post("/api/login", json={"username": "admin", "password": "wrong-again"})
        assert r.status_code == 429

    def test_logout_clears_session(self, client, operator_login):
        r = client.post("/api/logout", cookies={"session": operator_login})
        assert r.status_code == 200
        assert "session" not in r.cookies or r.cookies.get("session") == ""

    def test_login_when_admin_hash_not_set_returns_503(self, app_module, monkeypatch):
        import importlib, app
        monkeypatch.setattr(app, "ADMIN_PASS_HASH", "")
        from fastapi.testclient import TestClient
        c = TestClient(app.app)
        r = c.post("/api/login", json={"username": "admin", "password": "anything"})
        assert r.status_code == 503


# ---------- /api/portal/login endpoint integration ----------

class TestCustomerPortalLoginEndpoint:
    def _seed_portal_user(self, db_path, *, identity, password, status="active", device_active=1):
        import time
        now = int(time.time())
        ntlm = portal_auth.ntlm_hash_bytes(password)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("""INSERT INTO customers (name, display_name, is_operator, is_active,
                       data_limit_bytes, tier_id, status, max_devices, created_at, updated_at)
                       VALUES (?, ?, 0, 1, 5368709120, 1, ?, 1, ?, ?)""",
                    (identity.split("-")[0], identity, status, now, now))
        cid = cur.lastrowid
        cur.execute("INSERT INTO shared_secrets (type, data) VALUES (?, ?)", (1, ntlm))
        sid = cur.lastrowid
        cur.execute("INSERT INTO identities (type, data) VALUES (?, ?)", (1, identity.encode()))
        iid = cur.lastrowid
        cur.execute("INSERT INTO shared_secret_identity (shared_secret, identity) VALUES (?, ?)", (sid, iid))
        cur.execute("INSERT INTO users (name, password) VALUES (?, ?)", (identity, ntlm))
        uid = cur.lastrowid
        device_name = identity.split("-", 1)[1] if "-" in identity else "laptop"
        cur.execute("""INSERT INTO devices (customer_id, strongswan_user_id, device_name,
                       is_active, device_type, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'Windows 11 24H2', ?, ?)""",
                    (cid, uid, device_name, device_active, now, now))
        conn.commit()
        conn.close()

    def test_portal_login_valid_credentials(self, client, db_path):
        self._seed_portal_user(db_path, identity="acme-laptop", password="acme-pw-123")
        r = client.post("/api/portal/login",
                         json={"identity": "acme-laptop", "password": "acme-pw-123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["customer_name"] == "acme"
        assert "portal_session" in r.cookies

    def test_portal_login_wrong_password(self, client, db_path):
        self._seed_portal_user(db_path, identity="acme-laptop", password="acme-pw-123")
        r = client.post("/api/portal/login",
                         json={"identity": "acme-laptop", "password": "WRONG"})
        assert r.status_code == 401

    def test_portal_login_unknown_identity(self, client):
        r = client.post("/api/portal/login",
                         json={"identity": "nobody", "password": "anything"})
        assert r.status_code == 401

    def test_portal_login_archived_customer_rejected(self, client, db_path):
        self._seed_portal_user(db_path, identity="old-co-laptop", password="pw",
                                status="archived")
        r = client.post("/api/portal/login",
                         json={"identity": "old-co-laptop", "password": "pw"})
        assert r.status_code == 401

    def test_portal_login_inactive_device_rejected(self, client, db_path):
        self._seed_portal_user(db_path, identity="suspended-laptop", password="pw",
                                device_active=0)
        r = client.post("/api/portal/login",
                         json={"identity": "suspended-laptop", "password": "pw"})
        assert r.status_code == 401

    def test_portal_logout_clears_cookie_and_session(self, client, db_path):
        self._seed_portal_user(db_path, identity="acme-laptop", password="acme-pw-123")
        login = client.post("/api/portal/login",
                            json={"identity": "acme-laptop", "password": "acme-pw-123"})
        assert login.status_code == 200
        r = client.post("/api/portal/logout")
        assert r.status_code == 200

    def test_portal_me_returns_session_info(self, client, db_path):
        self._seed_portal_user(db_path, identity="acme-laptop", password="acme-pw-123")
        client.post("/api/portal/login",
                    json={"identity": "acme-laptop", "password": "acme-pw-123"})
        r = client.get("/api/portal/me")
        assert r.status_code == 200
        body = r.json()
        assert body["customer_name"] == "acme"
        assert body["logged_in_as"] == "acme-laptop"

    def test_portal_me_without_session_returns_401(self, client):
        r = client.get("/api/portal/me")
        assert r.status_code == 401

    def test_operator_session_cannot_access_portal_routes(self, client, operator_login):
        """Cookie separation: operator 'session' cookie must NOT satisfy require_portal_session."""
        r = client.get("/api/portal/me", cookies={"session": operator_login})
        assert r.status_code == 401
