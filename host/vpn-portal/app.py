#!/usr/bin/env python3
"""
databyte VPN Portal — FastAPI backend (5C.1, MVP)

Single-file app. Reads SQLite + charon state from the VPN gateway (VPS in prod
via 127.0.0.1, LXC 903 lab via 192.168.10.98 — selected by VPN_HOST env var).
Wraps swanctl/ipBan/firewalld.

Endpoints:
  GET  /api/health                     public — service + DB + charon reach
  POST /api/login                      admin auth (Argon2id + DB session cookie)
  POST /api/logout                     deletes DB session + clears cookie
  GET  /api/customers                  list w/ tier, used, quota, over_quota, vip
  GET  /api/customers/{id}             + devices[] + alerts[]
  GET  /api/tiers                      tier defs (5GB/10GB/20GB/demo_100MB) — Tier 1/2/3 at $3/$5/$8 USD
  GET  /api/speed_plans                per-customer bandwidth presets (standard 20/20, asymmetric_40_20)
  GET  /api/quota/{customer_id}        live used/quota + cap state
  POST /api/quota/{customer_id}/reset  sqlite UPDATE, returns reset_from_bytes
  GET  /api/vpn/sessions               docker exec swanctl --list-sas (raw)
  GET  /api/vpn/sessions/parsed         structured parse: VIP, public_ip, algos, fingerprint
  GET  /api/vpn/pools                  docker exec swanctl --list-pools (parsed)
  GET  /api/devices                    list all devices with metadata
  GET  /api/devices/{id}               single device metadata
  PUT  /api/devices/{id}               update device_type, hostname, os_version, notes, is_active
  GET  /api/security/bans              ipban-ctl list (parsed)
  GET  /api/security/whitelist         firewalld trusted zone sources
  POST /api/security/unban             ipban-ctl unban {ip}
  POST /api/security/whitelist/add     firewall-cmd --add-source {cidr}
  GET  /api/security/deadman           ipban-ctl deadman status (raw)

Config via env:
  VPN_HOST       VPN gateway IP/host. **MUST** be set explicitly. Use 127.0.0.1 on VPS
                (charon VICI socket on localhost) or the LXC 903 lab IP for development.
                No default — startup fails fast if unset (avoids accidentally connecting
                prod to lab if env var is forgotten).
  SSH_KEY        path to SSH private key (default /root/.ssh/id_ed25519_vpn)
  DB_PATH        SQLite on the gateway (default /var/lib/strongswan/ipsec.db)
  ADMIN_USER     admin username (default admin)
  ADMIN_PASS_HASH  Argon2id hash of admin password (REQUIRED). Generate with:
                    python -c "import portal_auth; print(portal_auth.hash_operator_password('YOURPASS'))"
  OPERATORS_JSON  Optional JSON object mapping username -> Argon2id hash. When set,
                  REPLACES the single-operator ADMIN_USER/ADMIN_PASS_HASH flow for
                  /api/login. The legacy single-operator path is preserved as a
                  fallback when OPERATORS_JSON is unset or empty.
                  Example: OPERATORS_JSON='{"admin":"$argon2id$...","misha":"$argon2id$..."}'
  COOKIE_SECURE   "true" / "1" to set Secure flag on cookies (REQUIRED when behind HTTPS)
"""
import os
import sys
import json
import re
import time
import hmac
import hashlib
import secrets
import subprocess
import logging
import asyncio
import contextlib
from collections import defaultdict
from datetime import datetime
from typing import Optional, Literal
import portal_auth  # v1.3.0 customer portal auth + v1.3.1 operator sessions

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------- Config ----------
VPN_HOST        = os.environ.get("VPN_HOST", "")
if not VPN_HOST:
    raise RuntimeError(
        "VPN_HOST env var is REQUIRED. "
        "Set to 127.0.0.1 on VPS, or the LXC 903 lab IP for dev."
    )
SSH_KEY         = os.environ.get("SSH_KEY", "/root/.ssh/id_ed25519_vpn")
DB_PATH         = os.environ.get("DB_PATH", "/var/lib/strongswan/ipsec.db")
ADMIN_USER      = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")
# Multi-operator support (added 2026-07-08 per Zun msg #24405).
# When OPERATORS_JSON is set, /api/login iterates the dict instead of
# using the single ADMIN_USER/ADMIN_PASS_HASH pair. Backwards compatible:
# if OPERATORS_JSON is unset/empty, falls back to the legacy path.
OPERATORS_JSON   = os.environ.get("OPERATORS_JSON", "")

def _load_operators():
    """Return ordered dict of operator username -> Argon2id hash.

    Precedence: OPERATORS_JSON (if set and valid JSON) > ADMIN_USER/ADMIN_PASS_HASH.
    """
    if OPERATORS_JSON:
        try:
            ops = json.loads(OPERATORS_JSON)
            if isinstance(ops, dict) and ops:
                return {str(k): str(v) for k, v in ops.items() if v}
        except (json.JSONDecodeError, ValueError) as e:
            log.error("OPERATORS_JSON is set but not valid JSON: %s", e)
    # Legacy fallback
    if ADMIN_USER and ADMIN_PASS_HASH:
        return {ADMIN_USER: ADMIN_PASS_HASH}
    return {}
RATE_LIMIT_PER_MIN = 5
SSH_TIMEOUT     = 10

# ---------- Logging ----------
# CP7 JSON logging. Emits one JSON object per line on stdout (captured by
# journald → Loki/Promtail/etc). Fields: ts, level, logger, msg, plus any
# extra fields passed via extra={...} to the logger call.
import json as _json
class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Promote any extra fields attached to the record
        for k, v in record.__dict__.items():
            if k not in ("name", "msg", "args", "levelname", "levelno", "pathname",
                         "filename", "module", "exc_info", "exc_text", "stack_info",
                         "lineno", "funcName", "created", "msecs", "relativeCreated",
                         "thread", "threadName", "processName", "process", "message",
                         "taskName"):
                try:
                    _json.dumps(v)  # only include JSON-serializable values
                    payload[k] = v
                except (TypeError, ValueError):
                    payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _json.dumps(payload, separators=(",", ":"))

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger("vpn-portal")

# ---------- App ----------
app = FastAPI(title="databyte vpn-portal", version="2.1.0")

# Serve frontend (static assets + SPA index)
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
if os.path.isdir(WWW_DIR):
    app.mount("/static", StaticFiles(directory=os.path.join(WWW_DIR, "static")), name="static")

    @app.get("/", include_in_schema=False)
    def root_index():
        return FileResponse(os.path.join(WWW_DIR, "index.html"))

    # v1.3.0 — Customer portal at /portal/. Separate SPA with its own auth.
    # Lab build — LAN-only. Re-do for production (HTTPS, public exposure).
    @app.get("/portal", include_in_schema=False)
    @app.get("/portal/", include_in_schema=False)
    def portal_index():
        return FileResponse(os.path.join(WWW_DIR, "portal", "index.html"))

# ---------- Session cleanup (HIGH #3 fix) ----------
# Both purge_expired_sessions() (customer) and purge_expired_operator_sessions()
# (operator) are defined in portal_auth.py but were never called, so expired
# sessions accumulated indefinitely. Fix: asyncio background task that runs
# every 5 min, deletes expired rows from both tables. Idempotent + safe to run
# concurrently with reads (SQLite WAL mode allows concurrent readers + 1 writer).
async def _session_cleanup_loop():
    while True:
        try:
            await asyncio.sleep(300)  # 5 min
            c = portal_auth.purge_expired_sessions()
            o = portal_auth.purge_expired_operator_sessions()
            if c or o:
                log.info("session cleanup: deleted customer=%d operator=%d", c, o)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("session cleanup error: %s", e)

@app.on_event("startup")
async def _start_session_cleanup():
    app.state.session_cleanup_task = asyncio.create_task(_session_cleanup_loop())
    log.info("session cleanup task scheduled (every 5 min)")

@app.on_event("shutdown")
async def _stop_session_cleanup():
    task = getattr(app.state, "session_cleanup_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

# ---------- Rate limit (in-memory, per-IP) ----------
_login_attempts: dict[str, list[float]] = defaultdict(list)


def rate_limit(ip: str):
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < 60]
    if len(attempts) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Too many login attempts; try again in a minute")
    _login_attempts[ip] = attempts + [now]


# ---------- Session: server-side (DB-backed) ----------
#
# v1.3.1 — replaced the HMAC-signed-JSON cookie pattern with an opaque random
# token + DB lookup (operator_sessions table). Trade-off: we can now revoke
# (logout-everywhere, ban stolen cookie). Cookie value is `secrets.token_urlsafe(32)`
# — 256 bits of entropy, not user data.
#
# The require_session dep delegates to portal_auth.require_operator_session
# which does the cookie name + DB lookup + sliding expiry dance. We keep the
# name `require_session` so we don't have to touch every route signature.

require_session = portal_auth.require_operator_session


# ---------- SSH + DB helpers ----------
def ssh_903(cmd_args: list, timeout: int = SSH_TIMEOUT, stdin_text: str = "") -> str:
    """Run a command on the VPN gateway. cmd_args is a list.

    If stdin_text is provided, it's piped to the remote command's stdin.
    """
    # Quote args safely (single-quote wrap, escape internal quotes)
    def shq(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"
    remote = " ".join(shq(a) for a in cmd_args)
    full = [
        "ssh", "-i", SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        f"root@{VPN_HOST}",
        remote,
    ]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout, input=stdin_text or None)
    if r.returncode != 0:
        raise HTTPException(502, f"VPN gateway error: {r.stderr.strip()[:200]}")
    return r.stdout


def db_query(sql: str, params=None) -> list:
    """Phase 4E: query MariaDB `radius` DB directly via portal_auth._db().

    Phase 4E unified portal-local state (customers, devices, users, etc.) into
    MariaDB. db_query/db_exec now run against localhost MariaDB, no SSH hop.
    Returns list of dicts.

    sqlite3 -json used to serialize NULL as the literal string "None". MariaDB
    returns proper NULL, so the post-process null-coercion is now a defensive
    no-op kept for legacy callers that fed sqlite3 output downstream.
    """
    with portal_auth._db() as conn:
        result = conn.execute(sql, params or ())
        rows = []
        for r in result.fetchall():
            d = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
            for k, v in d.items():
                if v == "None":
                    d[k] = None
            rows.append(d)
        return rows


def db_exec(sql: str, params=None) -> None:
    """Phase 4E: execute non-SELECT SQL on MariaDB `radius` DB."""
    with portal_auth._db() as conn:
        conn.execute(sql, params or ())
        conn.commit()


# ---------- charon / ipBan / firewalld wrappers ----------
def leases_active() -> list:
    """Currently active virtual-IP leases with customer + device info.

    Source: charon's live pool (swanctl --list-pools --leases). Replaces the
    attr-sql `addresses` table which is empty (charon uses an inline pool
    defined in swanctl.conf, not the DB-managed pool the original design
    assumed — see docs/ARCHITECTURE.md §"Sticky VIP via attr-sql").

    Enrichment layers:
      1. DB devices/customers/tiers — joined on device_name = pool-lease identity
      2. SA parser (swanctl --list-sas) — adds public_ip, remote_port, algo

    Output shape is identical to the old addresses-table version (back-compat
    for the UI in app.js + the customer_active_sessions endpoint).
    """
    pool_leases = swanctl_list_pool_leases()
    if not pool_leases:
        return []

    # Fetch all active devices joined with their strongSwan user + customer + tier.
    # Index by users.name (the EAP identity, e.g. 'saalieg-laptop') — that is
    # what charon's --list-pools --leases reports. The portal's `device_name`
    # is a user-friendly label ('laptop') and does NOT match the pool identity.
    # Join path: users.name (EAP identity) -> users.id -> devices.strongswan_user_id
    #                                              -> devices.customer_id -> customers.id
    device_sql = """
      SELECT u.id              AS user_id,
             u.name            AS eap_identity,
             d.id              AS device_id,
             d.device_name     AS device_name,
             d.device_type     AS device_type_meta,
             d.os_version      AS os_version_meta,
             d.hostname        AS hostname_meta,
             d.last_seen_at    AS acquired_at,
             c.id              AS customer_id,
             c.name            AS customer_name,
             c.is_operator     AS is_operator,
             c.data_used_bytes AS data_used_bytes,
             c.data_limit_bytes AS data_limit_bytes,
             c.over_quota      AS over_quota,
             c.tier_id         AS tier_id,
             t.name            AS tier_name
      FROM users u
      LEFT JOIN devices   d ON d.strongswan_user_id = u.id AND d.is_active = 1
      LEFT JOIN customers c ON c.id = d.customer_id
      LEFT JOIN tiers     t ON t.id = c.tier_id
    """
    try:
        all_users = db_query(device_sql)
    except HTTPException:
        return []
    # Index by EAP identity (matches charon pool lease)
    devices_by_identity = {
        u["eap_identity"]: u for u in all_users if u.get("eap_identity")
    }

    # Parse live SAs once — keyed by VIP for enrichment.
    sas_by_vip = {}
    for sa in swanctl_parse_sas():
        if sa.get("vip"):
            sas_by_vip[sa["vip"]] = sa

    out = []
    for pl in pool_leases:
        vip      = pl["vip"]
        identity = pl["identity"]
        r        = devices_by_identity.get(identity, {})
        sa       = sas_by_vip.get(vip, {})

        algo     = sa.get("algo")
        algo_fp  = sa.get("algo_fingerprint") or fingerprint_device(algo or "")
        manual_type = r.get("device_type_meta")
        if manual_type:
            device_type = {"label": manual_type, "confidence": 1.0, "source": "manual"}
        elif algo_fp.get("label"):
            device_type = algo_fp
        else:
            device_type = {"label": None, "confidence": 0, "source": None}

        used  = r.get("data_used_bytes")  or 0
        limit = r.get("data_limit_bytes") or 0
        pct   = (used / limit * 100) if limit else 0

        out.append({
            "address":             vip,
            "identity_id":         None,  # was attr-sql identities.id; not used downstream
            "identity_name":       identity,
            "device_id":           r.get("device_id"),
            "device_name":         r.get("device_name") or identity,
            "device_type":         device_type,
            "os_version":          r.get("os_version_meta"),
            "hostname":            r.get("hostname_meta"),
            "customer_id":         r.get("customer_id"),
            "customer_name":       r.get("customer_name") or "(unknown identity)",
            "is_operator":         bool(r.get("is_operator")),
            "data_used_bytes":     used,
            "data_limit_bytes":    limit,
            "data_pct":            round(pct, 1),
            "over_quota":          bool(r.get("over_quota")),
            "tier_name":           r.get("tier_name"),
            "acquired_at":         r.get("acquired_at"),
            "public_ip":           sa.get("remote_ip"),
            "remote_port":         sa.get("remote_port"),
            "ike_proposal":        algo,
            "sa_state":            sa.get("state"),
            "sa_established_secs": sa.get("established_secs"),
            "sa_bytes_in":         sa.get("bytes_in"),
            "sa_bytes_out":        sa.get("bytes_out"),
            "sa_uniqueid":         sa.get("uniqueid"),
            # v1.4.6 — live pool state (not in old addresses-table shape)
            "online":              pl["online"],
            "pool":                pl.get("pool"),
        })
    return out


def swanctl_list_sas() -> str:
    """Raw swanctl --list-sas output. Parsing is the UI's job (different versions differ)."""
    return ssh_903(["docker", "exec", "strongswan",
                    "swanctl", "--uri=tcp://127.0.0.1:4502", "--list-sas"])


# IKE proposal fingerprints for OS/device detection.
# Based on observed client behavior + strongSwan/Android source code signatures.
# NOT authoritative — use as "inferred" badge only, never as primary auth signal.
_IKE_FINGERPRINTS = [
    # (algo_substring, label, confidence)
    # iOS / macOS native IKEv2 client (also strongSwan Apple clients)
    ("AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048", "iOS/macOS", 0.85),
    ("AES_CBC-128/HMAC_SHA1_96/PRF_HMAC_SHA1/MODP_1024",          "iOS/macOS (legacy)", 0.70),
    ("AES_GCM_16-256/PRF_HMAC_SHA2_384/MODP_2048",                "iOS/macOS (modern)", 0.80),
    # Windows 10/11 native IKEv2 (AgileVPN)
    ("AES_GCM_16-256/HMAC_SHA2_384_192/PRF_HMAC_SHA2_384/MODP_2048", "Windows 10/11", 0.90),
    ("AES_GCM_16-128/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048", "Windows 10/11", 0.85),
    ("AES_CBC-256/HMAC_SHA1_96/PRF_HMAC_SHA1/MODP_2048",          "Windows (legacy)", 0.60),
    # strongSwan Android app (uses charon-cmd by default)
    ("AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/ECP_256",   "strongSwan Android", 0.90),
    ("AES_GCM_16-256/PRF_HMAC_SHA2_256/ECP_256",                  "strongSwan Android", 0.85),
    # strongSwan desktop client (Linux/macOS/Windows)
    ("AES_CBC-128/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048",  "strongSwan desktop", 0.75),
    # Linux NetworkManager-strongswan
    ("AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_1536",  "NetworkManager",     0.70),
]


def fingerprint_device(algo_str: str) -> dict:
    """Heuristic device-type detection from IKE proposal string.

    Returns {label, confidence, source: "inferred"|null}.
    """
    if not algo_str:
        return {"label": None, "confidence": 0, "source": None}
    for needle, label, conf in _IKE_FINGERPRINTS:
        if needle == algo_str or needle in algo_str:
            return {"label": label, "confidence": conf, "source": "inferred"}
    return {"label": None, "confidence": 0, "source": None}


# swanctl --list-sas parser — extracts structured data for the UI.
# Format (strongSwan 6.x):
#   rw-eap: #22, ESTABLISHED, IKEv2, <spi_i>_i <spi_r>_r*
#     local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]
#     remote '192.168.10.18' @ 102.182.117.43[4500] EAP: 'saalieg-laptop' [10.99.0.2]
#     AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
#     established 614s ago, rekeying in 79344s, reauth in 78406s
#     net: #3, reqid 1, INSTALLED, TUNNEL-in-UDP, ESP:AES_CBC-256/HMAC_SHA2_256_128
#       installed 614s ago, rekeying in 2648s, expires in 3346s
#       in  cbe261ee, 4199276 bytes, 52155 packets,     0s ago
#       out 040b08d2, 128451591 bytes, 105627 packets,     0s ago
#       local  0.0.0.0/0
#       remote 10.99.0.2/32
# SPIs end with _i / _r role markers; responder SPI also gets a trailing *
_SA_HEADER_RE = re.compile(
    r"^(?P<conn>\S+):\s+#(?P<id>\d+),\s+(?P<state>\S+),\s+(?P<version>\S+),\s+"
    r"(?P<spi_i>[0-9a-f]+)_i\s+"
    r"(?P<spi_r>[0-9a-f]+)_r\*?\s*$"
)
_SA_LOCAL_RE  = re.compile(
    r"local\s+'(?P<id>[^']*)'\s+@\s+(?P<ip>\S+?)\[(?P<port>\d+)\]"
)
_SA_REMOTE_RE = re.compile(
    r"remote\s+'(?P<id>[^']*)'"
    r"\s+@\s+(?P<ip>\S+?)\[(?P<port>\d+)\]"
    r"(?:\s+EAP:\s+'(?P<eap_id>[^']*)')?"
    r"(?:\s+\[(?P<vip>\d+\.\d+\.\d+\.\d+)\])?"
)
_SA_ALGO_RE   = re.compile(r"^\s*([A-Z][A-Z0-9_/-]+(?:/[A-Z0-9_]+)+)\s*$")
_SA_ESTAB_RE  = re.compile(r"established\s+(\d+)s")
_SA_INOUT_RE  = re.compile(
    r"^\s+(?P<dir>in|out)\s+(?P<spi>[0-9a-f]+),\s+"
    r"(?P<bytes>\d+)\s+bytes,\s+(?P<pkts>\d+)\s+packets"
)


def _parse_sas_text(raw: str) -> list:
    """Pure parser for swanctl --list-sas output. Testable without SSH/charon.

    Returns list of dicts: {uniqueid, conn, state, version, local_id, local_ip,
    local_port, remote_id, eap_id, remote_ip, remote_port, vip, algo,
    algo_fingerprint, established_secs, bytes_in, bytes_out, pkts_in, pkts_out}.

    remote_id is set to the EAP identity (when present) so the UI shows the
    username (saalieg-laptop) instead of the client's public IP. Falls back to
    the IKE identity (ip-based) for non-EAP conns (rw-psk).
    """
    sas = []
    cur = None
    in_child = False
    for line in raw.splitlines():
        m = _SA_HEADER_RE.match(line)
        if m:
            cur = {
                "uniqueid":         int(m.group("id")),
                "conn":             m.group("conn"),
                "state":            m.group("state"),
                "version":          m.group("version"),
                "local_id":         None, "local_ip": None, "local_port": None,
                "remote_id":        None, "eap_id": None,
                "remote_ip":        None, "remote_port": None,
                "vip":              None,
                "algo":             None, "algo_fingerprint": None,
                "established_secs": None,
                "bytes_in": 0, "bytes_out": 0, "pkts_in": 0, "pkts_out": 0,
            }
            in_child = False
            sas.append(cur)
            continue
        if not cur:
            continue
        m = _SA_LOCAL_RE.search(line)
        if m:
            cur["local_id"]   = m.group("id")
            cur["local_ip"]   = m.group("ip")
            cur["local_port"] = int(m.group("port"))
            continue
        m = _SA_REMOTE_RE.search(line)
        if m:
            cur["remote_id"]   = m.group("id")
            cur["eap_id"]      = m.group("eap_id")
            cur["remote_ip"]   = m.group("ip")
            cur["remote_port"] = int(m.group("port"))
            if m.group("vip"):
                cur["vip"] = m.group("vip")
            # Prefer EAP username as the displayed remote_id (matches the
            # rw-eap.conf eap-X block and the customers.devices.device_name).
            if cur["eap_id"]:
                cur["remote_id"] = cur["eap_id"]
            continue
        # Algorithm line — bare token list, not preceded by 'in'/'out'/'local'/'remote'
        if not in_child:
            stripped = line.strip()
            if stripped and not stripped.startswith(("net:", "local", "remote",
                                                      "in ", "out ", "installed",
                                                      "established", "rekeying",
                                                      "reauth", "expires")):
                if "/" in stripped and " " not in stripped:
                    cur["algo"] = stripped
                    cur["algo_fingerprint"] = fingerprint_device(stripped)
                    continue
        m = _SA_ESTAB_RE.search(line)
        if m:
            cur["established_secs"] = int(m.group(1))
            continue
        if line.strip().startswith("net:"):
            in_child = True
            continue
        m = _SA_INOUT_RE.match(line)
        if m:
            if m.group("dir") == "in":
                cur["bytes_in"]  = int(m.group("bytes"))
                cur["pkts_in"]   = int(m.group("pkts"))
            else:
                cur["bytes_out"] = int(m.group("bytes"))
                cur["pkts_out"]  = int(m.group("pkts"))
    return sas


def swanctl_parse_sas() -> list:
    """Fetch swanctl --list-sas and parse it. Thin wrapper around _parse_sas_text."""
    try:
        return _parse_sas_text(swanctl_list_sas())
    except Exception:
        return []


# swanctl --list-pools --leases parser — extract live pool state.
# Format (strongSwan 6.x):
#   rw-pool              10.99.0.1                           1 / 1 / 254
#     10.99.0.1                      offline  'safwaan-laptop'
#     10.99.0.2                      online   'saalieg-laptop'
# The header line is the pool summary (name, base addr, used/total/size).
# Indented (2+ spaces) lines are individual leases: VIP, status, identity.
_POOL_LEASE_RE = re.compile(
    r"^\s+(?P<vip>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<status>online|offline)\s+"
    r"'(?P<identity>[^']*)'\s*$"
)


def _parse_pool_leases_text(raw: str) -> list:
    """Pure parser for swanctl --list-pools --leases output. Returns list of
    dicts: {pool, vip, status, identity, online}. Testable without SSH/charon.
    """
    leases = []
    current_pool = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):
            # Header line: pool_name base_ip used/total/size
            parts = line.split()
            if parts:
                current_pool = parts[0]
            continue
        m = _POOL_LEASE_RE.match(line)
        if m:
            leases.append({
                "pool":     current_pool,
                "vip":      m.group("vip"),
                "status":   m.group("status"),
                "identity": m.group("identity"),
                "online":   m.group("status") == "online",
            })
    return leases


def swanctl_list_pool_leases() -> list:
    """Fetch live pool leases from charon. Returns [] on error (no leases)."""
    try:
        out = ssh_903([
            "docker", "exec", "strongswan",
            "swanctl", "--uri=tcp://127.0.0.1:4502",
            "--list-pools", "--leases",
        ])
        return _parse_pool_leases_text(out)
    except Exception:
        return []


def swanctl_list_pools() -> list:
    """Parse 'name  base  size' lines."""
    out = ssh_903(["docker", "exec", "strongswan",
                   "swanctl", "--uri=tcp://127.0.0.1:4502", "--list-pools"])
    pools = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            pools.append({"name": parts[0], "base": parts[1], "size": parts[2]})
    return pools


def ipban_list_bans() -> list:
    """Query ipBan SQLite IPAddresses table. State > 0 = currently banned."""
    try:
        out = ssh_903(["sudo", "sqlite3", "-json", "/opt/ipban/ipban.sqlite",
                       "SELECT IPAddressText AS ip, FailedLoginCount AS count, "
                       "UserName AS user_name, Source AS source, BanDate AS ban_date, "
                       "BanEndDate AS ban_end_date, State AS state "
                       "FROM IPAddresses WHERE State > 0 ORDER BY BanDate DESC;"])
    except HTTPException:
        return []
    return json.loads(out) if out.strip() else []


def firewalld_whitelist() -> list:
    try:
        out = ssh_903(["sudo", "firewall-cmd", "--zone=trusted", "--list-sources"])
    except HTTPException:
        return []
    # firewalld may print sources space-separated on one or more lines
    tokens = out.replace("\n", " ").split()
    return [{"cidr": t} for t in tokens if "/" in t or t.count(".") == 3]


# ---------- Models ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class UnbanRequest(BaseModel):
    ip: str = Field(..., pattern=r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")


class WhitelistAddRequest(BaseModel):
    cidr: str = Field(..., pattern=r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")


# ---------- v1.2.7 — Operator client onboarding ----------
# Cherry-picked from v1.2.5 (reflog a09a478). Tested primitives:
#   - NTLM hash computation
#   - read / atomic write / reload charon creds for rw-eap.conf
#   - append new EAP block (idempotent on identity)
#   - replace existing EAP block's secret (used by rotate, not in this PR)

DEVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$")
SLUG_RE        = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")  # customers.name + users.name
EMAIL_RE       = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")          # RFC 5322 lite
RW_EAP_CONF    = os.environ.get("RW_EAP_CONF",        "/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf")
BACKUP_DIR     = os.environ.get("RW_EAP_BACKUP_DIR",   "/home/zunaid/strongswan/swanctl/conf.d/.backups")
ALLOWED_DEVICE_TYPES = {"iOS", "Android", "Windows", "macOS", "Linux", "Other"}


def ntlm_hash_bytes(pw: str) -> bytes:
    """NTLM = MD4(UTF-16-LE(password)) — 16 raw bytes, what charon expects in users.password."""
    pw_utf16 = pw.encode("utf-16-le")
    r = subprocess.run(
        ["openssl", "dgst", "-md4", "-provider", "legacy", "-binary"],
        input=pw_utf16, capture_output=True, check=True,
    )
    return r.stdout


def read_rw_eap_conf() -> str:
    """Read rw-eap.conf from VPN_HOST (LXC 903 lab or VPS, via env vars). Returns empty string on failure."""
    try:
        return ssh_903(["cat", RW_EAP_CONF])
    except HTTPException:
        return ""


def write_rw_eap_conf(content: str) -> None:
    """Atomic write: backup first, then write."""
    ts = int(time.time())
    backup_path = f"{BACKUP_DIR}/rw-eap.conf.bak-portal-{ts}"
    ssh_903(["mkdir", "-p", BACKUP_DIR])
    ssh_903(["cp", RW_EAP_CONF, backup_path])
    # Write via stdin over SSH (no shell escaping issues)
    r = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=accept-new",
         f"root@{VPN_HOST}", "cat > " + RW_EAP_CONF],
        input=content.encode(), capture_output=True, timeout=SSH_TIMEOUT,
    )
    if r.returncode != 0:
        raise HTTPException(502, f"write conf failed: {r.stderr.decode(errors='replace')[:200]}")


def reload_charon_creds() -> None:
    """swanctl --load-creds inside the strongSwan container."""
    ssh_903(["docker", "exec", "strongswan", "swanctl",
             "--uri=tcp://127.0.0.1:4502", "--load-creds"])


def append_eap_block(identity: str, password: str) -> None:
    """Append a new EAP block to rw-eap.conf if not present (idempotent on id)."""
    conf = read_rw_eap_conf()
    block_id = f"eap-{identity}"
    if re.search(rf"^\s*{re.escape(block_id)}\s*\{{", conf, re.MULTILINE):
        raise HTTPException(409, f"EAP block '{block_id}' already exists in rw-eap.conf")
    addition = (
        f"\n  {block_id} {{\n"
        f"    id     = {identity}\n"
        f'    secret = "{password}"\n'
        f"  }}\n"
    )
    if not conf.rstrip().endswith("}"):
        raise HTTPException(500, "rw-eap.conf has unexpected shape (no trailing '}')")
    new_conf = conf.rstrip()[:-1].rstrip() + addition + "}\n"
    write_rw_eap_conf(new_conf)


def eap_block_exists(identity: str) -> bool:
    conf = read_rw_eap_conf()
    block_id = f"eap-{identity}"
    return bool(re.search(rf"^\s*{re.escape(block_id)}\s*\{{", conf, re.MULTILINE))


def ensure_tier(name: str, display_name: str, data_limit_bytes: int) -> int:
    """Look up tier by name; if missing, create it. Return tier_id.

    Used by POST /api/customers when the operator picks a custom cap on the fly.
    Tier name is auto-generated (custom_<N>mb_<ts>) to avoid collisions.
    """
    rows = db_query(f"SELECT id, is_active FROM tiers WHERE name = {_q(name)};")
    if rows:
        if not rows[0].get("is_active"):
            raise HTTPException(400, f"tier '{name}' is archived (is_active=0); pick another")
        return rows[0]["id"]
    ts = int(time.time())
    db_exec(
        f"INSERT INTO tiers (name, display_name, data_limit_bytes, price_zar, is_active, created_at, notes) "
        f"VALUES ({_q(name)}, {_q(display_name)}, {int(data_limit_bytes)}, NULL, 1, {ts}, "
        f"{_q('auto-created by v1.2.7 portal onboarding')});"
    )
    new = db_query(f"SELECT id FROM tiers WHERE name = {_q(name)};")
    return new[0]["id"]


def slugify(s: str) -> str:
    """Best-effort slug for customers.name from a display name. Operator can override."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:32] or "client"


# Speed-plan lookup table (v1.5.0). Per-customer, NOT tier-driven.
# Tiers (tier_5gb/tier_10gb/tier_20gb) only set DATA QUOTA, not bandwidth.
# Operator picks the speed plan at customer creation; explicit bandwidth_*
# overrides win.
SPEED_PLANS = {
    "standard":         {"bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20},
    "asymmetric_40_20": {"bandwidth_down_mbps": 40, "bandwidth_up_mbps": 20},
}


def resolve_bandwidth(
    speed_plan: Optional[str],
    explicit_down: Optional[int],
    explicit_up: Optional[int],
) -> tuple[int, int]:
    """Return (down_mbps, up_mbps) for a new customer.

    Precedence (high to low):
      1. Explicit bandwidth_down_mbps / bandwidth_up_mbps from the request body
         (advanced override). Both must be provided if either is — partial is an error.
      2. speed_plan lookup ('standard' or 'asymmetric_40_20').
      3. Default = 'standard' (20/20).
    """
    # If either explicit value is set, both must be — partial is ambiguous.
    if (explicit_down is None) != (explicit_up is None):
        raise HTTPException(
            400,
            "bandwidth_down_mbps and bandwidth_up_mbps must be provided together "
            "(both or neither). To use a preset, set speed_plan instead."
        )

    if explicit_down is not None and explicit_up is not None:
        return int(explicit_down), int(explicit_up)

    if speed_plan is not None:
        if speed_plan not in SPEED_PLANS:
            raise HTTPException(
                400,
                f"speed_plan must be one of {sorted(SPEED_PLANS.keys())}, got '{speed_plan}'"
            )
        plan = SPEED_PLANS[speed_plan]
        return plan["bandwidth_down_mbps"], plan["bandwidth_up_mbps"]

    # Default: standard (20/20 symmetric). Matches existing schema default.
    return SPEED_PLANS["standard"]["bandwidth_down_mbps"], SPEED_PLANS["standard"]["bandwidth_up_mbps"]


def validate_bandwidth(down_mbps: int, up_mbps: int) -> None:
    """Bounds-check the resolved bandwidth. 1..1000 mbps per the existing schema."""
    if not 1 <= down_mbps <= 1000:
        raise HTTPException(400, f"bandwidth_down_mbps must be 1..1000, got {down_mbps}")
    if not 1 <= up_mbps <= 1000:
        raise HTTPException(400, f"bandwidth_up_mbps must be 1..1000, got {up_mbps}")


# ---------- v1.2.7 Pydantic models ----------
class ClientCreate(BaseModel):
    # Customer
    name:             Optional[str] = Field(None, min_length=1, max_length=32,
                                           description="URL-safe slug; auto-derived from display_name if omitted")
    display_name:     str           = Field(..., min_length=1, max_length=128)
    billing_id:       Optional[str] = Field(None, max_length=128)
    email:            Optional[str] = Field(None, max_length=128)
    telegram_username: Optional[str] = Field(None, max_length=64)
    notes:            Optional[str] = Field(None, max_length=1024)
    # Tier — either existing tier_name OR 'custom' with custom_cap_mb
    tier_name:        str           = Field(..., description="Existing tier name (e.g. 'tier_5gb', 'tier_10gb', 'tier_20gb') OR 'custom'")
    custom_cap_mb:    Optional[int] = Field(None, ge=1, le=1024*1024,
                                           description="Cap in MiB. Required iff tier_name=='custom'")
    # v1.5.0 — Speed plan (per-customer, NOT tier-driven). Two preset options:
    #   'standard'           → 20/20 mbps symmetric (default; matches existing default)
    #   'asymmetric_40_20'   → 40 mbps down / 20 mbps up
    # Precedence: explicit bandwidth_down_mbps / bandwidth_up_mbps (below) wins.
    # If both omitted → defaults to 'standard'.
    speed_plan:       Optional[Literal["standard", "asymmetric_40_20"]] = Field(
        None,
        description="Per-customer bandwidth preset. 'standard' (20/20) or 'asymmetric_40_20' (40/20). "
                    "If both speed_plan and explicit bandwidth_* are provided, explicit wins."
    )
    # Per-customer bandwidth (advanced override). Wins over speed_plan when set.
    bandwidth_down_mbps: Optional[int] = Field(None, ge=1, le=1000,
                                               description="Override download mbps (1..1000). Wins over speed_plan.")
    bandwidth_up_mbps:   Optional[int] = Field(None, ge=1, le=1000,
                                               description="Override upload mbps (1..1000). Wins over speed_plan.")
    # Device (1 creds = 1 device, per v1.2.6 model)
    device_name:      str           = Field(..., min_length=1, max_length=32)
    device_type:      str           = Field(..., description="iOS/Android/Windows/macOS/Linux/Other")
    os_version:       Optional[str] = Field(None, max_length=32)


# ---------- Routes ----------
@app.get("/api/health")
def health():
    """Public. Service + DB + charon reachability."""
    db_ok, db_count, charon_ok = False, 0, False
    err = None
    try:
        rows = db_query("SELECT id FROM customers;")
        db_count = len(rows)
        db_ok = True
    except Exception as e:
        err = f"db: {e}"
    try:
        ssh_903(["docker", "exec", "strongswan", "true"], timeout=5)
        charon_ok = True
    except Exception as e:
        err = (err + " | " if err else "") + f"charon: {e}"
    return {
        "status": "ok" if (db_ok and charon_ok) else "degraded",
        "db_ok": db_ok, "db_customers": db_count,
        "charon_ok": charon_ok,
        "vpn_host": VPN_HOST,
        "ts": datetime.utcnow().isoformat() + "Z",
        "error": err,
    }


@app.get("/api/admin/audit")
def admin_audit(
    request: Request,
    since: Optional[int] = None,         # unix epoch; default = last 24h
    limit: int = 100,                    # max rows to return
    action: Optional[str] = None,        # substring match on action column
    actor: Optional[str] = None,         # exact match on actor column
):
    """CP7 — operator audit trail. Returns recent audit_log rows.

    Auth: requires a valid operator session cookie. Returns 401 otherwise.
    Query params:
      - since: unix epoch; default = now - 24h
      - limit: cap rows (default 100, max 1000)
      - action: filter to actions containing this substring (e.g. "login", "delete")
      - actor: filter to exact actor match (e.g. "admin", "portal", "system")
    """
    # Inline auth check (mirrors require_session dep). Doing it manually here so
    # the endpoint can be defined anywhere in the file without depending on the
    # order of FastAPI dep registration.
    import portal_auth as _pa
    sess_cookie = request.cookies.get(_pa.OPERATOR_COOKIE)
    if not sess_cookie:
        raise HTTPException(401, "Not authenticated")
    sess = _pa.verify_operator_session(sess_cookie)
    if not sess:
        raise HTTPException(401, "Session expired")
    if limit < 1: limit = 1
    if limit > 1000: limit = 1000
    if since is None: since = int(time.time()) - 86400

    where = [f"created_at >= {int(since)}"]
    if action:
        # SQL injection guard: action is a filter keyword, not user-supplied SQL
        safe_action = action.replace("'", "''")
        where.append(f"action LIKE '%{safe_action}%'")
    if actor:
        safe_actor = actor.replace("'", "''")
        where.append(f"actor = '{safe_actor}'")

    rows = db_query(
        f"SELECT id, actor, action, target_type, target_id, payload, created_at "
        f"FROM audit_log WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC LIMIT {int(limit)};"
    )
    # Parse payload JSON, fall back to raw string
    out = []
    for r in rows:
        try:
            payload = _json.loads(r["payload"]) if r["payload"] else None
        except (ValueError, TypeError):
            payload = r["payload"]
        out.append({
            "id": r["id"],
            "actor": r["actor"],
            "action": r["action"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "payload": payload,
            "ts": datetime.utcfromtimestamp(r["created_at"]).isoformat() + "Z",
        })
    return {"rows": out, "count": len(out), "since": int(since), "limit": int(limit)}


@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    # Unix socket requests have no client info (request.client is None).
    # Behind nginx, prefer X-Forwarded-For from trusted proxy.
    ip = (
        (request.headers.get("x-forwarded-for", "").split(",")[0].strip() if request.headers.get("x-forwarded-for") else None)
        or (request.client.host if request.client else None)
        or "127.0.0.1"
    )
    rate_limit(ip)
    # Multi-operator support: load from OPERATORS_JSON if set, else fall back
    # to the legacy single ADMIN_USER/ADMIN_PASS_HASH pair.
    operators = _load_operators()
    if not operators:
        log.error("no operators configured (set OPERATORS_JSON or ADMIN_USER + ADMIN_PASS_HASH) — refusing login")
        raise HTTPException(503, "Server not configured")
    # Constant-time username lookup across the operator set.
    # Avoid leaking which usernames exist by always running Argon2 verify
    # against a dummy hash when the username doesn't match, and against the
    # real hash for the matched user.
    matched_hash = None
    matched_user = None
    for op_user, op_hash in operators.items():
        if hmac.compare_digest(req.username.encode(), op_user.encode()):
            matched_user = op_user
            matched_hash = op_hash
            break
    if matched_hash is None:
        # No username matched — burn ~70ms on a dummy Argon2 verify to mask
        # username-enumeration timing.
        portal_auth.verify_operator_password(
            "$argon2id$v=19$m=19456,t=2,p=1$YWFhYWFhYWFhYWFhYWFhYQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG",
            req.password or "x",
        )
        log.info("login FAIL (no user) ip=%s identity=%s", ip, req.username)
        raise HTTPException(401, "Invalid credentials")
    pw_match = portal_auth.verify_operator_password(matched_hash, req.password)
    if not pw_match:
        log.info("login FAIL (bad password) ip=%s identity=%s", ip, req.username)
        raise HTTPException(401, "Invalid credentials")
    ua = request.headers.get("user-agent", "")
    session_id = portal_auth.create_operator_session(req.username, ua, ip)
    secure_cookie = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    response.set_cookie(
        key="session",
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=secure_cookie,  # only True when behind HTTPS
        max_age=portal_auth.OPERATOR_TTL,
        path="/",
    )
    log.info("login ok user=%s ip=%s session_id_prefix=%s",
             req.username, ip, session_id[:8])
    # CP7 — audit_log entry for every login
    try:
        payload = _json.dumps({"ip": ip, "ua": ua, "session_id_prefix": session_id[:8]})
        db_exec(f"""INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at)
                    VALUES ('admin', 'operator_login', 'user', 0, '{payload.replace("'", "''")}', strftime('%s','now'));""")
    except Exception as e:
        log.warning("audit_log write failed for login: %s", e)
    return {"ok": True, "user": req.username}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    session_id = request.cookies.get("session")
    if session_id:
        portal_auth.delete_operator_session(session_id)
    response.delete_cookie("session", path="/")
    return {"ok": True}


@app.get("/api/customers")
def list_customers(
    include_archived: bool = False,
    sort_by: str = "name",
    sort_dir: str = "asc",
    _: dict = Depends(require_session),
):
    """List customers with current usage and tier. VIPs are per-device, not per-customer.

    v1.2.12 — ?include_archived=1 shows archived customers too (default: active only).
    Archived = status='archived'. Operators always shown regardless.

    v1.2.14 — ?sort_by=name|usage|tier|created, ?sort_dir=asc|desc. Default: name asc.
    sort_by whitelist enforced server-side (never raw user input into SQL).
    Operators always pinned first regardless of sort, then by the chosen column.
    """
    where = ""
    if not include_archived:
        where = "WHERE c.status = 'active' OR c.is_operator = 1"

    # v1.2.14 — whitelisted ORDER BY. The first ORDER BY is the stable tiebreaker.
    # Map sort_by → (column, type) so we can NULL-handle correctly. NULLS LAST
    # makes sense for usage (0 is "no usage yet") and created_at (operators pinned regardless).
    sort_col_map = {
        "name":    ("c.name", "TEXT"),
        "usage":   ("c.data_used_bytes", "NUM"),
        "tier":    ("t.display_name", "TEXT"),
        "created": ("c.created_at", "NUM"),
    }
    if sort_by not in sort_col_map:
        raise HTTPException(400, f"sort_by must be one of {sorted(sort_col_map.keys())}, got '{sort_by}'")
    if sort_dir not in ("asc", "desc"):
        raise HTTPException(400, "sort_dir must be 'asc' or 'desc'")
    sort_col, sort_type = sort_col_map[sort_by]
    # MariaDB doesn't support NULLS LAST/FIRST. Emulate by sorting NULLs-first via
    # (col IS NULL) then the actual column.  (col IS NULL) returns 1 for NULL, 0
    # for non-NULL.  ASC + (IS NULL DESC) = NULLs last.  DESC + (IS NULL DESC)
    # = NULLs last.
    _nulls_first = "ASC" if sort_dir == "asc" else "DESC"  # NULL first = same dir as sort

    rows = db_query(f"""
        SELECT c.id, c.name, c.display_name, c.telegram_username, c.is_operator,
               c.is_active, c.status, c.data_used_bytes, c.data_limit_bytes,
               c.over_quota, c.billing_id, c.email, c.max_devices,
               c.bandwidth_down_mbps, c.bandwidth_up_mbps,
               t.name AS tier_name, t.display_name AS tier_display,
               t.data_limit_bytes AS tier_limit
        FROM customers c
        LEFT JOIN tiers t ON c.tier_id = t.id
        {where}
        ORDER BY c.is_operator DESC,
                 ({sort_col} IS NULL) {_nulls_first},
                 {sort_col} {sort_dir.upper()},
                 c.name ASC;
    """)
    out = []
    for r in rows:
        used = r.get("data_used_bytes") or 0
        # Limit = tier_limit + any custom additions in data_limit_bytes column
        tier_limit = r.get("tier_limit") or 0
        custom_add = (r.get("data_limit_bytes") or 0) - tier_limit
        quota = tier_limit + max(custom_add, 0)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "display_name": r.get("display_name"),
            "telegram_username": r.get("telegram_username"),
            "is_operator": bool(r["is_operator"]),
            "is_active": bool(r["is_active"]),
            "status": r["status"],
            "tier": r["tier_name"],
            "tier_display": r.get("tier_display"),
            "billing_id": r.get("billing_id"),
            "email": r.get("email"),
            "max_devices": r.get("max_devices"),
            "bandwidth_down_mbps": r.get("bandwidth_down_mbps") or 20,
            "bandwidth_up_mbps": r.get("bandwidth_up_mbps") or 20,
            "used_bytes": used,
            "quota_bytes": quota,
            "pct": round(used / quota * 100, 1) if quota else 0,
            "over_quota": bool(r["over_quota"]),
        })
    return out


@app.get("/api/customers/active-sessions")
def customer_active_sessions(_: dict = Depends(require_session)):
    """v1.2.14 — Live count of active IKE_SAs per customer.

    Calls leases_active() (joins strongSwan attr-sql pool with customers/devices)
    and returns a dict {customer_id: active_session_count}. Customers with no
    active sessions are omitted.

    Cost: ~50ms (one swanctl --list-sas call). Safe to poll at 30s.
    """
    leases = leases_active()
    counts: dict[int, int] = {}
    for l in leases:
        cid = l.get("customer_id")
        if cid is None:
            continue
        counts[cid] = counts.get(cid, 0) + 1
    return {"counts": counts, "total_active": sum(counts.values())}


# ---------- v1.2.7 — POST /api/customers (operator creates a new client) ----------
@app.post("/api/customers")
def create_client(req: ClientCreate, _user: dict = Depends(require_session)):
    """Operator-only: create a new customer + their single device + creds.

    One-shot transaction:
      1. Validate inputs (name shape, email, tier, device_type).
      2. Resolve tier — either existing by name OR auto-create a new tier from
         custom_cap_mb (binary MiB, 1..1M).
      3. Generate password (secrets.token_urlsafe(16)) + NTLM hash.
      4. INSERT customers row (with billing_id, email, telegram_username,
         max_devices=1 per v1.2.6 model).
      5. INSERT users row (EAP identity = '{customer.name}-{device_name}').
      6. INSERT devices row (links customer ↔ user, device_type, os_version).
      7. Append EAP block to rw-eap.conf.
      8. Reload charon creds.
      9. Audit log.
     10. Return {customer, device, password} — password is shown ONCE in the
         portal modal; never logged, never returned again.

    409 on duplicate customers.name or users.name, 400 on invalid inputs,
    502 on rw-eap.conf write failure (rolled back by charon not seeing block).
    """
    # 1. Validate
    if req.tier_name == "custom":
        if req.custom_cap_mb is None:
            raise HTTPException(400, "custom_cap_mb is required when tier_name='custom'")
        if req.custom_cap_mb < 1:
            raise HTTPException(400, "custom_cap_mb must be >= 1 MiB")
    elif req.custom_cap_mb is not None:
        raise HTTPException(400, "custom_cap_mb must be omitted when tier_name is an existing tier")

    if req.email and not EMAIL_RE.match(req.email):
        raise HTTPException(400, f"email '{req.email}' is not a valid address")

    if req.device_type not in ALLOWED_DEVICE_TYPES:
        raise HTTPException(400, f"device_type must be one of {sorted(ALLOWED_DEVICE_TYPES)}")

    if not DEVICE_NAME_RE.match(req.device_name):
        raise HTTPException(400, "device_name must be alphanumeric + dash, 1-32 chars, no leading dash")
    if ".." in req.device_name or "/" in req.device_name:
        raise HTTPException(400, "device_name cannot contain '..' or '/'")

    # customers.name — explicit or derived from display_name
    cust_name = req.name.strip() if req.name else slugify(req.display_name)
    if not SLUG_RE.match(cust_name):
        raise HTTPException(400, f"customer name '{cust_name}' must be alphanumeric + dash/underscore, 1-32 chars, no leading dash")

    eap_identity = f"{cust_name}-{req.device_name}"

    # v1.2.7.2 — collision guard. The EAP identity is f"{cust_name}-{device_name}".
    # If device_name equals cust_name, EAP identity becomes "X-X" (useless, ugly).
    # If device_name starts with "{cust_name}-", EAP identity becomes "X-X-..."
    # (duplicates the customer stem). Both cases are user mistakes, not intent —
    # reject with a clear message. (Real-world bug: Zun typed "Zayd-iphone" while
    # customer slug was "Zayd" → EAP identity "Zayd-Zayd-iphone".)
    if req.device_name.lower() == cust_name.lower():
        raise HTTPException(
            400,
            f"device_name '{req.device_name}' duplicates the customer name '{cust_name}' "
            f"(would yield EAP identity '{cust_name}-{req.device_name}'). "
            f"Use a different device name (e.g. 'iphone', 'laptop', 'pixel9')."
        )
    if req.device_name.lower().startswith(cust_name.lower() + "-"):
        raise HTTPException(
            400,
            f"device_name '{req.device_name}' starts with the customer name '{cust_name}-' "
            f"(would yield EAP identity '{cust_name}-{req.device_name}' with a duplicated prefix). "
            f"Drop the '{cust_name}-' prefix (e.g. use 'iphone' instead of '{req.device_name}')."
        )

    if not SLUG_RE.match(eap_identity):
        raise HTTPException(400, f"derived EAP identity '{eap_identity}' is too long (max 32)")

    # 1b. v1.5.0 — Resolve bandwidth (speed_plan + explicit override).
    # Per-customer, NOT tier-driven (Bug-fix from roadmap: tier drives data
    # quota, NOT bandwidth). Defaults to 'standard' (20/20) if both omitted.
    bandwidth_down_mbps, bandwidth_up_mbps = resolve_bandwidth(
        req.speed_plan, req.bandwidth_down_mbps, req.bandwidth_up_mbps
    )
    validate_bandwidth(bandwidth_down_mbps, bandwidth_up_mbps)

    # 2. Resolve tier
    if req.tier_name == "custom":
        ts = int(time.time())
        tier_name = f"custom_{req.custom_cap_mb}mb_{ts}"
        tier_display = f"Custom {req.custom_cap_mb} MiB"
        data_limit = req.custom_cap_mb * 1024 * 1024  # binary MiB
        tier_id = ensure_tier(tier_name, tier_display, data_limit)
    else:
        rows = db_query(f"SELECT id, data_limit_bytes, is_active FROM tiers WHERE name = {_q(req.tier_name)};")
        if not rows:
            raise HTTPException(400, f"tier '{req.tier_name}' does not exist")
        if not rows[0].get("is_active"):
            raise HTTPException(400, f"tier '{req.tier_name}' is archived")
        tier_id = rows[0]["id"]
        data_limit = rows[0]["data_limit_bytes"]
        tier_name = req.tier_name
        tier_display = None

    # 3. Uniqueness
    if db_query(f"SELECT id FROM customers WHERE name = {_q(cust_name)};"):
        raise HTTPException(409, f"customer '{cust_name}' already exists")
    if db_query(f"SELECT id FROM users WHERE name = {_q(eap_identity)};"):
        raise HTTPException(409, f"EAP identity '{eap_identity}' already exists in users")
    if eap_block_exists(eap_identity):
        raise HTTPException(409, f"EAP block 'eap-{eap_identity}' already exists in rw-eap.conf")

    # 4-6. Generate + insert
    password = secrets.token_urlsafe(16)
    ntlm = ntlm_hash_bytes(password)
    now = int(time.time())

    # We insert customers + users + devices; on failure of 7-8, we need to roll
    # back DB rows. SQLite here is just files via SSH; we have no transaction
    # support over the boundary. Compensate by deleting in reverse on later
    # failure (best-effort).
    cust_id = None
    user_id = None
    dev_id  = None
    try:
        db_exec(
            f"INSERT INTO customers (name, display_name, telegram_username, is_operator, is_active, "
            f"over_quota, data_limit_bytes, data_used_bytes, tier_id, status, max_devices, "
            f"bandwidth_down_mbps, bandwidth_up_mbps, "
            f"created_at, updated_at, notes, billing_id, email) VALUES "
            f"({_q(cust_name)}, {_q(req.display_name)}, {_q(req.telegram_username)}, 0, 1, "
            f"0, {int(data_limit)}, 0, {int(tier_id)}, 'active', 1, "
            f"{int(bandwidth_down_mbps)}, {int(bandwidth_up_mbps)}, "
            f"{now}, {now}, {_q(req.notes)}, {_q(req.billing_id)}, {_q(req.email)});"
        )
        cust_id = db_query(f"SELECT id FROM customers WHERE name = {_q(cust_name)};")[0]["id"]

        db_exec(
            f"INSERT INTO users (name, password) VALUES ({_q(eap_identity)}, X'{ntlm.hex().upper()}');"
        )
        user_id = db_query(f"SELECT id FROM users WHERE name = {_q(eap_identity)};")[0]["id"]

        db_exec(
            f"INSERT INTO devices (customer_id, strongswan_user_id, device_name, device_type, "
            f"os_version, notes, is_active, created_at, updated_at) VALUES "
            f"({int(cust_id)}, {int(user_id)}, {_q(req.device_name)}, {_q(req.device_type)}, "
            f"{_q(req.os_version)}, {_q(req.notes)}, 1, {now}, {now});"
        )

        # v1.4.0 — Bug #2: populate customers.user_id with the user's PK.
        # Operator customers (is_operator=1) have no user and skip this path.
        db_exec(
            f"UPDATE customers SET user_id = {int(user_id)} WHERE id = {int(cust_id)};"
        )
        dev_id = db_query(f"SELECT id FROM devices WHERE device_name = {_q(req.device_name)} "
                          f"AND customer_id = {int(cust_id)};")[0]["id"]

        # Phase 4.3 + 4.7 (RADIUS migration): write radcheck + usergroup rows
        # for the new customer. Best-effort: if RADIUS DB write fails here, the
        # customer is still in MariaDB and PSK still works (charon uses
        # rw-eap.conf for now). Phase 5 cutover will read from FreeRADIUS.
        try:
            portal_auth.add_customer_radcheck(
                eap_identity, password, ntlm.hex().upper()
            )
            portal_auth.add_customer_usergroup(eap_identity, "default")
        except Exception as e:
            log.warning(
                f"Phase 4B RADIUS write failed for {eap_identity} "
                f"(non-fatal until Phase 5 cutover): {e}"
            )

        # 7. EAP block
        append_eap_block(eap_identity, password)

        # 8. Reload charon
        reload_charon_creds()
    except Exception as e:
        # Best-effort rollback
        log.error(f"v1.2.7 create_client failed at sub-step; rolling back: {e}")
        if dev_id:  db_exec(f"DELETE FROM devices WHERE id = {int(dev_id)};")
        if user_id: db_exec(f"DELETE FROM users   WHERE id = {int(user_id)};")
        if cust_id: db_exec(f"DELETE FROM customers WHERE id = {int(cust_id)};")
        # If we already appended the EAP block, try to remove it (best-effort)
        if eap_block_exists(eap_identity):
            try:
                conf = read_rw_eap_conf()
                pat = re.compile(
                    rf"\n?\s*eap-{re.escape(eap_identity)}\s*\{{[^{{}}]*?\}}\n?",
                    re.DOTALL,
                )
                write_rw_eap_conf(pat.sub("", conf, count=1))
            except Exception:
                pass
        raise

    # 9. Audit
    _audit("zun", "create_client", {
        "_target_type": "customer",
        "_target_id":   cust_id,
        "customer_name": cust_name,
        "display_name":  req.display_name,
        "billing_id":    req.billing_id,
        "email":         req.email,
        "tier":          tier_name,
        "device_name":   req.device_name,
        "device_type":   req.device_type,
        "os_version":    req.os_version,
        "eap_identity":  eap_identity,
    })

    # 10. Return one-shot response
    return {
        "customer": {
            "id":             cust_id,
            "name":           cust_name,
            "display_name":   req.display_name,
            "billing_id":     req.billing_id,
            "email":          req.email,
            "telegram_username": req.telegram_username,
            "tier":           tier_name,
            "tier_display":   tier_display,
            "is_active":      True,
            "is_operator":    False,
            "max_devices":    1,
            "status":         "active",
            "data_used_bytes": 0,
            "data_limit_bytes": data_limit,
            "notes":          req.notes,
            "created_at":     now,
            "updated_at":     now,
        },
        "device": {
            "id":          dev_id,
            "customer_id": cust_id,
            "device_name": req.device_name,
            "device_type": req.device_type,
            "os_version":  req.os_version,
            "is_active":   True,
            "created_at":  now,
            "updated_at":  now,
        },
        "eap_identity": eap_identity,
        "password":     password,   # ONE-SHOT — never returned again
    }


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: int, _: dict = Depends(require_session)):
    """Customer detail incl. devices, recent alerts, purchases, audit_log."""
    cust = db_query(f"""
        SELECT c.id, c.name, c.display_name, c.telegram_id, c.telegram_username,
               c.is_operator, c.is_active, c.status, c.data_used_bytes,
               c.data_limit_bytes, c.over_quota, c.notes, c.created_at, c.updated_at,
               c.billing_id, c.email,
               c.bandwidth_down_mbps, c.bandwidth_up_mbps, c.max_devices,
               t.name AS tier_name, t.display_name AS tier_display,
               t.data_limit_bytes AS tier_limit
        FROM customers c
        LEFT JOIN tiers t ON c.tier_id = t.id
        WHERE c.id = {int(customer_id)};
    """)
    if not cust:
        raise HTTPException(404, "Customer not found")
    cust = cust[0]
    devices = db_query(f"""
        SELECT id, device_name, device_type, os_version, hostname,
               is_active, last_seen_v4, last_seen_at, notes
        FROM devices WHERE customer_id = {int(customer_id)}
        ORDER BY (last_seen_at IS NULL) ASC, last_seen_at DESC, device_name;
    """)
    alerts = db_query(f"""
        SELECT id, threshold, sent_at, customer_notified, data_used_bytes_at_alert
        FROM alerts WHERE customer_id = {int(customer_id)}
        ORDER BY sent_at DESC LIMIT 20;
    """)
    purchases = db_query(f"""
        SELECT id, tier_id, data_added_bytes, data_used_before, data_used_reset,
               created_at, notes FROM purchases
        WHERE customer_id = {int(customer_id)} ORDER BY created_at DESC LIMIT 20;
    """)
    audit = db_query(f"""
        SELECT id, actor, action, payload, created_at FROM audit_log
        WHERE target_type = 'customer' AND target_id = {int(customer_id)}
        ORDER BY created_at DESC LIMIT 20;
    """)
    tier_limit = cust.get("tier_limit") or 0
    custom_add = (cust.get("data_limit_bytes") or 0) - tier_limit
    quota = tier_limit + max(custom_add, 0)
    used = cust.get("data_used_bytes") or 0

    # v1.2.7 — current_session: server-side join of active leases for this customer
    current_session = None
    try:
        leases = leases_active()
        for lease in leases:
            if lease.get("customer_id") == int(customer_id):
                current_session = {
                    "public_ip":  lease.get("public_ip"),
                    "remote_port": lease.get("remote_port"),
                    "vip":        lease.get("address"),
                    "device":     lease.get("device_name"),
                    "since":      lease.get("acquired_at"),
                    "ike":        lease.get("ike_proposal"),
                    "sa_state":   lease.get("sa_state"),
                    "bytes_in":   lease.get("sa_bytes_in"),
                    "bytes_out":  lease.get("sa_bytes_out"),
                    "established_secs": lease.get("sa_established_secs"),
                }
                break
    except Exception:
        pass  # non-fatal; just no live session

    return {
        **{k: v for k, v in cust.items() if k not in ("tier_limit",)},
        "tier": cust.get("tier_name"),
        "tier_display": cust.get("tier_display"),
        "used_bytes": used,
        "quota_bytes": quota,
        "pct": round(used / quota * 100, 1) if quota else 0,
        "is_operator": bool(cust["is_operator"]),
        "is_active": bool(cust["is_active"]),
        "over_quota": bool(cust["over_quota"]),
        "billing_id": cust.get("billing_id"),
        "email":      cust.get("email"),
        "current_session": current_session,
        "devices": devices,
        "alerts": alerts,
        "purchases": purchases,
        "audit_log": audit,
    }


@app.get("/api/tiers")
def list_tiers(_: dict = Depends(require_session)):
    rows = db_query("""
        SELECT id, name, display_name, data_limit_bytes, price_zar, is_active, notes
        FROM tiers ORDER BY data_limit_bytes;
    """)
    return [{
        "id": r["id"],
        "name": r["name"],
        "display_name": r["display_name"],
        "quota_bytes": r["data_limit_bytes"],
        "price_zar": r["price_zar"],
        "is_active": bool(r["is_active"]),
        "notes": r["notes"],
    } for r in rows]


# v1.7.0 — Expose SPEED_PLANS so the frontend (Create + Edit modals) can render
# the dropdown options without duplicating the list. Single source of truth.
@app.get("/api/speed_plans")
def list_speed_plans(_: dict = Depends(require_session)):
    """Per-customer speed-plan presets. Tiers drive DATA QUOTA, not bandwidth."""
    return [
        {"name": name, "bandwidth_down_mbps": plan["bandwidth_down_mbps"],
         "bandwidth_up_mbps": plan["bandwidth_up_mbps"]}
        for name, plan in sorted(SPEED_PLANS.items())
    ]


@app.get("/api/quota/{customer_id}")
def get_quota(customer_id: int, _: dict = Depends(require_session)):
    rows = db_query(f"""
        SELECT c.data_used_bytes, c.data_limit_bytes, c.over_quota,
               t.data_limit_bytes AS tier_limit
        FROM customers c LEFT JOIN tiers t ON c.tier_id = t.id
        WHERE c.id = {int(customer_id)};
    """)
    if not rows:
        raise HTTPException(404, "Customer not found")
    r = rows[0]
    used = r.get("data_used_bytes") or 0
    tier_limit = r.get("tier_limit") or 0
    custom_add = (r.get("data_limit_bytes") or 0) - tier_limit
    quota = tier_limit + max(custom_add, 0)
    pct = round(used / quota * 100, 1) if quota else 0
    return {
        "customer_id": customer_id,
        "used_bytes": used,
        "quota_bytes": quota,
        "pct": pct,
        "state": "exceeded" if pct >= 100 else ("near" if pct >= 80 else "ok"),
        "over_quota": bool(r["over_quota"]),
    }


def _restore_radcheck_from_rw_eap_backup(customer_id: int) -> dict:
    """Phase 5+ reset bug fix (2026-07-06): after a 100% cut, radcheck
    Cleartext-Password gets replaced with `DISABLED-<16hex>`. The plaintext
    customer password is preserved IN NO PLACE server-side as plaintext —
    `users.password` is a BLOB of the NTLM hash (one-way). The ONLY on-disk
    copy of the plaintext lives in the latest pre-cut rw-eap.conf backup
    (filename = `.backups/rw-eap.conf.bak-quotamon-<unix_epoch>` — written
    just before the `KILLED-<hex>` substitution).

    This helper:
      1. SELECTs the customer's EAP identity (users.name joined via devices)
      2. SELECTs the latest backup file newer than any current KILLED block
      3. Streams the backup, finds `eap-<identity> { id = <identity> ...
         secret = "..." ... }`, extracts the plaintext secret
      4. Computes NTLM hash; calls portal_auth.enable_customer_radcheck()
         which REPLACES the DISABLED marker with real Cleartext-Password +
         NT-Password in MariaDB radcheck table. Future charon eap-radius
         authentications ACCEPT the customer's password.

    Returns a step dict for the audit log. Idempotent:
      - If no DISABLED row exists for this username (e.g., customer was
        never cut), ok=True, already_enabled=True, no-op.
      - If customer has no devices (operator, archived-without-devices),
        ok=True, skipped=True.
      - If newest backup doesn't have an eap-<identity> block (cut happened
        BEFORE this customer was migrated to Phase 5+), ok=False with
        "no secret_in_backup". The reset still proceeds for other steps;
        the operator must run /rotate_eap to set a fresh password.

    Security note: the plaintext secret is held in a Python str only during
    this function's execution; it is passed verbatim to enable_customer_radcheck
    and not logged.
    """
    import json as _json
    step = {"step": "restore_radcheck", "ok": False}

    # 1. Get the customer's EAP identity (one device row, since v1.2.6 enforces 1:1)
    devs = db_query(
        f"SELECT u.name AS eap_identity "
        f"FROM devices d JOIN users u ON u.id = d.strongswan_user_id "
        f"WHERE d.customer_id = {int(customer_id)};"
    )
    if not devs or not devs[0].get("eap_identity"):
        return {**step, "ok": True, "skipped": True,
                "reason": "no_devices_or_eap_identity"}

    eap_identity = devs[0]["eap_identity"]

    # 2. Is the radcheck row currently DISABLED? (Skip if not — nothing to fix.)
    try:
        with portal_auth._db() as conn:
            rc_rows = conn.execute(
                "SELECT attribute, value FROM radcheck WHERE username = :u",
                {"u": eap_identity},
            ).fetchall()
        if not rc_rows:
            # No radcheck rows at all = cut never triggered for this user
            # (no DISABLED marker was inserted). Nothing to restore.
            return {**step, "ok": True, "skipped": True,
                    "reason": "no_radcheck_rows_for_user"}
        cp_value = next((r["value"] for r in rc_rows
                         if r["attribute"] == "Cleartext-Password"), None)
        if cp_value and not cp_value.startswith("DISABLED-"):
            # Already has a real password. No-op.
            return {**step, "ok": True, "skipped": True,
                    "reason": "radcheck_already_enabled"}
    except Exception as e:
        log.warning("radcheck pre-check failed for %s: %s", eap_identity, e)
        # Fall through — try restore anyway, enable_customer_radcheck is idempotent.

    # 3. Find the latest backup that contains eap-<identity> with a real secret.
    try:
        ls_out = ssh_903(["ls", "-1", BACKUP_DIR + "/"])
        candidates = sorted(
            [f.strip() for f in ls_out.splitlines()
             if f.strip().startswith("rw-eap.conf.bak-quotamon-")]
        )
    except HTTPException as e:
        return {**step, "ok": False, "error": f"list_backups_failed: {e.detail}"}

    if not candidates:
        return {**step, "ok": False, "error": "no_pre_cut_backups_on_disk"}

    plaintext = None
    used_backup = None
    # Walk from newest to oldest — the most recent backup before the cut is
    # what we want. Limit to the last 10 to bound read cost.
    for backup_name in reversed(candidates[-10:]):
        try:
            bak = ssh_903(["cat", f"{BACKUP_DIR}/{backup_name}"])
        except HTTPException:
            continue
        # Find the eap-<identity> block: starts with `eap-<identity> {`,
        # ends with matching `}`. Capture `secret = "..."` inside.
        m = re.search(
            rf"eap-{re.escape(eap_identity)}\s*\{{[^}}]*"
            rf"secret\s*=\s*\"([^\"]*)\"[^}}]*\}}",
            bak, flags=re.DOTALL,
        )
        if m:
            plaintext = m.group(1)
            used_backup = backup_name
            break

    if not plaintext or plaintext.startswith("KILLED-"):
        return {**step, "ok": False,
                "error": "no_secret_in_any_backup",
                "backups_scanned": len(candidates[-10:])}

    # 4. Push to radcheck.
    try:
        ntlm = ntlm_hash_bytes(plaintext).hex().upper()
    except Exception as e:
        return {**step, "ok": False, "error": f"ntlm_compute_failed: {e}"}

    try:
        portal_auth.enable_customer_radcheck(eap_identity, plaintext, ntlm)
    except Exception as e:
        log.error("enable_customer_radcheck(%s) failed: %s", eap_identity, e)
        return {**step, "ok": False, "error": f"enable_customer_radcheck_failed: {e}",
                "eap_identity": eap_identity, "backup": used_backup}

    log.info("radcheck restored for %s from %s (Phase 5+ reset)", eap_identity, used_backup)
    return {**step, "ok": True,
            "eap_identity": eap_identity,
            "backup": used_backup,
            # Never log the plaintext secret. ntlm-safe (one-way, can't recover pw).
            "nt_hash_hex": ntlm[:8] + "..."}


@app.post("/api/quota/{customer_id}/reset")
def reset_quota(customer_id: int, _: dict = Depends(require_session)):
    """Full operator reset for a customer. Does everything `reset_demo.sh` does, idempotently:

      1. data_used_bytes → 0, over_quota → 0 in customers
      2. Restore radcheck Cleartext-Password + NT-Password from latest
         rw-eap.conf backup (Phase 5+ — without this, RADIUS rejects auth
         and the customer's iPhone shows "unable to connect" after cut+reset)
      3. Detect KILLED EAP secrets in rw-eap.conf → restore from backup,
         reload charon creds (Phase <5 fallback)
      4. Zero iptables FORWARD counters for the customer's VIPs
      5. Clear the quota-monitor session sidecar so it re-baselines
      6. Audit-log each step

    Safe to run repeatedly; no-ops when nothing needs resetting.
    Returns a per-step report so the UI can show what happened.
    """
    import json as _json
    cu_rows = db_query(f"SELECT id, name, data_used_bytes, over_quota FROM customers WHERE id = {int(customer_id)};")
    if not cu_rows:
        raise HTTPException(404, "Customer not found")
    cu = cu_rows[0]
    steps = []

    # 1. DB reset
    db_exec(f"UPDATE customers SET data_used_bytes = 0, over_quota = 0, updated_at = strftime('%s','now') WHERE id = {int(customer_id)};")
    db_reset_from = cu.get("data_used_bytes", 0)
    steps.append({"step": "db_reset", "ok": True, "reset_from_bytes": db_reset_from})

    # 2. Phase 5+ RADIUS: restore radcheck Cleartext-Password + NT-Password
    #    from the latest pre-cut rw-eap.conf backup. Phase 4 quota-monitor
    #    (cut_100pct) replaces radcheck with a DISABLED-<16hex> marker; without
    #    restoring it, charon eap-radius rejects every auth after reset and the
    #    customer's iPhone stays disconnected.
    steps.append(_restore_radcheck_from_rw_eap_backup(int(customer_id)))

    # 3. Restore EAP secrets if KILLED
    # v1.6.7 — Bug fix: must query the EAP IDENTITY (users.name), not device_name.
    # rw-eap.conf blocks use `id = zade-cellphone` (EAP identity format =
    # `{customer.name}-{device.device_name}`), not `cellphone`. The old code
    # checked `"zade-cellphone" in ["cellphone"]` which always failed, so
    # KILLED detection missed every customer after a hard cut. Caught by Zun
    # 2026-06-25: "I reset zade data usage after he reach the hard cut but
    # he was still unable to connect back to the vpn after the data reset".
    devs = db_query(
        f"SELECT u.name AS eap_identity "
        f"FROM devices d JOIN users u ON u.id = d.strongswan_user_id "
        f"WHERE d.customer_id = {int(customer_id)};"
    )
    secret_restored = False
    secret_devices = []
    backup_path = ""

    if devs:
        dev_names = [d.get("eap_identity") for d in devs if d.get("eap_identity")]

        # 2a. Read the current conf file (ssh_903 with no bash -c)
        try:
            conf = ssh_903(["cat", RW_EAP_CONF])
        except HTTPException as e:
            conf = ""
            steps.append({"step": "read_conf", "ok": False, "error": str(e.detail)})

        # 2b. Find latest backup via `ls -1` + local sort (avoids bash -c)
        try:
            ls_out = ssh_903(["ls", "-1", BACKUP_DIR + "/"])
            files = [f.strip() for f in ls_out.splitlines()
                     if f.strip().startswith("rw-eap.conf.bak-quotamon-")]
            if files:
                # Filenames include unix epoch — newest is the largest number
                files.sort()
                backup_path = BACKUP_DIR + "/" + files[-1]
        except HTTPException as e:
            steps.append({"step": "find_backup", "ok": False, "error": str(e.detail)})

        # 2c. Detect KILLED secrets for any of this customer's devices.
        # Parse the conf locally: find blocks "id = X\nsecret = Y" and check Y for KILLED.
        # State machine: track current block's id; on each secret line, check if it's
        # KILLED AND belongs to one of our customer's devices.
        killed_devs = []
        if conf:
            current_id = None
            for line in conf.splitlines():
                m_id = re.match(r"^\s*id\s*=\s*(\S+)\s*$", line)
                m_sec = re.match(r"^\s*secret\s*=\s*\"([^\"]*)\"", line)
                if m_id:
                    current_id = m_id.group(1)
                elif m_sec and current_id in dev_names:
                    if m_sec.group(1).startswith("KILLED"):
                        killed_devs.append(current_id)
                    current_id = None
                elif line.strip() == "}":
                    current_id = None
            secret_devices = killed_devs

        # 2d. If any KILLED, restore backup + reload charon
        if secret_devices and backup_path:
            try:
                ssh_903(["cp", backup_path, RW_EAP_CONF])
                ssh_903([
                    "docker", "exec", "strongswan",
                    "swanctl", "--uri=tcp://127.0.0.1:4502", "--load-creds"
                ])
                secret_restored = True
                steps.append({"step": "restore_secret", "ok": True,
                              "backup": backup_path, "devices": secret_devices})
            except HTTPException as e:
                steps.append({"step": "restore_secret", "ok": False,
                              "error": str(e.detail), "devices": secret_devices})

    # 3. Zero iptables FORWARD counters
    try:
        ssh_903(["iptables-legacy", "-Z", "FORWARD"])
        steps.append({"step": "zero_iptables", "ok": True, "chain": "FORWARD"})
    except HTTPException as e:
        steps.append({"step": "zero_iptables", "ok": False, "error": str(e.detail)})

    # 4. Clear daemon session sidecar
    try:
        ssh_903(["rm", "-f", "/var/run/quota-monitor.session"])
        steps.append({"step": "clear_daemon_sidecar", "ok": True})
    except HTTPException as e:
        steps.append({"step": "clear_daemon_sidecar", "ok": False, "error": str(e.detail)})

    # 5. Audit log entry
    payload = _json.dumps({
        "reset_from_bytes": db_reset_from,
        "secret_restored": secret_restored,
        "secret_devices": secret_devices,
        "steps": steps,
        "actor": "portal",
    })
    payload_escaped = payload.replace("'", "''")
    db_exec(f"""INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at)
                VALUES ('portal', 'reset_quota', 'customer', {int(customer_id)}, '{payload_escaped}', strftime('%s','now'));""")
    log.info("quota reset customer=%s id=%s from=%s steps=%d", cu["name"], customer_id, cu["data_used_bytes"], len(steps))

    return {
        "ok": True,
        "customer": cu["name"],
        "customer_id": int(customer_id),
        "reset_from_bytes": db_reset_from,
        "secret_restored": secret_restored,
        "secret_devices": secret_devices,
        "steps": steps,
    }


@app.get("/api/vpn/sessions")
def list_sessions(_: dict = Depends(require_session)):
    """Active IKE SAs (raw text — charon's --list-sas format is human-readable, not stable JSON)."""
    return {"raw": swanctl_list_sas()}


@app.get("/api/vpn/sessions/parsed")
def list_sessions_parsed(_: dict = Depends(require_session)):
    """Active IKE SAs as structured JSON, keyed by VIP where available."""
    return swanctl_parse_sas()


@app.get("/api/vpn/pools")
def list_pools(_: dict = Depends(require_session)):
    return swanctl_list_pools()


@app.get("/api/vpn/leases")
def list_leases(_: dict = Depends(require_session)):
    """Active virtual-IP leases, joined to customer + device + live SA data.

    Each row shows: VIP, identity (IKE name), device, customer, acquired timestamp.
    The list is empty when no clients are connected.
    """
    return leases_active()


@app.get("/api/security/bans")
def list_bans(_: dict = Depends(require_session)):
    return ipban_list_bans()


@app.get("/api/security/whitelist")
def list_whitelist(_: dict = Depends(require_session)):
    return firewalld_whitelist()


@app.post("/api/security/unban")
def unban(req: UnbanRequest, _: dict = Depends(require_session)):
    """Unban by setting State=0 + BanEndDate=now. ipBan picks up on next poll."""
    db_path = "/opt/ipban/ipban.sqlite"
    # Validate IP exists in ipban first
    rows = ssh_903(["sudo", "sqlite3", "-json", db_path,
                    f"SELECT IPAddressText FROM IPAddresses WHERE IPAddressText='{req.ip}';"])
    found = json.loads(rows) if rows.strip() else []
    if not found:
        raise HTTPException(404, f"IP {req.ip} not found in ipBan database")
    ssh_903(["sudo", "sqlite3", db_path,
             f"UPDATE IPAddresses SET State=0, BanEndDate=strftime('%s','now') "
             f"WHERE IPAddressText='{req.ip}';"])
    log.info("unban ip=%s", req.ip)
    return {"ok": True, "ip": req.ip}


@app.post("/api/security/whitelist/add")
def whitelist_add(req: WhitelistAddRequest, _: dict = Depends(require_session)):
    ssh_903(["sudo", "firewall-cmd", "--zone=trusted", "--add-source", req.cidr])
    log.info("whitelist add cidr=%s", req.cidr)
    return {"ok": True, "cidr": req.cidr}


# ---------- Devices (metadata admin) ----------
class DeviceUpdate(BaseModel):
    device_name: Optional[str] = None
    device_type: Optional[str] = None       # e.g. "iPhone 14 Pro", "Windows 11 laptop"
    os_version:  Optional[str] = None       # e.g. "iOS 18.5", "Windows 11 23H2"
    hostname:    Optional[str] = None       # device hostname (manual entry)
    notes:       Optional[str] = None
    is_active:   Optional[int] = None       # 0 or 1


@app.get("/api/devices")
def list_devices(_: dict = Depends(require_session)):
    """All devices with customer + metadata. For the admin Devices view."""
    sql = """
      SELECT d.id, d.customer_id, d.device_name, d.device_type, d.os_version,
             d.hostname, d.is_active, d.last_seen_v4, d.last_seen_at,
             d.created_at, d.updated_at, d.notes,
             c.name AS customer_name
      FROM devices d
      LEFT JOIN customers c ON c.id = d.customer_id
      ORDER BY c.name, d.device_name;
    """
    try:
        rows = db_query(sql)
    except HTTPException:
        return []
    return rows


@app.get("/api/devices/{device_id}")
def get_device(device_id: int, _: dict = Depends(require_session)):
    rows = db_query(f"""
      SELECT d.id, d.customer_id, d.device_name, d.device_type, d.os_version,
             d.hostname, d.is_active, d.last_seen_v4, d.last_seen_at,
             d.created_at, d.updated_at, d.notes,
             c.name AS customer_name
      FROM devices d
      LEFT JOIN customers c ON c.id = d.customer_id
      WHERE d.id = {int(device_id)};
    """)
    if not rows:
        raise HTTPException(404, "device not found")
    return rows[0]


@app.put("/api/devices/{device_id}")
def update_device(device_id: int, req: DeviceUpdate,
                  _: dict = Depends(require_session)):
    """Update device metadata (manual entry for hostname, OS, type, notes).

    Only updates fields that are explicitly provided (non-None). NULL clears.
    """
    fields = []
    if req.device_name is not None:
        fields.append(f"device_name = {_q(req.device_name)}")
    if req.device_type is not None:
        fields.append(f"device_type = {_q(req.device_type)}")
    if req.os_version is not None:
        fields.append(f"os_version = {_q(req.os_version)}")
    if req.hostname is not None:
        fields.append(f"hostname = {_q(req.hostname)}")
    if req.notes is not None:
        fields.append(f"notes = {_q(req.notes)}")
    if req.is_active is not None:
        fields.append(f"is_active = {int(req.is_active)}")
    if not fields:
        raise HTTPException(400, "no fields to update")
    fields.append("updated_at = strftime('%s','now')")
    sql = f"UPDATE devices SET {', '.join(fields)} WHERE id = {int(device_id)};"
    try:
        db_exec(sql)
    except HTTPException as e:
        raise HTTPException(500, f"db error: {e.detail}")
    # audit
    actor = "portal"
    try:
        _audit(actor, "device_update",
               {"device_id": device_id, "fields": list(req.model_dump(exclude_none=True).keys())})
    except Exception:
        pass
    return get_device(device_id, _={})  # type: ignore


def _q(s: str) -> str:
    """SQLite single-quote escape."""
    return "'" + str(s).replace("'", "''") + "'"


def _audit(actor: str, action: str, payload: dict) -> None:
    """Write to audit_log on LXC 903.

    Schema: actor TEXT, action TEXT, target_type TEXT, target_id INTEGER,
    payload TEXT, created_at INTEGER.
    """
    import json as _json
    raw = _json.dumps(payload, separators=(",", ":"))
    target_type = payload.pop("_target_type", None) if isinstance(payload, dict) else None
    target_id   = payload.pop("_target_id",   None) if isinstance(payload, dict) else None
    sql = (
        f"INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at) "
        f"VALUES ({_q(actor)}, {_q(action)}, "
        f"{_q(target_type) if target_type is not None else 'NULL'}, "
        f"{int(target_id) if target_id is not None else 'NULL'}, "
        f"{_q(raw)}, strftime('%s','now'));"
    )
    try:
        db_exec(sql)
    except HTTPException:
        pass


# ─── SSE: live data stream ──────────────────────────────────────────────
# v1.9.0 — Server-Sent Events. One persistent connection replaces the multiple
# setInterval polls the frontend used to do. Browser opens EventSource and
# receives 'snapshot' events every 2s with fresh leases/customers/pools data.
#
# Why: setInterval in background tabs is throttled to ~1x/min by Chrome/Edge/
# Firefox ("heavy throttling" rule, Chrome 88+ for timers >= 4s). Polling was
# unreliable when the operator was looking at other tabs while traffic flowed.
# SSE pushes data when it's fresh, so the UI shows real-time regardless of
# tab visibility. EventSource auto-reconnects on disconnect.
#
# Requirements (deployment):
#  - nginx: `proxy_buffering off` + `proxy_cache off` on /api/events/stream
#  - response header: `X-Accel-Buffering: no` (disables nginx buffering for
#    this response even if proxy_buffering is on elsewhere)
#  - gunicorn worker: uvicorn.workers.UvicornWorker (already in use)
#  - gunicorn --timeout must be > heartbeat interval (we heartbeat every 30s,
#    timeout is 120s, safe)
#
# Auth: same operator session cookie as other endpoints (Depends(require_session))
#
# MERGED FROM: backup-broken-v1.9.1-pre-reset commit 63ea17a
# PROVEN LIVE: snapshot stream verified 2026-06-28, ~3KB every 2s, 33 customers
@app.get("/api/events/stream")
async def events_stream(request: Request, _: dict = Depends(require_session)):
    async def event_gen():
        # Initial connected event so client knows the stream is live
        yield f": connected at {int(time.time())} v1.9.0\n\n"
        # SSE comment lines (": ...") are heartbeats — keep connection alive
        # through proxies/load-balancers that idle-out long connections.
        last_heartbeat = time.time()
        while True:
            if await request.is_disconnected():
                log.info("sse client disconnected")
                break
            try:
                # Snapshot: every field the operator UI needs to stay live.
                # Keep this small — it's pushed every 2s to every connected
                # operator, so size matters. ~3-5 KB per snapshot typical.
                snapshot = {
                    "ts": int(time.time()),
                    "leases": leases_active(),
                    "pools":  swanctl_list_pools(),
                }
                # Customer quota snapshot — same shape as /api/customers list
                # but only the fields the UI needs to update live:
                #   id, data_used_bytes, data_limit_bytes, over_quota, pct
                # Plus name + is_operator so the table can re-render without
                # a separate fetch.
                try:
                    customers = db_query(
                        "SELECT c.id, c.name, c.is_operator, "
                        "c.data_used_bytes, c.data_limit_bytes, c.over_quota, "
                        "t.data_limit_bytes AS tier_limit "
                        "FROM customers c LEFT JOIN tiers t ON t.id = c.tier_id "
                        "WHERE c.status != 'archived'"
                    )
                    for c in customers:
                        used = c.get("data_used_bytes") or 0
                        limit = c.get("data_limit_bytes") or 0
                        if c.get("is_operator"):
                            pct = 0
                        else:
                            pct = round((used / limit * 100), 1) if limit else 0
                        c["pct"] = pct
                        c["quota_bytes"] = limit  # alias for frontend compat
                        c["used_bytes"] = used    # alias for frontend compat
                    snapshot["customers"] = customers
                except Exception as e:
                    log.warning("sse customers snapshot failed: %s", e)
                    snapshot["customers"] = []

                yield f"event: snapshot\ndata: {_json.dumps(snapshot, separators=(',', ':'))}\n\n"
            except Exception as e:
                log.warning("sse snapshot error: %s", e)
                yield f"event: error\ndata: {_json.dumps({'msg': str(e)})}\n\n"

            # Heartbeat every 30s (keeps connection alive through proxies)
            now = time.time()
            if now - last_heartbeat >= 30:
                yield f": hb {int(now)}\n\n"
                last_heartbeat = now

            await asyncio.sleep(2)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",  # disable nginx buffering for THIS response
            "Connection": "keep-alive",
        },
    )


@app.get("/api/security/deadman")
def deadman(_: dict = Depends(require_session)):
    """ipBan service status + recent banned IP count + last log lines."""
    try:
        svc = ssh_903(["systemctl", "is-active", "ipban"]).strip()
    except HTTPException as e:
        svc = f"error: {e.detail}"
    try:
        log_tail = ssh_903(["sudo", "tail", "-n", "20", "/opt/ipban/logfile.txt"])
    except HTTPException:
        log_tail = ""
    try:
        count_out = ssh_903(["sudo", "sqlite3", "/opt/ipban/ipban.sqlite",
                             "SELECT COUNT(*) FROM IPAddresses WHERE State > 0;"]).strip()
        active_bans = int(count_out) if count_out.isdigit() else 0
    except HTTPException:
        # SSH failed — could be ipBan not installed (e.g. VPS uses OS firewall + fail2ban
        # instead of ipban). Return 0 instead of -1 so the dashboard shows a clean count,
        # not a misleading negative. The `service` field will carry the actual error.
        active_bans = 0
    return {"service": svc, "active_bans": active_bans, "log_tail": log_tail}


# ---------- v1.2.12 — PATCH/Archive/Unarchive/Delete customers ----------
class CustomerUpdate(BaseModel):
    display_name: Optional[str] = None
    telegram_username: Optional[str] = None
    email: Optional[str] = None
    billing_id: Optional[str] = None
    notes: Optional[str] = None
    tier_name: Optional[str] = None  # change tier
    custom_cap_mb: Optional[int] = None  # if tier_name='custom'
    max_devices: Optional[int] = None  # 1..10
    # v1.7.0 — speed_plan in PATCH. Per-customer bandwidth preset.
    # 'standard'         → 20/20 mbps symmetric
    # 'asymmetric_40_20' → 40 down / 20 up
    # 'custom'           → keep raw bandwidth_* fields as the source of truth
    # Precedence (consistent with ClientCreate): explicit bandwidth_* > speed_plan.
    # If only speed_plan provided, it resolves to bandwidth_* via resolve_bandwidth().
    # If neither provided, bandwidth_* columns are NOT touched.
    speed_plan:        Optional[Literal["standard", "asymmetric_40_20", "custom"]] = None
    bandwidth_down_mbps: Optional[int] = None  # 1..1000 (5D per-customer bandwidth)
    bandwidth_up_mbps:   Optional[int] = None  # 1..1000 (5D per-customer bandwidth)


class BulkAction(BaseModel):
    """v1.2.13 — bulk action on multiple customers.
    action: archive | unarchive | delete | change_tier
    customer_ids: list of customer IDs (max 100)
    tier_name: required if action=change_tier
    confirm: required if action=delete; the literal string 'DELETE <N> CUSTOMERS'
    """
    action: str
    customer_ids: list
    tier_name: Optional[str] = None
    confirm: Optional[str] = None

def _remove_eap_block(identity: str) -> bool:
    """Remove an `eap-<identity> { ... }` block from rw-eap.conf. Returns True if found+removed."""
    content = read_rw_eap_conf()
    # Match: eap-<id> { ... }  (block is balanced; iterate char-by-char)
    needle = f"eap-{identity} "
    if needle not in content:
        return False
    idx = content.index(needle)
    # Find opening brace on same line
    brace_open = content.find("{", idx)
    if brace_open == -1:
        return False
    depth = 1
    i = brace_open + 1
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    block_end = i
    # Include preceding whitespace and trailing newline
    start = idx
    while start > 0 and content[start - 1] in " \t":
        start -= 1
    end = block_end
    if end < len(content) and content[end] == "\n":
        end += 1
    new_content = content[:start] + content[end:]
    write_rw_eap_conf(new_content)
    return True


@app.patch("/api/customers/{customer_id}")
def update_customer(customer_id: int, req: CustomerUpdate, user: dict = Depends(require_session)):
    """v1.2.12 — edit customer fields. Operator only. Refuses to edit operators or archive a non-existent customer."""
    cust = db_query(f"SELECT id, name, is_operator, tier_id, data_limit_bytes FROM customers WHERE id = {int(customer_id)};")
    if not cust:
        raise HTTPException(404, "Customer not found")
    cust = cust[0]
    if cust["is_operator"]:
        raise HTTPException(403, "cannot edit the operator account")

    sets = []
    params = []
    if req.display_name is not None:
        sets.append(f"display_name = {_q(req.display_name)}")
    if req.telegram_username is not None:
        sets.append(f"telegram_username = {_q(req.telegram_username)}")
    if req.email is not None:
        if req.email and not EMAIL_RE.match(req.email):
            raise HTTPException(400, f"email '{req.email}' is not a valid address")
        sets.append(f"email = {_q(req.email)}")
    if req.billing_id is not None:
        sets.append(f"billing_id = {_q(req.billing_id)}")
    if req.notes is not None:
        sets.append(f"notes = {_q(req.notes)}")
    if req.max_devices is not None:
        if not 1 <= req.max_devices <= 10:
            raise HTTPException(400, "max_devices must be 1..10")
        sets.append(f"max_devices = {int(req.max_devices)}")
    # v1.7.0 — Bandwidth precedence (consistent with ClientCreate):
    #   1. Explicit bandwidth_down_mbps + bandwidth_up_mbps (both required if either)
    #   2. speed_plan preset ('standard' / 'asymmetric_40_20')
    #   3. 'custom' speed_plan → keep current bandwidth_* untouched
    #   4. No speed_plan AND no explicit → don't touch bandwidth_* columns
    speed_plan_effective = req.speed_plan
    bw_explicit_provided = (req.bandwidth_down_mbps is not None) or (req.bandwidth_up_mbps is not None)
    if bw_explicit_provided:
        if (req.bandwidth_down_mbps is None) != (req.bandwidth_up_mbps is None):
            raise HTTPException(
                400,
                "bandwidth_down_mbps and bandwidth_up_mbps must be provided together "
                "(both or neither). To use a preset, set speed_plan instead.",
            )
        # explicit wins over speed_plan; ignore speed_plan
        speed_plan_effective = None
        bandwidth_down_mbps = int(req.bandwidth_down_mbps)
        bandwidth_up_mbps = int(req.bandwidth_up_mbps)
        validate_bandwidth(bandwidth_down_mbps, bandwidth_up_mbps)
    elif speed_plan_effective is not None and speed_plan_effective != "custom":
        # speed_plan preset resolves to bandwidth
        bandwidth_down_mbps, bandwidth_up_mbps = resolve_bandwidth(
            speed_plan_effective, None, None
        )
    else:
        # speed_plan is None or 'custom' and no explicit → don't touch bandwidth_*
        bandwidth_down_mbps = None
        bandwidth_up_mbps = None
    if bandwidth_down_mbps is not None:
        sets.append(f"bandwidth_down_mbps = {bandwidth_down_mbps}")
    if bandwidth_up_mbps is not None:
        sets.append(f"bandwidth_up_mbps = {bandwidth_up_mbps}")

    # Tier change
    if req.tier_name is not None:
        if req.tier_name == "custom":
            if req.custom_cap_mb is None or req.custom_cap_mb < 1:
                raise HTTPException(400, "custom_cap_mb (>=1) is required when tier_name='custom'")
            ts = int(time.time())
            tier_name = f"custom_{req.custom_cap_mb}mb_{ts}"
            tier_display = f"Custom {req.custom_cap_mb} MiB"
            data_limit = req.custom_cap_mb * 1024 * 1024
            tier_id = ensure_tier(tier_name, tier_display, data_limit)
        else:
            rows = db_query(f"SELECT id, data_limit_bytes, is_active FROM tiers WHERE name = {_q(req.tier_name)};")
            if not rows:
                raise HTTPException(400, f"tier '{req.tier_name}' does not exist")
            if not rows[0].get("is_active"):
                raise HTTPException(400, f"tier '{req.tier_name}' is archived")
            tier_id = rows[0]["id"]
            data_limit = rows[0]["data_limit_bytes"]
        sets.append(f"tier_id = {int(tier_id)}")
        sets.append(f"data_limit_bytes = {int(data_limit)}")

    if not sets:
        # v1.7.0 — speed_plan='custom' sent standalone is a meaningful no-op
        # (operator chose "keep current raw bandwidth values"). Accept as 200
        # instead of 400 to avoid breaking the Edit modal's "Save with no
        # bandwidth change" path.
        if req.speed_plan == "custom" and not bw_explicit_provided:
            return {"ok": True, "customer_id": int(customer_id), "no_op": True}
        raise HTTPException(400, "no fields to update")

    sets.append(f"updated_at = {int(time.time())}")
    sql = f"UPDATE customers SET {', '.join(sets)} WHERE id = {int(customer_id)};"
    db_exec(sql)
    _audit(user.get("name") or "operator", "customer_update", {
        "customer_id": int(customer_id),
        "fields": list(req.model_dump(exclude_none=True).keys()),
    })
    return {"ok": True, "customer_id": int(customer_id)}


# ---------- v1.3.2 — Rotate EAP credentials (Bug #4 fix) ----------

def _replace_eap_secret(identity: str, new_password: str) -> None:
    """Replace the `secret = "..."` line inside the eap-{identity} block.
    Preserves block id (EAP identity is immutable by design — see Lesson #193,
    Bug #4 history). Idempotent only if new_password matches current; otherwise
    raises 404 if block not found.

    The EAP identity in rw-eap.conf is the source of truth for charon. We
    MUST keep it stable across rotations or the customer's Windows laptop
    will silently fail auth (Lesson #193 lineage).
    """
    conf = read_rw_eap_conf()
    block_id = f"eap-{identity}"
    # Match the block: eap-X { ... secret = "..." ... }
    block_pat = re.compile(
        rf"(^\s*{re.escape(block_id)}\s*\{{[^}}]*?secret\s*=\s*)\"[^\"]*\"",
        re.MULTILINE | re.DOTALL,
    )
    if not block_pat.search(conf):
        raise HTTPException(404, f"EAP block '{block_id}' not found in rw-eap.conf")
    new_conf = block_pat.sub(rf'\1"{new_password}"', conf, count=1)
    write_rw_eap_conf(new_conf)


@app.post("/api/customers/{customer_id}/rotate_eap")
def rotate_customer_eap(customer_id: int, user: dict = Depends(require_session)):
    """v1.3.2 — rotate the EAP password for a customer's active device.

    Generates a new random password, computes new NTLM hash, updates
    users.password in DB, replaces the secret inside the existing
    eap-{identity} block in rw-eap.conf (the EAP identity itself is
    preserved — only the password changes), then reloads charon creds.

    Behavior:
      - Identity is NEVER changed (Lesson #193 lineage, Bug #4 history).
      - Customer's Windows laptop will silently fail auth until they
        re-onboard with the new credentials via the installer token flow.
      - The customer's `eap_rotated_at` is set to now so the UI can
        surface a "credentials rotated" banner.
      - Operator does NOT receive the new password in the response —
        it travels to the customer only via the installer token flow
        (defense in depth: operator screen, logs, screenshots all
        excluded from credential exposure).

    Refuses on:
      - Missing customer (404)
      - Operator account (403) — operator has no EAP creds to rotate
      - Archived customer (409)
      - Customer with no devices (409)
    """
    cust_rows = db_query(
        f"SELECT id, name, is_operator, status, user_id FROM customers WHERE id = {int(customer_id)};"
    )
    if not cust_rows:
        raise HTTPException(404, "Customer not found")
    cust = cust_rows[0]
    if cust["is_operator"]:
        raise HTTPException(403, "cannot rotate EAP for the operator account")
    if cust["status"] == "archived":
        raise HTTPException(409, "cannot rotate EAP for an archived customer (unarchive first)")

    # Find the customer's active device (still needed for the is_active check;
    # we keep the devices join as a sanity check that devices and the FK agree).
    devices = db_query(
        f"SELECT id, strongswan_user_id, device_name, is_active FROM devices "
        f"WHERE customer_id = {int(customer_id)} ORDER BY id LIMIT 1;"
    )
    if not devices:
        raise HTTPException(409, "customer has no devices; nothing to rotate")
    dev = devices[0]

    # v1.4.0 — Bug #2: use customers.user_id FK directly when populated.
    # Falls back to devices.strongswan_user_id for pre-migration customers
    # where user_id is still NULL (operator-only, archived, etc.).
    eap_user_id = cust.get("user_id") or dev["strongswan_user_id"]

    # Look up EAP identity from users table
    user_rows = db_query(
        f"SELECT id, name FROM users WHERE id = {int(eap_user_id)};"
    )
    if not user_rows:
        raise HTTPException(500, f"customer points to missing users row id={eap_user_id}")
    eap_identity = user_rows[0]["name"]

    # Generate new password
    new_password = secrets.token_urlsafe(16)
    ntlm = ntlm_hash_bytes(new_password)
    now = int(time.time())

    # 1. Update users.password (BLOB column holds NTLM hash as X'...')
    db_exec(
        f"UPDATE users SET password = X'{ntlm.hex().upper()}' WHERE id = {int(user_rows[0]['id'])};"
    )

    # 2. Replace the secret in rw-eap.conf (preserve EAP identity)
    _replace_eap_secret(eap_identity, new_password)

    # 3. Mark customer as rotated (timestamp column added in v1.3.2 migration)
    db_exec(
        f"UPDATE customers SET eap_rotated_at = {now} WHERE id = {int(customer_id)};"
    )

    # 4. Reload charon creds so the new secret is active
    reload_charon_creds()

    # 4b. Phase 4B (RADIUS migration): write the rotated password to FreeRADIUS
    # radcheck rows. Non-fatal: rw-eap.conf keeps PSK auth working until
    # Phase 5 cutover.
    try:
        portal_auth.update_customer_password_radcheck(
            eap_identity, new_password, ntlm.hex().upper()
        )
    except Exception as e:
        log.warning(
            f"Phase 4B RADIUS write during rotate_eap failed for {eap_identity} "
            f"(non-fatal until Phase 5 cutover): {e}"
        )

    # 5. Audit (the new_password is NOT in the audit payload — no plaintext in logs)
    _audit(user.get("name") or "operator", "customer_eap_rotate", {
        "_target_type": "customer",
        "_target_id":   int(customer_id),
        "customer_name": cust["name"],
        "eap_identity":  eap_identity,
        "device_id":     dev["id"],
        "device_name":   dev["device_name"],
        "rotated_at":    now,
    })

    log.info(
        "EAP rotated customer=%s identity=%s device=%s by=%s",
        cust["name"], eap_identity, dev["device_name"],
        user.get("name") or "operator",
    )

    return {
        "ok": True,
        "customer_id":  int(customer_id),
        "customer_name": cust["name"],
        "eap_identity": eap_identity,
        "eap_rotated_at": now,
        "device_id":    dev["id"],
        "device_name":  dev["device_name"],
        # Operator gets confirmation; new password travels ONLY via installer token flow.
        "next_step":    "customer must re-onboard via the installer token to receive new credentials",
    }


@app.post("/api/customers/{customer_id}/archive")
def archive_customer(customer_id: int, user: dict = Depends(require_session)):
    """v1.2.12 — soft-delete: set status='archived'. Reversible. Keeps all data, devices, audit, leases."""
    cust = db_query(f"SELECT id, name, is_operator, status FROM customers WHERE id = {int(customer_id)};")
    if not cust:
        raise HTTPException(404, "Customer not found")
    if cust[0]["is_operator"]:
        raise HTTPException(403, "cannot archive the operator account")
    if cust[0]["status"] == "archived":
        return {"ok": True, "customer_id": int(customer_id), "already_archived": True}
    db_exec(f"UPDATE customers SET status='archived', is_active=0, updated_at={int(time.time())} WHERE id={int(customer_id)};")

    # Phase 4B (RADIUS migration): replace radcheck Cleartext-Password + NT-Password
    # with DISABLED-<uuid> marker so FreeRADIUS rejects EAP-MSCHAPv2 auth.
    # Look up every device's EAP identity (one customer may have multiple).
    dev_rows = db_query(
        f"SELECT id, device_name, strongswan_user_id FROM devices "
        f"WHERE customer_id = {int(customer_id)};"
    )
    for dev in dev_rows:
        if dev["strongswan_user_id"]:
            id_rows = db_query(
                f"SELECT name FROM users WHERE id = {int(dev['strongswan_user_id'])};"
            )
            if id_rows:
                try:
                    portal_auth.disable_customer_radcheck(id_rows[0]["name"])
                except Exception as e:
                    log.warning(
                        f"Phase 4B RADIUS disable failed for {id_rows[0]['name']}: {e}"
                    )

    _audit(user.get("name") or "operator", "customer_archive", {
        "customer_id": int(customer_id),
        "name": cust[0]["name"],
    })
    return {"ok": True, "customer_id": int(customer_id)}


@app.post("/api/customers/{customer_id}/unarchive")
def unarchive_customer(customer_id: int, user: dict = Depends(require_session)):
    """v1.2.12 — restore an archived customer. Sets status='active', is_active=1.

    Phase 4D (RADIUS migration): we can't reverse-engineer the original
    EAP password from the stored NT hash. Instead, unarchive regenerates
    a fresh EAP password and rewires everything — charon reloads the new
    secret, radcheck Cleartext-Password + NT-Password are written, and
    rw-eap.conf is updated. The customer must re-onboard via the
    installer-token flow (same as rotate_eap) to pick up the new
    credentials. This trades a one-time re-onboard for clean,
    no-edge-case code (no fragile BLOB→hex dance through SQLite -json).

    Earlier implementation tried to derive Cleartext-Password from the
    raw 16-byte MD4 hash stored in users.password BLOB, but sqlite3 -json
    encodes BLOBs inconsistently (x'<hex>' wrapper for safe bytes,
    JSON-escaped string form for non-ASCII) making the path fragile.
    Regenerating the password sidesteps the encoding issue entirely.
    """
    cust = db_query(f"SELECT id, name, is_operator, status FROM customers WHERE id = {int(customer_id)};")
    if not cust:
        raise HTTPException(404, "Customer not found")
    if cust[0]["is_operator"]:
        raise HTTPException(403, "cannot unarchive the operator account")
    if cust[0]["status"] != "archived":
        return {"ok": True, "customer_id": int(customer_id), "already_active": True}
    db_exec(f"UPDATE customers SET status='active', is_active=1, updated_at={int(time.time())} WHERE id={int(customer_id)};")

    # Phase 4D (RADIUS migration): regenerate EAP password.
    # Look up first active device, derive EAP identity via users table.
    devices = db_query(
        f"SELECT id, device_name, strongswan_user_id FROM devices "
        f"WHERE customer_id = {int(customer_id)} ORDER BY id LIMIT 1;"
    )
    regenerated = 0
    if devices:
        dev = devices[0]
        eap_user_id = dev["strongswan_user_id"]
        if eap_user_id:
            id_rows = db_query(f"SELECT name FROM users WHERE id = {int(eap_user_id)};")
            if id_rows:
                eap_identity = id_rows[0]["name"]
                new_password = secrets.token_urlsafe(16)
                ntlm = ntlm_hash_bytes(new_password)
                now = int(time.time())

                # 1. Update users.password BLOB
                db_exec(
                    f"UPDATE users SET password = X'{ntlm.hex().upper()}' "
                    f"WHERE id = {int(eap_user_id)};"
                )
                # 2. Replace rw-eap.conf secret (preserve EAP identity)
                try:
                    _replace_eap_secret(eap_identity, new_password)
                except Exception as ex:
                    log.warning(f"unarchive: could not update rw-eap.conf for {eap_identity}: {ex}")
                # 3. Mark customer as rotated
                db_exec(
                    f"UPDATE customers SET eap_rotated_at = {now} WHERE id = {int(customer_id)};"
                )
                # 4. Phase 4D: write fresh radcheck rows
                try:
                    portal_auth.update_customer_password_radcheck(
                        eap_identity, new_password, ntlm.hex().upper()
                    )
                    regenerated += 1
                except Exception as e:
                    log.warning(
                        f"Phase 4D RADIUS unarchive-rotate failed for {eap_identity}: {e}"
                    )
                # 5. Reload charon creds
                try:
                    reload_charon_creds()
                except Exception as ex:
                    log.warning(f"unarchive: reload_charon_creds failed: {ex}")
                log.info(
                    "unarchive regenerated password for customer=%s identity=%s",
                    cust[0]["name"], eap_identity,
                )

    _audit(user.get("name") or "operator", "customer_unarchive", {
        "customer_id": int(customer_id),
        "name": cust[0]["name"],
        "password_regenerated_for_devices": regenerated,
    })
    return {"ok": True, "customer_id": int(customer_id), "password_regenerated_for_devices": regenerated}


@app.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: int, confirm: str = "", user: dict = Depends(require_session)):
    """v1.2.12 — HARD delete. Cascades: devices, alerts, purchases, audit_log, EAP secret from rw-eap.conf.

    Required: ?confirm=<customer_name>. Returns 400 if name doesn't match (prevents accidental deletes).
    Cannot delete operators.
    """
    cust = db_query(f"SELECT id, name, is_operator FROM customers WHERE id = {int(customer_id)};")
    if not cust:
        raise HTTPException(404, "Customer not found")
    cust = cust[0]
    if cust["is_operator"]:
        raise HTTPException(403, "cannot delete the operator account")
    if confirm != cust["name"]:
        raise HTTPException(400, f"to delete '{cust['name']}', pass ?confirm={cust['name']}")

    # Cascade: devices (links to customers.id), then audit, alerts, purchases
    devices = db_query(f"SELECT id, device_name FROM devices WHERE customer_id = {int(customer_id)};")
    eap_identities = [f"{cust['name']}-{d['device_name']}" for d in devices]
    # v1.2.13 — also clear strongSwan attr-sql pool (users table) so identity isn't orphaned
    if eap_identities:
        in_list = ",".join(_q(i) for i in eap_identities)
        db_exec(f"DELETE FROM users WHERE name IN ({in_list});")
    db_exec(f"DELETE FROM devices WHERE customer_id = {int(customer_id)};")
    db_exec(f"DELETE FROM alerts WHERE customer_id = {int(customer_id)};")
    db_exec(f"DELETE FROM purchases WHERE customer_id = {int(customer_id)};")
    db_exec(f"DELETE FROM audit_log WHERE target_type='customer' AND target_id={int(customer_id)};")

    # Remove EAP block(s) — one per device
    for dev in devices:
        eap_identity = f"{cust['name']}-{dev['device_name']}"
        try:
            _remove_eap_block(eap_identity)
        except Exception as ex:
            log.warning("could not remove eap block %s: %s", eap_identity, ex)

        # Phase 4B (RADIUS migration): delete radcheck + radusergroup + radreply
        # so FreeRADIUS no longer holds stale customer creds.
        try:
            portal_auth.remove_customer_radcheck_and_usergroup(eap_identity)
        except Exception as e:
            log.warning(
                f"Phase 4B RADIUS cleanup failed for {eap_identity}: {e}"
            )
    reload_charon_creds()

    db_exec(f"DELETE FROM customers WHERE id = {int(customer_id)};")
    _audit(user.get("name") or "operator", "customer_delete_hard", {
        "customer_id": int(customer_id),
        "name": cust["name"],
        "devices_deleted": len(devices),
    })
    return {"ok": True, "customer_id": int(customer_id), "devices_deleted": len(devices)}


@app.post("/api/customers/bulk-action")
def bulk_customer_action(req: BulkAction, user: dict = Depends(require_session)):
    """v1.2.13 — Bulk action on multiple customers in one transactional call.

    Supported actions:
    - archive:     status='archived', is_active=0  (reversible via unarchive)
    - unarchive:   status='active',   is_active=1
    - change_tier: UPDATE tier_id + data_limit_bytes (req.tier_name required)
    - delete:      HARD delete with cascade + EAP block removal (req.confirm required)

    Atomicity: All work runs in one BEGIN TRANSACTION inside a single SSH call to LXC 903.
    On any error, the transaction rolls back — no partial state.

    Skipped (not failed): operators (cannot edit/delete) and missing IDs — returned in 'skipped'.
    Cannot delete zun-operator (is_operator=1).

    Audit: ONE row per call, payload includes action + customer_ids + skipped.
    """
    import json as _json
    action = req.action
    ids = req.customer_ids or []
    if not ids:
        raise HTTPException(400, "customer_ids is required")
    if len(ids) > 100:
        raise HTTPException(400, "max 100 customers per bulk call")
    if action not in ("archive", "unarchive", "delete", "change_tier"):
        raise HTTPException(400, f"unknown action '{action}'")
    if action == "change_tier" and not req.tier_name:
        raise HTTPException(400, "tier_name required for change_tier")
    if action == "delete":
        expected = f"DELETE {len(ids)} CUSTOMERS"
        if req.confirm != expected:
            raise HTTPException(400, f"to bulk-delete {len(ids)} customers, pass confirm='{expected}'")

    # Normalize IDs to int, drop dupes, drop non-positive
    clean_ids = []
    seen = set()
    for x in ids:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in seen:
            seen.add(v)
            clean_ids.append(v)
    if not clean_ids:
        raise HTTPException(400, "no valid customer IDs")

    # Resolve tier_id if needed
    tier_id = None
    if action == "change_tier":
        tier_rows = db_query(f"SELECT id, data_limit_bytes FROM tiers WHERE name = {_q(req.tier_name)};")
        if not tier_rows:
            raise HTTPException(404, f"tier '{req.tier_name}' not found")
        tier_id = tier_rows[0]["id"]

    # Build payload to ship to LXC 903
    payload = {
        "action": action,
        "ids": clean_ids,
        "tier_id": tier_id,
    }

    # Single SSH call to LXC 903 — atomic Python script over there.
    # Script lives at /opt/vpn-portal/scripts/bulk_action.py on LXC 903.
    # It reads JSON action spec from stdin, runs in BEGIN IMMEDIATE,
    # COMMITs on success or rolls back on any error.
    out = ssh_903(["sudo", "/opt/vpn-portal/scripts/bulk_action.py"],
                  stdin_text=_json.dumps(payload), timeout=60)

    if not out.strip():
        raise HTTPException(500, "no response from LXC 903")
    try:
        result = _json.loads(out)
    except _json.JSONDecodeError:
        log.error("bulk-action raw output: %r", out[:500])
        raise HTTPException(500, f"unparseable response from LXC 903: {out[:200]}")

    if "error" in result:
        log.error("bulk-action error: %s", result["error"])
        raise HTTPException(500, f"transaction failed and rolled back: {result['error']}")

    # If delete: remove EAP blocks from rw-eap.conf (single reload at end)
    eap_blocks_removed = 0
    if action == "delete" and result.get("eap_targets"):
        for identity in result["eap_targets"]:
            try:
                if _remove_eap_block(identity):
                    eap_blocks_removed += 1
            except Exception as ex:
                log.warning("could not remove eap block %s: %s", identity, ex)
        try:
            reload_charon_creds()
        except Exception as ex:
            log.warning("could not reload charon creds: %s", ex)

    # Audit log (one row for the whole bulk action)
    _audit(user.get("name") or "operator", f"customer_bulk_{action}", {
        "action": action,
        "affected": result.get("affected", []),
        "skipped": result.get("skipped", []),
        "tier_name": req.tier_name if action == "change_tier" else None,
        "devices_deleted": result.get("devices_deleted", 0),
        "eap_blocks_removed": eap_blocks_removed,
    })

    return {
        "ok": True,
        "action": action,
        "affected": result.get("affected", []),
        "skipped": result.get("skipped", []),
        "devices_deleted": result.get("devices_deleted", 0),
        "eap_blocks_removed": eap_blocks_removed,
    }



# ---------- v1.3.0 Customer portal (NTC) ----------

class PortalLoginRequest(BaseModel):
    identity: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


@app.post("/api/csp-report")
async def csp_report(request: Request):
    """CP7/LOW2 — CSP violation report endpoint.

    Browsers POST a JSON report to this URL when CSP blocks a resource.
    Body format: {"csp-report": {"violated-directive": "...", "blocked-uri": "...", ...}}.

    We log at WARN level so they show up in fail2ban-style alerts later.
    No auth (reports are from browsers of unauthenticated visitors).

    Returns 204 No Content to prevent client retries.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=204)
    report = body.get("csp-report", body) if isinstance(body, dict) else {}
    violated = report.get("violated-directive", "?")
    blocked = report.get("blocked-uri", "?")
    doc_uri = report.get("document-uri", "?")
    log.warning(
        "CSP report: violated=%s blocked=%s doc=%s ua=%s",
        violated, blocked, doc_uri,
        request.headers.get("user-agent", "?")[:120],
    )
    return Response(status_code=204)


@app.post("/api/portal/login")
def portal_login(req: PortalLoginRequest, request: Request, response: Response):
    """Customer logs in with their VPN credentials (EAP identity + password).

    Same NTLM hash that charon uses for MSCHAPv2 — no new secrets stored.
    Cookie scoped to Path=/portal/ — cannot access operator routes.
    """
    ip = request.client.host if request.client else "unknown"
    portal_auth._portal_rate_limit(ip)
    ua = request.headers.get("user-agent", "")

    # CORR-2026-07-11-026: case-insensitive login. FreeRADIUS uses MariaDB default collation
    # (utf8_general_ci) so VPN auth is case-insensitive, but charon SQLite lookup is
    # exact-match. Normalize to lowercase so portal web login matches VPN auth behavior.
    normalized_identity = req.identity.strip().lower()
    user = portal_auth.lookup_user_and_customer(normalized_identity)
    if not user:
        log.info("portal login FAIL (no user) ip=%s identity=%s", ip, req.identity)
        raise HTTPException(401, "Invalid credentials")

    if user["customer_status"] != "active":
        log.info("portal login FAIL (inactive customer) ip=%s identity=%s customer=%s status=%s",
                 ip, req.identity, user["customer_name"], user["customer_status"])
        raise HTTPException(401, "Account not active")

    if not user["device_is_active"]:
        log.info("portal login FAIL (inactive device) ip=%s identity=%s device=%s",
                 ip, req.identity, user["device_name"])
        raise HTTPException(401, "Device not active")

    if not portal_auth.verify_password(user["password_hash"], req.password):
        log.info("portal login FAIL (bad password) ip=%s identity=%s customer=%s",
                 ip, req.identity, user["customer_name"])
        raise HTTPException(401, "Invalid credentials")

    # Issue session
    session_id = portal_auth.create_session(
        customer_id=user["customer_id"],
        identity=normalized_identity,
        user_agent=ua,
        ip_address=ip,
    )
    # Cookie scoped to /api/portal/ — browser does NOT send this to /api/*
    # Secure flag controlled by COOKIE_SECURE env (set to true behind HTTPS).
    secure_cookie = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    response.set_cookie(
        key=portal_auth.PORTAL_COOKIE,
        value=session_id,
        httponly=True,
        samesite="strict",
        secure=secure_cookie,
        max_age=portal_auth.PORTAL_TTL,
        path="/api/portal/",
    )
    log.info("portal login OK ip=%s identity=%s customer_id=%s customer=%s",
             ip, req.identity, user["customer_id"], user["customer_name"])
    return {
        "ok": True,
        "customer_id": user["customer_id"],
        "customer_name": user["customer_name"],
        "customer_display_name": user["customer_display_name"],
    }


@app.post("/api/portal/logout")
def portal_logout(request: Request, response: Response,
                  _session: dict = Depends(portal_auth.require_portal_session)):
    """Clear the portal session cookie + delete the session row."""
    sid = request.cookies.get(portal_auth.PORTAL_COOKIE)
    if sid:
        portal_auth.delete_session(sid)
    response.delete_cookie(portal_auth.PORTAL_COOKIE, path="/api/portal/")
    return {"ok": True}


@app.get("/api/portal/usage")
def portal_usage(session: dict = Depends(portal_auth.require_portal_session)):
    """Return tier + usage for the authenticated customer.

    Strictly scoped: customer_id comes from the session cookie, NEVER
    from a request parameter. SQL JOINs tier by id, scoped to the
    authenticated customer.
    """
    customer_id = session["customer_id"]
    cust = portal_auth.lookup_customer_full(customer_id)
    if not cust:
        raise HTTPException(404, "Customer not found")

    used = cust.get("data_used_bytes") or 0
    limit = cust.get("data_limit_bytes") or 0
    is_operator = bool(cust.get("is_operator"))
    no_cap = is_operator or limit == 0

    pct = None
    if not no_cap and limit > 0:
        pct = round(used / limit * 100, 1)

    return {
        "customer_id": cust["id"],
        "customer_name": cust["name"],
        "customer_display_name": cust.get("display_name"),
        "tier_name": cust.get("tier_name"),
        "tier_display": cust.get("tier_display"),
        "data_used_bytes": used,
        "data_limit_bytes": limit,
        "data_pct": pct,
        "no_cap": no_cap,
        "over_quota": bool(cust.get("over_quota")),
        "max_devices": cust.get("max_devices"),
        "is_operator": is_operator,
        "status": cust.get("status"),
    }


@app.get("/api/portal/me")
def portal_me(session: dict = Depends(portal_auth.require_portal_session)):
    """Return the authenticated customer's identity info (name, email, login)."""
    customer_id = session["customer_id"]
    cust = portal_auth.lookup_customer_full(customer_id)
    if not cust:
        raise HTTPException(404, "Customer not found")
    return {
        "customer_id": cust["id"],
        "customer_name": cust["name"],
        "customer_display_name": cust.get("display_name"),
        "email": cust.get("email"),
        "logged_in_as": session["identity"],
        "session_created_at": session["created_at"],
    }


# ---------- installer_tokens (v1.5.0) ----------
# One-time installer tokens for production customer onboarding.
# See installer_tokens.py for full design notes.
# Registered AFTER db_query/db_exec/_q are defined, but BEFORE entrypoint.
import installer_tokens  # noqa: E402
installer_tokens.register(
    app,
    db_query=db_query,
    db_exec=db_exec,
    q=_q,
    audit_fn=_audit,
    require_session_dep=require_session,
)


# ---------- Entrypoint ----------
if __name__ == "__main__":
    import uvicorn
    if not ADMIN_PASS_HASH:
        log.warning("ADMIN_PASS_HASH not set — /api/login will refuse all requests")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
