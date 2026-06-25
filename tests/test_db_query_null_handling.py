"""
test_db_query_null_handling.py — Regression test for the 'None' string bug.

Bug (2026-06-25, caught by Zun):
  `db_query()` uses `sqlite3 -json` which serializes SQL NULL as the literal
  string "None". That meant /api/customers returned:
      {"telegram_username": "None", "email": "None", "billing_id": "None"}
  instead of real null. The Edit Customer modal pre-filled with literal "None"
  text, then on Save sent `"email": "None"` to the backend, which failed email
  regex validation with 400. Operator saw no toast change and concluded
  "this doesn't allow me to save".

  Fix: db_query now converts "None" string back to None at the boundary.

  These tests directly exercise the conversion logic by mocking ssh_903 to
  return sqlite3 -json-style output (literal "None" strings), then asserting
  db_query returns proper nulls. Without the fix, the assertions fail.
"""

import json
import sqlite3
import time


def _make_sqlite3_cli_json(db_path, sql):
    """Mimic what `ssh ... sqlite3 -json /path "SQL"` returns on the wire.

    sqlite3 CLI serializes NULL as the literal string "None" — that's the bug.
    Python's json.dumps (used by conftest's fake) serializes NULL as null.
    This helper reproduces the broken CLI output so the test actually
    exercises the fix.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        # Render like sqlite3 -json: NULL → "None" (the bug). Strings get
        # quoted like JSON strings, ints/bools go bare.
        def _render(v):
            if v is None:
                return '"None"'  # the bug: sqlite3 -json quotes NULL as the string "None"
            if isinstance(v, (bytes, bytearray)):
                return f'"{v.hex()}"'  # bytes → quoted hex string
            if isinstance(v, (int, float, bool)):
                return str(v)
            return json.dumps(str(v))  # quoted JSON string
        return "[" + ",".join(
            "{" + ",".join(f'"{k}":{_render(v)}' for k, v in zip(cols, r)) + "}"
            for r in rows
        ) + "]"
    finally:
        conn.close()


class TestDbQueryNullConversion:
    """db_query must convert sqlite3 -json's literal 'None' strings back to None."""

    def _insert_test_customer(self, db_path, name, telegram, email, billing_id, notes, display_name=None):
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM customers WHERE name=?", (name,))
            ts = int(time.time())
            cur.execute("""
                INSERT INTO customers
                    (name, display_name, telegram_username, email, billing_id, notes,
                     is_active, status, data_limit_bytes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 'active', 104857600, ?, ?)
            """, (name, display_name, telegram, email, billing_id, notes, ts, ts))
            conn.commit()
        finally:
            conn.close()

    def test_db_query_converts_None_string_to_None(self, app_module, db_path, monkeypatch):
        """The exact bug: a null DB field came back as 'None' string over sqlite3 CLI.

        We override ssh_903 to return the same JSON format that sqlite3 -json
        produces (with NULL rendered as the literal string 'None'), and assert
        db_query normalizes it back to None.
        """
        self._insert_test_customer(
            db_path, "null-test",
            telegram=None, email=None, billing_id=None, notes=None,
        )

        # Mimic sqlite3 -json over the wire: NULL becomes the string "None"
        fake_wire = _make_sqlite3_cli_json(
            db_path,
            "SELECT telegram_username, email, billing_id, notes FROM customers WHERE name='null-test';",
        )
        # Sanity: the wire format MUST contain the literal "None" string — that's the bug
        assert '"None"' in fake_wire, f"test setup error: wire format doesn't simulate the bug. Got: {fake_wire}"

        monkeypatch.setattr(app_module, "ssh_903", lambda cmd_args, **kw: fake_wire)

        rows = app_module.db_query(
            "SELECT telegram_username, email, billing_id, notes FROM customers WHERE name='null-test';"
        )
        assert len(rows) == 1
        r = rows[0]
        for k in ("telegram_username", "email", "billing_id", "notes"):
            assert r[k] is None, (
                f"{k} should be None but got {r[k]!r} (type {type(r[k]).__name__})"
            )

    def test_db_query_preserves_string_None_substring(self, app_module, db_path, monkeypatch):
        """Defensive: a real string value that CONTAINS 'None' must NOT be clobbered.

        The fix uses exact-match (v == 'None'), so 'NoneType' or 'None.' would
        not be touched. This test ensures we don't accidentally over-broaden
        the conversion (e.g. with a regex or substring match).
        """
        self._insert_test_customer(
            db_path, "none-substring-test",
            telegram=None, email=None, billing_id=None, notes=None,
            display_name="NoneType",  # legitimate string containing "None"
        )

        fake_wire = _make_sqlite3_cli_json(
            db_path,
            "SELECT display_name, telegram_username FROM customers WHERE name='none-substring-test';",
        )
        monkeypatch.setattr(app_module, "ssh_903", lambda cmd_args, **kw: fake_wire)

        rows = app_module.db_query(
            "SELECT display_name, telegram_username FROM customers WHERE name='none-substring-test';"
        )
        assert len(rows) == 1
        assert rows[0]["display_name"] == "NoneType", (
            f"Real string 'NoneType' was clobbered to {rows[0]['display_name']!r}"
        )
        assert rows[0]["telegram_username"] is None

    def test_db_query_preserves_lowercase_none(self, app_module, db_path, monkeypatch):
        """Only the literal string 'None' (sqlite3 CLI's exact format) is converted.
        Lowercase 'none' (a real value someone might store) must be preserved."""
        self._insert_test_customer(
            db_path, "none-case-test",
            telegram="none", email=None, billing_id=None, notes=None,
        )

        fake_wire = _make_sqlite3_cli_json(
            db_path,
            "SELECT telegram_username FROM customers WHERE name='none-case-test';",
        )
        monkeypatch.setattr(app_module, "ssh_903", lambda cmd_args, **kw: fake_wire)

        rows = app_module.db_query(
            "SELECT telegram_username FROM customers WHERE name='none-case-test';"
        )
        assert len(rows) == 1
        assert rows[0]["telegram_username"] == "none", (
            f"Lowercase 'none' was wrongly converted to {rows[0]['telegram_username']!r}"
        )


class TestListCustomersReturnsProperNulls:
    """The /api/customers endpoint must return JSON null (not 'None' string)."""

    def test_list_customers_returns_null_for_unset_optional_fields(
        self, client, db_path, operator_login, monkeypatch
    ):
        """Hit /api/customers and assert telegram/email/billing_id are JSON null,
        not the string 'None'. Mocks ssh_903 to return the buggy sqlite3 -json
        format, then asserts the conversion still produces proper nulls."""
        import app
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM customers WHERE name='api-null-test'")
            ts = int(time.time())
            cur.execute("""
                INSERT INTO customers
                    (name, telegram_username, email, billing_id, notes,
                     is_active, status, data_limit_bytes, created_at, updated_at)
                VALUES (?, NULL, NULL, NULL, NULL, 1, 'active', 104857600, ?, ?)
            """, ("api-null-test", ts, ts))
            conn.commit()
        finally:
            conn.close()

        # Mock all sqlite3 -json calls to use the CLI-style renderer (NULL → "None")
        def fake_ssh(cmd_args, **kw):
            cmd_str = " ".join(str(a) for a in cmd_args)
            # Pull the SQL out of the last arg (it's a single string after `ssh`)
            sql = cmd_args[-1] if cmd_args else ""
            return _make_sqlite3_cli_json(db_path, sql)

        monkeypatch.setattr(app, "ssh_903", fake_ssh)

        r = client.get("/api/customers")
        assert r.status_code == 200, f"got {r.status_code}: {r.text}"
        customers = r.json()
        target = next((c for c in customers if c["name"] == "api-null-test"), None)
        assert target is not None, "customer 'api-null-test' not in response"
        for k in ("telegram_username", "email", "billing_id"):
            assert target[k] is None, (
                f"list endpoint returned {k}={target[k]!r} "
                f"(type {type(target[k]).__name__}); should be JSON null"
            )