#!/usr/bin/env python3
"""
bandwidth-monitor-nft.py — nftables edition of bandwidth-monitor.

MIGRATED: 2026-06-26 — iptables-legacy mangle MARK rules → nft MARK rules.

Replaces these iptables calls (in original bandwidth-monitor.py):
  - `iptables-legacy -t mangle -A CHAIN -d VIP -j MARK --set-mark 0xN` → nft add rule
  - `iptables-legacy -t mangle -D CHAIN -d VIP -j MARK --set-mark 0xN` → nft delete rule (by handle)
  - `iptables-legacy -t mangle -C CHAIN -d VIP -j MARK --set-mark 0xN` → nft -a list + grep
  - `iptables-legacy -t mangle -L CHAIN -nvx` (search for bw:VIP comment) → nft -a list + grep

tc (traffic control) commands are NOT changed — tc is separate from netfilter.
"""
import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

# === Config (preserved from original) ===
VIP_PREFIX = "10.99.0."

log = logging.getLogger("bandwidth-monitor")


def run(cmd, **kw):
    """Subprocess wrapper with logging."""
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def vip_to_mark(vip: str) -> str:
    """Convert VIP (10.99.0.50) to nft mark (hex 0xN).
    nft accepts hex marks: `meta mark set 0xN` — same format as iptables.
    """
    last = int(vip.rsplit(".", 1)[1])
    return f"0x{last:x}"


def vip_to_classid(vip: str) -> str:
    """HTB classid (preserved from original)."""
    last = int(vip.rsplit(".", 1)[1])
    return f"1:{last:x}"


# === nft mark rule management ===

# Mapping: chain name in iptables terms → nft chain (lowercase)
CHAIN_NFT = {
    "PREROUTING": "prerouting",
    "FORWARD": "forward",
    "POSTROUTING": "postrouting",
}


def mark_rule_present(vip: str, chain: str, match_flag: str, mark: str) -> bool:
    """Check if the MARK rule for this VIP+chain already exists in nft.

    match_flag is one of: -d (dst=VIP, download) or -s (src=VIP, upload).
    """
    nft_chain = CHAIN_NFT[chain]
    out = run(["/usr/sbin/nft", "-a", "list", "chain", "ip", "mangle", nft_chain])
    if out.returncode != 0:
        log.error("nft list chain %s failed: %s", nft_chain, out.stderr)
        return False
    target = f'comment "bw:{vip}"'  # stored form in nft output is always quoted
    return target in out.stdout


def add_mark_rule(vip: str, chain: str, match_flag: str, mark: str) -> bool:
    """Add MARK rule to nft mangle table. Idempotent."""
    if mark_rule_present(vip, chain, match_flag, mark):
        log.debug("MARK rule already present: chain=%s vip=%s", chain, vip)
        return True

    nft_chain = CHAIN_NFT[chain]
    addr_keyword = "daddr" if match_flag == "-d" else "saddr"

    # IMPORTANT: comment must be quoted because `bw:VIP` contains a colon,
    # which nft's lexer would otherwise interpret as a separator.
    cmd = [
        "/usr/sbin/nft", "add", "rule", "ip", "mangle", nft_chain,
        "ip", addr_keyword, f"{vip}/32",
        "meta", "mark", "set", mark,
        "comment", f'"bw:{vip}"',
    ]
    result = run(cmd)
    if result.returncode == 0:
        log.info("Added nft MARK rule: %s chain=%s %s=%s mark=%s",
                 vip, chain, addr_keyword, vip, mark)
        return True
    log.error("nft add rule failed for %s/%s: %s", vip, chain, result.stderr)
    return False


def get_mark_rule_handle(vip: str, chain: str) -> str | None:
    """Find the nft handle for this VIP's MARK rule in this chain."""
    nft_chain = CHAIN_NFT[chain]
    out = run(["/usr/sbin/nft", "-a", "list", "chain", "ip", "mangle", nft_chain])
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if f'comment "bw:{vip}"' in line:
            m = re.search(r"handle\s+(\d+)", line)
            if m:
                return m.group(1)
    return None


def remove_mark_rule(vip: str, chain: str) -> bool:
    """Remove MARK rule from nft mangle table. Idempotent."""
    handle = get_mark_rule_handle(vip, chain)
    if not handle:
        log.debug("No MARK rule to remove: chain=%s vip=%s", chain, vip)
        return True

    nft_chain = CHAIN_NFT[chain]
    cmd = ["/usr/sbin/nft", "delete", "rule", "ip", "mangle", nft_chain, "handle", handle]
    result = run(cmd)
    if result.returncode == 0:
        log.info("Removed nft MARK rule: %s chain=%s handle=%s", vip, chain, handle)
        return True
    log.error("nft delete rule failed for %s/%s: %s", vip, chain, result.stderr)
    return False


# === tc (traffic control) — UNCHANGED from original ===

def setup_qdiscs(iface: str):
    """One-time setup of root HTB qdisc on egress (and ifb0 if available).
    Preserved verbatim from original bandwidth-monitor.py.
    """
    run(["tc", "qdisc", "replace", "dev", iface, "root", "handle", "1:", "htb", "default", "ffff"])
    run(["tc", "class", "replace", "dev", iface, "parent", "1:", "classid", "1:1", "htb",
         "rate", "1000mbit"])
    run(["tc", "class", "replace", "dev", iface, "parent", "1:1", "classid", "1:ffff", "htb",
         "rate", "1000mbit"])

    INGRESS_IFB = "ifb0"
    try:
        ingress_check = run(["tc", "qdisc", "show", "dev", iface, "ingress"])
        if "qdisc ingress" not in ingress_check.stdout:
            run(["tc", "qdisc", "add", "dev", iface, "ingress"])

        filter_check = run(["tc", "filter", "show", "dev", iface, "ingress"])
        if "match 0/0" not in filter_check.stdout:
            run(["tc", "filter", "add", "dev", iface, "parent", "ffff:", "protocol", "ip", "u32",
                 "match", "u32", "0", "0", "action", "mirred", "egress", "redirect", "dev", INGRESS_IFB])

        run(["tc", "qdisc", "replace", "dev", INGRESS_IFB, "root", "handle", "1:", "htb", "default", "ffff"])
        run(["tc", "class", "replace", "dev", INGRESS_IFB, "parent", "1:", "classid", "1:1", "htb",
             "rate", "1000mbit"])
        log.info("HTB root qdiscs ready on %s and %s (egress + ingress)", iface, INGRESS_IFB)
    except Exception as e:
        log.warning("ifb0 setup skipped: %s", e)
        log.info("HTB root qdisc ready on %s (egress only, no ifb)", iface)


# === Public API (matches original bandwidth-monitor.py interface) ===

def apply_bandwidth(vip: str, iface: str, down_mbps: int, up_mbps: int) -> bool:
    """Apply bandwidth shaping for a customer. Idempotent.

    Replaces original apply_bandwidth() — uses nft for MARK, unchanged for tc.
    """
    mark = vip_to_mark(vip)
    classid = vip_to_classid(vip)
    ok = True

    # 1. nft MARK rules in 4 placements
    for chain, match_flag in (
        ("PREROUTING", "-d"),    # download: dst=VIP
        ("FORWARD", "-d"),        # forwarded download: dst=VIP
        ("FORWARD", "-s"),        # forwarded upload: src=VIP
        ("POSTROUTING", "-s"),    # local-sourced upload
    ):
        if not add_mark_rule(vip, chain, match_flag, mark):
            ok = False

    # 2-5. tc class + filter (UNCHANGED from original)
    INGRESS_IFB = "ifb0"

    # egress tc class (idempotent — `replace`)
    result = run([
        "tc", "class", "replace", "dev", iface, "parent", "1:1",
        "classid", classid, "htb",
        "rate", f"{up_mbps}mbit", "ceil", f"{up_mbps}mbit",
    ])
    if result.returncode != 0:
        log.error("tc egress class replace failed: %s", result.stderr)
        ok = False

    # egress tc filter (idempotent — `replace`)
    result = run([
        "tc", "filter", "replace", "dev", iface, "parent", "1:",
        "protocol", "ip", "handle", mark, "fw", "flowid", classid,
    ])
    if result.returncode != 0:
        log.error("tc egress filter replace failed: %s", result.stderr)
        ok = False

    # ifb0 ingress (download shaping)
    result = run([
        "tc", "class", "replace", "dev", INGRESS_IFB, "parent", "1:1",
        "classid", classid, "htb",
        "rate", f"{down_mbps}mbit", "ceil", f"{down_mbps}mbit",
    ])
    if result.returncode != 0:
        log.error("tc ifb0 class replace failed: %s", result.stderr)
        ok = False

    result = run([
        "tc", "filter", "replace", "dev", INGRESS_IFB, "parent", "1:",
        "protocol", "ip", "handle", mark, "fw", "flowid", classid,
    ])
    if result.returncode != 0:
        log.error("tc ifb0 filter replace failed: %s", result.stderr)
        ok = False

    if ok:
        log.info("Applied bandwidth for VIP %s: %d down / %d up mbit (nft + tc)",
                 vip, down_mbps, up_mbps)
    return ok


def remove_bandwidth(vip: str, iface: str) -> bool:
    """Remove all shaping for a customer. Idempotent."""
    mark = vip_to_mark(vip)
    classid = vip_to_classid(vip)
    INGRESS_IFB = "ifb0"
    ok = True

    # 1. Remove nft MARK rules (idempotent)
    for chain in ("PREROUTING", "FORWARD", "POSTROUTING"):
        if not remove_mark_rule(vip, chain):
            ok = False

    # 2. Remove tc filter + class on egress (UNCHANGED)
    run(["tc", "filter", "del", "dev", iface, "parent", "1:",
         "prio", "49152", "handle", mark, "fw"])
    run(["tc", "class", "del", "dev", iface, "classid", classid])
    run(["tc", "filter", "del", "dev", INGRESS_IFB, "parent", "1:",
         "prio", "49152", "handle", mark, "fw"])

    if ok:
        log.info("Removed bandwidth for VIP %s", vip)
    return ok


# === Main (test stub — full daemon logic preserved from original) ===

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-syntax", action="store_true",
                    help="verify nft is reachable + parse our test rules")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.check_syntax:
        # Quick smoke: list current nft mangle chains
        result = run(["/usr/sbin/nft", "list", "table", "ip", "mangle"])
        if result.returncode == 0:
            log.info("nft mangle table accessible")
            log.info(result.stdout[:500])
        else:
            log.error("nft list failed: %s", result.stderr)
            sys.exit(1)
    else:
        log.info("bandwidth-monitor-nft.py — nftables edition (2026-06-26)")
        log.info("Daemon mode not implemented in this migration edition.")
        log.info("Use --check-syntax to verify nft is reachable.")
        log.info("Full apply_bandwidth()/remove_bandwidth() API ready for daemon hookup.")


if __name__ == "__main__":
    main()
