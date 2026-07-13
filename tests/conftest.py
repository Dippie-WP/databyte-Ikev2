"""
conftest.py — shared pytest fixtures for portal integration tests.

Strategy: intercept `subprocess.run` BEFORE the app module is imported,
because app.py's body runs `installer_tokens.register()` at import time
which calls ssh_903 (which calls subprocess.run with `ssh ... sqlite3 ...`).

The fake `subprocess.run` parses the shell-quoted SQL from the remote
command and runs it against a tmp SQLite DB seeded from real schemas.
"""
import contextlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Repo root on path so we can `import app`, `import portal_auth`, `import installer_tokens`
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "host" / "vpn-portal"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# app.py requires VPN_HOST env var (fail-fast RuntimeError at import time
# since commit 2022736, which closed the 192.168.10.98 leak). CI doesn't set it,
# so set a safe default before any test imports app. Tests that exercise
# subprocess.run mock ssh commands anyway — they don't actually connect.
os.environ.setdefault("VPN_HOST", "127.0.0.1")
os.environ.setdefault("SSH_KEY", "/dev/null")  # tests mock subprocess.run
os.environ.setdefault("DB_PATH", "/tmp/test_ipsec.db")


# ---------- MariaDB → SQLite UDF bridge ----------
# Phase 4E cutover (commit 805ea84) made app.py / portal_auth use MariaDB
# syntax in SQL: UNIX_TIMESTAMP() for "now epoch", UNIX_TIMESTAMP(string) for
# "parse datetime", etc. Tests still use SQLite fixtures (see _test_db_sqlite
# in patch_app_module below) because spinning up MariaDB in CI for every
# test would slow things down 10x. Bridge the dialect gap by registering
# the small set of MariaDB functions app code uses as SQLite UDFs. The
# behaviour matches MariaDB closely enough for assertion purposes.
_orig_sqlite3_connect = sqlite3.connect

def _patched_sqlite3_connect(*args, **kwargs):
    conn = _orig_sqlite3_connect(*args, **kwargs)
    # UNIX_TIMESTAMP()  → epoch seconds (no args)
    # UNIX_TIMESTAMP(ts) → epoch seconds from a datetime string
    def _unix_timestamp(*a):
        if not a:
            return int(time.time())
        v = a[0]
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            # MariaDB accepts "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return int(time.mktime(time.strptime(v, fmt)))
                except ValueError:
                    continue
            return int(time.time())  # unparseable → now
        return int(time.time())
    conn.create_function("UNIX_TIMESTAMP", -1, _unix_timestamp)
    return conn

sqlite3.connect = _patched_sqlite3_connect


# ---------- DB schema ----------

@pytest.fixture
def db_path(tmp_path) -> Path:
    """Create a fresh SQLite DB with all schemas applied."""
    db = tmp_path / "test_ipsec.db"
    conn = sqlite3.connect(db)
    for schema_file in (
        "strongswan-schema.sql",
        "quota-schema.sql",
        "test-users-extension.sql",
        "portal-schema.sql",
        "portal-customers-extensions.sql",
        "portal-user-id-fk.sql",
    ):
        sql = (FIXTURES / schema_file).read_text()
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError:
            # Some ALTER TABLE ADD COLUMN statements fail if already applied.
            # Tolerate.
            pass
    now = int(time.time())
    conn.executescript(f"""
        INSERT INTO tiers (name, display_name, data_limit_bytes, is_active, created_at, notes)
        VALUES
            ('tier_5gb',  'Tier 1 — 5GB / $3 USD',  5368709120,  1, {now}, 'seed'),
            ('tier_10gb', 'Tier 2 — 10GB / $5 USD', 10737418240, 1, {now}, 'seed'),
            ('tier_20gb', 'Tier 3 — 20GB / $8 USD', 21474836480, 1, {now}, 'seed'),
            ('demo_100mb','Demo 100MB',            104857600,  1, {now}, 'seed');

        INSERT INTO customers (name, display_name, is_operator, is_active, data_limit_bytes,
                               tier_id, status, max_devices, created_at, updated_at, notes)
        VALUES
            ('admin', 'Admin', 1, 1, 0, NULL, 'active', 1, {now}, {now}, 'seed operator');

        INSERT INTO pools (name, start, end, timeout)
        VALUES ('databyte-pool', X'0A630001', X'0A6300FE', 86400);
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def rw_eap_conf(tmp_path) -> Path:
    """A writable rw-eap.conf starting with the standard strongswan block."""
    conf = tmp_path / "rw-eap.conf"
    conf.write_text("""# rw-eap.conf — IKEv2 EAP connections
connections {
  rw-eap {
    version = 2
    proposals = aes256-sha256-modp2048, aes128-sha256-modp2048
    local {
      auth = pubkey
      certs = server.pem
    }
    remote {
      auth = eap-mschapv2
      eap_id = %any
    }
    children {
      rw-eap {
        mode = tunnel
        local_ts = 0.0.0.0/0
        remote_ts = 0.0.0.0/0
        esp_proposals = aes256-sha256, aes128-sha256
      }
    }
  }
}
""")
    return conf


# ---------- portal_auth DB patching ----------

@pytest.fixture
def patch_portal_auth_db(db_path, monkeypatch, rw_eap_conf):
    """portal_auth._db() reads module-level DB_URL (Phase 4A) via SQLAlchemy.
    Patch DB_URL to sqlite + override _db() with a sqlite version for tests.

    Phase 4E removed _sqlite_query (portal data unified into MariaDB), so this
    fixture no longer needs to intercept subprocess.run for portal-auth reads.
    """
    import portal_auth

    # 1. Patch DB_URL + _db() so RADIUS data reads use sqlite.
    monkeypatch.setattr(portal_auth, "DB_URL", f"sqlite:///{db_path}")

    @contextlib.contextmanager
    def _test_db():
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(portal_auth, "_db", _test_db)



# ---------- App + TestClient ----------

@pytest.fixture
def app_module(db_path, rw_eap_conf, request):
    """The portal FastAPI app with subprocess.run intercepted.

    app.py's body at import time runs installer_tokens.register() which calls
    ssh_903 (which calls subprocess.run with a `ssh ... sqlite3 ...` argv).
    We intercept subprocess.run BEFORE app is imported.
    """
    import subprocess as _subprocess

    def _parse_sqlite_call(cmd_str: str) -> tuple[str, str]:
        """Extract (db_path, sql) from the shell-quoted remote arg in cmd_str.

        ssh_903 builds the remote cmd by shq-wrapping each arg. When the WHOLE
        SSH argv (cmd_str) is shlex.split, the embedded quoted strings are
        de-quoted. So the SQL comes out as a single outer token.

        Layout of outer_tokens for a sqlite3 call:
            ['ssh', '-i', KEY, ..., 'root@HOST', 'sqlite3', [-json], '/path', 'SQL_STRING']
        where the LAST token is the SQL string, with embedded 'tier_5gb' literals
        preserved as plain text.
        """
        try:
            outer_tokens = shlex.split(cmd_str)
            if "sqlite3" not in outer_tokens:
                return "", ""
            idx = outer_tokens.index("sqlite3")
            # SQL is the token AFTER sqlite3 + optional -json + db path
            sql_idx = idx + 1
            if sql_idx < len(outer_tokens) and outer_tokens[sql_idx] == "-json":
                sql_idx += 1
            if sql_idx < len(outer_tokens):
                db_arg = outer_tokens[sql_idx]
                sql_idx += 1
            else:
                db_arg = ""
            # SQL is everything from sql_idx onward, rejoined with single space.
            # In practice the SQL is a single token (no spaces were in it after
            # shq unwrapping), but be defensive.
            sql = " ".join(outer_tokens[sql_idx:]) if sql_idx < len(outer_tokens) else ""
            return db_arg, sql
        except Exception:
            return "", ""

    def fake_run(cmd_args, *args, **kwargs):
        cmd = cmd_args if isinstance(cmd_args, list) else (
            cmd_args.split() if isinstance(cmd_args, str) else []
        )
        cmd_str = " ".join(str(c) for c in cmd)

        if cmd and cmd[0] == "ssh":
            if "sqlite3" in cmd_str:
                _db_arg, sql = _parse_sqlite_call(cmd_str)
                c = sqlite3.connect(str(db_path))
                try:
                    cur = c.cursor()
                    if sql.strip().upper().startswith(("SELECT", "PRAGMA", "WITH")):
                        cur.execute(sql)
                        cols = [d[0] for d in cur.description] if cur.description else []
                        rows = cur.fetchall()
                        # Coerce bytes to hex string (JSON-serializable).
                        # E.g. users.password (BLOB) becomes hex of the bytes.
                        def _coerce(v):
                            if isinstance(v, (bytes, bytearray)):
                                return v.hex()
                            return v
                        out = json.dumps([{k: _coerce(v) for k, v in zip(cols, r)} for r in rows])
                    else:
                        if ";" in sql and "\n" in sql:
                            cur.executescript(sql)
                        else:
                            cur.execute(sql)
                        c.commit()
                        out = ""
                finally:
                    c.close()
                class _R:
                    returncode = 0
                    stdout = out
                    stderr = ""
                return _R()
            if "rw-eap.conf" in cmd_str:
                # write_rw_eap_conf uses subprocess.run directly with "cat > ..." or "tee"
                # We catch both. The "cat >" form passes content via stdin (input=).
                if "tee" in cmd_str or "cat >" in cmd_str:
                    stdin_text = kwargs.get("input") or ""
                    rw_eap_conf.write_text(
                        stdin_text if isinstance(stdin_text, str) else stdin_text.decode()
                    )
                    class _R:
                        returncode = 0
                        stdout = ""
                        stderr = ""
                    return _R()
                # cat (no redirect) / read / mkdir / cp / etc.
                # The cmd_str has shell-quoted tokens like 'cat' '/path'. Check for
                # 'cat' as a separate quoted token (not 'cat >' which is write).
                is_read = bool(re.search(r"'cat'\s+'[^']*rw-eap\.conf'", cmd_str))
                out = rw_eap_conf.read_text() if is_read else ""
                class _R:
                    returncode = 0
                    stdout = out
                    stderr = ""
                return _R()
            if "swanctl" in cmd_str:
                class _R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return _R()
            # Other SSH commands (firewall-cmd, ipban-ctl, etc.) — return empty
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()

        # Not an ssh command — pass through
        return _orig_run(cmd_args, *args, **kwargs)

    _orig_run = _subprocess.run
    _subprocess.run = fake_run

    # Drop `app` and `installer_tokens` from sys.modules so app.py body re-runs
    # (its installer_tokens.register() at import time needs the patched subprocess).
    # Keep `portal_auth` cached: the test file imports it at module load time,
    # and monkeypatch.setattr from patch_portal_auth_db needs a stable module.
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("app", "installer_tokens"):
            del sys.modules[mod_name]

    # Patch DB_PATH on the (cached) portal_auth module
    import portal_auth
    portal_auth.DB_PATH = str(db_path)
    # Phase 4A: portal_auth also has DB_URL (MariaDB SQLAlchemy) + _db() that calls _engine().
    # Tests don't have a real MariaDB, so point DB_URL at a sqlite file + override _db()
    # with the sqlite version (same pattern as patch_portal_auth_db fixture).
    import sqlite3 as _sqlite3
    portal_auth.DB_URL = f"sqlite:///{db_path}"

    @contextlib.contextmanager
    def _test_db_sqlite():
        conn = _sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.row_factory = _sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    portal_auth._db = _test_db_sqlite

    import app

    # Teardown
    def _restore_subprocess():
        _subprocess.run = _orig_run
    request.addfinalizer(_restore_subprocess)

    app.ADMIN_PASS_HASH = portal_auth.hash_operator_password("test-admin-pw-12345")
    app.COOKIE_SECURE = "false"
    # Reset rate limiter state between tests (operator + portal)
    app._login_attempts.clear()
    portal_auth._portal_login_attempts.clear()
    return app


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


@pytest.fixture
def operator_login(client, admin_password):
    r = client.post("/api/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, f"operator login failed: {r.status_code} {r.text}"
    return r.cookies.get("session")


@pytest.fixture
def admin_password() -> str:
    return "test-admin-pw-12345"
