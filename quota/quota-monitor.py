#!/usr/bin/env python3
"""
quota-monitor.py — Phase 7 pool-LEASE edition (2026-06-27)

v1.7.0 / Phase 7 (2026-06-27 05:50 UTC): Source of truth for VIP ownership
is now `swanctl --list-pools --leases` instead of `swanctl --list-sas`.

Why the change:
  - iOS native IKEv2 client tears down SAs aggressively when the device
    sleeps (background-suspend) — typical SA lifetime is <30s during normal
    phone use, often <10s.
  - The 10s quota-monitor poll therefore missed most iOS SAs entirely;
    bytes accumulated in the nft meter but no DB row was updated.
    Concrete case: customer 74 (zunaid-cellphone) connected multiple times
    in a 12h window; meter accumulated 1.45 MB; DB stayed at 0; sidecar
    never received an entry for customer 74.

What this fixes:
  - Pool leases are sticky for `reauth_time` (24h default, 1h after D).
    While a lease is held (online OR offline), VIP→customer ownership is
    known and meter bytes get attributed.
  - First-observation baseline: when a customer first gets a lease on a
    VIP, we baseline the meter (no catch-up delta). Prevents the previous
    bug where a new customer on a recycled VIP got credited with the old
    customer's bytes via `delta = meter_now - DB.data_used`.
  - Released-lease cleanup: when a customer's lease is released (no longer
    in `swanctl --list-pools --leases`), we drop the sidecar entry so the
    next user of that VIP starts clean.

What this preserves:
  - The 80% warn + 100% cut logic, the kill_customer_credentials flow,
    the operator/inactive skips, the meter source (nft named meters), the
    10s poll interval, the audit + alerts tables.

Reads per-VIP netfilter byte counters (nft named meters), resolves VIP →
customer via pool lease → EAP identity, applies 80% warn + 100% hard-cut.

Data flow (one iteration):
  1. `swanctl --list-pools --leases` → list of VIP+identity+online
  2. Snapshot per-VIP byte counters from named meters (client_src, client_dst)
  3. Drop sidecar entries for VIPs no longer leased (lease released)
  4. For each current lease (online OR offline):
     a. Resolve identity → users → devices → customers
     b. Skip operator / inactive / no-mapping (legacy EAP)
     c. First observation of this customer-VIP pair → just baseline
     d. Else: delta = meter_now - sidecar[customer_id], update DB if > 0
     e. Check 80%/100% thresholds; act on cut (kill creds + terminate SAs)
  5. Save updated sidecar with current leases only
  6. Sleep POLL_INTERVAL, repeat

Source of truth: nft named meters in /etc/nftables.conf + pool leases.
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
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

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

# === DAE (RFC 5176 Disconnect-Request) — Phase 5D, 2026-07-06 14:11 SAST ===
#
# vpn_disconnect.py lives in the same dir as quota-monitor.py (systemd runs
# us from /home/zunaid/strongswan/quota/). sys.path insert below is a
# defensive belt that covers manual runs, future venv moves, or any case
# where cwd != script dir.
_VPN_DISC_DIR = "/home/zunaid/strongswan/quota"
if _VPN_DISC_DIR not in sys.path:
    sys.path.insert(0, _VPN_DISC_DIR)
try:
    from vpn_disconnect import send_dae_disconnect  # type: ignore[import-not-found]
    _DAE_HELPER_IMPORTED = True
except Exception as _dae_imp_err:  # ImportError, SyntaxError, anything
    # Fall back to a no-op so the daemon still runs if vpn_disconnect.py
    # is missing or broken on disk. We log loudly; _cut_customer will
    # record "dae_result=missing_helper" in the audit row.
    log.error("vpn_disconnect import failed (%s) — DAE disabled, "
              "fallback no-op used; cut will fall back to "
              "swanctl --terminate", _dae_imp_err)

    def send_dae_disconnect(*_args, **_kwargs):  # type: ignore[no-redef]
        return "error"

    _DAE_HELPER_IMPORTED = False

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


# Pool lease line format (strongSwan 6.0.7 swanctl --list-pools --leases):
#   rw-pool              10.99.0.1                           0 / 1 / 254
#     10.99.0.1                      online  'saalieg-laptop'
# Header line (no indent) is pool summary; indented lines are leases.
# Phase 7 (2026-06-27): we treat ALL leases (online + offline) as the
# source of truth for VIP ownership, not just ESTABLISHED IKE_SAs.
_POOL_LEASE_LINE_RE = re.compile(
    r"^\s+(?P<vip>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<status>online|offline)\s+"
    r"'(?P<identity>[^']*)'\s*$"
)


def list_pool_leases() -> list[dict]:
    """Parse `swanctl --list-pools --leases` output.

    Phase 7 (2026-06-27): returns ALL leases (online + offline) so quota
    attribution survives SA churn. See module docstring.

    Returns one dict per lease with keys:
      - pool     (str, the pool name, e.g. "rw-pool")
      - vip      (str, e.g. "10.99.0.1")
      - identity (str, the EAP identity, e.g. "saalieg-laptop")
      - online   (bool)
    """
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
            # Header line: "<pool-name> <base> <used>/<total>/<size>"
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
    """Parse `swanctl --list-sas` output.

    Used only by terminate_customer_sas() to find SAs to kill on a 100%
    cut. The data flow no longer depends on this for billing — see
    list_pool_leases() (Phase 7).

    Returns one dict per ESTABLISHED IKE_SA with keys:
      - username  (str, the EAP identity)
      - vip       (str, e.g. "10.99.0.5")
      - uniqueid  (str, the IKE_SA unique id, e.g. "153")
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

    Phase 5 (post-cutover 2026-07-06): charon authenticates via RADIUS
    (`auth = eap-radius`). The rw-eap.conf `secrets { eap-XXX { ... } }`
    block is dead-weight — killing it does NOT lock the customer out.
    The PRIMARY kill mechanism is now disable_customer_radcheck() (RADIUS).
    This rw-eap.conf kill is kept as defense-in-depth: if eap-radius
    ever fails and charon falls back to eap-mschapv2, the dead secret
    is still useful.

    Returns True on success OR if no block was found (Phase 5+ customer
    that never had a rw-eap.conf entry — normal, not an error).
    Returns False only on write/reload failure.
    """
    if not CONF_PATH.exists():
        log.error("rw-eap.conf not found at %s", CONF_PATH)
        return False

    original = CONF_PATH.read_text()

    # Match:  eap-<username> {\n    id     = <username>\n    secret = "..."\n  }
    # Replace secret value with a random unguessable token.
    killed = f"KILLED-{secrets.token_hex(8)}"
    pattern = re.compile(
        r"(eap-" + re.escape(username) + r"\s*\{\s*id\s*=\s*"
        + re.escape(username) + r"\s*secret\s*=\s*\")[^\"]*(\")",
        re.MULTILINE,
    )
    new_text, n_subs = pattern.subn(r"\g<1>" + killed + r"\g<2>", original)
    if n_subs == 0:
        # No rw-eap.conf block — expected for customers created post-Phase-5
        # cutover (only radcheck rows exist). Not an error.
        log.info("No eap-%s block in rw-eap.conf (Phase 5+ customer, "
                 "auth via RADIUS only) — rw-eap kill skipped", username)
        return True

    # Backup before write
    CONF_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
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


# === Phase 5: RADIUS radcheck disable (primary kill mechanism) ===

_RADIUS_DB_NAME = "radius"
_RADIUS_PW_FILE = Path("/root/.mariadb-radius-pw")


def _read_radius_db_password() -> Optional[str]:
    """Read MariaDB radius@127.0.0.1 password from /root/.mariadb-radius-pw.

    The file is root-only (mode 600). quota-monitor.service runs as root,
    so this is safe. Returns None if the file is missing/unreadable.
    Skips comment lines (file starts with `# ...` metadata).
    """
    if not _RADIUS_PW_FILE.exists():
        log.error("RADIUS password file missing: %s", _RADIUS_PW_FILE)
        return None
    try:
        text = _RADIUS_PW_FILE.read_text()
    except (PermissionError, OSError) as e:
        log.error("Cannot read %s: %s (quota-monitor must run as root)",
                  _RADIUS_PW_FILE, e)
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    log.error("No password line found in %s (only comments?)", _RADIUS_PW_FILE)
    return None


def disable_customer_radcheck(username: str) -> bool:
    """Replace radcheck Cleartext-Password with DISABLED-<random> marker.

    Phase 5 cutover (2026-07-06): this is the PRIMARY kill mechanism.
    Charon authenticates via RADIUS (`auth = eap-radius` in rw-eap.conf);
    only the radcheck row matters for whether the customer can reconnect.

    SQL mirrors the portal_auth.disable_customer_radcheck() function:
      1. DELETE all existing radcheck rows for this username
      2. INSERT a Cleartext-Password := DISABLED-<random> marker
         (fails MSCHAPv2 verification — rejects every auth attempt)

    Returns True on success, False if DB unavailable or SQL failed.
    The original Cleartext-Password is preserved in customer_auth
    (managed by the portal) for restoration via /api/quota/{id}/reset.
    """
    pw = _read_radius_db_password()
    if not pw:
        return False

    disabled_marker = f"DISABLED-{secrets.token_hex(8)}"
    # token_hex output is `[0-9a-f]+` — safe to interpolate directly.
    # username comes from our DB join (already validated by SQLite path);
    # still escape single quotes defensively.
    safe_user = username.replace("'", "''")
    sql = (
        f"DELETE FROM radcheck WHERE username = '{safe_user}';\n"
        f"INSERT INTO radcheck (username, attribute, op, value) "
        f"VALUES ('{safe_user}', 'Cleartext-Password', ':=', '{disabled_marker}');\n"
    )

    try:
        # Use MYSQL_PWD env var instead of -p<pw> arg to avoid leaking
        # the password via `ps`. mariadb/mysql CLI both respect MYSQL_PWD.
        proc = subprocess.run(
            ["mariadb", "-u", "radius", "-h", "127.0.0.1", _RADIUS_DB_NAME],
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

    def _last_sampled_bytes(self) -> dict[str, int]:
        """Load {key: last_meter_total} from sidecar.

        Sidecar is keyed by 'cid:vip' string so we can correctly handle
        the case where a customer moves between VIPs across reconnects
        and the case where a recycled VIP gets a new customer. On first
        observation of a (cid, vip) pair, we baseline (no credit) to
        avoid leaking the previous VIP user's bytes into the new user.

        Dropped automatically by _save_session when the corresponding
        lease is released.
        """
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
        """Single iteration. Returns when done. Safe to call repeatedly.

        Phase 7 (2026-06-27): iterates over pool leases (online + offline)
        rather than active IKE_SAs. See module docstring for rationale.
        """
        log.info("=== quota-monitor iteration start ===")
        leases = list_pool_leases()
        if not leases:
            log.info("no pool leases — nothing to bill; clearing sidecar")
            # No leases means nobody owns any VIP. Drop all sidecar
            # entries so the next user of any recycled VIP starts clean.
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
                    # Lease held but no meter activity yet — common on
                    # brand-new connection before first packet. Skip; we'll
                    # pick it up on the next iteration.
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
                    # Still baseline so the next non-operator user of this
                    # VIP starts from the operator's end-state.
                    new_totals[key] = total_now
                    continue

                if not cust["is_active"]:
                    log.info("VIP %s: customer %s is_active=0 — skipping billing, baselining",
                             vip, cust["customer_name"])
                    new_totals[key] = total_now
                    continue

                prior = last_totals.get(key)
                if prior is None:
                    # First observation of this (customer, VIP) pair.
                    # Baseline only — do NOT credit catch-up delta.
                    # (Phase 5 bug: this used to do `delta = meter - data_used`
                    # which leaked the previous VIP user's bytes into the
                    # new user. Phase 7 fix: key by (cid, vip), not just cid.)
                    log.info("VIP %s (%s/%s): first observation on this (customer,VIP) pair, "
                             "baseline meter=%d (DB data_used=%d)",
                             vip, cust["customer_name"], cust["device_name"],
                             total_now, data_used)
                    new_totals[key] = total_now
                    continue

                delta = total_now - prior
                if delta < 0:
                    # Meter went backwards (e.g. nftables reloaded). Re-baseline.
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

                # Enrich cust for downstream functions
                cust["vip"] = vip
                cust["username"] = identity
                cust["online"] = online

                # Check thresholds — fires regardless of online/offline.
                # A customer who hit 100% while online should still get cut
                # even if their phone went to sleep in the meantime.
                if data_limit > 0:
                    pct = 100 * data_used / data_limit
                    if pct >= CUT_PCT and not cust["over_quota"]:
                        self._cut_customer(db, cust, data_used)
                    elif pct >= WARN_PCT and not alert_already_sent(db, customer_id, WARN_PCT):
                        self._warn_customer(db, cust, data_used, pct)

            db.commit()

            # Sidecar cleanup: new_totals already only contains (cid, vip)
            # pairs for current leases. Saving it automatically drops
            # entries for leases that were released since the last
            # iteration. This prevents the next user of a recycled VIP
            # from inheriting the previous user's baseline.
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
        """100% threshold — kill RADIUS radcheck (PRIMARY), kill rw-eap.conf
        (defense-in-depth), DAE Disconnect-Request (RFC 5176 standards-based
        SA kill), terminate SAs as belt-and-suspenders, set over_quota=1.

        Phase 5+ cutover (2026-07-06): charon authenticates via RADIUS
        (`auth = eap-radius`). The rw-eap.conf secret KILL is no longer
        the mechanism that locks a customer out — only the radcheck
        Cleartext-Password in MariaDB is. We do BOTH: radcheck disable
        is the primary kill (a hard fail in this step is a cut failure);
        rw-eap.conf kill is defense-in-depth (warn but don't fail if
        the block is missing — Phase 5+ customers don't have one).

        DAE Disconnect-Request (Phase 5D, 2026-07-06 14:11 SAST): the
        cleanest, standards-based way to actively terminate the
        customer's IKE_SA. Sent to charon's eap-radius.dae listener
        on UDP/127.0.0.1:3799 via vpn_disconnect.send_dae_disconnect().
        charon returns ACK = terminated, NAK = no SA matched (already
        offline = OK). Replaces (complements) swanctl --terminate
        which is kept as belt-and-suspenders for SAs the DAE path
        misses (half-open, mid-rekey).

        Reverts /api/quota/{id}/reset restores radcheck via
        enable_customer_radcheck() in the portal.
        """
        customer_id = cust["customer_id"]
        username = cust["username"]
        log.error("VIP %s user=%s cust=%s: 100%% CUT — used %d / %d",
                  cust["vip"], username, cust["customer_name"],
                  data_used, cust["data_limit_bytes"])

        # 1. PRIMARY: disable RADIUS radcheck (locks future auth)
        radcheck_killed = disable_customer_radcheck(username)

        # 2. DEFENSE-IN-DEPTH: kill rw-eap.conf secret (Phase-5 obsolete
        # but kept for safety; no-op for Phase 5+ customers)
        rw_eap_killed = kill_customer_credentials(db, customer_id, username)

        # 3. RFC 5176 DAE Disconnect-Request - standards-based active SA
        #    termination via charon's eap-radius.dae listener (UDP/3799).
        #    pyrad sends User-Name=<username>; charon matches against
        #    active IKE_SAs and sends IKE DELETE INFORMATIONAL. ACK = ok,
        #    NAK = no SA (already offline). Both are non-fatal in the
        #    cut log: we're not blocking on either result.
        dae_result = send_dae_disconnect(username)

        # 4. Belt-and-suspenders: parse swanctl --list-sas and terminate.
        #    Catches any SA the DAE path missed (e.g., half-open states).
        n_terminated = terminate_customer_sas(username)

        # 5. Mark + audit + alert
        if radcheck_killed:
            db.execute(
                "UPDATE customers SET over_quota = 1, updated_at = ? WHERE id = ?",
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
    p.add_argument(
        "--backfill-orphans", action="store_true",
        help="One-time migration: for each current pool lease with a "
             "customer mapping and data_used_bytes=0 in DB, credit the "
             "current nft meter total to data_used_bytes and write it to "
             "the sidecar as the baseline. Use this when deploying "
             "Phase 7 to recover meter bytes accumulated during the "
             "Phase 5/6 iOS-misses-SAs bug window.",
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
    """One-time migration for Phase 7 deploy.

    For each current pool lease:
      - Look up the customer via the EAP identity
      - Skip if operator / inactive / no mapping
      - If customer.data_used_bytes == 0 AND meter has bytes for the VIP:
        credit meter_total to data_used_bytes (so the historical bytes
        that the Phase 5/6 code missed are visible to the operator)
        AND seed the sidecar with the same meter total so subsequent
        iterations don't double-count.
    """
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
