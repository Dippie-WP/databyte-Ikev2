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

## Lessons learned (bandwidth-monitor + Windows client validation)

Hard-won lessons from LXC 903 + Xneelo VPS deployment on 2026-06-22. Keep these
in mind when extending or debugging the monitor.

### #41 — `iptables-legacy restore` wipes byte counters

Symptom: Zun pushed 140 MB through the VPN; iOS app showed 140 MB used; daemon
DB only saw 22 MB. Discrepancy grew every minute until the user was cut for
quota overflow.

Root cause: `strongswan-iptables-watchdog.sh` re-applied `iptables-restore`
on every Docker container event — including `exec_create`/`exec_start`/
`health_status*` — which fired on every Prometheus scrape (30s) and every
quota-monitor poll (60s). `iptables-restore` re-creates rules from scratch
and **does not preserve accumulated byte counters**, so all 508 per-VIP
counters reset to zero. Quota was charged on the delta (now-near-zero), so
only a fraction of actual traffic was counted.

Fix: narrow the watchdog case statement to `start|restart|unpause|die|stop|kill|oom`
only. Verified: three `docker exec swanctl` calls in a row leave the counter
alone (19292 → 19472 bytes naturally, not wiped to 0).

Lesson: **iptables-legacy `restore` does NOT preserve byte counters.** Any
production iptables-counter-based accounting must ensure `iptables-restore`
is called only when truly needed. `nftables` named counters don't have this
problem — migration is on the v1.3 backlog.

### #42 — `tc filter del` requires `prio` when `handle` is set

Symptom: bandwidth-monitor teardown failed silently when a user disconnected;
stale `tc` classes lingered, blocking the next `tc filter add`.

Root cause: `tc filter del dev eth0 parent 1: handle 0x32 fw` returned `EINVAL`
because the kernel matches on `(prio, handle)` — handle alone is insufficient.

Fix: also pass the original `prio` when deleting:
```bash
tc filter del dev eth0 parent 1: prio 1 handle 0x32 fw
```

Lesson: **`tc filter` operations must round-trip the same key the filter was
created with.** Always capture prio + handle + protocol on add and pass them
back on del.

### #43 — Windows native IKEv2 with self-signed CA hangs silently

Symptom: Windows IKEv2 client connected (charon showed `ESTABLISHED`) but no
traffic flowed; iperf3 hung at 0 bytes; `swanctl --list-sas` showed four
half-open SAs retrying before giving up.

Root cause: Windows IKEv2 client silently rejects cert chains it can't
validate against the local Trusted Root CAs store. Charon kept negotiating
with self-signed certs not in the Windows trust store, never reaching AUTH.

Fix: `Import-Certificate -FilePath strongswan-ca.crt.pem -CertStoreLocation Cert:\LocalMachine\Root`
on the Windows client, then reconnect.

Lesson: **Windows native IKEv2 will hang rather than warn** if the CA cert
is not in Trusted Root CAs. Bundled CA cert in `scripts/strongswan-ca.crt.pem`
must be installed before the first connect. v1.3 backlog includes Let's
Encrypt DNS-01 cert to eliminate this step entirely.

### #44 — Bake credentials into the client script

Signal: Zun said *"X script with username/password inside it"* — meaning
package everything into one self-contained file the user can run.

Applied in `scripts/connect-databyte-vpn.ps1`: creds, profile XML, CA cert,
and crypto settings are all baked into a single .ps1. Re-running is
idempotent — recreates the connection and reconnects.

Lesson: **Distribute scripts as self-contained files** when the operator IS
the customer. No prompts, no parameter passing, no separate cert downloads.
Reduce "how do I run this" to "open PowerShell as Admin, paste this".

### #45 — Parse EAP identity, not IKE identity, for Windows NAT clients

Symptom: bandwidth-monitor's SA parser failed to extract the VIP for Windows
clients behind NAT.

Root cause: Windows IKEv2 behind NAT sends:
```
remote '<private IP>' @ <public IP>[port] EAP: '<user>' [<VIP>]
```
The IKE identity is the private IP, the EAP identity is the user. Original
regex matched the first bracket pair and got the private IP.

Fix: anchor on the EAP: prefix discriminator and parse the trailing bracket
as the VIP:
```python
# Anchor on EAP: prefix + last bracket
match = re.search(r"EAP:\s*'[^']+'\s*\[([\d.]+)\]", line)
```

Lesson: **For multi-bracket swanctl lines, anchor on a discriminator
(EAP:) + last bracket**, not adjacency.

### #46 — `tc` regex anchor + discriminator (related)

Lesson: same family as #45. Use `EAP: '` as anchor, then look for the last
`]` on the line — never assume field ordering is stable.

### #47 — Windows IKEv2 split tunneling is on by default

Symptom: VPN connected, but `https://ifconfig.me` still showed the client's
ISP IP, not the VPS public IP. Quota was charged (FORWARD chain) but the
user's traffic wasn't actually going through the tunnel.

Root cause: `Add-VpnConnection` defaults `-SplitTunneling` to `$true`, so only
traffic destined for the VPN subnet goes through the tunnel — everything
else goes direct.

Fix: omit `-SplitTunneling` (or explicitly set `$false`), OR pre-configure
via profile XML:
```xml
<RoutingPolicyType>ForceTunnel</RoutingPolicyType>
```

Lesson: **Windows IKEv2 split tunneling is on by default.** Cap testing
from a Windows client without ForceTunnel is meaningless — traffic bypasses
the VPS entirely.

### #48 — PowerShell 5.1 requires `catch` on its own line

Symptom: `connect-databyte-vpn.ps1` parse errored with "Missing closing '}'"
in a try/catch block.

Root cause: PowerShell 5.1 doesn't allow `} catch {` on the same line after
the closing brace. Requires newline between `}` and `catch`.

Fix:
```powershell
try {
  ...
}
catch {
  ...
}
```

Lesson: **PowerShell 5.1 parser is line-sensitive for control flow keywords.**
Test on a vanilla Windows 10/11 (which ships 5.1), not on Windows 11 with
PowerShell 7.

### #49 — Cap only engages on FORWARD chain, not INPUT

Symptom: iperf3 from VPS to itself (`iperf3 -c 127.0.0.1`) ran at full
unshaped speed. Concluded that bandwidth shaping was broken.

Root cause: cap is enforced via iptables FORWARD chain (VIP → egress) +
tc on `eth0`/`ifb0`. The INPUT chain (VPS-local traffic) doesn't traverse
either.

Fix: validate via an **internet-bound test** from a connected client:
```powershell
# On Windows client connected to VPN
iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30
# Expected: ~17 Mbps (cap minus XFRM overhead)
```

Lesson: **IKEv2 cap only engages on FORWARD chain** (VPS-to-internet), not
INPUT (VPS-local). Always test via a public iperf3 target, never VPS-local.

### #50 — Xneelo edge firewall blocks all inbound TCP except 22

Symptom: bandwidth-monitor dashboard unreachable from the public internet on
port 9102 (quota-exporter), 8080 (portal), 3000 (Grafana). UDP 500/4500
worked fine — IKEv2 came up — so the tunnel itself was healthy.

Root cause: Xneelo's edge firewall default Security Group accepts only
TCP 22 (SSH) inbound. All other TCP ports are silently dropped. UDP 500/4500
are open by default (needed for IKEv2).

Fix: Xneelo control panel → Firewall → add Security Group rules for the
ports you need open.

Lesson: **UDP 500/4500 open ≠ all ports open.** Xneelo firewall is per-port;
every public-facing service needs its own rule.

---

**Last updated:** 2026-06-22 (Misha) — Lessons #41-#50 added; cap mechanism + Windows client validated end-to-end via Angola iperf3 (17.0 Mbps through 20 Mbit cap).
