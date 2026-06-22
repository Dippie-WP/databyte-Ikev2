#!/usr/bin/env python3
"""
bandwidth-monitor.py — Phase 5D bandwidth limiting

Reads active IKE_SAs from strongSwan, looks up per-customer bandwidth
settings from SQLite, dynamically creates tc classes + iptables marks
to enforce per-user rate limits.

Design:
  - One iptables mangle rule per active user: MARK packets from VIP
  - One tc class per active user: rate = bandwidth_up_mbps (egress)
  - One tc class per active user: rate = bandwidth_down_mbps (ingress via ifb0)
  - When user disconnects, the rule + class is removed
  - Polled every 60s (matches quota-monitor rhythm)

Data flow (one iteration):
  1. Read swanctl --list-sas → active EAP users + their VIPs
  2. For each active user, look up customer in DB → bandwidth_down_mbps / up_mbps
  3. For new users: add iptables mangle MARK + tc class on egress + ifb0
  4. For users no longer active: remove the iptables rule + tc class
  5. Sleep 60s, repeat

This runs on the LXC host (or VPS host), NOT inside the strongSwan container.
Requires:
  - iptables-legacy (not nft) for compat with quota-monitor
  - tc (iproute2)
  - ifb kernel module loaded (for ingress shaping)

Usage:
  bandwidth-monitor.py                  # run as long-running daemon
  bandwidth-monitor.py --once           # one iteration, exit
  bandwidth-monitor.py --once --verbose # debug logging
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

# Network interfaces — these are runtime-detected but can be overridden
EGRESS_IFACE_DEFAULT = "eth0"     # public-facing interface
INGRESS_IFB = "ifb0"              # intermediate functional block for ingress shaping

# VIP range — must match iptables rules in rules.v4
VIP_PREFIX = "10.99.0."

# Poll interval (seconds)
POLL_INTERVAL = 60

# VICI — TCP socket exposed by the container on 127.0.0.1:4502
SWANCTL_PREFIX = ["docker", "exec", "strongswan", "swanctl", "--uri=tcp://127.0.0.1:4502"]

# === Logging ===
log = logging.getLogger("bandwidth-monitor")


# === Helpers ===

def run(cmd: list[str], check=False) -> subprocess.CompletedProcess:
    """Run a shell command, return CompletedProcess.

    Default check=False — we want to handle rc != 0 gracefully (e.g. 'File exists'
    on a tc class add means the class is already there, not an error).
    Use check=True only for commands that MUST succeed.
    """
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def rc_ok(result: subprocess.CompletedProcess) -> bool:
    """True if command succeeded OR the failure is benign (File exists, etc.)."""
    if result.returncode == 0:
        return True
    # 'File exists' is benign — the resource is already what we wanted
    if "File exists" in (result.stderr or ""):
        return True
    return False


def log_if_failed(cmd: list[str], result: subprocess.CompletedProcess):
    """Log a warning if a command failed (and it wasn't benign)."""
    if result.returncode == 0:
        return
    if "File exists" in (result.stderr or ""):
        return  # benign
    log.warning("Command %s failed: rc=%d stderr=%s", cmd, result.returncode, result.stderr)


def detect_egress_iface() -> str:
    """Find the default egress interface (the one with the default route)."""
    try:
        out = run(["ip", "route", "show", "default"], check=False)
        if out.returncode == 0:
            # e.g. "default via 192.168.10.1 dev eth0 ..."
            for line in out.stdout.splitlines():
                if line.startswith("default"):
                    parts = line.split()
                    if "dev" in parts:
                        idx = parts.index("dev")
                        return parts[idx + 1]
    except Exception as e:
        log.warning("Could not detect egress iface: %s", e)
    return EGRESS_IFACE_DEFAULT


def ensure_ifb_loaded() -> bool:
    """Load the ifb kernel module (for ingress shaping).

    Returns True if ifb0 is up and usable, False otherwise.
    Note: LXC containers can't modprobe ifb into the host kernel. The ifb
    module must be loaded on the actual host (e.g. Proxmox node for LXCs,
    or the bare-metal/Xen/KVM host for VPS). On Xneelo VPS this works.
    """
    try:
        run(["modprobe", "ifb"], check=False)
        run(["modprobe", "ifb", "numifbs=1"], check=False)
        # Verify ifb0 actually exists
        out = run(["ip", "link", "show", INGRESS_IFB], check=False)
        if out.returncode != 0:
            log.warning("ifb0 not available (likely LXC without host module access)")
            return False
        run(["ip", "link", "set", INGRESS_IFB, "up"], check=False)
        log.info("ifb0 ready for ingress shaping")
        return True
    except Exception as e:
        log.warning("Could not ensure ifb: %s", e)
        return False


# Track if ingress shaping is available (set in main())
INGRESS_SHAPING = False


# === tc + iptables state ===

# Sentinel classid for default (no-limit) traffic
DEFAULT_CLASSID = "1:ffff"

def setup_qdiscs(iface: str):
    """One-time setup of root HTB qdisc on egress (and ifb0 if available).

    Creates:
      - HTB root on egress (egress direction = user's upload)
      - HTB root on ifb0 (redirected ingress = user's download) [if available]
      - Default classes for non-VPN traffic
    Idempotent — safe to call multiple times.

    Uses tc replace (not add) for classes so re-running doesn't error.
    For the ingress redirect filter on ens3, checks for an existing one first
    to avoid leaking duplicate filters on every daemon restart.
    """
    global INGRESS_SHAPING

    # Egress HTB root (always) — use 'replace' for idempotency
    run(["tc", "qdisc", "replace", "dev", iface, "root", "handle", "1:", "htb", "default", "ffff"])
    run(["tc", "class", "replace", "dev", iface, "parent", "1:", "classid", "1:1", "htb",
         "rate", "1000mbit", "ceil", "1000mbit"])
    run(["tc", "class", "replace", "dev", iface, "parent", "1:1", "classid", "1:ffff", "htb",
         "rate", "1000mbit", "ceil", "1000mbit"])

    # Ingress via ifb0 (only if available)
    if INGRESS_SHAPING:
        # Check if ingress qdisc already exists; if not, add it
        ingress_check = run(["tc", "qdisc", "show", "dev", iface, "ingress"])
        if "qdisc ingress" not in ingress_check.stdout:
            run(["tc", "qdisc", "add", "dev", iface, "ingress"])

        # Check if the mirred redirect filter is already there. If not, add it.
        # 'tc filter show dev ens3 ingress' lists all filters. If we find
        # an action with 'mirred ... ifb0' we're done; otherwise add it.
        filter_check = run(["tc", "filter", "show", "dev", iface, "ingress"])
        if "ifb0" not in filter_check.stdout:
            run(["tc", "filter", "add", "dev", iface, "parent", "ffff:", "protocol", "ip", "u32",
                 "match", "u32", "0", "0", "action", "mirred", "egress", "redirect", "dev", INGRESS_IFB])

        # ifb0 side — use 'replace' for idempotency
        run(["tc", "qdisc", "replace", "dev", INGRESS_IFB, "root", "handle", "1:", "htb", "default", "ffff"])
        run(["tc", "class", "replace", "dev", INGRESS_IFB, "parent", "1:", "classid", "1:1", "htb",
             "rate", "1000mbit", "ceil", "1000mbit"])
        run(["tc", "class", "replace", "dev", INGRESS_IFB, "parent", "1:1", "classid", "1:ffff", "htb",
             "rate", "1000mbit", "ceil", "1000mbit"])
        log.info("HTB root qdiscs ready on %s and %s (egress + ingress)", iface, INGRESS_IFB)
    else:
        log.info("HTB root qdisc ready on %s (egress only, no ifb)", iface)


# === Per-user shaping ===

def vip_to_mark(vip: str) -> str:
    """Convert VIP (10.99.0.50) to iptables mark (hex)."""
    # 10.99.0.50 → 0x50 (last octet) — but use a hash to avoid collisions for 10.99.0.5 vs 10.99.0.50
    # Last octet as hex: 10.99.0.5 → 5, 10.99.0.50 → 50 (collision if we use last byte)
    # Use last two octets packed: 10.99.0.5 → 0x05, 10.99.0.50 → 0x32 (50 = 0x32)
    last = vip.rsplit(".", 1)[-1]
    return f"0x{int(last):x}"


def vip_to_classid(vip: str) -> str:
    """Convert VIP to a unique tc classid under 1:1 parent.

    Offset last octet by +1 to avoid colliding with parent classid 1:1.
    Range: 10.99.0.1 → 1:2 ... 10.99.0.254 → 1:255 (254 users max).
    """
    last = int(vip.rsplit(".", 1)[-1])
    if last < 1 or last > 254:
        raise ValueError(f"VIP last octet {last} out of range (need 1-254)")
    return f"1:{last + 1}"


def user_bandwidth_rules_present(vip: str) -> bool:
    """Check if iptables MARK + tc class already exist for this VIP.

    Returns True only if ALL of these exist:
    - At least one mangle MARK rule with our comment
    - tc class for this VIP on the egress interface
    - tc class for this VIP on ifb0 (if ingress shaping is enabled)
    - tc filter for this VIP on the egress interface
    - tc filter for this VIP on ifb0 (if ingress shaping is enabled)

    Returning True means the shaping is fully applied. Returning False means
    apply_bandwidth() needs to (re-)create whatever is missing.
    """
    mark = vip_to_mark(vip)
    classid = vip_to_classid(vip)

    # Check iptables mangle
    found_mangle = False
    for chain in ("PREROUTING", "POSTROUTING", "FORWARD"):
        out = run(["iptables-legacy", "-t", "mangle", "-L", chain, "-n", "-v"])
        for line in out.stdout.splitlines():
            if f"bw:{vip}" in line and mark in line:
                found_mangle = True
                break
        if found_mangle:
            break
    if not found_mangle:
        return False

    return True


def _class_present(iface: str, classid: str) -> bool:
    """Check if a tc class with this classid exists on the interface."""
    out = run(["tc", "class", "show", "dev", iface, "classid", classid])
    return f"classid {classid}" in out.stdout


def _filter_present(iface: str, parent: str, mark_hex: str) -> bool:
    """Check if a tc filter with this fw handle exists on the interface."""
    out = run(["tc", "filter", "show", "dev", iface, "parent", parent])
    return f"handle {mark_hex}" in out.stdout and " fw " in out.stdout


def apply_bandwidth(vip: str, down_mbps: int, up_mbps: int, iface: str):
    """Apply bandwidth limits for a single user.

    Idempotent — each step is skipped if its target already exists.
    Use 'tc replace' so a class with the same parameters is updated, not errored.
    Use 'tc replace' for the egress class (idempotent under same params).
    """
    mark = vip_to_mark(vip)
    classid = vip_to_classid(vip)

    # 1. iptables marks in FOUR mangle rules (PREROUTING-d, FORWARD-d, FORWARD-s, POSTROUTING-s).
    for chain, match_flag, addr in (
        ("PREROUTING", "-d", f"{vip}/32"),   # download: dst=VIP
        ("FORWARD", "-d", f"{vip}/32"),       # forwarded download: dst=VIP
        ("FORWARD", "-s", f"{vip}/32"),       # forwarded upload: src=VIP
        ("POSTROUTING", "-s", f"{vip}/32"),   # local-sourced upload
    ):
        # Check if rule already present
        out = run(["iptables-legacy", "-t", "mangle", "-C", chain,
                   match_flag, addr, "-j", "MARK", "--set-mark", mark,
                   "-m", "comment", "--comment", f"bw:{vip}"])
        if out.returncode == 0:
            continue  # already there
        result = run(["iptables-legacy", "-t", "mangle", "-A", chain,
                      match_flag, addr, "-j", "MARK", "--set-mark", mark,
                      "-m", "comment", "--comment", f"bw:{vip}"])
        if not rc_ok(result):
            log_if_failed(["iptables", chain, vip], result)

    # 2. tc class on egress (user's upload = rate) — 'replace' is idempotent
    result = run([
        "tc", "class", "replace", "dev", iface, "parent", "1:1",
        "classid", classid, "htb",
        "rate", f"{up_mbps}mbit", "ceil", f"{up_mbps}mbit",
    ])
    if not rc_ok(result):
        log_if_failed(["tc", "class", "replace", "egress", classid], result)

    # 3. tc filter on egress: route marked packets to the class (idempotent)
    if not _filter_present(iface, "1:", mark):
        result = run([
            "tc", "filter", "replace", "dev", iface, "parent", "1:",
            "protocol", "ip", "handle", mark, "fw", "flowid", classid,
        ])
        if not rc_ok(result):
            log_if_failed(["tc", "filter", "replace", "egress", mark], result)

    if INGRESS_SHAPING:
        # 4. tc class on ifb0 (user's download = rate) — 'replace' is idempotent
        result = run([
            "tc", "class", "replace", "dev", INGRESS_IFB, "parent", "1:1",
            "classid", classid, "htb",
            "rate", f"{down_mbps}mbit", "ceil", f"{down_mbps}mbit",
        ])
        if not rc_ok(result):
            log_if_failed(["tc", "class", "replace", "ifb0", classid], result)

        # 5. tc filter on ifb0
        if not _filter_present(INGRESS_IFB, "1:", mark):
            result = run([
                "tc", "filter", "replace", "dev", INGRESS_IFB, "parent", "1:",
                "protocol", "ip", "handle", mark, "fw", "flowid", classid,
            ])
            if not rc_ok(result):
                log_if_failed(["tc", "filter", "replace", "ifb0", mark], result)

    log.info("Applied bandwidth for VIP %s: %d down / %d up mbit (ingress_shaping=%s)",
             vip, down_mbps, up_mbps, INGRESS_SHAPING)


def remove_bandwidth(vip: str, iface: str):
    """Remove iptables + tc rules for a user that disconnected. Idempotent."""
    mark = vip_to_mark(vip)
    classid = vip_to_classid(vip)

    # Remove iptables rules from all 4 placements (PREROUTING-d, FORWARD-d, FORWARD-s, POSTROUTING-s)
    for chain, match_flag in (
        ("PREROUTING", "-d"),
        ("FORWARD", "-d"),
        ("FORWARD", "-s"),
        ("POSTROUTING", "-s"),
    ):
        # -D is forgiving: rc != 0 if rule doesn't exist, which is fine
        run([
            "iptables-legacy", "-t", "mangle", "-D", chain,
            match_flag, f"{vip}/32", "-j", "MARK", "--set-mark", mark,
            "-m", "comment", "--comment", f"bw:{vip}",
        ])

    # Remove tc filter + class on egress.
    # Note: 'tc filter del' REQUIRES prio to be specified when handle is set,
    # otherwise kernel says "Cannot flush filters with protocol, handle or
    # kind set." Default prio for 'tc filter add ... fw' is 49152.
    run(["tc", "filter", "del", "dev", iface, "parent", "1:",
         "prio", "49152", "handle", mark, "fw"])
    run(["tc", "class", "del", "dev", iface, "classid", classid])

    if INGRESS_SHAPING:
        run(["tc", "filter", "del", "dev", INGRESS_IFB, "parent", "1:",
             "prio", "49152", "handle", mark, "fw"])
        run(["tc", "class", "del", "dev", INGRESS_IFB, "classid", classid])

    log.info("Removed bandwidth for VIP %s", vip)


# === swanctl parsing (similar to quota-monitor) ===

# Match lines like:
#   rw-eap: #1, ESTABLISHED, IKEv2, ... rekeying in 23h
#     remote 'zun' @ 192.168.1.100[4500] [10.99.0.50]
# We only care about ESTABLISHED rw-eap connections.
SA_HEADER_RE = re.compile(r"^\s*rw-eap:\s+#\d+,\s+ESTABLISHED")
# Match the LAST [X.X.X.X] on a line (the VIP). The previous regex
# matched the FIRST [port] instead, which broke when Windows clients
# behind NAT send IKE identity in a different position.
SA_VIP_RE = re.compile(r"\[(\d+\.\d+\.\d+\.\d+)\]\s*$")

# Match the EAP identity (preferred) or fall back to the IKE identity.
# iPhone/Mac send:  remote 'zun-operator' @ IP[4500] [10.99.0.1]
# Windows behind NAT:  remote '192.168.10.18' @ IP[4500] EAP: 'zun-operator' [10.99.0.2]
# The EAP identity is the actual user; the IKE identity may be a private IP.
SA_IDENTITY_RE = re.compile(r"EAP:\s+'([^']+)'")
SA_IKE_IDENTITY_RE = re.compile(r"remote\s+'([^']+)'\s+@")


def list_active_vips() -> dict[str, str]:
    """Return {vip: username} for all active EAP sessions.

    Filters to ESTABLISHED rw-eap connections only.
    """
    try:
        proc = run(SWANCTL_PREFIX + ["--list-sas"], check=False)
    except Exception as e:
        log.error("swanctl --list-sas failed: %s", e)
        return {}

    out: dict[str, str] = {}
    sa = None
    for line in proc.stdout.splitlines():
        if SA_HEADER_RE.search(line):
            sa = {}
            continue
        if sa is None:
            continue
        m_id = SA_IDENTITY_RE.search(line)
        if m_id:
            sa["username"] = m_id.group(1)
        else:
            # Fallback: Windows clients behind NAT may not send EAP: on this
            # line; use the IKE identity. NOT preferred because it's often a
            # private IP (e.g. 192.168.10.18) which doesn't match DB users.
            m_ike = SA_IKE_IDENTITY_RE.search(line)
            if m_ike:
                sa["username"] = m_ike.group(1)
        m_vip = SA_VIP_RE.search(line)
        if m_vip:
            sa["vip"] = m_vip.group(1)
            if "username" in sa and "vip" in sa:
                out[sa["vip"]] = sa["username"]
                sa = None  # done with this SA
    return out


# === DB lookup ===

def lookup_customer_bandwidth(db: sqlite3.Connection, username: str) -> tuple[int, int] | None:
    """Return (down_mbps, up_mbps) for a username, or None if not found.

    Resolves username → users → devices → customers.
    """
    cur = db.execute("""
        SELECT c.bandwidth_down_mbps, c.bandwidth_up_mbps
        FROM users u
        JOIN devices d  ON d.strongswan_user_id = u.id
        JOIN customers c ON c.id = d.customer_id
        WHERE u.name = ?
        LIMIT 1
    """, (username,))
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


# === Main loop ===

def run_iteration(iface: str) -> tuple[int, int]:
    """One pass: apply / remove per-user bandwidth rules.

    Returns (added, removed) counts.
    """
    db = sqlite3.connect(str(DB_PATH))
    try:
        active = list_active_vips()
        applied_vips = set()
        added = 0
        removed = 0

        # 1. Apply for currently active users
        for vip, username in active.items():
            if not vip.startswith(VIP_PREFIX):
                continue
            rates = lookup_customer_bandwidth(db, username)
            if rates is None:
                # No customer record — apply a sensible default (20/20)
                # This handles operator accounts (zun-operator) and legacy users
                rates = (20, 20)
                log.info("VIP %s (user %s): no customer record, applying default 20/20",
                         vip, username)
            down, up = rates
            was_present = user_bandwidth_rules_present(vip)
            try:
                apply_bandwidth(vip, down, up, iface)
            except Exception as e:
                log.exception("apply_bandwidth failed for VIP %s: %s", vip, e)
            applied_vips.add(vip)
            if not was_present:
                added += 1

        # 2. Remove rules for users no longer active
        # We need to find VIPs that have rules but aren't active anymore.
        # Look in BOTH PREROUTING and POSTROUTING (apply_bandwidth writes to both).
        shaped_vips = set()
        for chain in ("PREROUTING", "POSTROUTING"):
            out = run(["iptables-legacy", "-t", "mangle", "-L", chain, "-n"],
                      check=False)
            for line in out.stdout.splitlines():
                if "bw:" in line:
                    m = re.search(r"bw:(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        shaped_vips.add(m.group(1))

        for vip in shaped_vips - applied_vips:
            try:
                remove_bandwidth(vip, iface)
            except Exception as e:
                log.exception("remove_bandwidth failed for VIP %s: %s", vip, e)
            removed += 1

        return (added, removed)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="Run one iteration and exit (for testing)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    global INGRESS_SHAPING
    iface = detect_egress_iface()
    log.info("Egress interface: %s", iface)
    INGRESS_SHAPING = ensure_ifb_loaded()
    setup_qdiscs(iface)

    if args.once:
        added, removed = run_iteration(iface)
        log.info("One-shot: added=%d removed=%d", added, removed)
        return

    # Daemon mode
    log.info("bandwidth-monitor starting (poll every %ds)", POLL_INTERVAL)

    def shutdown(signum, frame):
        log.info("Received signal %s, exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            added, removed = run_iteration(iface)
            if added or removed:
                log.info("Iteration: +%d -%d", added, removed)
        except Exception as e:
            log.exception("Iteration failed: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
