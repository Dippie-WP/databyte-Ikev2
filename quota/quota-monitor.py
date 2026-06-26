#!/usr/bin/env python3
"""
quota-monitor.py — Phase 6 nft METER edition (2026-06-26)

MIGRATED 2026-06-26 17:23 SAST: Reads counters from nft NAMED METERS instead
of per-rule `comment "quota:VIP"` ACCEPT counters.

  - Replaces Phase 5 (2026-06-26): per-VIP ACCEPT rules + ensure_quota_rules()
  - Reads TWO meters:
      ip filter client_src { ip saddr counter }  → outbound (upload)
      ip filter client_dst { ip daddr counter }  → inbound (download)
  - Meters are declared in /etc/nftables.conf (source-of-truth, loaded at
    boot by nftables.service). NO runtime rule installation here.
  - Kernel auto-creates counter elements per VIP on first packet via
    `flags dynamic`.
  - 508 per-VIP rules → 2 meter rules. Single hash lookup vs 254 sequential
    evaluations per direction at high pps.

FIX (Phase 5 regression): ensure_quota_rules() previously appended per-VIP
ACCEPT rules AFTER the chain's `counter drop` rule, making them dead code.
Meters load in correct chain position from source-of-truth file.

Reads per-VIP netfilter byte counters, resolves VIP → customer,
applies 80% warn + 100% hard-cut rules.

Data flow (one iteration):
  1. Snapshot per-VIP byte counters from named meters (client_src, client_dst)
  2. Join counter[VIP] with leases.address (active leases only)
  3. Join leases.identity → users.id → devices.strongswan_user_id
     → devices.customer_id → customers
  4. For each customer (skipping is_operator=1):
     a. Read customers.data_used_bytes
     b. Update from counter delta (since last sample)
     c. Compute % used = data_used_bytes / data_limit_bytes
     d. If 80% crossed for the first time: log to alerts, write audit
     e. If 100% reached and over_quota=0: kill credentials, terminate SAs,
        set over_quota=1, write audit + alerts
  5. Sleep POLL_INTERVAL, repeat

Source of truth: nft named meters in /etc/nftables.conf.
DB is the persistence layer for cumulative usage + alert state.

Run on the VPS HOST (not in the strongSwan container).
The script uses `docker exec strongswan` for swanctl VICI calls.

Usage:
  quota-monitor.py                  # run as long-running daemon
  quota-monitor.py --once           # one iteration, exit
  quota-monitor.py --once --verbose # debug logging
"""
import argparse
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# === Config (paths) ===
DB_PATH = Path("/var/lib/strongswan/ipsec.db")
CONF_PATH = Path("/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf")
CONF_BACKUP_DIR = Path("/home/zunaid/strongswan/swanctl/conf.d/.backups")

# 10.99.0.0/24 VIP range — must match iptables rules in rules.v4
VIP_PREFIX = "10.99.0."

# Poll interval (seconds)
# Changed 2026-06-25 from 60s → 10s per Zun's directive (msg #22356).
# At 60s with 40Mbps cap and 5-10 Mbps real LTE throughput, customers
# were burning ~35-70 MB/min — zade hit 100% mid-poll and overran by
# ~34 MB before the next poll detected it. 10s reduces max overrun to
# ~6-12 MB while keeping CPU reasonable (10s × 40 customers × ~5ms/poll
# = 20ms/s = 2% CPU). See HEARTBEAT 2026-06-25 19:47 UTC.
POLL_INTERVAL = 10

# Alert thresholds (percent)
WARN_PCT = 80
CUT_PCT = 100

# VICI (charon) — TCP socket exposed by the container on 127.0.0.1:4502
# We call it via `docker exec strongswan swanctl --uri=tcp://...`
SWANCTL_PREFIX = ["docker", "exec", "strongswan", "swanctl", "--uri=tcp://127.0.0.1:4502"]

# === Logging ===
log = logging.getLogger("quota-monitor")

# === nft METER parsing (Phase 6, 2026-06-26) ===
#
# Meter output line example (from `nft list meter ip filter client_src`):
#   elements = { 10.99.0.5 counter packets 200 bytes 8000,
#                 10.99.0.7 counter packets 50 bytes 2000 }
#
# client_src meter: created by `ip saddr 10.99.0.0/24 meter client_src { ip saddr counter } accept`
# client_dst meter: created by `ip daddr 10.99.0.0/24 meter client_dst { ip daddr counter } accept`
#
# Counter elements are auto-created by the kernel on first match
# (`flags dynamic`). No runtime rule installation required.
METER_ELEM_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+counter packets (\d+) bytes (\d+)")


def _read_meter(meter_name: str) -> dict[str, tuple[int, int]]:
    """Read {VIP: (packets, bytes)} from a named meter in `ip filter` table.

    Returns empty dict on failure (e.g. meter missing during transition).
    """
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
    """Return {VIP: (out_pkts, out_bytes, in_pkts, in_bytes)} from named meters.

    Phase 6 (2026-06-26): reads two named meters instead of 508 per-rule counters.
      client_src meter: {ip saddr 10.99.0.X} → outbound (upload)
      client_dst meter: {ip daddr 10.99.0.X} → inbound (download)

    Return shape unchanged from Phase 5 so downstream logic (delta
    computation, DB writes, alert thresholds) needs no modification.

    Meters live in /etc/nftables.conf — declared in source-of-truth, loaded
    at boot by nftables.service. No runtime rule installation needed here.
    """
    src = _read_meter("client_src")
    dst = _read_meter("client_dst")

    all_vips = set(src) | set(dst)
    out: dict[str, tuple[int, int, int, int]] = {}
    for vip in all_vips:
        out_pkts, out_bytes = src.get(vip, (0, 0))
        in_pkts, in_bytes = dst.get(vip, (0, 0))
        out[vip] = (out_pkts, out_bytes, in_pkts, in_bytes)
    return out



# === DB queries ===

# Resolve username → customer/device using the canonical schema.
# identities.data is a BLOB containing the username as bytes; users.name is TEXT.
# We CAST the blob to TEXT for the join.
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
WHERE u.name = ?
LIMIT 1
"""


def lookup_customer_for_username(db: sqlite3.Connection, username: str) -> dict | None:
    """Resolve an IKE identity (username) to its device+customer record.

    Returns None if the username is not provisioned (orphan user, no
    customer mapping). This is the case for legacy EAP users (zun, etc.)
    that pre-date the 5B customer schema.
    """
    cur = db.execute(CUSTOMER_LOOKUP_SQL, (username,))
    r = cur.fetchone()
    return dict(r) if r else None


def list_active_sas() -> list[dict]:
    """Parse `swanctl --list-sas` output.

    Returns one dict per ESTABLISHED IKE_SA with keys:
      - username  (str, the EAP identity)
      - vip       (str, e.g. "10.99.0.5")
      - uniqueid  (str, the IKE_SA unique id, e.g. "153")
    We only care about ESTABLISHED IKE_SAs that are part of the rw-eap
    connection (i.e. the customer-facing pool).
    """
    try:
        proc = subprocess.run(
            SWANCTL_PREFIX + ["--list-sas"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("swanctl --list-sas failed: rc=%s stderr=%s", e.returncode, e.stderr)
        return []

    out: list[dict] = []
    sa = None
    for line in proc.stdout.splitlines():
        # Top of a new SA block, e.g.:
        # rw-eap: #153, ESTABLISHED, IKEv2, ...
        m = re.match(r"\s*rw-eap:\s+#(\d+),\s+ESTABLISHED", line)
        if m:
            sa = {"uniqueid": m.group(1), "username": None, "vip": None}
            out.append(sa)
            continue
        if sa is None:
            continue
        # "  remote '192.168.10.18' @ 102.182.117.43[4500] EAP: 'saalieg-laptop' [10.99.0.1]"
        # strongSwan 6.0.7 format: EAP identity after "EAP:", VIP is bracketed IP at end of line
        m = re.search(r"EAP:\s+'([^']+)'\s+\[(\d+\.\d+\.\d+\.\d+)\]", line)
        if m:
            sa["username"] = m.group(1)
            sa["vip"] = m.group(2)
    return [s for s in out if s["username"] and s["vip"]]


def update_used_bytes(db: sqlite3.Connection, customer_id: int, delta: int) -> None:
    """Add `delta` to customers.data_used_bytes. Idempotent caller-side."""
    db.execute(
        "UPDATE customers SET data_used_bytes = data_used_bytes + ?, updated_at = ? WHERE id = ?",
        (delta, int(time.time()), customer_id),
    )


def alert_already_sent(db: sqlite3.Connection, customer_id: int, threshold: int) -> bool:
    cur = db.execute(
        "SELECT 1 FROM alerts WHERE customer_id = ? AND threshold = ? LIMIT 1",
        (customer_id, threshold),
    )
    return cur.fetchone() is not None


def log_alert(db: sqlite3.Connection, customer_id: int, threshold: int, data_used: int) -> None:
    db.execute(
        "INSERT INTO alerts (customer_id, threshold, sent_at, data_used_bytes_at_alert) "
        "VALUES (?, ?, ?, ?)",
        (customer_id, threshold, int(time.time()), data_used),
    )


def log_audit(db: sqlite3.Connection, actor: str, action: str, target_type: str,
              target_id: int | None, payload: str) -> None:
    db.execute(
        "INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (actor, action, target_type, target_id, payload, int(time.time())),
    )


# === Kill credentials at 100% ===

def kill_customer_credentials(db: sqlite3.Connection, customer_id: int, username: str) -> bool:
    """Replace rw-eap.conf secret for `username` with KILLED-<random>.

    Returns True on success. Backups the original conf first.
    """
    if not CONF_PATH.exists():
        log.error("rw-eap.conf not found at %s", CONF_PATH)
        return False

    CONF_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    original = CONF_PATH.read_text()

    # Match:  eap-<username> {\n    id     = <username>\n    secret = "..."\n  }
    # Replace secret value with a random unguessable token.
    killed = f"KILLED-{os.urandom(8).hex()}"
    pattern = re.compile(
        r"(eap-" + re.escape(username) + r"\s*\{\s*id\s*=\s*"
        + re.escape(username) + r"\s*secret\s*=\s*\")[^\"]*(\")",
        re.MULTILINE,
    )
    new_text, n_subs = pattern.subn(r"\g<1>" + killed + r"\g<2>", original)
    if n_subs == 0:
        log.error("No eap-%s block found in rw-eap.conf — refusing to kill", username)
        return False

    # Backup before write
    bak = CONF_BACKUP_DIR / f"rw-eap.conf.bak-quotamon-{int(time.time())}"
    bak.write_text(original)
    log.info("Backed up original to %s", bak)

    CONF_PATH.write_text(new_text)
    log.info("Killed eap-%s secret in %s (subs=%d)", username, CONF_PATH, n_subs)

    # Reload charon via VICI
    try:
        subprocess.run(
            SWANCTL_PREFIX + ["--load-creds"],
            check=True, capture_output=True, text=True,
        )
        log.info("charon creds reloaded")
    except subprocess.CalledProcessError as e:
        log.error("charon --load-creds FAILED: rc=%s stderr=%s stdout=%s",
                  e.returncode, e.stderr, e.stdout)
        # Rollback the conf change — we don't want to leave a broken state
        CONF_PATH.write_text(original)
        log.warning("Rolled back conf change due to charon reload failure")
        return False

    return True


def terminate_customer_sas(username: str) -> int:
    """Terminate SAs for a specific EAP username.

    Uses `swanctl --terminate-sae --ike <id>` for each matching SA.
    Returns count of SAs terminated.
    """
    try:
        proc = subprocess.run(
            SWANCTL_PREFIX + ["--list-sas"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("swanctl --list-sas failed: rc=%s stderr=%s", e.returncode, e.stderr)
        return 0

    # Find all IKE_SA unique ids belonging to this username
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
            # Left the SA block (any line that doesn't look like a SA attribute)
            current_id = None

    if not target_ids:
        log.info("No ESTABLISHED rw-eap SAs for user %s to terminate", username)
        return 0

    terminated = 0
    for ike_id in target_ids:
        try:
            # swanctl 6.0.7: --ike-id is the IKE_SA unique id (not --ike)
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

    def _open_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(DB_PATH), timeout=5)
        db.row_factory = sqlite3.Row
        # The charon container writes to this DB via attr-sql. Use WAL-ish
        # busy timeout; charon holds short transactions.
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    def _last_sampled_bytes(self) -> dict[int, int]:
        """Track per-customer last seen counter total, to compute deltas.

        For Phase 5B.3, we use customers.data_used_bytes as the cumulative
        record, and read iptables counters as the SOURCE OF TRUTH for the
        current "total bytes used". So we don't need a separate in-memory
        cache of last-sampled values — we just read counters + DB and
        reconcile.

        Delta = current_counter_total - (DB.data_used_bytes - prior_counter_total)
        But iptables counters are CUMULATIVE since boot, so we can just
        compute: actual_bytes_used_now = counter_total_now - counter_at_session_start

        For simplicity, we treat counter_total_now as the truth, and update
        DB.data_used_bytes to match (clamped: DB >= counter_at_first_observation
        of the current connection).
        """
        # Read counter_at_session_start from a side file
        sidecar = Path("/var/run/quota-monitor.session")
        if not sidecar.exists():
            return {}
        import json
        return {int(k): v for k, v in json.loads(sidecar.read_text()).items()}

    def _save_session(self, totals: dict[int, int]) -> None:
        import json
        sidecar = Path("/var/run/quota-monitor.session")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({str(k): v for k, v in totals.items()}))

    def run_once(self) -> None:
        """Single iteration. Returns when done. Safe to call repeatedly."""
        log.info("=== quota-monitor iteration start ===")
        sas = list_active_sas()
        if not sas:
            log.info("no active SAs — nothing to bill")
            return

        counters = sample_counters()
        if not counters:
            log.info("no quota:* counters found in FORWARD chain — nothing to do")
            return

        db = self._open_db()
        try:
            last_totals = self._last_sampled_bytes()
            for sa in sas:
                vip = sa["vip"]
                if vip not in counters:
                    log.debug("VIP %s has SA but no iptables counter — skipping", vip)
                    continue
                cust = lookup_customer_for_username(db, sa["username"])
                if cust is None:
                    # Legacy EAP user (no customer mapping). Skip — no quota.
                    log.debug("VIP %s: user %s has no customer mapping — skipping",
                              vip, sa["username"])
                    continue
                _, out_bytes, _, in_bytes = counters[vip]
                total_now = out_bytes + in_bytes
                customer_id = cust["customer_id"]
                is_operator = cust["is_operator"]
                data_limit = cust["data_limit_bytes"]
                data_used = cust["data_used_bytes"]

                if is_operator:
                    log.debug("VIP %s: operator %s — skipping",
                              vip, cust["customer_name"])
                    continue

                if not cust["is_active"]:
                    log.info("VIP %s: customer %s is_active=0 — skipping",
                             vip, cust["customer_name"])
                    continue

                # Compute delta since last sample
                prior = last_totals.get(customer_id, data_used)
                delta = total_now - prior
                if delta < 0:
                    # Counter reset (e.g. iptables rules reloaded) — re-baseline
                    log.warning("VIP %s: counter went backwards (%d → %d), re-baselining",
                                vip, prior, total_now)
                    delta = 0

                if delta > 0:
                    update_used_bytes(db, customer_id, delta)
                    data_used += delta
                    log.info("VIP %s (%s/%s): +%d bytes, used=%d / %d (%.1f%%)",
                             vip, cust["customer_name"], cust["device_name"],
                             delta, data_used, data_limit,
                             100 * data_used / data_limit if data_limit else 0)

                # Enrich cust with sa-level fields for downstream functions
                cust["vip"] = vip
                cust["username"] = sa["username"]

                # Check thresholds
                if data_limit > 0:
                    pct = 100 * data_used / data_limit
                    if pct >= CUT_PCT and not cust["over_quota"]:
                        self._cut_customer(db, cust, data_used)
                    elif pct >= WARN_PCT and not alert_already_sent(db, customer_id, WARN_PCT):
                        self._warn_customer(db, cust, data_used, pct)

            db.commit()

            # Update session cache for next iteration
            new_totals = {}
            for sa in sas:
                vip = sa["vip"]
                if vip in counters:
                    cust = lookup_customer_for_username(db, sa["username"])
                    if cust is not None:
                        _, out_b, _, in_b = counters[vip]
                        new_totals[cust["customer_id"]] = out_b + in_b
            self._save_session(new_totals)
        finally:
            db.close()
        log.info("=== quota-monitor iteration done ===")

    def _warn_customer(self, db: sqlite3.Connection, cust: dict, data_used: int, pct: float) -> None:
        """80% threshold — log alert, write audit. (Telegram DM is a no-op for now.)"""
        customer_id = cust["customer_id"]
        log.warning("VIP %s user=%s cust=%s: 80%% WARN — used %d / %d (%.1f%%)",
                    cust["vip"], cust["username"], cust["customer_name"],
                    data_used, cust["data_limit_bytes"], pct)
        log_alert(db, customer_id, WARN_PCT, data_used)
        log_audit(db, "quota-monitor", "warn_80pct", "customer", customer_id,
                  f'{{"pct": {pct:.2f}, "data_used": {data_used}, '
                  f'"data_limit": {cust["data_limit_bytes"]}}}')

    def _cut_customer(self, db: sqlite3.Connection, cust: dict, data_used: int) -> None:
        """100% threshold — kill creds, terminate SAs, set over_quota=1."""
        customer_id = cust["customer_id"]
        username = cust["username"]
        log.error("VIP %s user=%s cust=%s: 100%% CUT — used %d / %d",
                  cust["vip"], username, cust["customer_name"],
                  data_used, cust["data_limit_bytes"])

        ok = kill_customer_credentials(db, customer_id, username)
        n_terminated = terminate_customer_sas(username)

        if ok:
            db.execute(
                "UPDATE customers SET over_quota = 1, updated_at = ? WHERE id = ?",
                (int(time.time()), customer_id),
            )
            log_alert(db, customer_id, CUT_PCT, data_used)
            log_audit(db, "quota-monitor", "cut_100pct", "customer", customer_id,
                      f'{{"data_used": {data_used}, "data_limit": {cust["data_limit_bytes"]}, '
                      f'"sas_terminated": {n_terminated}}}')
        else:
            log_audit(db, "quota-monitor", "cut_100pct_FAILED", "customer", customer_id,
                      f'{{"data_used": {data_used}, "reason": "kill_customer_credentials returned False"}}')

    def run_daemon(self) -> None:
        """Long-running loop with graceful shutdown on SIGTERM/SIGINT."""
        running = True

        def stop(*_):
            nonlocal running
            running = False
            log.info("shutdown signal received — finishing current iteration")

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        log.info("starting daemon (poll every %ds, warn=%d%%, cut=%d%%)",
                 POLL_INTERVAL, WARN_PCT, CUT_PCT)

        # Phase 6 (2026-06-26): no runtime rule installation needed.
        # Quota meters (`client_src` / `client_dst`) live in /etc/nftables.conf
        # and load at boot via nftables.service. Kernel auto-creates counter
        # elements per VIP on first packet match.

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
    p = argparse.ArgumentParser(description="5B.3 quota monitor")
    p.add_argument("--once", action="store_true", help="run a single iteration and exit")
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = p.parse_args()

    mon = QuotaMonitor(verbose=args.verbose)
    if args.once:
        mon.run_once()
    else:
        mon.run_daemon()


if __name__ == "__main__":
    main()
