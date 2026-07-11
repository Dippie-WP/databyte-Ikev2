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
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request, Response

# Make the sibling `scripts/build_installer.py` module importable from this
# file. The scripts dir is on the same level as installer_tokens.py.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

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
    # Polish Item #3 (installer_bakes table) — DEFERRED for vps-01 deploy.
    # vps-01 portal runs SQLite (not MariaDB); the cross-engine-safe schema
    # is a separate Polish #3 follow-up deploy. Re-enable here once the
    # SQLite-correct version exists. See CORR-2026-07-11-020.

    # POST /api/customers/{customer_id}/installer-token  (operator)
    @app.post("/api/customers/{customer_id}/installer-token")
    def create_installer_token(
        customer_id: int,
        request: Request,
        mode: str = "standard",  # Phase 1: "standard" | "hostile"
        _user: dict = Depends(_require_session),
    ):
        """Operator-only: generate a one-time installer link for a customer.

        ?mode=standard (default): canonical FROZEN v2.6.5 3-line block via 7-day token
        ?mode=hostile: self-contained baked .ps1 with creds inlined (Type H, no HTTPS)
                       - no token created, no expiry, file IS sensitive customer material

        Returns the full PowerShell command the operator sends to the customer.
        For standard: token is single-use and expires in 7 days.
        For hostile: no token; the script content IS delivered directly.
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

        # Validate mode explicitly (FastAPI auto-handles type, not value whitelist)
        if mode not in ("standard", "hostile"):
            raise HTTPException(400, f"unknown mode {mode!r}; expected 'standard' or 'hostile'")

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

        now = int(time.time())

        # ─── Mode = hostile: bake a self-contained .ps1 (no token, no expiry) ──────
        if mode == "hostile":
            # Read the customer's EAP secret from rw-eap.conf (charon source of truth).
            # The EAP identity convention is "{customer_name}-{device_name}" (matches
            # the `id =` line of each EAP-MSCHAPv2 conn block in rw-eap.conf).
            eap_identity = f"{cust['name']}-{dev['device_name']}"
            eap_password, conf_fingerprint = _read_eap_secret_from_conf(eap_identity)
            if not eap_password:
                # Diagnose the root cause before failing. Three things can cause this:
                #
                #   (a) rw-eap.conf is unreachable (SSH to charon failed, file gone,
                #       permissions wrong, network down). _read_eap_secret_from_conf
                #       returns (None, None) -- we cannot tell the operator which
                #       variant without the fingerprint.
                #
                #   (b) rw-eap.conf is readable but has NO block for this identity.
                #       Common when the customer was created but the device was never
                #       provisioned (devices.device_name mismatch, e.g. "Laptop" vs
                #       "laptop"). charon only knows about blocks whose `id` matches
                #       the EAP identity substring.
                #
                #   (c) rw-eap.conf has a block but its secret changed since the portal
                #       DB row was written (rotate_eap without re-fetch). The block is
                #       unreachable from this caller because _read_eap_secret_from_conf
                #       returned None for the identity.
                #
                # The error message MUST give the operator (i) what to look at,
                # (ii) a one-line actionable fix. Field-tested against the 2026-07-11
                # prod audit. BEFORE this enrichment, the message was
                # "could not find EAP secret for X in rw-eap.conf -- re-create the
                # customer or rotate EAP credentials", which forced the operator to
                # SSH to vps-01 to even start diagnosing.
                if conf_fingerprint is None:
                    # (a) rw-eap.conf unreachable
                    raise HTTPException(
                        503,
                        f"EAP secret lookup failed -- rw-eap.conf not reachable on the "
                        f"VPN gateway (vps-01). Check: "
                        f"(1) charon is up (`swanctl --list-sas` on vps-01), "
                        f"(2) /home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf exists "
                        f"and is readable, "
                        f"(3) SSH from this portal container to vps-01 works.",
                    )
                # (b) or (c) -- conf is readable but no exact-identity block.
                # List the identities that ARE present so the operator can see the
                # typo / case mismatch immediately.
                siblings = _list_eap_identities_in_conf() if hasattr(_read_eap_secret_from_conf, '__module__') else []
                hint = ""
                if siblings:
                    matches = [s for s in siblings if cust["name"] in s]
                    if matches:
                        hint = (
                            f" -- hint: rw-eap.conf has matching-customer blocks "
                            f"{matches!r}; check that device name '{dev['device_name']}' "
                            f"matches the `id` field of one of those (case-sensitive)."
                        )
                    else:
                        hint = (
                            f" -- rw-eap.conf has no block for customer '{cust['name']}' "
                            f"at all (known identities: {siblings[:10]!r}"
                            + (f' (+{len(siblings)-10} more)' if len(siblings) > 10 else '')
                            + f"). Recreate the customer or rotate its EAP credentials."
                        )
                raise HTTPException(
                    404,
                    f"EAP block '{eap_identity}' not found in rw-eap.conf"
                    + hint,
                )

            # Build the baked script via the pure-function module
            from build_installer import build as build_installer, MODE_HOSTILE

            artifact = build_installer(
                customer=cust,
                device=dev,
                mode=MODE_HOSTILE,
                eap_password=eap_password,
            )

            # Audit: hostile-mode generate (no token, just the bake)
            _audit(op_name, "installer_token_create_hostile", {
                "_target_type": "customer",
                "_target_id":   customer_id,
                "customer_name": cust["name"],
                "device_id":    dev["id"],
                "device_name":  dev["device_name"],
                "mode":         "hostile",
                "filename":     artifact["filename"],
                "content_bytes": len(artifact["content"] or ""),
            })
            log.info(
                "installer (hostile mode) baked customer=%s device=%s filename=%s by=%s",
                cust["name"], dev["device_name"], artifact["filename"], op_name,
            )
            # Polish #3 (record bake in installer_bakes table) — DEFERRED.
            return {
                "mode":            "hostile",
                "installer_kind":  artifact["installer_kind"],
                "customer_name":   cust["name"],
                "customer_display": cust.get("display_name"),
                "device_name":     dev["device_name"],
                "device_type":     dev.get("device_type"),
                "filename":        artifact["filename"],
                "content":         artifact["content"],
                "powershell_cmd":  artifact["powershell_cmd"],
                "skill_source":    artifact["skill_source"],
            }

        # ─── Mode = standard: 3-line token flow (FROZEN v2.6.5, unchanged) ──────────
        # Generate token
        token = secrets.token_urlsafe(24)  # 32 chars
        expires = now + TOKEN_TTL_SECONDS

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
            "mode":            "standard",
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

    # Polish Item #1 (2026-07-11) -- post-dial cert echo check.
    @app.get("/api/installer/bake/{bake_id}/verify")
    def verify_bake_against_sas(bake_id: int, request: Request):
        """Verify a hostile/standard bake's EAP identity has an active IKE_SA.

        Answer: did the script we sent the customer reach OUR strongSwan server,
        not an attacker's clone? Hostile networks (the only place hostile-baked
        scripts should ever be used) skip TLS chain validation in the script.
        Operator can manually verify with swanctl --list-sas on the server; this
        endpoint does the lookup for them.

        Returns: any active IKE_SA on the server whose eap_id matches the bake's
        customer+device EAP identity. If found, verified=true.
        """
        bake_rows = []
        try:
            bake_rows = _db_query(
                f"SELECT id, customer_id, device_id, mode, baked_at, filename, sha256 "
                f"FROM installer_bakes WHERE id = {int(bake_id)};"
            )
        except Exception as e:
            # Polish #3 (installer_bakes) is deferred for this deploy -- the
            # table doesn't exist on vps-01 yet. Return a clear 503 so the
            # operator/UI understands, instead of a 502 from the SQL engine.
            log.warning("installer_bakes table not present (Polish #3 deferred): %s", e)
            raise HTTPException(
                503,
                "installer_bakes table not initialized yet -- Polish #3 deploy pending. "
                "Verify endpoint requires the installer_bakes table from Polish #3; "
                "use `swanctl --list-sas` on vps-01 directly until then.",
            )
        if not bake_rows:
            raise HTTPException(404, f"bake_id {bake_id} not found")
        bake = bake_rows[0]

        cust_rows = _db_query(
            f"SELECT id, name, display_name FROM customers "
            f"WHERE id = {int(bake['customer_id'])};"
        )
        if not cust_rows:
            raise HTTPException(500, "bake references missing customer row")
        cust = cust_rows[0]

        device_name = ""
        if bake.get("device_id") is not None:
            dev_rows = _db_query(
                f"SELECT device_name FROM devices WHERE id = {int(bake['device_id'])};"
            )
            if dev_rows:
                device_name = dev_rows[0]["device_name"]

        eap_identity = f"{cust['name']}-{device_name}" if device_name else cust["name"]

        sas_text = ""
        try:
            # Lazy-load so we don't force `import app` during cold start (would
            # raise RuntimeError when VPN_HOST env is unset).
            app_module = sys.modules.get("app")
            if app_module is None:
                import app as app_module_late  # noqa: F401
                app_module = sys.modules["app"]
            swanctl_list_sas = getattr(app_module, "swanctl_list_sas", None)
            _parse_sas_text  = getattr(app_module, "_parse_sas_text", None)
            if not swanctl_list_sas or not _parse_sas_text:
                raise RuntimeError("app module missing swanctl helpers")
            sas_text = swanctl_list_sas()
            all_sas = _parse_sas_text(sas_text) if sas_text else []
        except Exception as e:
            log.error("swanctl unreachable for bake verify: %s", e)
            raise HTTPException(
                503,
                "charon on the VPN gateway is unreachable -- "
                "cannot verify SA. Check docker exec strongswan swanctl --list-sas "
                "on vps-01.",
            )

        matches = [
            sa for sa in all_sas
            if (sa.get("eap_id") or "").lower() == eap_identity.lower()
            or sa.get("conn") == f"eap-{eap_identity}"
        ]

        match_detail = None
        if matches:
            sa = matches[0]
            match_detail = {
                "conn":             sa.get("conn"),
                "remote_ip":        sa.get("remote_ip"),
                "vip":              sa.get("vip"),
                "established_secs": sa.get("established_secs"),
                "algo":             sa.get("algo"),
                "state":            sa.get("state"),
            }
        local_id = matches[0].get("local_id") if matches else (
            all_sas[0].get("local_id") if all_sas else None
        )

        log.info(
            "bake verify: bake_id=%d eap_identity=%s verified=%s sas_total=%d matches=%d",
            bake_id, eap_identity, bool(matches), len(all_sas), len(matches),
        )

        return {
            "ok":                       True,
            "bake_id":                  bake_id,
            "mode":                     bake.get("mode"),
            "customer_name":            cust["name"],
            "customer_display":         cust.get("display_name"),
            "device_name":              device_name,
            "eap_identity":             eap_identity,
            "verified":                 bool(matches),
            "server_local_id":          local_id,
            "match":                    match_detail,
            "all_matching_sas_count":   len(matches),
            "live_sas_total":           len(all_sas),
            "checked_at":               int(time.time()),
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


# ── Polish Item #3 (installer_bakes history table) — DEFERRED ────────
# Disabled for vps-01 deploy on 2026-07-11 per Option Y:
# - vps-01 portal runs SQLite (DB engine = sqlite3, per app.py line 264)
# - The Polish #3 CREATE TABLE used MariaDB syntax (AUTO_INCREMENT,
#   ENGINE=InnoDB, CHARACTER SET utf8mb4, COLLATE utf8mb4_unicode_ci,
#   VARBINARY(32) with 0x... literal) which SQLite rejects at boot.
# - This is the cross-engine-safe rewrite, slated for a separate deploy
#   (CORR-2026-07-11-020 was about exactly this).
# - Until then: no installer_bakes table; no per-Generate-click history row.
# - For this deploy: keep the OTHER polish items alive (#1 swanctl verify,
#   #2 enriched 503/404 diagnostics).


def _read_eap_secret_from_conf(eap_identity: str) -> Tuple[Optional[str], Optional[str]]:
    """Read the plaintext EAP secret from rw-eap.conf on the strongSwan host.

    Uses the same SSH path as append_eap_block() in app.py. Falls back to (None, None)
    if rw-eap.conf is unreachable, or (None, fingerprint) if the file is readable
    but no block for this exact identity exists. The fingerprint is a 12-char SHA-256
    prefix of the conf contents, used to disambiguate "cannot read file" (fingerprint
    is None) from "can read file but identity not present" (fingerprint is present).

    Returns:
        (password, fingerprint) where:
        - password is the EAP plaintext secret, or None if not found.
        - fingerprint is the 12-char SHA-256 prefix of the conf contents, or None
          if the conf could not be read at all.

    Pattern matched in rw-eap.conf:
        eap-{identity} {
            id = {identity}
            secret = "{plaintext}"
        }
    """
    import re as _re, hashlib
    try:
        from app import read_rw_eap_conf  # type: ignore
        conf = read_rw_eap_conf()
    except Exception as e:
        log.error("could not read rw-eap.conf via app helper: %s", e)
        return (None, None)
    if not conf:
        return (None, None)
    # SHA-256 prefix as a cheap fingerprint the operator can quote in bug reports
    # ("fingerprint 9f2c4a8b1e0d means I'm reading the same file as you")
    fingerprint = hashlib.sha256(conf.encode("utf-8", "replace")).hexdigest()[:12]
    # Match the eap block for this identity. The block name convention is eap-{identity}.
    pat = _re.compile(
        rf"eap-{_re.escape(eap_identity)}\s*\{{\s*id\s*=\s*{_re.escape(eap_identity)}\s*secret\s*=\s*\"([^\"]+)\"",
        _re.DOTALL,
    )
    m = pat.search(conf)
    if m:
        return (m.group(1), fingerprint)
    log.warning("eap block for %s not found in rw-eap.conf (fingerprint=%s)",
                eap_identity, fingerprint)
    return (None, fingerprint)


def _list_eap_identities_in_conf() -> list:
    """Return all `id = ` values that appear inside eap-{...} blocks.

    Used by the hostile-mode 404 path to give the operator a hint about which
    identities ARE present (typo / case mismatch).
    """
    import re as _re
    try:
        from app import read_rw_eap_conf  # type: ignore
        conf = read_rw_eap_conf()
    except Exception:
        return []
    if not conf:
        return []
    # Match `id = {value}` inside each eap-{...} block. The rw-eap.conf format
    # uses unquoted values for `id =` (quoted only for `secret = "..."`). The
    # previous regex required quoted values and missed every block.
    out = []
    for block_match in _re.finditer(r"eap-[^\s]+\s*\{([^}]*)\}", conf, _re.DOTALL):
        body = block_match.group(1)
        id_match = _re.search(r'id\s*=\s*"?([^\s"]+)"?', body)
        if id_match:
            out.append(id_match.group(1))
    return out