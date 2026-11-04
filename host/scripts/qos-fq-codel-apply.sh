#!/bin/bash
# qos-fq-codel-apply.sh — ensure each customer HTB class has an fq_codel leaf (latency/AQM).
# DAT-VPN-QOS (2026-11). Idempotent: only adds the leaf when it is missing.
# Safe to run on a timer; never flushes an existing fq_codel leaf.
set -u
IFACE="${1:-ens3}"
TC="${TC:-/usr/sbin/tc}"
LOG="${LOG:-/var/log/qos-fq-codel.log}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

apply_leaves() {
  local dev="$1"
  [ -d "/sys/class/net/$dev" ] || return 0
  # classid is field 3 of "class htb <classid> parent ... rate ..."
  $TC class show dev "$dev" 2>/dev/null | awk '/^class htb/ {print $3}' | sort -u | while read -r cid; do
    case "$cid" in
      1:1|1:ffff|"") continue ;;
    esac
    local rate mb limit
    rate="$($TC class show dev "$dev" classid "$cid" 2>/dev/null | grep -oE 'rate [0-9]+[KMG]?bit' | head -1 | awk '{print $2}')"
    mb="${rate%Mbit}"
    if [ "$rate" = "$mb" ] || [ -z "$mb" ]; then
      limit=1000
    elif [ "$mb" -ge 100 ]; then
      limit=10000
    elif [ "$mb" -ge 20 ]; then
      limit=1000
    else
      limit=600
    fi
    if $TC qdisc show dev "$dev" 2>/dev/null | grep -qE "parent ${cid} +.*fq_codel"; then
      continue
    fi
    if $TC qdisc replace dev "$dev" parent "$cid" fq_codel limit "$limit" quantum 300 target 5ms interval 100ms ecn; then
      log "added fq_codel on $dev parent $cid (limit $limit)"
    else
      log "FAILED fq_codel on $dev parent $cid"
    fi
  done
}

apply_leaves "$IFACE"
apply_leaves ifb0
log "done"
