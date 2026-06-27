# 5D-BANDWIDTH-LIMITING — Per-User Bandwidth Caps

> **Added 2026-06-22 (Phase 5D)**: Prevents a single user from saturating the
> VPS uplink and ruining service for everyone else. Flat 20/20 Mbps for all
> users. Per-user configurable via the `customers` table.
>
> **Updated 2026-06-27 (v1.8.0)**: Migrated from iptables-legacy to nftables,
> replaced ifb0 ingress redirect with egress-only shaping, fixed download cap
> FORWARD-mangle placement, added dynamic OUTPUT mark rule for locally-
> generated replies (iperf3/VPS apps).

## Why this exists

Without per-user bandwidth limits, one heavy user (torrents, speed test, large
downloads) can saturate the VPS uplink. The downstream effect: every other
paying customer sees a degraded VPN. Real production-grade VPNs solve this
with per-user QoS.

We solve it with Linux `tc` (traffic control) + nft mangle marks, one HTB class
per active user. Both upload AND download shaping happen on the egress
interface — the mark survives through xfrm encrypt to POSTROUTING.

## How it works

```
┌────────────────────────────────────────────────────────────────┐
│                    strongSwan container                        │
│  (IKEv2 + EAP-MSCHAPv2)                                       │
└──────────────────────┬─────────────────────────────────────────┘
                       │ UPLOAD: 10.99.0.50 (zun) → 8.8.8.8
                       │ DOWNLOAD: 8.8.8.8 → 10.99.0.50 (after xfrm decrypt)
                       ▼
┌────────────────────────────────────────────────────────────────┐
│ Host nft mangle (Phase 5/5D, nftables edition):               │
│                                                                │
│   UPLOAD mark (packet going OUT to internet):                  │
│     mangle FORWARD      ip saddr VIP meta mark set 0x01        │
│     mangle POSTROUTING  ip saddr VIP meta mark set 0x01        │
│                                                                │
│   DOWNLOAD mark (packet going IN from internet):               │
│     mangle FORWARD      ip daddr VIP meta mark set 0x81        │
│       (decrypted inner packet going to VIP)                    │
│     mangle OUTPUT       ip daddr REMOTE_IP meta mark set 0x81  │
│       (locally-generated replies: iperf3 server, VPS apps)     │
└──────────────────────┬─────────────────────────────────────────┘
                       │ marked packets
                       ▼
┌────────────────────────────────────────────────────────────────┐
│ Host tc (HTB) on ens3 (egress — sole shaping point):           │
│   class 1:2  parent 1:1 htb rate 20mbit ceil 20mbit  (upload) │
│   class 1:81 parent 1:1 htb rate 40mbit ceil 40mbit  (download)│
│   filter parent 1: handle 0x01 fw flowid 1:2                   │
│   filter parent 1: handle 0x81 fw flowid 1:81                  │
│                                                                │
│ Note: ifb0 is set up but UNUSED. The OLD ifb0 ingress redirect │
│ was stealing all incoming tunnel traffic (Bug D). Egress tc is │
│ the single shaping point for both directions.                  │
└────────────────────────────────────────────────────────────────┘
                       │
                       ▼
                 Public internet
```

## Components

| File | Purpose |
|---|---|
| `quota/bandwidth-monitor.py` | Daemon (Python, systemd) — reads swanctl + DB, applies/removes tc + nft rules |
| `quota/bandwidth-monitor-nft.py` | Helper module — nft mangle add/remove/lookup primitives |
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
| **Egress** (VPS → internet = user's upload) | `tc` on egress iface, mark 0x01, class 1:2 | ✅ Works | ✅ Works |
| **Ingress** (internet → VPS = user's download) | `tc` on egress iface, mark 0x81, class 1:130+ (egress-only, post-xfrm) | ✅ Works (lab has ifb too) | ✅ Works (Xneelo = full VM) |
| **Locally-generated replies** (iperf3/VPS apps) | OUTPUT mangle `ip daddr REMOTE_IP mark=0x81` | ✅ Works | ✅ Works |

The OLD design used ifb0 (intermediate functional block) to redirect ingress
traffic into a virtual interface where tc could shape it. This was abandoned
in v1.8.0 (Bug D — see "What we learned" below) in favor of egress-only
shaping. Mark is set in PREROUTING mangle before xfrm encrypts the
re-encrypted inner packet.

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
sudo tc class show dev ens3
# expected: classes 1:1 (root), 1:ffff (default no-shape), 1:2 (upload), 1:81 (download)

# 3. nft mangle rules (only when user is connected)
sudo nft -a list chain ip mangle forward
# expected: 'ip saddr VIP meta mark set 0x01' (upload) + 'ip daddr VIP meta mark set 0x81' (download)
sudo nft -a list chain ip mangle postrouting
# expected: 'ip saddr VIP meta mark set 0x01'
sudo nft -a list chain ip mangle output
# expected: 'ip daddr REMOTE_IP meta mark set 0x81 comment "bw-dl-output:REMOTE_IP"'

# 4. Class counters should show non-zero overlimits (cap is firing)
sudo tc -s class show dev ens3 classid 1:2    # upload
sudo tc -s class show dev ens3 classid 1:81   # download

# 5. Real-world test (from a connected client):
#    - Run a speed test from your phone while on VPN → should cap at your down_mbps
#    - Run iperf3 download to VPS:5201 → should ALSO cap (proves OUTPUT rule)
```

## Tunable knobs (top-of-file in `bandwidth-monitor.py`)

| Constant | Default | What it does |
|---|---|---|
| `POLL_INTERVAL` | 60s | How often to refresh per-user rules |
| `EGRESS_IFACE_DEFAULT` | `eth0` | Public interface to shape (Xneelo VPS uses `ens3`) |
| `INGRESS_IFB` | `ifb0` | (UNUSED in v1.8.0) Virtual interface, setup kept but redirect removed |
| `VIP_PREFIX` | `10.99.0.` | VIP range that gets shaped |

## Known limitations

1. **LXC containers can't load ifb** into the host kernel. On LXC 903, only egress shaping works. The Xneelo VPS has full kernel access and shapes both directions. **(Note: since v1.8.0 ifb is unused anyway, this limitation is moot.)**
2. **No bursting**: HTB `rate` is a hard cap, not a burst-then-shape model. If you want "burst to 50 Mbps for 5 seconds, then 20 Mbps", we need to use HTB's `ceil` parameter or switch to TBF.
3. **Per-user, not per-class-of-service**: A user on Tier 1 ($3) and a user on Tier 3 ($8) get the same speed. Differentiation is via the data cap, not bandwidth. If you want tier-differentiated speeds, change the DB columns per customer.
4. **No QoS for non-VPN traffic**: We don't shape the VPS's own traffic (apt updates, monitoring, etc.). The `1:ffff` default class gives everything else 1 Gbit.

## What we learned (5 bugs found in 2026-06-26/27)

### Bug A — PREROUTING `ip daddr VIP` never matches

At PREROUTING mangle time, conntrack NAT un-NATs the wire dst AFTER mangle
runs (mangle priority -150, nat priority -100). Wire dst is still the VPS
public IP 154.65.110.44, never the VIP 10.99.0.1. Verified with a counter
rule — packet counter stayed at 0 across full speedtest runs.

**Fix:** Use FORWARD mangle `ip daddr VIP` instead — matches the DECRYPTED
inner packet going to the VIP. Mark survives through xfrm re-encrypt to
POSTROUTING → ens3 egress → tc class.

### Bug B — PREROUTING `ct reply daddr VIP` never matches

VIP is the conntrack ORIGINAL src, not the REPLY dst. For an upload packet
(saddr=VIP → internet), the conntrack REPLY tuple has dst=internet, not VIP.
Counter verified = 0.

### Bug C — PREROUTING `ip saddr VIP` matches upload but mark lost on xfrm decrypt

Matches outgoing upload packets (saddr=VIP), but xfrm INPUT decrypts and
creates a NEW SKB that loses the mark. Mark 0x81 counter at POSTROUTING = 0 —
the mark does not survive the decrypt → re-encrypt cycle.

### Bug D — ifb0 ingress redirect was stealing ALL incoming tunnel traffic

The OLD `setup_qdiscs()` added a catch-all u32 filter
(`match 0/0 → action mirred egress redirect dev ifb0`) on ens3's ingress
qdisc. This redirected ALL incoming IPsec-decrypted packets to ifb0 BEFORE
ens3's egress tc could see them. Even with a correct mark rule, traffic
never reached the egress tc.

**Diagnostic technique:** tcpdump on both ens3 and ifb0:
`ens3 In IP 10.99.0.1 > ...` then `ifb0 Out IP 10.99.0.1 > ...` —
redirect was stealing traffic.

**Fix:** Removed ifb0 redirect from `setup_qdiscs()` (`if INGRESS_SHAPING
and False:` — always skipped). ifb0 qdisc/HTB structure still set up but
unused — leaves room for future ifb-based shaping without re-doing setup.

### Bug E — hex comparison bug in `download_mark_rule_present`

Compared string `f"meta mark set 0x81"` against nft's output
`meta mark set 0x00000081` (nft zero-pads to 8 hex digits). Present check
always returned False → daemon added a DUPLICATE rule every 60s poll.

**Fix:** Added `_mark_in_nft_output(stdout, mark)` helper using regex
`0x0*` prefix match + decimal form fallback.

### Bug F — OUTPUT mangle path bypasses FORWARD rule (v1.7.1)

After the v1.4.0 fixes, speedtest download was capped correctly but iperf3
download was still uncapped. Root cause: iperf3 server is a local process
on VPS. Its TCP replies have src=154.65.110.44:5201, dst=Zun's home IP,
and go through OUTPUT mangle, not FORWARD. The FORWARD rule catches
internet → VPS → tunnel traffic but misses locally-generated replies to
tunnel clients.

**Fix (commit a8caa3c):** Daemon queries `swanctl --list-sas` for each
active SA's remote IP and adds parallel mangle OUTPUT rule:
`ip daddr REMOTE_IP meta mark set 0x81 comment "bw-dl-output:REMOTE_IP"`.

**IP rotation safety:** `run_iteration()` scans OUTPUT chain for
`bw-dl-output:*` rules whose remote_ip is no longer in active SA set →
removes them. New IP gets a fresh rule on next iteration.

## Why FORWARD works for download (the only working placement)

The key insight is the **packet path through xfrm**:

```
1. iPhone → ESP encrypted packet → arrives at VPS ens3 with dst=154.65.110.44
2. PREROUTING (raw wire dst=154.65.110.44) — no VIP here, mangle rules don't match VIP
3. INPUT → xfrm decrypt → NEW SKB created with inner src/dst (VIP, internet)
4. FORWARD (decrypted inner packet) — daddr=VIP, this is where mangle rule fires
5. POSTROUTING → xfrm re-encrypt (mark survives, NEW outer SKB)
6. ens3 egress → tc filter on mark → classified into class 1:81 (download cap)
```

**Why PREROUTING never matches:** The decrypted inner packet doesn't exist
at PREROUTING time. xfrm hasn't consumed the outer packet yet. The
outer ESP packet has dst=VPS public IP, not VIP.

**Why FORWARD matches:** The decrypted inner packet IS routed FORWARD
through the box (with TS=0.0.0.0/0 strongSwan installs xfrm policies for
both `dir in` and `dir fwd`). At that point `ip daddr` is the VIP.

**Why mark survives to egress tc:** xfrm output creates a new SKB for the
re-encrypted packet, but the mark is inherited from the decrypted SKB
via `skb->mark` → preserved through `xfrm_output_one()` → preserved to
POSTROUTING → tc filter at ens3 sees the mark.

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

**Last updated:** 2026-06-27 (Misha) — v1.8.0 dl-cap fix + lessons #211-#219
from MEMORY.md. Previous: 2026-06-22 (initial design + LXC 903 deployment).