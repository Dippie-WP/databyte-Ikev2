#!/usr/bin/env python3
"""
quota-monitor.py — Phase 8 MariaDB edition (2026-07-12)

Phase 8 (2026-07-12 20:30 UTC): Phase 4E cutover replaced SQLite portal.db
with MariaDB radius.* schema. The monitor was reading the WRONG database
(charon's internal /var/lib/strongswan/ipsec.db, table `users` doesn't exist
there — that's an attr-sql table), causing "no such table: users" errors
for 13+ hours starting 06:45 UTC 2026-07-12.

This version:
  - Opens MariaDB radius.* schema via pymysql (DictCursor for dict-row returns)
  - All customer/device/alert/audit queries now hit MariaDB
  - RADIUS radcheck disable already uses MariaDB (Phase 5 cutover) — preserved
  - Sidecar (/var/run/quota-monitor.session) unchanged — still on local fs
  - nft meter reads unchanged — still on local nftables

Behaviour identical to v1.7.0 except for DB layer. No business-logic changes.

Reads per-VIP netfilter byte counters (nft named meters), resolves VIP →
customer via pool lease → EAP identity, applies 80% warn + 100% hard-cut.

Data flow (one iteration):
  1. `swanctl --list-pools --leases` → list of VIP+identity+online
  2. Snapshot per-VIP byte counters from named meters (client_src, client_dst)
  3. Drop sidecar entries for VIPs no longer leased (lease released)
  4. For each current lease (online OR offline):
     a. Resolve identity → users → devices → customers (MariaDB)
     b. Skip operator / inactive / no-mapping (legacy EAP)
     c. First observation of this customer-VIP pair → just baseline
     d. Else: delta = meter_now - sidecar[customer_id], update DB if > 0
     e. Check 80%/100% thresholds; act on cut (kill creds + terminate SAs)
  5. Save updated sidecar with current leases only
  6. Sleep POLL_INTERVAL, repeat
"""
import argparse
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pymysql
import pymysql.cursors

# === Config (paths) ===
# Phase 4E cutover (2026-07-12 06:20 UTC): customer/device/alert/audit tables
# moved from /var/lib/strongswan/ipsec.db (SQLite, charon's attr-sql DB) into
# MariaDB `radius` schema. The two RW rw-eap.conf and the radcheck password
# disable (already MariaDB since Phase 5) are unchanged.
_MARIADB_PW_FILE = Path("/root/.mariadb-radius-pw")
_MARIADB_HOST = "127.0.0.1"
_MARIADB_USER = "radius"
_MARIADB_DB = "radius"

CONF_PATH = Path("/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf")
CONF_BACKUP_DIR = Path("/home/zunaid/strongswan/swanctl/conf.d/.backups")

# 10.99.0.0/24 VIP range — must match iptables rules in rules.v4
VIP_PREFIX = "10.99.0."

# Poll interval (seconds) — unchanged from Phase 5
POLL_INTERVAL = 10

# Alert thresholds (percent)
WARN_PCT = 80
CUT_PCT = 100

# VICI (charon) — TCP socket exposed by the container on 127.0.0.1:4502
SWANCTL_PREFIX = ["docker", "exec", "strongswan", "swanctl", "--uri=tcp://127.0.0.1:4502"]

# === Logging ===
log = logging.getLogger("quota-monitor")

# === DAE helper import (unchanged) ===
_VPN_DISC_DIR = "/home/zunaid/strongswan/quota"
if _VPN_DISC_DIR not in sys.path:
    sys.path.insert(0, _VPN_DISC_DIR)
try:
    from vpn_disconnect import send_dae_disconnect  # type: ignore[import-not-found]
    _DAE_HELPER_IMPORTED = True
except Exception as _dae_imp_err:
    log.error("vpn_disconnect import failed (%s) — DAE disabled, "
              "fallback no-op used; cut will fall back to "
              "swanctl --terminate", _dae_imp_err)

    def send_dae_disconnect(*_args, **_kwargs):
        return "error"

    _DAE_HELPER_IMPORTED = False

# === nft METER parsing (Phase 6, unchanged) ===
METER_ELEM_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+counter packets (\d+) bytes (\d+)")


def _read_meter(meter_name: str) -> dict[str, tuple[int, int]]:
    """Read {VIP: (packets, bytes)} from a named meter in `ip filter` table."""
    try:
        out = subprocess.run(
            ["/usr/sbin/nft", "list", "meter", "ip", "filter", meter_name],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("nft list meter ip filter %s failed: rc=%s stderr=%s",
                  meter_name, e.returncode, e.stderr)
        return {}

    result: dict[str, tuple[int, int]] = {}
    for m in METER_ELEM_RE.finditer(out.stdout):
        vip = m.group(1)
        if not vip.startswith(VIP_PREFIX):
            continue
        try:
            pkts = int(m.group(2))
            bytes_ = int(m.group(3))
        except ValueError:
            continue
        result[vip] = (pkts, bytes_)
    return result


def sample_counters() -> dict[str, tuple[int, int, int, int]]:
    """Return {VIP: (out_pkts, out_bytes, in_pkts, in_bytes)} from named meters."""
    src = _read_meter("client_src")
    dst = _read_meter("client_dst")

    all_vips = set(src) | set(dst)
    out: dict[str, tuple[int, int, int, int]] = {}
    for vip in all_vips:
        out_pkts, out_bytes = src.get(vip, (0, 0))
        in_pkts, in_bytes = dst.get(vip, (0, 0))
        out[vip] = (out_pkts, out_bytes, in_pkts, in_bytes)
    return out


# === MariaDB layer ===

def _read_radius_db_password() -> Optional[str]:
    """Read MariaDB radius@127.0.0.1 password from /root/.mariadb-radius-pw."""
    if not _MARIADB_PW_FILE.exists():
        log.error("RADIUS password file missing: %s", _MARIADB_PW_FILE)
        return None
    try:
        text = _MARIADB_PW_FILE.read_text()
    except (PermissionError, OSError) as e:
        log.error("Cannot read %s: %s (quota-monitor must run as root)",
                  _MARIADB_PW_FILE, e)
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    log.error("No password line found in %s (only comments?)", _MARIADB_PW_FILE)
    return None


def _open_mariadb() -> Optional[pymysql.connections.Connection]:
    """Open a MariaDB connection with DictCursor.

    Returns None if password file is missing/unreadable or DB unreachable.
    Caller must close the connection (use `with` or try/finally).
    """
    pw = _read_radius_db_password()
    if not pw:
        return None
    try:
        return pymysql.connect(
            host=_MARIADB_HOST,
            user=_MARIADB_USER,
            password=pw,
            database=_MARIADB_DB,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )
    except pymysql.MySQLError as e:
        log.error("MariaDB connect failed: %s", e)
        return None


# === DB queries ===
# Note: MariaDB uses %s placeholders (not ? like SQLite). All queries
# that previously used `?` now use `%s`.

CUSTOMER_LOOKUP_SQL = """
SELECT
    u.id                 AS user_id,
    u.name               AS username,
    d.id                 AS device_id,
    d.device_name        AS device_name,
    c.id                 AS customer_id,
    c.name               AS customer_name,
    c.is_operator        AS is_operator,
    c.data_limit_bytes   AS data_limit_bytes,
    c.data_used_bytes    AS data_used_bytes,
    c.over_quota         AS over_quota,
    c.is_active          AS is_active
FROM users u
JOIN devices d          ON d.strongswan_user_id = u.id
JOIN customers c        ON c.id  = d.customer_id
WHERE u.name = %s
LIMIT 1
"""


def lookup_customer_for_username(db, username: str) -> dict | None:
    """Resolve an IKE identity (username) to its device+customer record.

    Phase 4E: MariaDB collation is utf8_general_ci (case-insensitive by default
    for VARCHAR with _ci suffix). We still lower() the username here for
    symmetry with v1.7.0 and so the sidecar baseline is keyed consistently.
    """
    with db.cursor() as cur:
        cur.execute(CUSTOMER_LOOKUP_SQL, (username.lower(),))
        r = cur.fetchone()
    return r if r else None


# Pool lease line format (unchanged from v1.7.0)
_POOL_LEASE_LINE_RE = re.compile(
    r"^\s+(?P<vip>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<status>online|offline)\s+"
    r"'(?P<identity>[^']*)'\s*$"
)


def list_pool_leases() -> list[dict]:
    """Parse `swanctl --list-pools --leases` output (unchanged)."""
    try:
        proc = subprocess.run(
            SWANCTL_PREFIX + ["--list-pools", "--leases"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("swanctl --list-pools --leases failed: rc=%s stderr=%s",
                  e.returncode, e.stderr)
        return []

    out: list[dict] = []
    current_pool: str | None = None
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):
            parts = line.split()
            if parts:
                current_pool = parts[0]
            continue
        m = _POOL_LEASE_LINE_RE.match(line)
        if m and current_pool:
            out.append({
                "pool":     current_pool,
                "vip":      m.group("vip"),
                "identity": m.group("identity"),
                "online":   m.group("status") == "online",
            })
    return out


def list_active_sas() -> list[dict]:
    """Parse `swanctl --list-sas` output (unchanged).

    Used only by terminate_customer_sas() to find SAs to kill on a 100% cut.
    """
    try:
        proc = subprocess.run(
            SWANCTL_PREFIX + ["--list-sas"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("swanctl --list-sas failed: rc=%s stderr=%s",
                  e.returncode, e.stderr)
        return []

    out: list[dict] = []
    sa = None
    for line in proc.stdout.splitlines():
        m = re.match(r"\s*rw-eap:\s+#(\d+),\s+ESTABLISHED", line)
        if m:
            sa = {"uniqueid": m.group(1), "username": None, "vip": None}
            out.append(sa)
            continue
        if sa is None:
            continue
        m = re.search(r"EAP:\s+'([^']+)'\s+\[(\d+\.\d+\.\d+\.\d+)\]", line)
        if m:
            sa["username"] = m.group(1)
            sa["vip"] = m.group(2)
    return [s for s in out if s["username"] and s["vip"]]


def update_used_bytes(db, customer_id: int, delta: int) -> None:
    """Add `delta` to customers.data_used_bytes. Idempotent caller-side."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE customers SET data_used_bytes = data_used_bytes + %s, "
            "updated_at = %s WHERE id = %s",
            (delta, int(time.time()), customer_id),
        )


def alert_already_sent(db, customer_id: int, threshold: int) -> bool:
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM alerts WHERE customer_id = %s AND threshold = %s LIMIT 1",
            (customer_id, threshold),
        )
        return cur.fetchone() is not None


def log_alert(db, customer_id: int, threshold: int, data_used: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts (customer_id, threshold, sent_at, data_used_bytes_at_alert) "
            "VALUES (%s, %s, %s, %s)",
            (customer_id, threshold, int(time.time()), data_used),
        )


def log_audit(db, actor: str, action: str, target_type: str,
              target_id: int | None, payload: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (actor, action, target_type, target_id, payload, int(time.time())),
        )


# === Kill credentials at 100% (unchanged from v1.7.0) ===

def kill_customer_credentials(db, customer_id: int, username: str) -> bool:
    """Replace rw-eap.conf secret for `username` with KILLED-<random>."""
    if not CONF_PATH.exists():
        log.error("rw-eap.conf not found at %s", CONF_PATH)
        return False

    original = CONF_PATH.read_text()

    killed = f"KILLED-{secrets.token_hex(8)}"
    pattern = re.compile(
        r"(eap-" + re.escape(username) + r"\s*\{\s*id\s*=\s*"
        + re.escape(username) + r"\s*secret\s*=\s*\")[^\"]*(\")",
        re.MULTILINE,
    )
    new_text, n_subs = pattern.subn(r"\g<1>" + killed + r"\g<2>", original)
    if n_subs == 0:
        log.info("No eap-%s block in rw-eap.conf (Phase 5+ customer, "
                 "auth via RADIUS only) — rw-eap kill skipped", username)
        return True

    CONF_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bak = CONF_BACKUP_DIR / f"rw-eap.conf.bak-quotamon-{int(time.time())}"
    bak.write_text(original)
    log.info("Backed up original to %s", bak)

    CONF_PATH.write_text(new_text)
    log.info("Killed eap-%s secret in %s (subs=%d)", username, CONF_PATH, n_subs)

    try:
        subprocess.run(
            SWANCTL_PREFIX + ["--load-creds"],
            check=True, capture_output=True, text=True,
        )
        log.info("charon creds reloaded")
    except subprocess.CalledProcessError as e:
        log.error("charon --load-creds FAILED: rc=%s stderr=%s stdout=%s",
                  e.returncode, e.stderr, e.stdout)
        CONF_PATH.write_text(original)
        log.warning("Rolled back conf change due to charon reload failure")
        return False

    return True


# === Phase 5: RADIUS radcheck disable (already MariaDB, unchanged) ===

def disable_customer_radcheck(username: str) -> bool:
    """Replace radcheck Cleartext-Password with DISABLED-<random> marker."""
    pw = _read_radius_db_password()
    if not pw:
        return False

    disabled_marker = f"DISABLED-{secrets.token_hex(8)}"
    safe_user = username.replace("'", "''")
    sql = (
        f"DELETE FROM radcheck WHERE username = '{safe_user}';\n"
        f"INSERT INTO radcheck (username, attribute, op, value) "
        f"VALUES ('{safe_user}', 'Cleartext-Password', ':=', '{disabled_marker}');\n"
    )

    try:
        proc = subprocess.run(
            ["mariadb", "-u", "radius", "-h", "127.0.0.1", _MARIADB_DB],
            input=sql,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "MYSQL_PWD": pw},
        )
        log.info("radcheck: disabled %s (marker=%s)", username, disabled_marker)
        return True
    except subprocess.CalledProcessError as e:
        log.error("mariadb radcheck disable failed for %s: rc=%s stderr=%s",
                  username, e.returncode, e.stderr.strip()[:500])
        return False
    except FileNotFoundError:
        log.error("mariadb CLI not found in PATH — cannot disable radcheck")
        return False


def terminate_customer_sas(username: str) -> int:
    """Terminate SAs for a specific EAP username (unchanged)."""
    try:
        proc = subprocess.run(
            SWANCTL_PREFIX + ["--list-sas"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("swanctl --list-sas failed: rc=%s stderr=%s",
                  e.returncode, e.stderr)
        return 0

    target_ids: list[str] = []
    current_id: str | None = None
    for line in proc.stdout.splitlines():
        m = re.match(r"\s*rw-eap:\s+#(\d+),\s+ESTABLISHED", line)
        if m:
            current_id = m.group(1)
            continue
        if current_id and re.search(r"remote\s+'" + re.escape(username) + r"'\s+@", line):
            target_ids.append(current_id)
            current_id = None
        elif current_id and not re.match(r"\s+(remote|local|AES_|established|rekeying|reauth|net:|in |out )", line):
            current_id = None

    if not target_ids:
        log.info("No ESTABLISHED rw-eap SAs for user %s to terminate", username)
        return 0

    terminated = 0
    for ike_id in target_ids:
        try:
            subprocess.run(
                SWANCTL_PREFIX + ["--terminate", "--ike-id", ike_id, "--force"],
                check=True, capture_output=True, text=True,
            )
            terminated += 1
            log.info("Terminated IKE_SA #%s for user %s", ike_id, username)
        except subprocess.CalledProcessError as e:
            log.error("terminate IKE_SA #%s failed: rc=%s stderr=%s stdout=%s",
                      ike_id, e.returncode, e.stderr, e.stdout)
    return terminated


# === Main loop ===

class QuotaMonitor:
    def __init__(self, verbose: bool = False):
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.INFO,
            format="%(asctime)s %(levelname)-5s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _open_db(self):
        """Open a MariaDB connection (Phase 4E cutover — was SQLite)."""
        db = _open_mariadb()
        if db is None:
            raise RuntimeError("Cannot open MariaDB — check /root/.mariadb-radius-pw")
        return db

    def _last_sampled_bytes(self) -> dict[str, int]:
        """Load {key: last_meter_total} from sidecar (unchanged)."""
        sidecar = Path("/var/run/quota-monitor.session")
        if not sidecar.exists():
            return {}
        import json
        return {k: int(v) for k, v in json.loads(sidecar.read_text()).items()}

    def _save_session(self, totals: dict[str, int]) -> None:
        import json
        sidecar = Path("/var/run/quota-monitor.session")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({k: int(v) for k, v in totals.items()}))

    @staticmethod
    def _sidecar_key(customer_id: int, vip: str) -> str:
        return f"{customer_id}:{vip}"

    def run_once(self) -> None:
        """Single iteration. Returns when done. Safe to call repeatedly."""
        log.info("=== quota-monitor iteration start ===")
        leases = list_pool_leases()
        if not leases:
            log.info("no pool leases — nothing to bill; clearing sidecar")
            self._save_session({})
            return

        counters = sample_counters()
        if not counters:
            log.info("no nft meter entries found — nothing to do")
            return

        db = self._open_db()
        try:
            last_totals = self._last_sampled_bytes()
            new_totals: dict[str, int] = {}

            for lease in leases:
                vip = lease["vip"]
                identity = lease["identity"]
                online = lease["online"]

                if vip not in counters:
                    log.debug("VIP %s (%s): lease held but no meter entry — skipping",
                              vip, identity)
                    continue

                cust = lookup_customer_for_username(db, identity)
                if cust is None:
                    log.debug("VIP %s: identity %s has no customer mapping — skipping",
                              vip, identity)
                    continue

                _, out_bytes, _, in_bytes = counters[vip]
                total_now = out_bytes + in_bytes
                customer_id = cust["customer_id"]
                is_operator = cust["is_operator"]
                data_limit = cust["data_limit_bytes"]
                data_used = cust["data_used_bytes"]
                key = self._sidecar_key(customer_id, vip)

                if is_operator:
                    log.debug("VIP %s: operator %s — skipping billing, baselining",
                              vip, cust["customer_name"])
                    new_totals[key] = total_now
                    continue

                if not cust["is_active"]:
                    log.info("VIP %s: customer %s is_active=0 — skipping billing, baselining",
                             vip, cust["customer_name"])
                    new_totals[key] = total_now
                    continue

                prior = last_totals.get(key)
                if prior is None:
                    log.info("VIP %s (%s/%s): first observation on this (customer,VIP) pair, "
                             "baseline meter=%d (DB data_used=%d)",
                             vip, cust["customer_name"], cust["device_name"],
                             total_now, data_used)
                    new_totals[key] = total_now
                    continue

                delta = total_now - prior
                if delta < 0:
                    log.warning("VIP %s: meter went backwards (%d → %d), re-baselining",
                                vip, prior, total_now)
                    delta = 0

                if delta > 0:
                    update_used_bytes(db, customer_id, delta)
                    data_used += delta
                    log.info("VIP %s (%s/%s) %s: +%d bytes, used=%d / %d (%.1f%%)",
                             vip, cust["customer_name"], cust["device_name"],
                             "online" if online else "OFFLINE",
                             delta, data_used, data_limit,
                             100 * data_used / data_limit if data_limit else 0)

                new_totals[key] = total_now

                cust["vip"] = vip
                cust["username"] = identity
                cust["online"] = online

                if data_limit > 0:
                    pct = 100 * data_used / data_limit
                    if pct >= CUT_PCT and not cust["over_quota"]:
                        self._cut_customer(db, cust, data_used)
                    elif pct >= WARN_PCT and not alert_already_sent(db, customer_id, WARN_PCT):
                        self._warn_customer(db, cust, data_used, pct)

            db.commit()

            self._save_session(new_totals)
        finally:
            db.close()
        log.info("=== quota-monitor iteration done ===")

    def _warn_customer(self, db, cust: dict, data_used: int, pct: float) -> None:
        customer_id = cust["customer_id"]
        log.warning("VIP %s user=%s cust=%s: 80%% WARN — used %d / %d (%.1f%%)",
                    cust["vip"], cust["username"], cust["customer_name"],
                    data_used, cust["data_limit_bytes"], pct)
        log_alert(db, customer_id, WARN_PCT, data_used)
        log_audit(db, "quota-monitor", "warn_80pct", "customer", customer_id,
                  f'{{"pct": {pct:.2f}, "data_used": {data_used}, '
                  f'"data_limit": {cust["data_limit_bytes"]}}}')

    def _cut_customer(self, db, cust: dict, data_used: int) -> None:
        customer_id = cust["customer_id"]
        username = cust["username"]
        log.error("VIP %s user=%s cust=%s: 100%% CUT — used %d / %d",
                  cust["vip"], username, cust["customer_name"],
                  data_used, cust["data_limit_bytes"])

        radcheck_killed = disable_customer_radcheck(username)
        rw_eap_killed = kill_customer_credentials(db, customer_id, username)
        dae_result = send_dae_disconnect(username)
        n_terminated = terminate_customer_sas(username)

        if radcheck_killed:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE customers SET over_quota = 1, updated_at = %s WHERE id = %s",
                    (int(time.time()), customer_id),
                )
            log_alert(db, customer_id, CUT_PCT, data_used)
            log_audit(db, "quota-monitor", "cut_100pct", "customer", customer_id,
                      f'{{"data_used": {data_used}, "data_limit": {cust["data_limit_bytes"]}, '
                      f'"dae_result": "{dae_result}", '
                      f'"sas_terminated": {n_terminated}, '
                      f'"radcheck_killed": {radcheck_killed}, '
                      f'"rw_eap_killed": {rw_eap_killed}}}')
        else:
            log_audit(db, "quota-monitor", "cut_100pct_FAILED", "customer", customer_id,
                      f'{{"data_used": {data_used}, '
                      f'"reason": "radcheck disable failed (customer NOT locked out)", '
                      f'"dae_result": "{dae_result}", '
                      f'"sas_terminated": {n_terminated}, '
                      f'"rw_eap_killed": {rw_eap_killed}}}')

    def run_daemon(self) -> None:
        running = True

        def stop(*_):
            nonlocal running
            running = False
            log.info("shutdown signal received — finishing current iteration")

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        log.info("starting daemon (poll every %ds, warn=%d%%, cut=%d%%)",
                 POLL_INTERVAL, WARN_PCT, CUT_PCT)

        while running:
            try:
                self.run_once()
            except Exception:
                log.exception("iteration failed — continuing")
            for _ in range(POLL_INTERVAL):
                if not running:
                    break
                time.sleep(1)
        log.info("daemon exiting cleanly")


def main():
    p = argparse.ArgumentParser(description="Phase 8 quota monitor (MariaDB)")
    p.add_argument("--once", action="store_true", help="run a single iteration and exit")
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    p.add_argument(
        "--backfill-orphans", action="store_true",
        help="One-time migration: for each current pool lease with a "
             "customer mapping and data_used_bytes=0 in DB, credit the "
             "current nft meter total to data_used_bytes and write it to "
             "the sidecar as the baseline.",
    )
    args = p.parse_args()

    mon = QuotaMonitor(verbose=args.verbose)
    if args.backfill_orphans:
        backfill_orphans(mon)
    elif args.once:
        mon.run_once()
    else:
        mon.run_daemon()


def backfill_orphans(mon: "QuotaMonitor") -> None:
    log.info("=== backfill-orphans: one-time migration ===")
    leases = list_pool_leases()
    counters = sample_counters()
    if not leases:
        log.warning("no leases; nothing to backfill")
        return

    db = mon._open_db()
    try:
        sidecar: dict[str, int] = mon._last_sampled_bytes()
        for lease in leases:
            vip = lease["vip"]
            identity = lease["identity"]
            if vip not in counters:
                continue
            cust = lookup_customer_for_username(db, identity)
            if cust is None or cust["is_operator"] or not cust["is_active"]:
                continue
            _, out_b, _, in_b = counters[vip]
            total_now = out_b + in_b
            if total_now <= 0:
                continue
            if cust["data_used_bytes"] != 0:
                log.info("VIP %s cust=%s: data_used_bytes already %d — skipping backfill",
                         vip, cust["customer_name"], cust["data_used_bytes"])
                continue
            update_used_bytes(db, cust["customer_id"], total_now)
            key = mon._sidecar_key(cust["customer_id"], vip)
            sidecar[key] = total_now
            log.warning("VIP %s cust=%s: BACKFILLED %d bytes (data_used 0 → %d)",
                        vip, cust["customer_name"], total_now, total_now)
        db.commit()
        mon._save_session(sidecar)
    finally:
        db.close()
    log.info("=== backfill-orphans done ===")


if __name__ == "__main__":
    main()
