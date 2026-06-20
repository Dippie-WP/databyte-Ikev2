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

from prometheus_client import start_http_server, Gauge, Counter, Info

log = logging.getLogger("quota-exporter")

LISTEN_PORT = 9102
SCRAPE_INTERVAL = 30  # seconds

# Same paths the rest of the 5B stack uses
DB_PATH = "/var/lib/strongswan/ipsec.db"
VIP_PREFIX = "10.99.0."  # rw-pool range

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

def db_query(sql: str) -> list[dict]:
    """Run a SQL query on the strongSwan SQLite DB, return rows as dicts."""
    if not Path(DB_PATH).exists():
        return []
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


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


def swanctl_pools() -> list[dict]:
    """
    Run `docker exec strongswan swanctl --list-pools --raw` and parse.

    VICI raw format: `rw-pool {base=10.99.0.1 size=254 online=0 offline=0}`
    Returns [{"name": str, "base": str, "size": int, "online": int, "offline": int}, ...]
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

    pools = []
    # VICI envelope: `get-pools reply {rw-pool {base=10.99.0.1 size=254 online=0 offline=0}}`
    # The inner pool block always starts with `base=` — use that as the sentinel.
    # Match exactly one level of nesting: `name {base=... size=... ...}`.
    for block in re.finditer(r"\{(\S+)\s+\{(base=[^}]+)\}\}", out.stdout):
        name = block.group(1)
        kv_text = block.group(2)
        kv = dict(re.findall(r"(\w+)=(\S+)", kv_text))
        if "base" in kv and "size" in kv:
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
    ]:
        try:
            gauge.clear()
        except AttributeError:
            # prometheus_client Gauge.clear() is available since 0.20
            pass


def scrape():
    """One scrape cycle. Updates all metrics. Errors do not raise."""
    t0 = time.monotonic()
    errors = 0

    try:
        _reset_gauges()

        # 1) customers + tiers
        customers = db_query("""
            SELECT c.id, c.name, c.display_name, c.is_operator, c.is_active,
                   c.over_quota, c.data_used_bytes, c.data_limit_bytes,
                   c.tier_id, t.name AS tier_name
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

        # 2) customer count by tier
        tier_counts = db_query("""
            SELECT COALESCE(t.name, 'operator') AS tier, COUNT(*) AS n
            FROM customers c
            LEFT JOIN tiers t ON t.id = c.tier_id
            WHERE c.is_active = 1
            GROUP BY tier
        """)
        for tc in tier_counts:
            g_customer_count.labels(tier=tc["tier"]).set(tc["n"])

        # 3) active leases (the join that portal uses)
        leases = db_query("""
            SELECT hex(a.address)        AS hex_addr,
                   i.id                 AS identity_id,
                   CAST(i.data AS TEXT) AS identity_name,
                   d.id                 AS device_id,
                   d.device_name        AS device_name,
                   c.id                 AS customer_id,
                   c.name               AS customer_name,
                   a.acquired           AS acquired_at
            FROM addresses a
            JOIN identities i ON i.id = a.identity
            LEFT JOIN devices   d ON d.device_name = CAST(i.data AS TEXT)
            LEFT JOIN customers c ON c.id = d.customer_id
            WHERE a.acquired > 0 AND a.released = 0
            ORDER BY a.acquired DESC
        """)

        # 4) iptables counters (per VIP)
        ipt = iptables_counters()
        g_lease_count.set(len(leases))
        now = int(time.time())
        for lease in leases:
            hex_addr = lease.get("hex_addr") or ""
            try:
                vip = ".".join(str(int(hex_addr[i:i+2], 16)) for i in (0, 2, 4, 6))
            except Exception:
                vip = "unknown"
            customer = lease.get("customer_name") or "unmapped"
            device = lease.get("device_name") or lease.get("identity_name") or "unknown"
            ctr = ipt.get(vip, {})
            g_lease_in.labels(vip=vip, customer=customer, device=device).set(ctr.get("in_bytes", 0))
            g_lease_out.labels(vip=vip, customer=customer, device=device).set(ctr.get("out_bytes", 0))
            if lease.get("acquired_at"):
                g_lease_acquired.labels(vip=vip, customer=customer).set(lease["acquired_at"])

        # 5) pools (swanctl)
        pools = swanctl_pools()
        for p in pools:
            g_pool_size.labels(pool=p["name"]).set(p["size"])
            g_pool_online.labels(pool=p["name"]).set(p["online"])
        if pools:
            # rw-pool is the configured /24 (10.99.0.0/24, .1 = gateway, so 254 usable)
            g_pool_size_actual.set(254)

        # 6) alerts by severity
        alerts = db_query("""
            SELECT threshold, COUNT(*) AS n
            FROM alerts
            GROUP BY threshold
        """)
        for a in alerts:
            severity = f"{a['threshold']}pct"
            g_alerts_total.labels(severity=severity).set(a["n"])

        # 7) audit_log by actor + action (last 1000 rows for now)
        audit = db_query("""
            SELECT actor, action, COUNT(*) AS n
            FROM (SELECT actor, action FROM audit_log ORDER BY id DESC LIMIT 1000)
            GROUP BY actor, action
        """)
        for a in audit:
            g_audit_total.labels(actor=a["actor"], action=a["action"]).set(a["n"])

        g_up.set(1)
    except Exception as e:
        errors += 1
        c_scrape_errors.inc()
        g_up.set(0)
        log.exception("scrape failed: %s", e)
    finally:
        dt = time.monotonic() - t0
        g_scrape_duration.set(dt)
        g_scrape_ts.set(time.time())
        log.info("scrape ok in %.2fs (leases=%d, errors=%d)",
                 dt, g_lease_count._value.get() if hasattr(g_lease_count, '_value') else 0, errors)


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
