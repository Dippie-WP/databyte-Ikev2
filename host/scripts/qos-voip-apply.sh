#!/bin/bash
# Restore VoIP QoS tc classes + filters + nft DSCP-EF marking after reboot.
# Created 2026-07-24; nft DSCP-EF re-added 2026-11-04 (the iptables half died in the
# nftables migration — see DAT-VPN-QOS plan). Idempotent.

set -u
IFACE=ens3
TC=/usr/sbin/tc
NFT=/usr/sbin/nft
VOIP_PEER=129.232.214.59
LOG=/var/log/qos-voip.log

log() { echo "[$(date +%Y-%m-%d\ %H:%M:%S)] $*" | tee -a "$LOG"; }

log "applying VoIP QoS tc classes + filters on $IFACE"

# --- nft: DSCP EF marking for the VoIP peer (both directions) ---
if $NFT list chain ip mangle forward 2>/dev/null | grep -q "voip-ef-dl"; then
  log "nft voip EF marks already present, skipping"
else
  $NFT add rule ip mangle forward ip daddr "$VOIP_PEER" ip dscp set ef comment "voip-ef-dl" || true
  $NFT add rule ip mangle forward ip saddr "$VOIP_PEER" ip dscp set ef comment "voip-ef-ul" || true
  log "nft voip EF marks added"
fi

# --- 1:90 class ---
if $TC class show dev $IFACE 2>/dev/null | grep -qE "^class htb 1:90 "; then
  log "class 1:90 already present, skipping"
else
  $TC class add dev $IFACE parent 1:1 classid 1:90 htb rate 100mbit ceil 1gbit prio 0
  log "class 1:90 added"
fi

# --- 90: leaf qdisc ---
if $TC qdisc show dev $IFACE 2>/dev/null | grep -qE "^qdisc pfifo 90:"; then
  log "qdisc 90: already present, skipping"
else
  $TC qdisc add dev $IFACE parent 1:90 handle 90: pfifo limit 10
  log "qdisc 90: added"
fi

# --- Filter 1: fwmark 0x40 ---
if $TC filter show dev $IFACE parent 1: 2>/dev/null | grep -q "handle 0x40.*classid 1:90"; then
  log "filter fwmark 0x40 already present, skipping"
else
  $TC filter add dev $IFACE parent 1: protocol ip prio 1 handle 0x40 fw classid 1:90
  log "filter fwmark 0x40 added"
fi

# --- Filter 2: DSCP EF (TOS 0xb8/0xfc) ---
if $TC filter show dev $IFACE parent 1: 2>/dev/null | grep -q "match 00b80000"; then
  log "filter u32 tos 0xb8 already present, skipping"
else
  $TC filter add dev $IFACE parent 1: protocol ip prio 2 u32 match ip tos 0xb8 0xfc classid 1:90
  log "filter u32 tos 0xb8 added"
fi

# --- strongswan conns reload ---
if docker ps --format "{{.Names}}" 2>/dev/null | grep -q "^strongswan$"; then
  docker exec strongswan swanctl --uri tcp://127.0.0.1:4502 --load-conns >> "$LOG" 2>&1 || true
  log "swanctl conns reloaded"
else
  log "strongswan container not running, skipping swanctl reload"
fi

log "done"
$TC -s class show dev $IFACE classid 1:90 >> "$LOG" 2>&1
