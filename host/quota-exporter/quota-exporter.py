#!/usr/bin/env python3
"""
quota-exporter.py — Prometheus exporter for the 5B quota layer.

Bridges the strongSwan attr-sql pool (10.99.0.0/24) + customer/device layer
to Prometheus time series. Without this, the per-customer view is only
available in the 5C VPN portal — operators have no time-series on it.

Data sources (every SCRAPE_INTERVAL seconds):
  - SQLite at /var/lib/strongswan/ipsec.db:
      customers, tiers, devices, addresses, identities,
      alerts, audit_log
  - iptables-legacy FORWARD chain:
      per-VIP byte counters (the `quota:VIP` comments, source of truth
      for cumulative per-connection traffic)

Exposes on http://0.0.0.0:9102/metrics

Metrics:
  vpn_customer_info                          {customer,tier,is_operator,is_active} 1
  vpn_customer_data_used_bytes               {customer,tier}                       gauge
  vpn_customer_data_limit_bytes              {customer,tier}                       gauge
  vpn_customer_over_quota                    {customer}                            gauge (0|1)
  vpn_customer_is_active                     {customer}                            gauge (0|1)
  vpn_active_lease_bytes_in_total            {vip,customer,device}                 gauge
  vpn_active_lease_bytes_out_total           {vip,customer,device}                 gauge
  vpn_active_lease_established_timestamp     {vip,customer}                        gauge (unix s)
  vpn_active_lease_count                     {}                                    gauge
  vpn_active_customer_count                  {tier}                                gauge
  vpn_pool_size                              {pool}                                gauge
  vpn_pool_online                            {pool}                                gauge (reported)
  vpn_pool_size_actual                                               gauge (from SQL)
  vpn_alerts_total                           {severity}                            gauge (count of rows)
  vpn_audit_log_total                        {actor,action}                        gauge (count of rows)
  vpn_exporter_scrape_errors_total                                                counter
  vpn_exporter_scrape_duration_seconds                                            gauge
  vpn_exporter_scrape_timestamp_seconds                                           gauge (unix s)
  vpn_exporter_up                                                  gauge (1 healthy, 0 error)

Notes on counter resets:
  The iptables FORWARD counters can be wiped by the strongSwan-iptables-watchdog
  (5B.6 fix narrowed the case statement, but the underlying iptables-restore
  still doesn't preserve byte counters). When that happens, our gauges will
  drop. Prometheus `rate()` will return negative for one window. Dashboards
  should use `increase()` with a `>= 0` clamp, or `resets()` to surface it.

Created 2026-06-20 (5C.3 implementation, Zun direction).
"""
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pymysql
import pymysql.cursors
from prometheus_client import start_http_server, Gauge, Counter, Info

log = logging.getLogger("quota-exporter")

LISTEN_PORT = 9102
SCRAPE_INTERVAL = 30  # seconds

# Charon DB -- authoritative for SA leases (addresses, identities)
CHARON_DB_PATH = "/var/lib/strongswan/ipsec.db"
VIP_PREFIX = "10.99.0."  # rw-pool range

# MariaDB config (Phase 4E, since 2026-07-12). Portal data lives here.
PORTAL_ENV_FILE = "/etc/vpn-portal.env"
MARIADB_HOST = "127.0.0.1"
MARIADB_PORT = 3306
MARIADB_USER = "portal"
MARIADB_PASSWORD = "portal"
MARIADB_DB = "radius"


def _read_mariadb_password():
    """Read portal MariaDB password from /etc/vpn-portal.env (DB_URL=...).
    Falls back to defaults if file missing or unparseable.
    """
    global MARIADB_PASSWORD, MARIADB_USER, MARIADB_HOST, MARIADB_PORT, MARIADB_DB
    try:
        with open(PORTAL_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line.startswith("DB_URL="):
                    continue
                url = line.split("=", 1)[1]
                m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(\w+)", url)
                if m:
                    user, pw, host, port, db = m.groups()
                    MARIADB_USER = user
                    MARIADB_PASSWORD = pw
                    MARIADB_HOST = host
                    MARIADB_PORT = int(port) if port else 3306
                    MARIADB_DB = db
                    log.info("MariaDB from %s: user=%s host=%s db=%s", PORTAL_ENV_FILE, user, host, db)
                    return
    except Exception as e:
        log.warning("Could not read %s: %s -- defaults", PORTAL_ENV_FILE, e)


_read_mariadb_password()


def _maria_connect():
    """Open a new pymysql connection. Fresh each call -- no idle pool on 30s scrape."""
    return pymysql.connect(
        host=MARIADB_HOST, port=MARIADB_PORT, user=MARIADB_USER,
        password=MARIADB_PASSWORD, database=MARIADB_DB, charset="utf8mb4",
        connect_timeout=5, read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
    )

# Prometheus metrics
g_customer_info = Gauge(
    "vpn_customer_info",
    "Customer metadata (1 per customer)",
    ["customer", "tier", "is_operator", "is_active"],
)
g_customer_used = Gauge(
    "vpn_customer_data_used_bytes",
    "Cumulative bytes used since last reset",
    ["customer", "tier"],
)
g_customer_limit = Gauge(
    "vpn_customer_data_limit_bytes",
    "Current data limit (tier + manual extensions)",
    ["customer", "tier"],
)
g_customer_over = Gauge(
    "vpn_customer_over_quota",
    "1 if customer hit 100% and is hard-cut, 0 otherwise",
    ["customer"],
)
g_customer_active = Gauge(
    "vpn_customer_is_active",
    "1 if customer account is active, 0 if suspended",
    ["customer"],
)
g_lease_in = Gauge(
    "vpn_active_lease_bytes_in_total",
    "Bytes received on this lease (raw iptables counter, may reset on watchdog restart)",
    ["vip", "customer", "device"],
)
g_lease_out = Gauge(
    "vpn_active_lease_bytes_out_total",
    "Bytes sent on this lease (raw iptables counter, may reset on watchdog restart)",
    ["vip", "customer", "device"],
)
g_lease_acquired = Gauge(
    "vpn_active_lease_established_timestamp",
    "Unix timestamp of lease acquisition",
    ["vip", "customer"],
)
g_lease_count = Gauge(
    "vpn_active_lease_count",
    "Number of currently active VIP leases",
)
g_customer_count = Gauge(
    "vpn_active_customer_count",
    "Number of customers (by tier) currently active in DB",
    ["tier"],
)
g_pool_size = Gauge(
    "vpn_pool_size",
    "strongSwan rw-pool declared size (from swanctl --list-pools)",
    ["pool"],
)
g_pool_online = Gauge(
    "vpn_pool_online",
    "strongSwan rw-pool online leases (from swanctl --list-pools, may lag SQL truth)",
    ["pool"],
)
g_pool_size_actual = Gauge(
    "vpn_pool_size_actual",
    "rw-pool size as configured in strongswan.conf (10.99.0.0/24 = 254)",
)
g_alerts_total = Gauge(
    "vpn_alerts_total",
    "Total alerts recorded (by severity: 80=warn, 100=cut)",
    ["severity"],
)
g_audit_total = Gauge(
    "vpn_audit_log_total",
    "Total audit log rows (by actor and action)",
    ["actor", "action"],
)
# RADIUS-based per-user data usage (added 2026-07-14, survives IKE_SA rekeys)
g_radacct_active_bytes = Gauge(
    "vpn_radacct_active_session_bytes",
    "Bytes (in+out) for currently-open RADIUS sessions (acctstoptime IS NULL)",
    ["username", "calling_station_id"],
)
g_radacct_active_duration = Gauge(
    "vpn_radacct_active_session_duration",
    "Seconds elapsed for currently-open RADIUS sessions",
    ["username", "calling_station_id"],
)
g_radacct_sessions_7d = Gauge(
    "vpn_radacct_sessions_total_7d",
    "RADIUS sessions in last 7 days, by terminate cause",
    ["cause"],
)
c_scrape_errors = Counter(
    "vpn_exporter_scrape_errors_total",
    "Number of scrape failures (DB or iptables)",
)
g_scrape_duration = Gauge(
    "vpn_exporter_scrape_duration_seconds",
    "Duration of last scrape cycle",
)
g_scrape_ts = Gauge(
    "vpn_exporter_scrape_timestamp_seconds",
    "Unix timestamp of last successful scrape",
)
g_up = Gauge(
    "vpn_exporter_up",
    "1 if last scrape was successful, 0 if errored",
)

# Tracker for stale-label removal (mirrors strongswan_exporter pattern)
QUOTA_COMMENT_RE = re.compile(r"quota:(\d+\.\d+\.\d+\.\d+)")


# ---------- data sources ----------

def db_query_charon(sql: str) -> list[dict]:
    """Query charon SQLite (charon-internal tables only: addresses, identities)."""
    if not Path(CHARON_DB_PATH).exists():
        return []
    conn = sqlite3.connect(CHARON_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


def db_query_mariadb(sql: str, params=None) -> list[dict]:
    """Query MariaDB `radius` DB. Returns [] on error -- caller decides."""
    try:
        with _maria_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return list(cur.fetchall())
    except Exception as e:
        log.warning("MariaDB query failed: %s -- sql=%s", e, sql[:200])
        return []


def iptables_counters() -> dict:
    """
    Parse iptables FORWARD chain for per-VIP counters.

    Returns {vip: {"in_pkts": int, "in_bytes": int, "out_pkts": int, "out_bytes": int}}
    Two rules per VIP (outbound source=VIP, inbound dest=VIP).
    """
    try:
        out = subprocess.run(
            ["iptables-legacy", "-L", "FORWARD", "-nvx"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("iptables-legacy failed: %s", e)
        return {}

    counters: dict = {}
    for line in out.stdout.splitlines():
        if line.startswith("Chain") or line.startswith("target") or line.startswith("pkts"):
            continue
        m = QUOTA_COMMENT_RE.search(line)
        if not m:
            continue
        vip = m.group(1)
        if not vip.startswith(VIP_PREFIX):
            continue
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pkts = int(parts[0])
            bytes_ = int(parts[1])
        except ValueError:
            continue
        src = parts[7]
        dst = parts[8]
        c = counters.setdefault(vip, {"in_pkts": 0, "in_bytes": 0, "out_pkts": 0, "out_bytes": 0})
        if src == vip:
            c["out_pkts"] += pkts
            c["out_bytes"] += bytes_
        if dst == vip:
            c["in_pkts"] += pkts
            c["in_bytes"] += bytes_
    return counters


def vici_parse(text: str) -> dict:
    """
    Parse VICI dump output (--raw or -P) into a nested dict.

    Authoritative source: charon src/libcharon/plugins/vici/vici_message.c:556
    METHOD(vici_message_t, dump, ...). Compact form uses assign="=", separ=" ",
    term="" (no newlines), indent=0. Pretty form uses assign=" = ", separ="",
    term="\\n", indent=2. Both produce the same logical tree.

    Returns {"label": <str>, "body": <dict-or-list>}.

    Handles:
      - Compact and pretty forms interchangeably
      - Arbitrarily nested sections `{ name { ... } }`
      - Lists `[ item1 item2 ]`
      - Empty sections `{ }` and empty leases blocks
      - Multiple top-level pools in one envelope

    Tested against: charon 6.0.7, `swanctl --list-pools [--raw|-P] [-l]`.
    """
    text = text.strip()
    m = re.match(r"^(\S(?:[\S\s]*?\S)?)\s*\{", text)
    if not m:
        raise ValueError(f"no VICI envelope in: {text!r}")
    label = m.group(1).strip()
    body, _ = _vici_section(text, m.end())
    return {"label": label, "body": body}


# A name in VICI is "an ASCII string" per the protocol spec. In practice it's
# always [A-Za-z0-9_.-]+ for our queries. We use a permissive-but-safe pattern
# that excludes the structural chars: whitespace, '=', '{', '}', '[', ']'.
_VICI_NAME = r"[^\s={}\[\]]+"


def _vici_section(text: str, pos: int) -> tuple:
    """Parse `{ ... }` starting one past the '{'. Returns (value, new_pos)."""
    assert text[pos - 1] == "{", f"expected '{{' at pos {pos-1}, got {text[pos-1]!r}"
    result = {}
    pending_key = None
    while pos < len(text):
        c = text[pos]
        if c == "}":
            return result, pos + 1
        if c == "]":
            return result, pos + 1
        if c in " \t\n":
            pos += 1
            continue
        if c == "=":
            # KEY_VALUE continuation: read value
            pos += 1
            while pos < len(text) and text[pos] in " \t":
                pos += 1
            value, pos = _vici_value(text, pos)
            if pending_key is None:
                raise ValueError(f"stray '=' at pos {pos}")
            result[pending_key] = value
            pending_key = None
            continue
        if c == "{":
            pos += 1
            inner, pos = _vici_section(text, pos)
            if pending_key is None:
                raise ValueError(f"stray '{{' at pos {pos}")
            result[pending_key] = inner
            pending_key = None
            continue
        if c == "[":
            pos += 1
            items = []
            while pos < len(text) and text[pos] != "]":
                if text[pos] in " \t\n":
                    pos += 1
                    continue
                item, pos = _vici_value(text, pos)
                items.append(item)
            if pos < len(text):
                pos += 1  # consume ']'
            if pending_key is None:
                raise ValueError(f"stray '[' at pos {pos}")
            result[pending_key] = items
            pending_key = None
            continue
        # Otherwise: it's a name (key, section header, list item, or bare value)
        m = re.match(_VICI_NAME, text[pos:])
        if not m:
            raise ValueError(f"unexpected char {c!r} at pos {pos}")
        name = m.group(0)
        pos += len(name)
        while pos < len(text) and text[pos] in " \t\n":
            pos += 1
        if pos < len(text) and text[pos] in "{=[":
            # Section header or list header — name is the key
            pending_key = name
        elif pos < len(text) and text[pos] == "=":
            # KEY_VALUE — name is the key, value follows
            pending_key = name
        else:
            # Bare trailing token (unusual)
            result[name] = None
    raise ValueError(f"unterminated section: {text!r}")


def _vici_value(text: str, pos: int) -> tuple:
    """Parse a value: bare string, section, or list. Returns (value, new_pos)."""
    if pos >= len(text):
        return "", pos
    c = text[pos]
    if c == "{":
        pos += 1
        return _vici_section(text, pos)
    if c == "[":
        pos += 1
        items = []
        while pos < len(text) and text[pos] != "]":
            if text[pos] in " \t\n":
                pos += 1
                continue
            item, pos = _vici_value(text, pos)
            items.append(item)
        if pos < len(text):
            pos += 1
        return items, pos
    m = re.match(_VICI_NAME, text[pos:])
    if m:
        return m.group(0), pos + len(m.group(0))
    return "", pos


def swanctl_pools() -> list[dict]:
    """
    Run `docker exec strongswan swanctl --list-pools --raw` and parse.

    Returns [{"name": str, "base": str, "size": int, "online": int, "offline": int}, ...]

    Works for both compact (`--raw`) and pretty (`-P`) swanctl output. The
    underlying wire format is the same; only whitespace differs. The parser
    is shared (see vici_parse above).
    """
    try:
        out = subprocess.run(
            ["docker", "exec", "strongswan", "swanctl",
             "--uri=tcp://127.0.0.1:4502", "--list-pools", "--raw"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("swanctl --list-pools failed: %s", e)
        return []

    try:
        parsed = vici_parse(out.stdout)
    except ValueError as e:
        log.warning("vici_parse failed on swanctl output: %s", e)
        log.debug("raw output: %r", out.stdout[:500])
        return []

    pools = []
    # The response body is the implicit root section; each pool is a key
    # whose value is a sub-section with base/size/online/offline.
    body = parsed["body"]
    if not isinstance(body, dict):
        return []
    for name, section in body.items():
        if not isinstance(section, dict):
            continue
        kv = {k: str(v) for k, v in section.items() if isinstance(v, (str, int, float))}
        if "base" not in kv or "size" not in kv:
            continue
        pools.append({
            "name": name,
            "base": kv["base"],
            "size": int(kv["size"]),
            "online": int(kv.get("online", 0)),
            "offline": int(kv.get("offline", 0)),
        })
    return pools


# ---------- scrape ----------

def _reset_gauges():
    """Remove all label combinations from label-bearing gauges.

    Simpler than tracking per-label seen sets — at our scale (handful of
    customers, max 254 leases), the cost is negligible. The alternative
    is mirroring strongswan_exporter.py's _metrics-dict-walk which is
    fiddly.
    """
    for gauge in [
        g_customer_info, g_customer_used, g_customer_limit,
        g_customer_over, g_customer_active,
        g_lease_in, g_lease_out, g_lease_acquired,
        g_customer_count,
        g_pool_size, g_pool_online,
        g_alerts_total, g_audit_total,
        g_radacct_active_bytes, g_radacct_active_duration, g_radacct_sessions_7d,
    ]:
        try:
            gauge.clear()
        except AttributeError:
            # prometheus_client Gauge.clear() is available since 0.20
            pass


# ---------- main ----------

# ---------- scrape steps (each isolated so one failure does not blank all) ----------

def step_customers_and_tiers():
    """Steps 1+2: customers + tiers (MariaDB).
    Emits: vpn_customer_info, vpn_customer_data_used_bytes, vpn_customer_data_limit_bytes,
           vpn_customer_over_quota, vpn_customer_is_active, vpn_active_customer_count.
    """
    customers = db_query_mariadb("""
        SELECT c.id, c.name, c.display_name, c.is_operator, c.is_active,
               c.over_quota, c.data_used_bytes, c.data_limit_bytes,
               c.tier_id, COALESCE(t.name, 'operator') AS tier_name
        FROM customers c
        LEFT JOIN tiers t ON t.id = c.tier_id
    """)
    for c in customers:
        name = c["name"]
        tier = c["tier_name"] or "operator"
        is_op = "1" if c["is_operator"] else "0"
        is_act = "1" if c["is_active"] else "0"
        g_customer_info.labels(
            customer=name, tier=tier,
            is_operator=is_op, is_active=is_act,
        ).set(1)
        g_customer_used.labels(customer=name, tier=tier).set(c["data_used_bytes"] or 0)
        g_customer_limit.labels(customer=name, tier=tier).set(c["data_limit_bytes"] or 0)
        g_customer_over.labels(customer=name).set(c["over_quota"] or 0)
        g_customer_active.labels(customer=name).set(c["is_active"] or 0)

    tier_counts = db_query_mariadb("""
        SELECT COALESCE(t.name, 'operator') AS tier, COUNT(*) AS n
        FROM customers c
        LEFT JOIN tiers t ON t.id = c.tier_id
        WHERE c.is_active = 1
        GROUP BY tier
    """)
    for tc in tier_counts:
        g_customer_count.labels(tier=tc["tier"]).set(tc["n"])


def step_active_leases():
    """Step 3: active SA leases -- hybrid (charon SQLite for SA state,
    MariaDB for customer/device join).

    Charon tracks SA state in `addresses`+`identities`. Portal tracks which
    identity belongs to which customer in `devices`+`customers`. Pull both,
    join on identity_name in Python.
    """
    leases = db_query_charon("""
        SELECT hex(a.address)        AS hex_addr,
               i.id                 AS identity_id,
               CAST(i.data AS TEXT) AS identity_name,
               a.acquired           AS acquired_at
        FROM addresses a
        JOIN identities i ON i.id = a.identity
        WHERE a.acquired > 0 AND a.released = 0
        ORDER BY a.acquired DESC
    """)
    if not leases:
        g_lease_count.set(0)
        return

    identities = [L["identity_name"] for L in leases if L.get("identity_name")]
    customer_map = {}
    if identities:
        placeholders = ",".join(["%s"] * len(identities))
        rows = db_query_mariadb(
            f"SELECT d.device_name, d.id AS device_id, d.device_type, "
            f"c.id AS customer_id, c.name AS customer_name "
            f"FROM devices d JOIN customers c ON c.id = d.customer_id "
            f"WHERE d.device_name IN ({placeholders})",
            tuple(identities),
        )
        customer_map = {r["device_name"]: r for r in rows}

    ipt = iptables_counters()
    g_lease_count.set(len(leases))
    for lease in leases:
        hex_addr = lease.get("hex_addr") or ""
        try:
            vip = ".".join(str(int(hex_addr[i:i+2], 16)) for i in (0, 2, 4, 6))
        except Exception:
            vip = "unknown"
        identity = lease.get("identity_name") or "unknown"
        cm = customer_map.get(identity) or {}
        customer = cm.get("customer_name") or "unmapped"
        device = identity  # identity_name == device_name by portal convention
        ctr = ipt.get(vip, {})
        g_lease_in.labels(vip=vip, customer=customer, device=device).set(ctr.get("in_bytes", 0))
        g_lease_out.labels(vip=vip, customer=customer, device=device).set(ctr.get("out_bytes", 0))
        if lease.get("acquired_at"):
            g_lease_acquired.labels(vip=vip, customer=customer).set(lease["acquired_at"])


def step_pools():
    """Step 5: strongSwan rw-pool state (swanctl, no DB)."""
    pools = swanctl_pools()
    for p in pools:
        g_pool_size.labels(pool=p["name"]).set(p["size"])
        g_pool_online.labels(pool=p["name"]).set(p["online"])
    if pools:
        g_pool_size_actual.set(254)


def step_alerts():
    """Step 6: alerts by severity (MariaDB)."""
    alerts = db_query_mariadb("""
        SELECT threshold, COUNT(*) AS n
        FROM alerts
        GROUP BY threshold
    """)
    for a in alerts:
        severity = f"{a['threshold']}pct"
        g_alerts_total.labels(severity=severity).set(a["n"])


def step_audit_log():
    """Step 7: audit_log by actor + action (MariaDB)."""
    audit = db_query_mariadb("""
        SELECT actor, action, COUNT(*) AS n
        FROM (SELECT actor, action FROM audit_log ORDER BY id DESC LIMIT 1000) AS recent
        GROUP BY actor, action
    """)
    for a in audit:
        g_audit_total.labels(actor=a["actor"], action=a["action"]).set(a["n"])


def step_radacct_active():
    """RADIUS step (added 2026-07-14): currently-open accounting sessions.
    Source of truth for "who is connected right now" from radacct's view.
    Survives IKE_SA rekeys (radacct updates on every interim 300s).
    """
    sessions = db_query_mariadb("""
        SELECT username, callingstationid,
               acctsessiontime AS session_seconds,
               COALESCE(acctinputoctets, 0) AS bytes_in,
               COALESCE(acctoutputoctets, 0) AS bytes_out
        FROM radacct
        WHERE acctstoptime IS NULL
        ORDER BY acctstarttime DESC
    """)
    for s in sessions:
        username = s["username"] or "unknown"
        csid = s["callingstationid"] or "unknown"
        g_radacct_active_bytes.labels(
            username=username, calling_station_id=csid,
        ).set(int(s["bytes_in"]) + int(s["bytes_out"]))
        g_radacct_active_duration.labels(
            username=username, calling_station_id=csid,
        ).set(int(s["session_seconds"]))


def step_radacct_sessions_7d():
    """RADIUS step (added 2026-07-14): 7-day session count by terminate cause.
    Useful for satellite users -- track Lost-Service vs User-Request ratio.
    """
    rows = db_query_mariadb("""
        SELECT COALESCE(NULLIF(acctterminatecause, ''), 'Unknown') AS cause,
               COUNT(*) AS n
        FROM radacct
        WHERE acctstarttime > UNIX_TIMESTAMP() - 7*86400
        GROUP BY cause
    """)
    for r in rows:
        g_radacct_sessions_7d.labels(cause=r["cause"]).set(r["n"])


# ---------- main scrape ----------

def scrape():
    """One scrape cycle. Each step has its own try/except so one failure
    does not blank the whole dashboard.

    Lesson learned 2026-07-14: the old design wrapped all steps in one giant
    try/except. When step 1 (customers) started failing 5 days post-Phase-4E,
    it short-circuited every other metric -- the dashboard went silently
    wrong instead of visibly broken. Per-step isolation makes failures
    observable (per-step exception logs + g_up = 0).
    """
    t0 = time.monotonic()
    errors = []

    try:
        _reset_gauges()
    except Exception as e:
        errors.append(f"reset:{e}")

    for step_fn in (
        step_customers_and_tiers,    # MariaDB
        step_active_leases,          # hybrid (charon SQLite + MariaDB)
        step_pools,                  # swanctl
        step_alerts,                 # MariaDB
        step_audit_log,              # MariaDB
        step_radacct_active,         # MariaDB (RADIUS)
        step_radacct_sessions_7d,    # MariaDB (RADIUS)
    ):
        try:
            step_fn()
        except Exception as e:
            errors.append(f"{step_fn.__name__}:{e}")
            log.warning("step %s failed: %s", step_fn.__name__, e)

    dt = time.monotonic() - t0
    g_scrape_duration.set(dt)
    g_scrape_ts.set(time.time())
    if errors:
        c_scrape_errors.inc(len(errors))
        g_up.set(0)
        log.warning("scrape ok with %d errors in %.2fs: %s",
                    len(errors), dt, errors[:3])
    else:
        g_up.set(1)
        log.info("scrape ok in %.2fs (leases=%d)",
                 dt, g_lease_count._value.get() if hasattr(g_lease_count, "_value") else 0)


# ---------- main ----------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s quota-exporter %(message)s",
    )
    log.info("starting on 0.0.0.0:%d (scrape every %ds)", LISTEN_PORT, SCRAPE_INTERVAL)
    start_http_server(LISTEN_PORT)
    while True:
        scrape()
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
