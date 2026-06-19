#!/usr/bin/env python3
"""
quota-monitor.py — Phase 5B.3

Reads per-VIP iptables-legacy byte counters, resolves VIP → customer,
applies 80% warn + 100% hard-cut rules.

Data flow (one iteration):
  1. Snapshot per-VIP byte counters from FORWARD chain
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

Source of truth: iptables-legacy FORWARD chain comments `quota:VIP`.
DB is the persistence layer for cumulative usage + alert state.

Run on the LXC 903 HOST (not in the strongSwan container).
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
POLL_INTERVAL = 60

# Alert thresholds (percent)
WARN_PCT = 80
CUT_PCT = 100

# VICI (charon) — TCP socket exposed by the container on 127.0.0.1:4502
# We call it via `docker exec strongswan swanctl --uri=tcp://...`
SWANCTL_PREFIX = ["docker", "exec", "strongswan", "swanctl", "--uri=tcp://127.0.0.1:4502"]

# === Logging ===
log = logging.getLogger("quota-monitor")

# === iptables counter parsing ===

# Output line example (iptables-legacy -L FORWARD -nvx):
#   1560 1660173 ACCEPT all -- * * 10.99.0.5 0.0.0.0/0 /* quota:10.99.0.5 */
# When split on whitespace, we get exactly 12 fields:
#   [pkts, bytes, target, prot, opt, in, out, source, dest, "/*", "quota:VIP", "*/"]
# We use simple split() to avoid regex parsing pain.
QUOTA_COMMENT_RE = re.compile(r"quota:(\d+\.\d+\.\d+\.\d+)")


def sample_counters() -> dict[str, tuple[int, int, int, int]]:
    """Return {VIP: (out_pkts, out_bytes, in_pkts, in_bytes)}.

    Two rules per VIP: outbound (source=VIP) and inbound (destination=VIP).
    We group them in one pass.
    """
    try:
        out = subprocess.run(
            ["iptables-legacy", "-L", "FORWARD", "-nvx"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("iptables-legacy failed: rc=%s stderr=%s", e.returncode, e.stderr)
        return {}

    out_counters: dict[str, tuple[int, int, int, int]] = {}
    for line in out.stdout.splitlines():
        # Skip the header line
        if line.startswith("Chain") or line.startswith("target"):
            continue
        m = QUOTA_COMMENT_RE.search(line)
        if not m:
            continue
        vip = m.group(1)
        if not vip.startswith(VIP_PREFIX):
            continue
        parts = line.split()
        # parts = [pkts, bytes, target, prot, opt, in, out, source, dest, "/*", "quota:VIP", "*/"]
        if len(parts) < 9:
            continue
        try:
            pkts = int(parts[0])
            bytes_ = int(parts[1])
        except ValueError:
            continue
        src = parts[7]
        # out_counters[vip] = (out_pkts, out_bytes, in_pkts, in_bytes)
        cur = out_counters.get(vip, (0, 0, 0, 0))
        if src == vip:
            cur = (cur[0] + pkts, cur[1] + bytes_, cur[2], cur[3])
        else:
            cur = (cur[0], cur[1], cur[2] + pkts, cur[3] + bytes_)
        out_counters[vip] = cur
    return out_counters


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
        # "  remote 'demo-phone' @ 102.249.0.0[33094] [10.99.0.5]"
        m = re.search(r"remote\s+'([^']+)'\s+@\s+\S+\s+\[(\d+\.\d+\.\d+\.\d+)\]", line)
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
