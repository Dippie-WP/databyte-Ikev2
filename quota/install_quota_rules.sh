#!/usr/bin/env bash
# install_quota_rules.sh — Install per-VIP iptables byte counters for the
# strongSwan quota engine (Phase 5B.2).
#
# What this does:
#   1. Adds 254 outbound (-s) + 254 inbound (-d) per-VIP ACCEPT rules to
#      the FORWARD chain, each with a comment "quota:VIP" for parsing.
#   2. Removes the old /24 ACCEPT rules (redundant — per-VIP rules cover
#      every valid VIP in the rw-pool).
#   3. Saves the resulting state to /etc/iptables/rules.v4 so the
#      strongswan-iptables-watchdog re-applies it on every container
#      start/restart event (counters persist across container lifecycle).
#
# VIP range: 10.99.0.1 - 10.99.0.254 (254 VIPs, the rw-pool range).
# 254 + 254 = 508 new rules in FORWARD. Each has its own packet/byte counter.
#
# Idempotent: uses `iptables-legacy -C` (check) before `-I` (insert).
#   Running twice = same result.
#
# quota-monitor.py (5B.3) reads these counters via:
#   iptables-legacy -L FORWARD -nvx | grep 'quota:'
# and parses the VIP from the rule's -s or -d + the comment.
#
# Usage:
#   sudo bash quota/install_quota_rules.sh
#   sudo bash quota/install_quota_rules.sh --check   # dry-run, no changes
#
# Exit codes:
#   0  rules applied (or already applied)
#   1  not root
#   2  DOCKER-USER not found in FORWARD (chain state unexpected)
#   3  iptables-save failed
#   4  post-install verification failed (expected count != actual)

set -euo pipefail

RULES_FILE="${RULES_FILE:-/etc/iptables/rules.v4}"
VIP_NET="10.99.0.0/24"
VIP_PREFIX="10.99.0"
VIP_FIRST=1
VIP_LAST=254
EXPECTED_PER_VIP_TOTAL=$(( (VIP_LAST - VIP_FIRST + 1) * 2 ))  # 508

# --- preflight ---
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be root (uses iptables-legacy + writes to /etc/iptables)" >&2
    exit 1
fi

if ! command -v iptables-legacy >/dev/null 2>&1; then
    echo "ERROR: iptables-legacy not installed" >&2
    exit 1
fi

# Find DOCKER-USER position in FORWARD chain (our insertion anchor)
DOCKER_USER_POS=$(iptables-legacy -L FORWARD --line-numbers -n 2>/dev/null | awk '/DOCKER-USER/ {print $1; exit}')
if [ -z "$DOCKER_USER_POS" ]; then
    echo "ERROR: DOCKER-USER not found in FORWARD chain" >&2
    echo "       This chain state is unexpected — investigate before running." >&2
    exit 2
fi

echo "=== Install per-VIP quota iptables rules ==="
echo "  Anchor:      DOCKER-USER at FORWARD position $DOCKER_USER_POS"
echo "  VIP range:   $VIP_PREFIX.$VIP_FIRST - $VIP_PREFIX.$VIP_LAST"
echo "  Expected:    $EXPECTED_PER_VIP_TOTAL per-VIP rules (254 out + 254 in)"
echo "  Rules file:  $RULES_FILE"

# --- dry-run mode ---
if [ "${1:-}" = "--check" ]; then
    CURRENT=$(iptables-legacy -L FORWARD -nvx | grep -c "quota:" || true)
    echo "  Current:     $CURRENT per-VIP rules in FORWARD"
    if [ "$CURRENT" -eq "$EXPECTED_PER_VIP_TOTAL" ]; then
        echo "  Status:      already in desired state"
    else
        echo "  Status:      needs install (current=$CURRENT, expected=$EXPECTED_PER_VIP_TOTAL)"
    fi
    exit 0
fi

# --- backup current rules.v4 ---
BACKUP="${RULES_FILE}.bak-5B2-$(date -u +%Y%m%d-%H%M%S)"
cp -a "$RULES_FILE" "$BACKUP"
echo "  Backup:      $BACKUP"

# --- add per-VIP rules (idempotent) ---
INSERT_POS=$((DOCKER_USER_POS + 1))
ADDED=0
SKIPPED=0

for ((i=VIP_FIRST; i<=VIP_LAST; i++)); do
    VIP="${VIP_PREFIX}.${i}"

    # Outbound (-s): packets FROM the VIP going out to the internet
    if iptables-legacy -C FORWARD -s "$VIP" -j ACCEPT -m comment --comment "quota:${VIP}" 2>/dev/null; then
        SKIPPED=$((SKIPPED+1))
    else
        iptables-legacy -I FORWARD "$INSERT_POS" -s "$VIP" -j ACCEPT \
            -m comment --comment "quota:${VIP}"
        INSERT_POS=$((INSERT_POS+1))
        ADDED=$((ADDED+1))
    fi

    # Inbound (-d): packets FROM the internet coming back TO the VIP
    if iptables-legacy -C FORWARD -d "$VIP" -j ACCEPT -m comment --comment "quota:${VIP}" 2>/dev/null; then
        SKIPPED=$((SKIPPED+1))
    else
        iptables-legacy -I FORWARD "$INSERT_POS" -d "$VIP" -j ACCEPT \
            -m comment --comment "quota:${VIP}"
        INSERT_POS=$((INSERT_POS+1))
        ADDED=$((ADDED+1))
    fi
done

# --- remove old /24 rules (now redundant) ---
REMOVED_24=0
if iptables-legacy -C FORWARD -s "$VIP_NET" -j ACCEPT 2>/dev/null; then
    iptables-legacy -D FORWARD -s "$VIP_NET" -j ACCEPT
    REMOVED_24=$((REMOVED_24+1))
    echo "  Removed:     old /24 outbound ACCEPT rule"
fi
if iptables-legacy -C FORWARD -d "$VIP_NET" -j ACCEPT 2>/dev/null; then
    iptables-legacy -D FORWARD -d "$VIP_NET" -j ACCEPT
    REMOVED_24=$((REMOVED_24+1))
    echo "  Removed:     old /24 inbound ACCEPT rule"
fi

# --- save state so the watchdog re-applies on next container event ---
if ! iptables-save > "$RULES_FILE"; then
    echo "ERROR: iptables-save failed; $RULES_FILE may be partially written" >&2
    exit 3
fi

# --- verify ---
ACTUAL=$(iptables-legacy -L FORWARD -nvx | grep -c "quota:" || true)
echo
echo "=== Result ==="
echo "  Added:       $ADDED per-VIP rules"
echo "  Skipped:     $SKIPPED (already present, idempotent)"
echo "  Removed /24: $REMOVED_24 old rules"
echo "  In FORWARD:  $ACTUAL per-VIP rules (expected $EXPECTED_PER_VIP_TOTAL)"

if [ "$ACTUAL" -ne "$EXPECTED_PER_VIP_TOTAL" ]; then
    echo "FAIL: post-install verification failed" >&2
    echo "      expected $EXPECTED_PER_VIP_TOTAL, got $ACTUAL" >&2
    exit 4
fi

echo "  Status:      OK"
echo
echo "  Verify with:"
echo "    iptables-legacy -L FORWARD -nvx | grep -c quota:           # 508"
echo "    iptables-legacy -L FORWARD -nvx | grep 'quota:10.99.0.50'  # specific VIP"
echo "    iptables-legacy -Z FORWARD                                  # zero all counters (CAUTION: loses data)"
