#!/usr/bin/env python3
"""
installer_tokens.py — One-time installer tokens for production customer onboarding

Add to app.py via: `import installer_tokens; installer_tokens.register(app)`

Why this exists
---------------
The customer-facing installer at
  https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1
needs the customer's EAP credentials to bind them to the Windows VPN profile.

We CAN'T put credentials in the installer URL (logs every proxy server, customer's
browser history, telegram preview, etc.). We CAN'T show them in the operator
modal alone because the customer would need to copy/paste into the script.

The pattern: a one-time installer token, valid for 7 days.

  Operator portal: "Generate installer link" button → returns URL with token
  Customer runs:   iex (irm https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1'?t=BASE64')
  Script fetches:  GET /api/installer/{token} → {username, password, server}
  Script binds:    RasSetCredentials(username, password)
  Server burns:    DELETE row from installer_tokens (single-use)

Token format
------------
32-character url-safe random (secrets.token_urlsafe(24) → ~32 chars after base64).
Cryptographically opaque. NOT derived from customer name or any predictable input.

Security properties
-------------------
- Single-use: consumed_at is set after first fetch; subsequent fetches return 404
- Time-bounded: 7-day expiry from creation
- Bound to customer: token row references customer_id; cross-customer fetch fails
- Audit logged: operator creation + script consumption both logged
- No credentials in URL logs: only slug + opaque token (token expires in 7d)
- HTTPS-only: portal enforces Strict-Transport-Security, cookies are Secure

Schema
------
CREATE TABLE IF NOT EXISTS installer_tokens (
    token         TEXT PRIMARY KEY,         -- 32-char url-safe random
    customer_id   INTEGER NOT NULL,          -- FK customers.id
    device_id     INTEGER,                   -- which device (NULL = any active device)
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,          -- created_at + 7*86400
    consumed_at   INTEGER,                   -- NULL = unused, else timestamp of consumption
    consumed_ip   TEXT,                      -- IP that consumed it (for audit)
    created_by    TEXT                       -- operator username who generated it
);
CREATE INDEX idx_installer_tokens_customer ON installer_tokens(customer_id);
CREATE INDEX idx_installer_tokens_expires  ON installer_tokens(expires_at);
"""
import json
import logging
import secrets
import time
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response

# Use the same DB exec / query helpers as app.py (db_query, db_exec, _q)
# These get injected at register() time
_db_query = None
_db_exec = None
_q = None
_audit = None
_require_session = None

log = logging.getLogger("installer_tokens")

# Token validity window — 7 days. Generous to handle: customer delays, weekend
# installs, re-installs on new device. After 7 days, operator generates a fresh
# link. Shorter would frustrate customers; longer would extend blast radius if
# link leaks.
TOKEN_TTL_SECONDS = 7 * 24 * 3600


def register(app: FastAPI, db_query, db_exec, q, audit_fn, require_session_dep):
    """Wire this module into the FastAPI app.

    Called from app.py:
        import installer_tokens
        installer_tokens.register(
            app,
            db_query=db_query,
            db_exec=db_exec,
            q=_q,
            audit_fn=_audit,
            require_session_dep=require_session,
        )
    """
    global _db_query, _db_exec, _q, _audit, _require_session
    _db_query = db_query
    _db_exec = db_exec
    _q = q
    _audit = audit_fn
    _require_session = require_session_dep

    # Ensure table exists (idempotent)
    _ensure_table()

    # POST /api/customers/{customer_id}/installer-token  (operator)
    @app.post("/api/customers/{customer_id}/installer-token")
    def create_installer_token(
        customer_id: int,
        request: Request,
        _user: dict = Depends(_require_session),
    ):
        """Operator-only: generate a one-time installer link for a customer.

        Returns the full PowerShell command the operator sends to the customer.
        Token is single-use and expires in 7 days. Token burned on first fetch.
        """
        # Validate customer exists + is active
        cust = _db_query(
            f"SELECT id, name, display_name, is_active, is_operator FROM customers "
            f"WHERE id = {int(customer_id)};"
        )
        if not cust:
            raise HTTPException(404, f"customer {customer_id} not found")
        cust = cust[0]
        if not cust.get("is_active"):
            raise HTTPException(400, f"customer '{cust['name']}' is not active")

        # Find an active device for this customer (latest created)
        devs = _db_query(
            f"SELECT id, device_name, device_type, os_version FROM devices "
            f"WHERE customer_id = {int(customer_id)} AND is_active = 1 "
            f"ORDER BY created_at DESC LIMIT 1;"
        )
        if not devs:
            raise HTTPException(
                400,
                f"customer '{cust['name']}' has no active device — create one first",
            )
        dev = devs[0]

        # Generate token
        token = secrets.token_urlsafe(24)  # 32 chars
        now = int(time.time())
        expires = now + TOKEN_TTL_SECONDS

        # Operator name from session (best-effort)
        op_name = "operator"
        try:
            cookie = request.cookies.get("portal_session")
            if cookie:
                # The operator_session cookie value is the session_id; lookup via portal_auth
                import portal_auth
                sess = portal_auth._get_operator_session(cookie)
                if sess:
                    op_name = sess.get("username", "operator")
        except Exception:
            pass

        _db_exec(
            f"INSERT INTO installer_tokens (token, customer_id, device_id, "
            f"created_at, expires_at, created_by) VALUES "
            f"({_q(token)}, {int(customer_id)}, {int(dev['id'])}, "
            f"{now}, {expires}, {_q(op_name)});"
        )

        # Build the PowerShell block the operator sends to customer.
        # v2.5.2 (2026-06-25) — Zun caught me: iex (irm 'URL') hits two problems:
        #   1. PowerShell 5.1 parses `&` in the URL as a background-job operator
        #      (ParserError: AmpersandNotAllowed) — fixed by packing slug+token
        #      as base64 `?t=BASE64` so there's no `&` in the URL.
        #   2. $MyInvocation.MyCommand.Definition doesn't reliably carry the URL
        #      into the script when invoked via iex (irm 'URL'), so the script's
        #      URL-detection regex often misses the token entirely.
        # Cleaner fix: give the customer a 3-line block (per DAT-VPN-WINDOWS-
        # CLIENT-MASTER-001.md canonical invocation):
        #   curl.exe -o $env:TEMP\setup.ps1 'URL'   # download without query
        #   & $env:TEMP\setup.ps1 -t BASE64PACKED   # execute with token arg
        #   rasdial DatabyteVPN                     # connect
        # -t flag is handled by the script's param block (decodes the packed
        # slug:token). Works in PS 5.1, PS 7, no MyInvocation dependency.
        import base64
        packed = base64.urlsafe_b64encode(f"{cust['name']}:{token}".encode()).decode().rstrip("=")
        installer_url = (
            f"https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1"
        )
        ps_cmd = "\n".join([
            f"curl.exe -o $env:TEMP\\setup.ps1 '{installer_url}'",
            f"& $env:TEMP\\setup.ps1 -t {packed}",
            "rasdial DatabyteVPN",
        ])

        _audit(op_name, "installer_token_create", {
            "_target_type": "customer",
            "_target_id":   customer_id,
            "customer_name": cust["name"],
            "device_id":    dev["id"],
            "device_name":  dev["device_name"],
            "token_prefix": token[:8] + "…",  # log only prefix, never full token
            "expires_at":   expires,
        })

        log.info(
            "installer token created customer=%s device=%s expires_in=%dd by=%s",
            cust["name"], dev["device_name"], TOKEN_TTL_SECONDS // 86400, op_name,
        )

        return {
            "customer_name":   cust["name"],
            "customer_display": cust.get("display_name"),
            "device_name":     dev["device_name"],
            "device_type":     dev.get("device_type"),
            "installer_url":   installer_url,
            "powershell_cmd":  ps_cmd,
            "token_prefix":    token[:8] + "…",
            "expires_at":      expires,
            "expires_in_days": TOKEN_TTL_SECONDS // 86400,
        }

    # GET /api/installer/{token}  (PUBLIC — no auth, single-use)
    @app.get("/api/installer/{token}")
    def consume_installer_token(token: str, request: Request, response: Response):
        """Public endpoint: fetch EAP credentials using a one-time token.

        Token is burned on first successful fetch. Subsequent fetches return 404.
        No auth required (the token IS the auth).

        Response shape:
          {
            "ok": true,
            "customer_name": "acme-corp",
            "username":      "acme-corp-laptop",  # EAP identity
            "password":      "...",                # plain-text, only returned ONCE
            "server":        "myvpn.databyte.co.za",
            "portal":        "https://vpn-portal.databyte.co.za/portal/",
            "device_name":   "laptop",
            "device_type":   "windows",
            "tier":          "tier1",
            "tier_display":  "Tier 1 — 5GB / $3 USD"
          }
        """
        if len(token) < 16:
            # tokens are 32 chars; reject obviously-wrong tokens early
            raise HTTPException(400, "invalid token format")

        # Lookup token (still unused)
        rows = _db_query(
            f"SELECT t.token, t.customer_id, t.device_id, t.expires_at, t.consumed_at, "
            f"c.name AS customer_name, c.display_name, c.tier_id, c.bandwidth_down_mbps, "
            f"c.bandwidth_up_mbps, c.is_active "
            f"FROM installer_tokens t "
            f"JOIN customers c ON c.id = t.customer_id "
            f"WHERE t.token = {_q(token)};"
        )
        if not rows:
            raise HTTPException(404, "installer token not found")
        row = rows[0]

        now = int(time.time())

        if row["consumed_at"] is not None:
            log.warning(
                "installer token reuse attempt token_prefix=%s… consumed_at=%s",
                token[:8], row["consumed_at"],
            )
            raise HTTPException(404, "installer token already consumed")
        if row["expires_at"] < now:
            log.info(
                "expired installer token fetch token_prefix=%s… expired_at=%s",
                token[:8], row["expires_at"],
            )
            raise HTTPException(404, "installer token expired")
        if not row.get("is_active"):
            raise HTTPException(403, "customer is suspended")

        # Get the device + EAP identity + plain-text password (reconstructed from NTLM hash).
        # v1.4.0 — Bug #2: prefer customers.user_id FK for the user lookup when set;
        # fall back to devices.strongswan_user_id for pre-migration customers.
        devs = _db_query(
            f"SELECT id, device_name, device_type, os_version, strongswan_user_id "
            f"FROM devices WHERE id = {int(row['device_id'])} AND is_active = 1;"
        )
        if not devs:
            raise HTTPException(500, "device not found or inactive")
        dev = devs[0]

        cust_fk = _db_query(
            f"SELECT user_id FROM customers WHERE id = {int(row['customer_id'])};"
        )
        eap_user_id = (
            cust_fk[0]["user_id"]
            if cust_fk and cust_fk[0].get("user_id")
            else dev["strongswan_user_id"]
        )

        users = _db_query(
            f"SELECT name, password FROM users WHERE id = {int(eap_user_id)};"
        )
        if not users:
            raise HTTPException(500, "EAP user not found")
        user = users[0]
        eap_identity = user["name"]
        # users.password stores NTLM hash (X'...'). We need plaintext for RasSetCredentials.
        # Look it up from rw-eap.conf instead (it's the source of truth for charon).
        plain_password = _read_eap_secret_from_conf(eap_identity)
        if not plain_password:
            raise HTTPException(
                500,
                "could not retrieve EAP secret from rw-eap.conf "
                "(charon config drift — re-create the customer)",
            )

        # Tier info
        tier_name = ""
        tier_display = ""
        if row.get("tier_id"):
            tiers = _db_query(
                f"SELECT name, display_name FROM tiers WHERE id = {int(row['tier_id'])};"
            )
            if tiers:
                tier_name = tiers[0].get("name", "")
                tier_display = tiers[0].get("display_name", "")

        # Burn the token
        client_ip = request.client.host if request.client else "unknown"
        _db_exec(
            f"UPDATE installer_tokens SET consumed_at = {now}, consumed_ip = {_q(client_ip)} "
            f"WHERE token = {_q(token)};"
        )

        _audit(eap_identity, "installer_token_consume", {
            "_target_type": "customer",
            "_target_id":   row["customer_id"],
            "customer_name": row["customer_name"],
            "device_id":    dev["id"],
            "device_name":  dev["device_name"],
            "token_prefix": token[:8] + "…",
            "consumed_ip":  client_ip,
        })

        log.info(
            "installer token consumed customer=%s device=%s ip=%s",
            row["customer_name"], dev["device_name"], client_ip,
        )

        return {
            "ok":              True,
            "customer_name":   row["customer_name"],
            "customer_display": row.get("display_name"),
            "username":        eap_identity,
            "password":        plain_password,
            "server":          "myvpn.databyte.co.za",
            "portal":          "https://vpn-portal.databyte.co.za/portal/",
            "device_name":     dev["device_name"],
            "device_type":     dev.get("device_type"),
            "tier":            tier_name,
            "tier_display":    tier_display,
            "bandwidth_down_mbps": row.get("bandwidth_down_mbps", 20),
            "bandwidth_up_mbps":   row.get("bandwidth_up_mbps", 20),
        }


def _ensure_table():
    """Create the installer_tokens table if it doesn't exist. Idempotent."""
    _db_exec("""
        CREATE TABLE IF NOT EXISTS installer_tokens (
            token         TEXT PRIMARY KEY,
            customer_id   INTEGER NOT NULL,
            device_id     INTEGER,
            created_at    INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL,
            consumed_at   INTEGER,
            consumed_ip   TEXT,
            created_by    TEXT
        );
    """)
    _db_exec("CREATE INDEX IF NOT EXISTS idx_installer_tokens_customer ON installer_tokens(customer_id);")
    _db_exec("CREATE INDEX IF NOT EXISTS idx_installer_tokens_expires  ON installer_tokens(expires_at);")


def _read_eap_secret_from_conf(eap_identity: str) -> Optional[str]:
    """Read the plaintext EAP secret from rw-eap.conf on the strongSwan host.

    Uses the same SSH path as append_eap_block() in app.py. Falls back to None
    if the secret can't be found (drift between DB and config).

    Pattern matched in rw-eap.conf:
        eap-{identity} {
            id = {identity}
            secret = "{plaintext}"
        }
    """
    import re as _re
    try:
        from app import read_rw_eap_conf  # type: ignore
        conf = read_rw_eap_conf()
    except Exception as e:
        log.error("could not read rw-eap.conf via app helper: %s", e)
        return None
    # Match the eap block for this identity
    pat = _re.compile(
        rf"eap-{_re.escape(eap_identity)}\s*\{{\s*id\s*=\s*{_re.escape(eap_identity)}\s*secret\s*=\s*\"([^\"]+)\"",
        _re.DOTALL,
    )
    m = pat.search(conf)
    if m:
        return m.group(1)
    log.warning("eap block for %s not found in rw-eap.conf", eap_identity)
    return None