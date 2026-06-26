# 5D-BANDWIDTH-LIMITING — Per-User Bandwidth Caps

> **Added 2026-06-22 (Phase 5D)**: Prevents a single user from saturating the
> VPS uplink and ruining service for everyone else. Flat 20/20 Mbps for all
> users. Per-user configurable via the `customers` table.

## Why this exists

Without per-user bandwidth limits, one heavy user (torrents, speed test, large
downloads) can saturate the VPS uplink. The downstream effect: every other
paying customer sees a degraded VPN. Real production-grade VPNs solve this
with per-user QoS.

We solve it with Linux `tc` (traffic control) + `iptables` mangle marks, one
HTB class per active user.

## How it works

```
┌────────────────────────────────────────────────────────────────┐
│                    strongSwan container                        │
│  (IKEv2 + EAP-MSCHAPv2)                                       │
└──────────────────────┬─────────────────────────────────────────┘
                       │ 10.99.0.50 (zun) → 8.8.8.8
                       ▼
┌────────────────────────────────────────────────────────────────┐
│ Host iptables mangle:                                          │
│  PREROUTING -d 10.99.0.50/32 -j MARK --set-mark 0x32          │
│  POSTROUTING -s 10.99.0.50/32 -j MARK --set-mark 0x32         │
└──────────────────────┬─────────────────────────────────────────┘
                       │ marked packets
                       ▼
┌────────────────────────────────────────────────────────────────┐
│ Host tc (HTB) on eth0 (egress):                                │
│  class 1:50 parent 1:1 htb rate 20mbit ceil 20mbit           │
│  filter parent 1: handle 0x32 fw flowid 1:50                  │
│                                                                │
│ Host tc (HTB) on ifb0 (ingress via ifb mirror):               │
│  class 1:50 parent 1:1 htb rate 20mbit ceil 20mbit           │
│  filter parent 1: handle 0x32 fw flowid 1:50                  │
└────────────────────────────────────────────────────────────────┘
                       │
                       ▼
                 Public internet
```

## Components

| File | Purpose |
|---|---|
| `quota/bandwidth-monitor.py` | Daemon (Python, systemd) — reads swanctl, applies/removes tc + iptables rules |
| `quota/bandwidth-monitor.service` | systemd unit file |
| `quota/quota_schema.sql` | DB schema with `bandwidth_down_mbps` + `bandwidth_up_mbps` columns |

## DB schema (Phase 5D addition)

```sql
ALTER TABLE customers ADD COLUMN bandwidth_down_mbps INTEGER NOT NULL DEFAULT 20;
ALTER TABLE customers ADD COLUMN bandwidth_up_mbps   INTEGER NOT NULL DEFAULT 20;
```

**Defaults: 20/20 Mbps for everyone.** To change per-user:

```bash
sudo sqlite3 /var/lib/strongswan/ipsec.db \
  "UPDATE customers SET bandwidth_down_mbps=50, bandwidth_up_mbps=20 WHERE name='vip-customer';"
```

The `bandwidth-monitor` daemon picks up the new values within 60s on the next
SAs refresh (or immediately on next connection).

## What gets shaped

| Direction | Mechanism | Status on LXC 903 | Status on Xneelo VPS |
|---|---|---|---|
| **Egress** (VPS → internet = user's upload) | `tc` on `eth0` | ✅ Works | ✅ Works |
| **Ingress** (internet → VPS = user's download) | `tc` on `ifb0` (mirrors ingress) | ❌ ifb module not in LXC host kernel | ✅ Works (Xneelo = full VM) |

The `bandwidth-monitor` script auto-detects ifb0 availability and enables
ingress shaping only when available. LXC 903 lab = egress-only shaping.
Xneelo VPS = both directions.

## Service management

```bash
# Status
sudo systemctl status bandwidth-monitor

# Logs
sudo journalctl -u bandwidth-monitor -f

# Manual one-shot run (for testing)
sudo python3 /home/zunaid/strongswan/quota/bandwidth-monitor.py --once --verbose
```

## Verify it's working

```bash
# 1. Service is active
sudo systemctl is-active bandwidth-monitor
# expected: active

# 2. tc root class is set up
sudo tc class show dev eth0
# expected: classes 1:1 (root), 1:ffff (default no-shape), and one 1:XX per active user

# 3. iptables mangle rules (only when user is connected)
sudo iptables-legacy -t mangle -L PREROUTING -n -v
sudo iptables-legacy -t mangle -L POSTROUTING -n -v
# expected: MARK rules with comment "bw:VIP"

# 4. Real-world test (from a connected client):
#    - Run a speed test from your phone while on VPN
#    - Should cap at ~20 Mbps (give or take 10% for overhead)
```

## Tunable knobs (top-of-file in `bandwidth-monitor.py`)

| Constant | Default | What it does |
|---|---|---|
| `POLL_INTERVAL` | 60s | How often to refresh per-user rules |
| `EGRESS_IFACE_DEFAULT` | `eth0` | Public interface to shape |
| `INGRESS_IFB` | `ifb0` | Virtual interface for ingress shaping |
| `VIP_PREFIX` | `10.99.0.` | VIP range that gets shaped |

## Known limitations

1. **LXC containers can't load ifb** into the host kernel. On LXC 903, only egress shaping works. The Xneelo VPS has full kernel access and shapes both directions.
2. **No bursting**: HTB `rate` is a hard cap, not a burst-then-shape model. If you want "burst to 50 Mbps for 5 seconds, then 20 Mbps", we need to use HTB's `ceil` parameter or switch to TBF.
3. **Per-user, not per-class-of-service**: A user on Tier 1 ($3) and a user on Tier 3 ($8) get the same speed. Differentiation is via the data cap, not bandwidth. If you want tier-differentiated speeds, change the DB columns per customer.
4. **No QoS for non-VPN traffic**: We don't shape the VPS's own traffic (apt updates, monitoring, etc.). The `1:ffff` default class gives everything else 1 Gbit.

## Migration path (already applied to LXC 903 live DB, 2026-06-22)

```bash
# Add the columns (idempotent — fails silently if already present)
sudo sqlite3 /var/lib/strongswan/ipsec.db \
  "ALTER TABLE customers ADD COLUMN bandwidth_down_mbps INTEGER NOT NULL DEFAULT 20;
   ALTER TABLE customers ADD COLUMN bandwidth_up_mbps   INTEGER NOT NULL DEFAULT 20;"
```

For **fresh Xneelo deploys**, the columns are baked into `quota_schema.sql`,
so `quota/apply_quota_schema.sh` includes them automatically.

## Audit log entry

```sql
SELECT * FROM audit_log WHERE action='bandwidth_policy_added' ORDER BY id DESC LIMIT 1;
```

---

**Last updated:** 2026-06-22 (Misha) — Initial design + LXC 903 deployment.
