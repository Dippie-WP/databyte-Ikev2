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
  GET  /api/vpn/pools                  docker exec swanctl --list-pools (parsed)
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
import time
import hmac
import hashlib
import secrets
import subprocess
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
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
def swanctl_list_sas() -> str:
    """Raw swanctl --list-sas output. Parsing is the UI's job (different versions differ)."""
    return ssh_903(["docker", "exec", "strongswan",
                    "swanctl", "--uri=tcp://127.0.0.1:4502", "--list-sas"])


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
    """Parse ipban-ctl list output. Format: IP SOURCE COUNT LAST_BAN"""
    try:
        out = ssh_903(["sudo", "ipban-ctl", "list"])
    except HTTPException:
        return []
    bans = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        bans.append({
            "ip": parts[0],
            "source": parts[1] if len(parts) > 1 else "?",
            "count": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1,
        })
    return bans


def firewalld_whitelist() -> list:
    try:
        out = ssh_903(["sudo", "firewall-cmd", "--zone=trusted", "--list-sources"])
    except HTTPException:
        return []
    return [{"cidr": line.strip()} for line in out.strip().splitlines() if line.strip()]


# ---------- Models ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class UnbanRequest(BaseModel):
    ip: str = Field(..., pattern=r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")


class WhitelistAddRequest(BaseModel):
    cidr: str = Field(..., pattern=r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")


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
    if not bcrypt.checkpw(req.password.encode(), ADMIN_PASS_HASH.encode()):
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
    """List customers with current usage and tier."""
    rows = db_query("""
        SELECT c.id, c.name, c.is_operator, c.vip,
               c.data_used_bytes, c.over_quota,
               t.name AS tier_name, t.quota_bytes
        FROM customers c
        LEFT JOIN tiers t ON c.tier_id = t.id
        ORDER BY c.is_operator DESC, c.name;
    """)
    out = []
    for r in rows:
        used = r.get("data_used_bytes") or 0
        quota = r.get("quota_bytes") or 0
        out.append({
            "id": r["id"],
            "name": r["name"],
            "is_operator": bool(r["is_operator"]),
            "vip": r["vip"],
            "tier": r["tier_name"],
            "used_bytes": used,
            "quota_bytes": quota,
            "pct": round(used / quota * 100, 1) if quota else 0,
            "over_quota": bool(r["over_quota"]),
        })
    return out


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: int, _: dict = Depends(require_session)):
    """Customer detail incl. devices, recent alerts, purchases."""
    cust = db_query(f"""
        SELECT c.id, c.name, c.is_operator, c.vip,
               c.data_used_bytes, c.over_quota,
               t.name AS tier_name, t.quota_bytes
        FROM customers c
        LEFT JOIN tiers t ON c.tier_id = t.id
        WHERE c.id = {int(customer_id)};
    """)
    if not cust:
        raise HTTPException(404, "Customer not found")
    cust = cust[0]
    devices = db_query(f"""
        SELECT id, name, vip, last_seen FROM devices
        WHERE customer_id = {int(customer_id)} ORDER BY last_seen DESC;
    """)
    alerts = db_query(f"""
        SELECT id, kind, message, created_at FROM alerts
        WHERE customer_id = {int(customer_id)}
        ORDER BY created_at DESC LIMIT 20;
    """)
    return {
        **cust,
        "used_bytes": cust.get("data_used_bytes") or 0,
        "quota_bytes": cust.get("quota_bytes") or 0,
        "is_operator": bool(cust["is_operator"]),
        "over_quota": bool(cust["over_quota"]),
        "devices": devices,
        "alerts": alerts,
    }


@app.get("/api/tiers")
def list_tiers(_: dict = Depends(require_session)):
    rows = db_query("SELECT id, name, quota_bytes, is_demo FROM tiers ORDER BY quota_bytes;")
    return [{
        "id": r["id"],
        "name": r["name"],
        "quota_bytes": r["quota_bytes"],
        "is_demo": bool(r["is_demo"]),
    } for r in rows]


@app.get("/api/quota/{customer_id}")
def get_quota(customer_id: int, _: dict = Depends(require_session)):
    rows = db_query(f"""
        SELECT c.data_used_bytes, c.over_quota, t.quota_bytes
        FROM customers c LEFT JOIN tiers t ON c.tier_id = t.id
        WHERE c.id = {int(customer_id)};
    """)
    if not rows:
        raise HTTPException(404, "Customer not found")
    r = rows[0]
    used = r.get("data_used_bytes") or 0
    quota = r.get("quota_bytes") or 0
    pct = round(used / quota * 100, 1) if quota else 0
    return {
        "customer_id": customer_id,
        "used_bytes": used,
        "quota_bytes": quota,
        "pct": pct,
        "state": "exceeded" if pct >= 95 else ("near" if pct >= 80 else "ok"),
        "over_quota": bool(r["over_quota"]),
    }


@app.post("/api/quota/{customer_id}/reset")
def reset_quota(customer_id: int, _: dict = Depends(require_session)):
    """Reset data_used_bytes + over_quota for a customer. Operator-only (no per-customer auth for v1)."""
    cu = db_query(f"SELECT name, data_used_bytes FROM customers WHERE id = {int(customer_id)};")
    if not cu:
        raise HTTPException(404, "Customer not found")
    cu = cu[0]
    db_exec(f"UPDATE customers SET data_used_bytes = 0, over_quota = 0 WHERE id = {int(customer_id)};")
    db_exec(f"""INSERT INTO audit_log (customer_id, kind, message, created_at)
                VALUES ({int(customer_id)}, 'reset', 'Manual reset via portal', strftime('%s','now'));""")
    log.info("quota reset customer=%s id=%s from=%s", cu["name"], customer_id, cu["data_used_bytes"])
    return {"ok": True, "customer": cu["name"], "reset_from_bytes": cu["data_used_bytes"]}


@app.get("/api/vpn/sessions")
def list_sessions(_: dict = Depends(require_session)):
    """Active IKE SAs (raw text — charon's --list-sas format is human-readable, not stable JSON)."""
    return {"raw": swanctl_list_sas()}


@app.get("/api/vpn/pools")
def list_pools(_: dict = Depends(require_session)):
    return swanctl_list_pools()


@app.get("/api/security/bans")
def list_bans(_: dict = Depends(require_session)):
    return ipban_list_bans()


@app.get("/api/security/whitelist")
def list_whitelist(_: dict = Depends(require_session)):
    return firewalld_whitelist()


@app.post("/api/security/unban")
def unban(req: UnbanRequest, _: dict = Depends(require_session)):
    ssh_903(["sudo", "ipban-ctl", "unban", req.ip])
    log.info("unban ip=%s", req.ip)
    return {"ok": True, "ip": req.ip}


@app.post("/api/security/whitelist/add")
def whitelist_add(req: WhitelistAddRequest, _: dict = Depends(require_session)):
    ssh_903(["sudo", "firewall-cmd", "--zone=trusted", "--add-source", req.cidr])
    log.info("whitelist add cidr=%s", req.cidr)
    return {"ok": True, "cidr": req.cidr}


@app.get("/api/security/deadman")
def deadman(_: dict = Depends(require_session)):
    try:
        return {"raw": ssh_903(["sudo", "ipban-ctl", "deadman", "status"])}
    except HTTPException as e:
        return {"error": str(e.detail)}


# ---------- Entrypoint ----------
if __name__ == "__main__":
    import uvicorn
    if not ADMIN_PASS_HASH:
        log.warning("ADMIN_PASS_HASH not set — /api/login will refuse all requests")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
