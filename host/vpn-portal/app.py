#!/usr/bin/env python3
"""
databyte VPN Portal — FastAPI backend (5C.1, MVP)

Single-file app. Reads SQLite from LXC 903 via SSH. Wraps swanctl/ipBan/firewalld.

Endpoints:
  GET  /api/health                     public — service + DB + charon reach
  POST /api/login                      admin auth (bcrypt + HMAC-signed cookie)
  POST /api/logout
  GET  /api/customers                  list w/ tier, used, quota, over_quota, vip
  GET  /api/customers/{id}             + devices[] + alerts[]
  GET  /api/tiers                      tier defs (3GB/10GB/15GB/demo_100MB)
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
  VPN_HOST       LXC 903 IP/host (default 192.168.10.98)
  SSH_KEY        path to SSH private key (default /root/.ssh/id_ed25519_vpn)
  DB_PATH        SQLite on 903 (default /var/lib/strongswan/ipsec.db)
  ADMIN_USER     admin username (default admin)
  ADMIN_PASS_HASH  bcrypt hash of admin password (REQUIRED)
  SESSION_SECRET  HMAC secret (random default; set explicitly for multi-instance)
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
from collections import defaultdict
from datetime import datetime
from typing import Optional

import bcrypt
from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ---------- Config ----------
VPN_HOST        = os.environ.get("VPN_HOST", "192.168.10.98")
SSH_KEY         = os.environ.get("SSH_KEY", "/root/.ssh/id_ed25519_vpn")
DB_PATH         = os.environ.get("DB_PATH", "/var/lib/strongswan/ipsec.db")
ADMIN_USER      = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")
SESSION_SECRET  = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
SESSION_TTL     = int(os.environ.get("SESSION_TTL", "86400"))   # 24h
RATE_LIMIT_PER_MIN = 5
SSH_TIMEOUT     = 10

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vpn-portal")

# ---------- App ----------
app = FastAPI(title="databyte vpn-portal", version="0.1.0")

# Serve frontend (static assets + SPA index)
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
if os.path.isdir(WWW_DIR):
    app.mount("/static", StaticFiles(directory=os.path.join(WWW_DIR, "static")), name="static")

    @app.get("/", include_in_schema=False)
    def root_index():
        return FileResponse(os.path.join(WWW_DIR, "index.html"))

# ---------- Rate limit (in-memory, per-IP) ----------
_login_attempts: dict[str, list[float]] = defaultdict(list)


def rate_limit(ip: str):
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < 60]
    if len(attempts) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Too many login attempts; try again in a minute")
    _login_attempts[ip] = attempts + [now]


# ---------- Session signing (HMAC, no external dep) ----------
def sign_session(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return f"{payload.hex()}.{sig}"


def verify_session(token: str) -> Optional[dict]:
    try:
        payload_hex, sig = token.split(".", 1)
        payload = bytes.fromhex(payload_hex)
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if time.time() - data.get("iat", 0) > SESSION_TTL:
            return None
        return data
    except Exception:
        return None


def require_session(session: Optional[str] = Cookie(None)) -> dict:
    if not session:
        raise HTTPException(401, "Not authenticated")
    data = verify_session(session)
    if not data:
        raise HTTPException(401, "Invalid or expired session")
    return data


# ---------- SSH + DB helpers ----------
def ssh_903(cmd_args: list, timeout: int = SSH_TIMEOUT) -> str:
    """Run a command on the VPN gateway. cmd_args is a list."""
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
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise HTTPException(502, f"VPN gateway error: {r.stderr.strip()[:200]}")
    return r.stdout


def db_query(sql: str) -> list:
    """Query SQLite on the VPN gateway, return list of dicts."""
    out = ssh_903(["sqlite3", "-json", DB_PATH, sql])
    return json.loads(out) if out.strip() else []


def db_exec(sql: str) -> None:
    """Execute non-SELECT SQL on the VPN gateway."""
    ssh_903(["sqlite3", DB_PATH, sql])


# ---------- charon / ipBan / firewalld wrappers ----------
def leases_active() -> list:
    """Currently active virtual-IP leases with customer + device info.

    Joins the strongSwan attr-sql pool with the 5B customers/devices layer.
    Note: identities.data is a BLOB; we CAST to TEXT to make the join work.
    Includes per-customer usage data so the Sessions tab can show how much
    each active session has used vs its tier limit.

    Also enriches each lease with live SA data from swanctl --list-sas:
    public_ip, remote_port, ike_proposal, device_type (inferred from algo).
    Device hostname + OS version come from the devices table if set.
    """
    sql = """
      SELECT
        hex(a.address)        AS hex_addr,
        i.id                 AS identity_id,
        CAST(i.data AS TEXT) AS identity_name,
        d.id                 AS device_id,
        d.device_name        AS device_name,
        d.device_type        AS device_type_meta,
        d.os_version         AS os_version_meta,
        d.hostname           AS hostname_meta,
        c.id                 AS customer_id,
        c.name               AS customer_name,
        c.is_operator        AS is_operator,
        c.data_used_bytes    AS data_used_bytes,
        c.data_limit_bytes   AS data_limit_bytes,
        c.over_quota         AS over_quota,
        c.tier_id            AS tier_id,
        t.name               AS tier_name,
        a.acquired           AS acquired_at
      FROM addresses a
      JOIN identities i ON i.id = a.identity
      LEFT JOIN devices   d ON d.device_name = CAST(i.data AS TEXT)
      LEFT JOIN customers c ON c.id = d.customer_id
      LEFT JOIN tiers     t ON t.id = c.tier_id
      WHERE a.acquired > 0 AND a.released = 0
      ORDER BY a.acquired DESC
    """
    try:
        rows = db_query(sql)
    except HTTPException:
        return []
    # Parse live SAs once — keyed by VIP
    sas_by_vip = {}
    for sa in swanctl_parse_sas():
        if sa.get("vip"):
            sas_by_vip[sa["vip"]] = sa

    out = []
    for r in rows:
        hex_addr = r.get("hex_addr") or ""
        # hex '0A630005' -> '10.99.0.5'
        try:
            ip = ".".join(str(int(hex_addr[i:i+2], 16)) for i in (0, 2, 4, 6))
        except Exception:
            ip = "?"
        used   = r.get("data_used_bytes")  or 0
        limit  = r.get("data_limit_bytes") or 0
        pct    = (used / limit * 100) if limit else 0

        sa = sas_by_vip.get(ip, {})
        algo  = sa.get("algo")
        algo_fp = sa.get("algo_fingerprint") or fingerprint_device(algo or "")
        # Prefer manually-set device_type; fall back to inferred
        manual_type = r.get("device_type_meta")
        if manual_type:
            device_type = {"label": manual_type, "confidence": 1.0, "source": "manual"}
        elif algo_fp.get("label"):
            device_type = algo_fp
        else:
            device_type = {"label": None, "confidence": 0, "source": None}

        out.append({
            "address":           ip,
            "identity_id":       r.get("identity_id"),
            "identity_name":     r.get("identity_name"),
            "device_id":         r.get("device_id"),
            "device_name":       r.get("device_name"),
            "device_type":       device_type,
            "os_version":        r.get("os_version_meta"),
            "hostname":          r.get("hostname_meta"),
            "customer_id":       r.get("customer_id"),
            "customer_name":     r.get("customer_name"),
            "is_operator":       bool(r.get("is_operator")),
            "data_used_bytes":   used,
            "data_limit_bytes":  limit,
            "data_pct":          round(pct, 1),
            "over_quota":        bool(r.get("over_quota")),
            "tier_name":         r.get("tier_name"),
            "acquired_at":       r.get("acquired_at"),
            "public_ip":         sa.get("remote_ip"),
            "remote_port":       sa.get("remote_port"),
            "ike_proposal":      algo,
            "sa_state":          sa.get("state"),
            "sa_established_secs": sa.get("established_secs"),
            "sa_bytes_in":       sa.get("bytes_in"),
            "sa_bytes_out":      sa.get("bytes_out"),
            "sa_uniqueid":       sa.get("uniqueid"),
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
#     local  'vpn.homelab.local' @ 192.168.10.98[4500]
#     remote 'demo-phone' @ 105.174.188.166[51234] [10.99.0.5]
#     AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
#     established 614s ago, rekeying in 79344s, reauth in 78406s
#     net: #3, reqid 1, INSTALLED, TUNNEL-in-UDP, ESP:AES_CBC-256/HMAC_SHA2_256_128
#       installed 614s ago, rekeying in 2648s, expires in 3346s
#       in  cbe261ee, 4199276 bytes, 52155 packets,     0s ago
#       out 040b08d2, 128451591 bytes, 105627 packets,     0s ago
#       local  0.0.0.0/0
#       remote 10.99.0.5/32
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
    r"remote\s+'(?P<id>[^']*)'\s+@\s+(?P<ip>\S+?)\[(?P<port>\d+)\]"
    r"(?:\s+\[(?P<vip>\d+\.\d+\.\d+\.\d+)\])?"
)
_SA_ALGO_RE   = re.compile(r"^\s*([A-Z][A-Z0-9_/-]+(?:/[A-Z0-9_]+)+)\s*$")
_SA_ESTAB_RE  = re.compile(r"established\s+(\d+)s")
_SA_INOUT_RE  = re.compile(
    r"^\s+(?P<dir>in|out)\s+(?P<spi>[0-9a-f]+),\s+"
    r"(?P<bytes>\d+)\s+bytes,\s+(?P<pkts>\d+)\s+packets"
)


def swanctl_parse_sas() -> list:
    """Parse swanctl --list-sas output into structured records keyed by VIP.

    Returns list of dicts: {uniqueid, conn, state, version, local_id, local_ip,
    local_port, remote_id, remote_ip, remote_port, vip, algo, algo_fingerprint,
    established_secs, bytes_in, bytes_out, pkts_in, pkts_out}.
    """
    raw = ""
    try:
        raw = swanctl_list_sas()
    except Exception:
        return []
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
                "remote_id":        None, "remote_ip": None, "remote_port": None,
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
            cur["remote_ip"]   = m.group("ip")
            cur["remote_port"] = int(m.group("port"))
            if m.group("vip"):
                cur["vip"] = m.group("vip")
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
RW_EAP_CONF    = "/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf"
BACKUP_DIR     = "/home/zunaid/strongswan/swanctl/conf.d/.backups"
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
    """Read rw-eap.conf from LXC 903. Returns empty string on failure."""
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
    tier_name:        str           = Field(..., description="Existing tier name (e.g. 'tier_3gb') OR 'custom'")
    custom_cap_mb:    Optional[int] = Field(None, ge=1, le=1024*1024,
                                           description="Cap in MiB. Required iff tier_name=='custom'")
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


@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    ip = request.client.host
    rate_limit(ip)
    if not ADMIN_PASS_HASH:
        log.error("ADMIN_PASS_HASH not set — refusing login")
        raise HTTPException(503, "Server not configured")
    if not hmac.compare_digest(req.username.encode(), ADMIN_USER.encode()):
        # Constant-time compare to avoid username enumeration (length-bake aside)
        raise HTTPException(401, "Invalid credentials")
    pw_bytes = req.password.encode()
    pw_match = bcrypt.checkpw(pw_bytes, ADMIN_PASS_HASH.encode())
    log.info("login debug user=%s pw_repr=%s pw_len=%d hash_repr=%s pw_match=%s",
             req.username, repr(pw_bytes[:30]), len(pw_bytes),
             repr(ADMIN_PASS_HASH[:30]), pw_match)
    if not pw_match:
        raise HTTPException(401, "Invalid credentials")
    token = sign_session({"u": req.username, "iat": time.time()})
    response.set_cookie(key="session", value=token, httponly=True, samesite="lax",
                        max_age=SESSION_TTL, path="/")
    log.info("login ok user=%s ip=%s", req.username, ip)
    return {"ok": True, "user": req.username}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("session", path="/")
    return {"ok": True}


@app.get("/api/customers")
def list_customers(_: dict = Depends(require_session)):
    """List customers with current usage and tier. VIPs are per-device, not per-customer."""
    rows = db_query("""
        SELECT c.id, c.name, c.display_name, c.telegram_username, c.is_operator,
               c.is_active, c.status, c.data_used_bytes, c.data_limit_bytes,
               c.over_quota, c.billing_id, c.email,
               t.name AS tier_name, t.display_name AS tier_display,
               t.data_limit_bytes AS tier_limit
        FROM customers c
        LEFT JOIN tiers t ON c.tier_id = t.id
        ORDER BY c.is_operator DESC, c.name;
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
            "used_bytes": used,
            "quota_bytes": quota,
            "pct": round(used / quota * 100, 1) if quota else 0,
            "over_quota": bool(r["over_quota"]),
        })
    return out


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
    if not SLUG_RE.match(eap_identity):
        raise HTTPException(400, f"derived EAP identity '{eap_identity}' is too long (max 32)")

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
            f"created_at, updated_at, notes, billing_id, email) VALUES "
            f"({_q(cust_name)}, {_q(req.display_name)}, {_q(req.telegram_username)}, 0, 1, "
            f"0, {int(data_limit)}, 0, {int(tier_id)}, 'active', 1, "
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
        dev_id = db_query(f"SELECT id FROM devices WHERE device_name = {_q(req.device_name)} "
                          f"AND customer_id = {int(cust_id)};")[0]["id"]

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
        ORDER BY last_seen_at DESC NULLS LAST, device_name;
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


@app.post("/api/quota/{customer_id}/reset")
def reset_quota(customer_id: int, _: dict = Depends(require_session)):
    """Full operator reset for a customer. Does everything `reset_demo.sh` does, idempotently:

      1. data_used_bytes → 0, over_quota → 0 in customers
      2. Detect KILLED EAP secrets for any of this customer's devices
         → restore from latest pre-cut backup, reload charon creds
      3. Zero iptables FORWARD counters for the customer's VIPs
      4. Clear the quota-monitor session sidecar so it re-baselines
      5. Audit-log each step

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

    # 2. Restore EAP secrets if KILLED
    devs = db_query(
        f"SELECT d.device_name FROM devices d WHERE d.customer_id = {int(customer_id)};"
    )
    secret_restored = False
    secret_devices = []
    backup_path = ""

    if devs:
        dev_names = [d.get("device_name") for d in devs if d.get("device_name")]

        # 2a. Read the current conf file (ssh_903 with no bash -c)
        try:
            conf = ssh_903(["cat", "/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf"])
        except HTTPException as e:
            conf = ""
            steps.append({"step": "read_conf", "ok": False, "error": str(e.detail)})

        # 2b. Find latest backup via `ls -1` + local sort (avoids bash -c)
        try:
            ls_out = ssh_903(["ls", "-1", "/home/zunaid/strongswan/swanctl/conf.d/.backups/"])
            files = [f.strip() for f in ls_out.splitlines()
                     if f.strip().startswith("rw-eap.conf.bak-quotamon-")]
            if files:
                # Filenames include unix epoch — newest is the largest number
                files.sort()
                backup_path = "/home/zunaid/strongswan/swanctl/conf.d/.backups/" + files[-1]
        except HTTPException as e:
            steps.append({"step": "find_backup", "ok": False, "error": str(e.detail)})

        # 2c. Detect KILLED secrets for any of this customer's devices.
        # Parse the conf locally: find blocks "id = X\nsecret = Y" and check Y for KILLED.
        killed_devs = []
        if conf:
            for line in conf.splitlines():
                m_id = re.match(r"^\s*id\s*=\s*(\S+)\s*$", line)
                if m_id and m_id.group(1) in dev_names:
                    dev_in_block = m_id.group(1)
            # Use a simple state-machine parser
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
                ssh_903(["cp", backup_path, "/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf"])
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
        active_bans = -1
    return {"service": svc, "active_bans": active_bans, "log_tail": log_tail}


# ---------- Entrypoint ----------
if __name__ == "__main__":
    import uvicorn
    if not ADMIN_PASS_HASH:
        log.warning("ADMIN_PASS_HASH not set — /api/login will refuse all requests")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
