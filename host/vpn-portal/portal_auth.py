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

Lab build (2026-06-21): LAN-only at http://192.168.10.98:8080/portal/.
No HTTPS, no public exposure. Re-do for production when going client-facing.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
import subprocess
import time
from typing import Optional

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from fastapi import Cookie, HTTPException, Request, Response


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


# ---------- DB helpers (direct sqlite3, not via ssh_903) ----------

# We need a sqlite3 connection that R/W-s to the same DB charon writes to.
# WAL mode + busy_timeout makes this safe alongside charon.
DB_PATH = os.environ.get("DB_PATH", "/var/lib/strongswan/ipsec.db")


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def lookup_user_and_customer(identity: str) -> Optional[dict]:
    """Look up a user by EAP identity (= users.name) and find their customer.

    Returns dict with keys: user_id, identity, password_hash, customer_id, customer_name,
    customer_status, customer_is_operator, customer_data_*, devices info.
    Returns None if user not found or device not found.
    """
    with _db() as conn:
        user_row = conn.execute(
            "SELECT id, name, password FROM users WHERE name = ? AND password IS NOT NULL AND length(password) > 0",
            (identity,)
        ).fetchone()
        if not user_row:
            return None

        # Find the device row that matches the user name
        device_row = conn.execute(
            "SELECT d.id AS device_id, d.device_name, d.customer_id, d.device_type, d.os_version, d.is_active, "
            "c.name AS customer_name, c.status AS customer_status, c.is_operator AS customer_is_operator, "
            "c.tier_id, c.data_used_bytes, c.data_limit_bytes, c.over_quota, c.email, c.display_name "
            "FROM devices d JOIN customers c ON c.id = d.customer_id "
            "WHERE d.strongswan_user_id = ?",
            (user_row["id"],)
        ).fetchone()
        if not device_row:
            return None

        return {
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
        }


def lookup_customer_full(customer_id: int) -> Optional[dict]:
    """Look up customer + tier info for the portal usage endpoint. Scoped to customer_id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT c.id, c.name, c.display_name, c.email, c.status, c.is_operator, c.is_active, "
            "c.data_used_bytes, c.data_limit_bytes, c.over_quota, c.max_devices, c.created_at, c.updated_at, "
            "t.name AS tier_name, t.display_name AS tier_display "
            "FROM customers c LEFT JOIN tiers t ON t.id = c.tier_id "
            "WHERE c.id = ?",
            (customer_id,)
        ).fetchone()
        if not row:
            return None
        return dict(row)


def list_customer_devices(customer_id: int) -> list:
    """List devices for a customer. Scoped to customer_id — caller can only see their own devices."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, device_name, device_type, os_version, hostname, is_active, last_seen_at, created_at "
            "FROM devices WHERE customer_id = ? ORDER BY id",
            (customer_id,)
        ).fetchall()
        return [dict(r) for r in rows]


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
    """Verify a session_id and return the session info. Updates last_active if slide=True."""
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT session_id, customer_id, identity, created_at, last_active, expires_at "
            "FROM customer_portal_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now:
            conn.execute("DELETE FROM customer_portal_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        if slide:
            new_expires = now + PORTAL_TTL
            conn.execute(
                "UPDATE customer_portal_sessions SET last_active = ?, expires_at = ? WHERE session_id = ?",
                (now, new_expires, session_id)
            )
            conn.commit()
        return dict(row)


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
    """
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT session_id, username, created_at, last_active, expires_at, revoked "
            "FROM operator_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        if row["revoked"]:
            return None
        if row["expires_at"] < now:
            conn.execute("DELETE FROM operator_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        if slide:
            new_expires = now + OPERATOR_TTL
            conn.execute(
                "UPDATE operator_sessions SET last_active = ?, expires_at = ? WHERE session_id = ?",
                (now, new_expires, session_id)
            )
            conn.commit()
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
