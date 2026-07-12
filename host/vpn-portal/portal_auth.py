"""
v1.3.0 — Customer portal auth (NTC).

Customers log in at /portal/ with their VPN credentials (EAP identity + password).
Same secret that charon uses for MSCHAPv2 — no new secrets stored.

Auth flow:
  1. Customer submits {identity, password}
  2. Look up users row by name (identity == users.name)
  3. Look up the device for that user (devices.device_name == users.name)
  4. Get the customer_id from the device
  5. Verify password against the stored NTLM hash (constant-time compare)
  6. Issue a session cookie scoped to Path=/portal/ — CANNOT access operator routes

Isolation guarantees:
  - Cookie Path=/portal/ → browser doesn't attach it to /api/* (operator paths)
  - /api/portal/* routes have their own require_portal_session dep that ONLY
    accepts cookies with key "portal_session" AND verifies customer_id from DB
  - All SQL is scoped to session.customer_id — no path takes customer_id from input
  - Operator endpoints use require_session dep that REJECTS portal_session cookie
  - Portal cookie: HttpOnly (no JS), SameSite=Strict (no cross-site), 30-day sliding
  - Login rate limit: 5 attempts/IP/min (same as operator login)
  - Audit: every login (success + fail) and every portal API call logged

Production build (v1.0.0+, 2026-06-24): live at https://vpn-portal.databyte.co.za/portal/ via Cloudflare proxy.
"""

import hashlib
import hmac
import os
import secrets
import subprocess
import time
from contextlib import contextmanager
from typing import Optional

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError, InvalidHashError

import logging
from fastapi import Cookie, HTTPException, Request, Response
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

log = logging.getLogger("vpn-portal.portal_auth")


# Portal session cookie name. Different from operator "session" cookie.
# The operator require_session dep explicitly REJECTS this cookie name.
PORTAL_COOKIE = "portal_session"

# Operator session cookie name. Distinct from portal_session for the same
# reason (separation of concerns, defense in depth).
OPERATOR_COOKIE = "session"

# 1h sliding expiry for the customer portal.
#
# Threat model: customer portal session grants ability to:
#   - view usage / data burned
#   - download mobileconfig / Windows installer with embedded EAP creds
#   - reset EAP password (v1.3.x feature)
# Stolen phone + stolen cookie = full account takeover until expiry.
# 1h sliding window limits blast radius to a single coffee-shop session.
# Was 30 days until 2026-06-24 fix (Bug #1: portal idle expiry 30d).
PORTAL_TTL = 3600

# Absolute maximum lifetime for a customer portal session (Bug #2/R2 fix 2026-06-25).
#
# Without this, a session that stays active (slides forward every hour) can
# live indefinitely — a single continuously-used session could persist for
# months, defeating the point of "≤ 1h sliding window". The absolute cap
# forces a full re-auth after 7 days even if the user is active every hour.
# Threat model: passive observation of a long-lived cookie is a higher-value
# target than an active 1h sliding session.
# Computed from `created_at` so no DB schema change needed.
CUSTOMER_MAX_SESSION_AGE = 7 * 86400  # 7 days in seconds

# Operator session TTL — 8h sliding. After 8h inactivity, operator must re-auth.
# Longer because operators need to manage customers throughout a workday.
OPERATOR_TTL = 8 * 3600

# Login rate limit (per IP per minute). Same as operator login.
PORTAL_RATE_LIMIT = 5

# Argon2id parameters per OWASP 2026 Password Storage Cheat Sheet:
# - memory_cost: 19 MiB
# - time_cost: 2 iterations
# - parallelism: 1
# https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
_ARGON2 = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,   # 19 MiB in KiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


# ---------- Operator password hashing (Argon2id) ----------

def hash_operator_password(password: str) -> str:
    """Hash an operator password using Argon2id with OWASP 2026 parameters.

    Returns the encoded hash string (includes salt + parameters). Safe to store
    in DB or env file. ~70ms per hash on modern hardware.
    """
    return _ARGON2.hash(password)


def verify_operator_password(stored_hash: str, submitted: str) -> bool:
    """Constant-time verify of submitted password against stored Argon2id hash.

    Returns False on any error (wrong hash, malformed stored hash, etc.) to
    avoid leaking which failure mode occurred.
    """
    if not stored_hash or not submitted:
        return False
    try:
        _ARGON2.verify(stored_hash, submitted)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def operator_password_needs_rehash(stored_hash: str) -> bool:
    """Check if stored hash uses outdated Argon2id parameters (for future migration)."""
    try:
        return _ARGON2.check_needs_rehash(stored_hash)
    except Exception:
        return True  # Treat malformed as needs-rehash


# ---------- Password hash helpers ----------

def ntlm_hash_bytes(pw: str) -> bytes:
    """NTLM = MD4(UTF-16-LE(password)) — 16 raw bytes, what charon stores in users.password."""
    pw_utf16 = pw.encode("utf-16-le")
    r = subprocess.run(
        ["openssl", "dgst", "-md4", "-provider", "legacy", "-binary"],
        input=pw_utf16, capture_output=True, check=True,
    )
    return r.stdout


def _stored_hash_bytes(stored) -> Optional[bytes]:
    """Decode the stored hash. Supports bytes (raw 16-byte BLOB), hex (32 chars), or text.

    The users.password column is a BLOB in charon schema. sqlite3 returns it as:
    - bytes (raw 16 bytes, older entries)  # most common
    - str (text representation; very old)
    - str (32-char hex, even older operator entries that pre-date the schema)
    """
    if not stored:
        return None
    # Case 1: raw bytes (BLOB column). If exactly 16 bytes, that is the NTLM hash.
    if isinstance(stored, (bytes, bytearray)):
        b = bytes(stored)
        if len(b) == 16:
            return b
        # Maybe it is hex-encoded as bytes
        try:
            return bytes.fromhex(b.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
    # Case 2: str (text column or text-mode)
    s = stored.strip()
    if not s:
        return None
    if len(s) == 32:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    # Case 3: str that is 16 chars of raw binary (text-mode legacy)
    try:
        raw = s.encode("latin-1")
    except UnicodeEncodeError:
        return None
    if len(raw) == 16:
        return raw
    return None


def verify_password(stored_hash: str, submitted: str) -> bool:
    """Constant-time compare of submitted password against stored NTLM hash."""
    target = _stored_hash_bytes(stored_hash)
    if not target:
        return False
    candidate = ntlm_hash_bytes(submitted)
    return hmac.compare_digest(candidate, target)


# ---------- DB helpers (SQLAlchemy + MariaDB, Phase 4) ----------

# Phase 4 (RADIUS migration): all portal data lives in MariaDB `radius` DB.
# Connection via SQLAlchemy 2.0 + PyMySQL. Loopback-only — nginx reverse-proxies
# the public-facing portal, the portal talks to MariaDB on 127.0.0.1:3306.
#
# DB_URL format: mysql+pymysql://portal:<pw>@127.0.0.1:3306/radius
# password is read from /etc/vpn-portal.env (DB_URL key) at portal start.

DB_URL = os.environ.get("DB_URL", "mysql+pymysql://portal:portal@127.0.0.1:3306/radius")


def _engine() -> Engine:
    """Return a process-wide SQLAlchemy engine. Lazy-initialized."""
    global _ENGINE
    if "_ENGINE" not in globals():
        globals()["_ENGINE"] = create_engine(
            DB_URL,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return globals()["_ENGINE"]


def _qmark_to_named(sql: str, params):
    """Convert qmark (?) placeholders to SQLAlchemy named (:p1, :p2, ...) style.

    Lets us keep the existing SQLite-style ?-param SQL strings intact while
    executing via SQLAlchemy text() which uses named params by default.
    """
    if params is None:
        return sql, {}
    if not isinstance(params, (list, tuple)):
        return sql, params
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError(
            f"Placeholder count mismatch: {len(parts) - 1} ? vs {len(params)} params"
        )
    new_sql = "".join(
        f"{parts[i]}:p{i + 1}" if i < len(params) else parts[i]
        for i in range(len(parts))
    )
    named = {f"p{i + 1}": v for i, v in enumerate(params)}
    return new_sql, named


class _DictRow:
    """dict-like row wrapper. Lets existing `row["col"]` code work unchanged.

    Wraps SQLAlchemy Row._mapping (a Mapping). Supports __getitem__ with str
    keys, and .keys() / .values() / __iter__ for completeness.
    """

    __slots__ = ("_m",)

    def __init__(self, mapping):
        self._m = mapping

    def __getitem__(self, k):
        return self._m[k]

    def get(self, k, default=None):
        return self._m.get(k, default) if hasattr(self._m, "get") else (self._m[k] if k in self._m else default)

    def __contains__(self, k):
        return k in self._m

    def __iter__(self):
        return iter(self._m)

    def keys(self):
        return self._m.keys()

    def values(self):
        return self._m.values()

    def __repr__(self):
        return f"_DictRow({dict(self._m)!r})"


class _Result:
    """Wraps a SQLAlchemy Result so .fetchone()/.fetchall() return _DictRow.

    Each Row is converted via ._mapping (a Mapping) into _DictRow, which
    behaves like sqlite3.Row: row["col"] works.
    """

    def __init__(self, result):
        self._r = result
        self.rowcount = getattr(result, "rowcount", -1)
        self.lastrowid = getattr(result, "lastrowid", None)

    def _wrap(self, row):
        if row is None:
            return None
        return _DictRow(row._mapping)

    def fetchone(self):
        return self._wrap(self._r.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._r.fetchall()]

    def fetchmany(self, size=None):
        if size is None:
            return [self._wrap(r) for r in self._r.fetchmany()]
        return [self._wrap(r) for r in self._r.fetchmany(size)]


class _Conn:
    """SQLAlchemy Connection wrapper that accepts ?-style params + dict-like rows.

    Lets every existing conn.execute("...", (param,)) call work unchanged.
    Internally converts ? → :p1/:p2/... and runs via text().
    Result rows are wrapped in _DictRow so `row["col_name"]` works (sqlite3 compat).
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        _sql, _params = _qmark_to_named(sql, params)
        return _Result(self._conn.execute(text(_sql), _params))

    def commit(self):
        self._conn.commit()


@contextmanager
def _db():
    """Context manager yielding a _Conn wrapper around a SQLAlchemy Connection.

    Replaces the sqlite3 connection context manager. Use:
        with _db() as conn:
            row = conn.execute("SELECT ... WHERE id = ?", (id,)).fetchone()
    """
    with _engine().connect() as raw:
        yield _Conn(raw)


def lookup_user_and_customer(identity: str) -> Optional[dict]:
    """Look up a user by EAP identity (= users.name) and find their customer.

    Returns dict with keys: user_id, identity, password_hash, customer_id, customer_name,
    customer_status, customer_is_operator, customer_data_*, devices info.
    Returns None if user not found or device not found.

    Phase 4E: reads from MariaDB `radius` DB (post-unification). Previously read
    from portal-local SQLite (per CORR-022 fix v1.9.2). After Phase 4E migration
    the data lives in MariaDB alongside the RADIUS tables, so we use _db() now.
    """
    # 1. Find the user row (EAP identity = users.name)
    with _db() as conn:
        user_rows = conn.execute(
            "SELECT id, name, HEX(password) AS password FROM users "
            "WHERE name = ? AND password IS NOT NULL AND LENGTH(password) > 0",
            (identity,)
        ).fetchall()
    if not user_rows:
        return None
    user_row = user_rows[0]

    # 2. Find the device row that links user -> customer
    with _db() as conn:
        device_rows = conn.execute(
            "SELECT d.id AS device_id, d.device_name, d.customer_id, d.device_type, d.os_version, d.is_active, "
            "c.name AS customer_name, c.status AS customer_status, c.is_operator AS customer_is_operator, "
            "c.tier_id, c.data_used_bytes, c.data_limit_bytes, c.over_quota, c.email, c.display_name "
            "FROM devices d JOIN customers c ON c.id = d.customer_id "
            "WHERE d.strongswan_user_id = ?",
            (int(user_row["id"]),)
        ).fetchall()
    if not device_rows:
        return None
    device_row = device_rows[0]

    return _row_to_dict({
        "user_id": user_row["id"],
        "identity": user_row["name"],
        "password_hash": user_row["password"],
        "device_id": device_row["device_id"],
        "device_name": device_row["device_name"],
        "device_type": device_row["device_type"],
        "device_os_version": device_row["os_version"],
        "device_is_active": bool(device_row["is_active"]),
        "customer_id": device_row["customer_id"],
        "customer_name": device_row["customer_name"],
        "customer_display_name": device_row["display_name"],
        "customer_status": device_row["customer_status"],
        "customer_is_operator": bool(device_row["customer_is_operator"]),
        "customer_email": device_row["email"],
        "customer_data_used_bytes": device_row["data_used_bytes"] or 0,
        "customer_data_limit_bytes": device_row["data_limit_bytes"] or 0,
        "customer_over_quota": bool(device_row["over_quota"]),
    })


def _row_to_dict(row):
    """Convert a row (dict, sqlite3.Row, _DictRow, or _mapping) to a real dict."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def lookup_customer_full(customer_id: int) -> Optional[dict]:
    """Look up customer + tier info for the portal usage endpoint. Scoped to customer_id.

    Phase 4E: reads from MariaDB (post-unification). See lookup_user_and_customer.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.display_name, c.email, c.status, c.is_operator, c.is_active, "
            "c.data_used_bytes, c.data_limit_bytes, c.over_quota, c.max_devices, c.created_at, c.updated_at, "
            "t.name AS tier_name, t.display_name AS tier_display "
            "FROM customers c LEFT JOIN tiers t ON t.id = c.tier_id "
            "WHERE c.id = ?",
            (int(customer_id),)
        ).fetchall()
    if not rows:
        return None
    return _row_to_dict(rows[0])


def list_customer_devices(customer_id: int) -> list:
    """List devices for a customer. Scoped to customer_id — caller can only see their own devices.

    Phase 4E: reads from MariaDB (post-unification).
    """
    with _db() as conn:
        return conn.execute(
            "SELECT id, device_name, device_type, os_version, hostname, is_active, last_seen_at, created_at "
            "FROM devices WHERE customer_id = ? ORDER BY id",
            (int(customer_id),)
        ).fetchall()


# ---------- Session helpers ----------

def create_session(customer_id: int, identity: str, user_agent: str, ip_address: str) -> str:
    """Create a new portal session, return the session_id token."""
    session_id = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + PORTAL_TTL
    with _db() as conn:
        conn.execute(
            "INSERT INTO customer_portal_sessions (session_id, customer_id, identity, created_at, last_active, expires_at, user_agent, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, customer_id, identity, now, now, expires, user_agent[:256], ip_address[:64])
        )
        conn.commit()
    return session_id


def verify_session(session_id: str, slide: bool = True) -> Optional[dict]:
    """Verify a session_id and return the session info. Updates last_active if slide=True.

    Bug fix 2026-07-06: same SELECT-then-UPDATE race condition as
    verify_operator_session — see that docstring. Fixed by UPDATE-first pattern.
    Also folded the absolute-age cap into the WHERE clause so the check is atomic.
    """
    now = int(time.time())
    max_age_cutoff = now - CUSTOMER_MAX_SESSION_AGE
    with _db() as conn:
        if slide:
            new_expires = now + PORTAL_TTL
            # Bug #2/R2 absolute cap is now part of the WHERE — atomic check.
            # created_at > max_age_cutoff means (now - created_at) <= CUSTOMER_MAX_SESSION_AGE.
            cur = conn.execute(
                "UPDATE customer_portal_sessions "
                "SET last_active = ?, expires_at = ? "
                "WHERE session_id = ? AND expires_at >= ? AND created_at > ?",
                (now, new_expires, session_id, now, max_age_cutoff),
            )
            if cur.rowcount == 0:
                # Either row is gone/expired OR exceeded absolute max age.
                # Best-effort cleanup of both classes.
                conn.execute(
                    "DELETE FROM customer_portal_sessions "
                    "WHERE session_id = ? AND (expires_at < ? OR created_at <= ?)",
                    (session_id, now, max_age_cutoff),
                )
                conn.commit()
                return None
        else:
            # No slide: just verify the row is still valid. UPDATE...WHERE locks
            # the row to prevent concurrent purge from racing our read.
            cur = conn.execute(
                "UPDATE customer_portal_sessions SET last_active = last_active "
                "WHERE session_id = ? AND expires_at >= ? AND created_at > ?",
                (session_id, now, max_age_cutoff),
            )
            if cur.rowcount == 0:
                return None
        row = conn.execute(
            "SELECT session_id, customer_id, identity, created_at, last_active, expires_at "
            "FROM customer_portal_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.commit()
        return dict(row) if row else None


def delete_session(session_id: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM customer_portal_sessions WHERE session_id = ?", (session_id,))
        conn.commit()


def purge_expired_sessions() -> int:
    """Delete expired sessions. Returns count deleted. Call periodically."""
    now = int(time.time())
    with _db() as conn:
        cur = conn.execute("DELETE FROM customer_portal_sessions WHERE expires_at < ?", (now,))
        conn.commit()
        return cur.rowcount


# ---------- FastAPI dependencies ----------

_portal_login_attempts: dict[str, list[float]] = {}


def _portal_rate_limit(ip: str):
    now = time.time()
    attempts = [t for t in _portal_login_attempts.get(ip, []) if now - t < 60]
    if len(attempts) >= PORTAL_RATE_LIMIT:
        raise HTTPException(429, "Too many login attempts; try again in a minute")
    _portal_login_attempts[ip] = attempts + [now]


def require_portal_session(portal_session: Optional[str] = Cookie(None)) -> dict:
    """FastAPI dep: require a valid portal session. Returns session info dict.

    Only accepts the portal_session cookie. Operator session cookies are rejected.
    """
    if not portal_session:
        raise HTTPException(401, "Not authenticated")
    info = verify_session(portal_session)
    if not info:
        raise HTTPException(401, "Invalid or expired session")
    return info


# ---------- Operator sessions (DB-backed, server-side) ----------

def create_operator_session(username: str, user_agent: str, ip_address: str) -> str:
    """Create a new operator session. Returns the session_id (random URL-safe token).

    Token format: secrets.token_urlsafe(32) — 256 bits of entropy. Stored as-is
    in the operator_sessions table; the same value goes into the cookie.
    """
    session_id = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + OPERATOR_TTL
    with _db() as conn:
        conn.execute(
            "INSERT INTO operator_sessions (session_id, username, created_at, last_active, expires_at, user_agent, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, username, now, now, expires, user_agent[:256], ip_address[:64])
        )
        conn.commit()
    return session_id


def verify_operator_session(session_id: str, slide: bool = True) -> Optional[dict]:
    """Look up an operator session by session_id. Returns the row dict, or None.

    Side effects (when slide=True): refreshes last_active + extends expires_at
    (sliding 8h window). Sessions past expires_at are deleted and return None.
    Revoked sessions (revoked=1) return None.

    Bug fix 2026-07-06 (race condition on MariaDB / InnoDB):
        Previous version did SELECT-then-UPDATE inside the same transaction.
        With 4 gunicorn workers + a periodic purge_expired_operator_sessions()
        task, two workers could race on the same row. The 2nd worker's UPDATE
        failed with MariaDB error 1020 ("Record has changed since last read in
        table 'operator_sessions'") because InnoDB's REPEATABLE READ snapshot
        detected the row had been modified by a concurrent committed tx.

        Fix: do the UPDATE FIRST (acquires an exclusive row lock + checks
        rowcount atomically), then SELECT for the return value. rowcount==0
        means the row is gone, revoked, or expired — return None.
    """
    now = int(time.time())
    with _db() as conn:
        if slide:
            new_expires = now + OPERATOR_TTL
            cur = conn.execute(
                "UPDATE operator_sessions SET last_active = ?, expires_at = ? "
                "WHERE session_id = ? AND revoked = 0 AND expires_at >= ?",
                (now, new_expires, session_id, now),
            )
            if cur.rowcount == 0:
                # Either row is gone, revoked, or expired.
                # If expired, clean it up (best-effort, ignore failures).
                conn.execute(
                    "DELETE FROM operator_sessions WHERE session_id = ? AND expires_at < ?",
                    (session_id, now),
                )
                conn.commit()
                return None
        else:
            # No slide: just verify the row is still valid (not revoked/expired).
            # Use UPDATE...WHERE so the row is locked during our read, preventing
            # a concurrent purge from modifying it between our SELECT and return.
            cur = conn.execute(
                "UPDATE operator_sessions SET last_active = last_active "
                "WHERE session_id = ? AND revoked = 0 AND expires_at >= ?",
                (session_id, now),
            )
            if cur.rowcount == 0:
                return None
        row = conn.execute(
            "SELECT session_id, username, created_at, last_active, expires_at, revoked "
            "FROM operator_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.commit()
        if not row or row["revoked"]:
            return None
        return dict(row)


def delete_operator_session(session_id: str) -> None:
    """Delete an operator session (logout). Idempotent."""
    with _db() as conn:
        conn.execute("DELETE FROM operator_sessions WHERE session_id = ?", (session_id,))
        conn.commit()


def revoke_all_operator_sessions(username: Optional[str] = None) -> int:
    """Mark sessions as revoked. If username is given, only that user's. Returns count.

    Used for: "logout everywhere", suspicious activity response, after password
    change, after role change.
    """
    with _db() as conn:
        if username:
            cur = conn.execute(
                "UPDATE operator_sessions SET revoked = 1 WHERE username = ? AND revoked = 0",
                (username,)
            )
        else:
            cur = conn.execute("UPDATE operator_sessions SET revoked = 1 WHERE revoked = 0")
        conn.commit()
        return cur.rowcount


def purge_expired_operator_sessions() -> int:
    """Delete expired operator sessions. Returns count deleted. Call periodically."""
    now = int(time.time())
    with _db() as conn:
        cur = conn.execute("DELETE FROM operator_sessions WHERE expires_at < ?", (now,))
        conn.commit()
        return cur.rowcount


# ---------- Operator session FastAPI dep ----------

def require_operator_session(
    request: Request,
    session: Optional[str] = Cookie(None, alias=OPERATOR_COOKIE),
    portal_session: Optional[str] = Cookie(None, alias=PORTAL_COOKIE),
) -> dict:
    """FastAPI dep: require a valid operator session. Returns session info dict.

    Only accepts the operator session cookie. Portal session cookies are rejected
    (defense in depth — portal_session cookie scoped to /portal/ but we double-check).
    """
    if portal_session and not session:
        raise HTTPException(401, "Portal session not valid for operator endpoints")
    if not session:
        raise HTTPException(401, "Not authenticated")
    info = verify_operator_session(session)
    if not info:
        raise HTTPException(401, "Invalid or expired session")
    # Stash for /api/audit-style logging
    request.state.operator_session = info
    return info


# ---------- Phase 4.3-4.7: RADIUS lifecycle helpers ----------
#
# These functions write radcheck/usergroup rows when customers are created,
# have their EAP password rotated, are disabled (archived), or re-enabled.
# Phase 5 will cut charon over to read FreeRADIUS → these rows become the
# authoritative source of truth for VPN auth.

def add_customer_radcheck(username: str, plaintext_password: str, nt_hash_hex_upper: str):
    """INSERT radcheck row(s) for a new customer.

    Writes BOTH:
      - Cleartext-Password — needed for portal verify (Phase 4 self-service password reset)
      - NT-Password        — pre-computed MD4 hash for FreeRADIUS EAP-MSCHAPv2

    Wipes any prior radcheck rows for this username first (idempotent re-create).

    Args:
        username: EAP identity (e.g. "alice-iphone").
        plaintext_password: the literal password the operator saw in the modal.
            Stored as Cleartext-Password. Security note: this is the cost of
            password rotation + portal verify. daloRADIUS operator passwords
            are similarly stored as bcrypt. Customer passwords must be reversible
            for FreeRADIUS, hence plaintext.
        nt_hash_hex_upper: MD4(UTF-16LE(password)) as 32-char uppercase hex.
            Computed by ntlm_hash_bytes().hex().upper().
    """
    with _db() as conn:
        # Wipe any prior radcheck rows for this user (defense in depth)
        conn.execute("DELETE FROM radcheck WHERE username = ?", (username,))
        # Cleartext-Password for portal-side verify + RADIUS fallback
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)",
            (username, plaintext_password),
        )
        # NT-Password for FreeRADIUS EAP-MSCHAPv2 (pre-computed MD4 hash)
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'NT-Password', ':=', ?)",
            (username, nt_hash_hex_upper),
        )
        conn.commit()
        log.info(f"radcheck: added Cleartext-Password + NT-Password for {username}")


def update_customer_password_radcheck(username: str, new_plaintext_password: str, new_nt_hash_hex_upper: str):
    """UPDATE radcheck rows after password rotation (Phase 4.4).

    Replaces BOTH Cleartext-Password and NT-Password atomically.
    """
    with _db() as conn:
        conn.execute("DELETE FROM radcheck WHERE username = ?", (username,))
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)",
            (username, new_plaintext_password),
        )
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'NT-Password', ':=', ?)",
            (username, new_nt_hash_hex_upper),
        )
        conn.commit()
        log.info(f"radcheck: rotated password for {username}")


def disable_customer_radcheck(username: str):
    """UPDATE radcheck to deny auth (Phase 4.5).

    Replaces Cleartext-Password with a unique impossible value, which causes
    FreeRADIUS to reject MSCHAPv2 password verification. The original password
    is preserved in the audit_log for restoration.
    """
    disabled_marker = f"DISABLED-{secrets.token_hex(8)}"
    with _db() as conn:
        conn.execute("DELETE FROM radcheck WHERE username = ?", (username,))
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)",
            (username, disabled_marker),
        )
        conn.commit()
        log.info(f"radcheck: disabled {username} (marker={disabled_marker})")


def enable_customer_radcheck(username: str, plaintext_password: str, nt_hash_hex_upper: str):
    """UPDATE radcheck to restore auth (Phase 4.6).

    Replaces the DISABLED- marker with the customer's original password.
    Caller is responsible for retrieving the stored password (from
    customer_auth table or audit_log) before calling.
    """
    with _db() as conn:
        conn.execute("DELETE FROM radcheck WHERE username = ?", (username,))
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'Cleartext-Password', ':=', ?)",
            (username, plaintext_password),
        )
        conn.execute(
            "INSERT INTO radcheck (username, attribute, op, value) VALUES (?, 'NT-Password', ':=', ?)",
            (username, nt_hash_hex_upper),
        )
        conn.commit()
        log.info(f"radcheck: re-enabled {username}")


def add_customer_usergroup(username: str, groupname: str = "default", priority: int = 0):
    """INSERT radusergroup row (Phase 4.7) — assigns customer to RADIUS group.

    NOTE: FreeRADIUS's user-group membership table is `radusergroup`, NOT
    `usergroup` (daloRADIUS convention). We follow FreeRADIUS's name.

    The `default` group is daloRADIUS's standard customer group. Group
    membership drives RADIUS reply attributes (e.g., bandwidth caps,
    Simultaneous-Use limits set in radgroupreply).
    """
    with _db() as conn:
        conn.execute(
            "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, ?, ?)",
            (username, groupname, priority),
        )
        conn.commit()
        log.info(f"radusergroup: added {username} → {groupname} (priority {priority})")


def remove_customer_radcheck_and_usergroup(username: str):
    """DELETE all RADIUS data for a customer (used on full delete, not archive).

    Removes:
      - radcheck rows
      - radusergroup rows
      - radreply rows (if any)
    """
    with _db() as conn:
        conn.execute("DELETE FROM radcheck WHERE username = ?", (username,))
        conn.execute("DELETE FROM radusergroup WHERE username = ?", (username,))
        conn.execute("DELETE FROM radreply WHERE username = ?", (username,))
        conn.commit()
        log.info(f"radcheck+radusergroup+radreply: removed all for {username}")
