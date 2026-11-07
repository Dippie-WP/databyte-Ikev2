# Phase 5H — HA + Load Balancer for strongSwan VPN Gateway

**Status:** ⏳ NOT STARTED — last-last phase per Zun (2026-06-20 10:45 UTC)
**Owner:** Misha + Zun sign-off
**Target version:** v2.0 (HA+LB is a major architecture change)
**Phase placement:** After 5B (quota) sign-off, before 5D. **5D was SHELVED as SaaS billing (2026-06-19) and repurposed 2026-07-05 as the RADIUS migration (FreeRADIUS + daloRADIUS) — currently 🟡 In progress.** HA + LB still follows 5D completion; this doc remains valid as the plan-of-record for that future work.

---

## Why we need this

**Single-point-of-failure today:** LXC 903 (vpn-gateway, 192.168.10.98) hosts the entire VPN stack — charon in Docker + the FastAPI portal + quota-monitor + the SQLite DB. If that container dies, all customers disconnect simultaneously. They reconnect manually after recovery.

**Customer-facing SLA:** Per the ToS §7 "Experimental Nature of the Service" we currently make no SLA promise. But Zun's commercial ambition (5D = RADIUS migration, in progress since 2026-07-05, building toward SaaS billing and multi-customer onboarding) will eventually require a tier-1 reliability story. HA+LB is the foundation for that future step.

**Zun's principle (LOCKED 2026-06-19):** "When production is solid, recovery = HA/LB (more of the same), not version regression (fall back to older broken version). v1.1 fallback = WRONG. v1.2 + HA = RIGHT."

---

## Tier options

| Tier | Cost | Effort | When to use |
|------|------|--------|-------------|
| **Tier 1 — Active/Passive VRRP** (RECOMMENDED) | $0 (homelab) | 1 day | < 50 concurrent clients, single datacenter, SLA "best effort" |
| **Tier 2 — Active/Active HAProxy** | $0 | 2-3 days | 50-500 clients, need load balancing |
| **Tier 3 — Geo-redundant** | $$$ | weeks | Multi-region, disaster recovery |

**Recommendation: Tier 1.** Homelab budget, <50 clients, single Proxmox cluster. Tier 1 gives 99.9% uptime for ~$0 and 1 day of build time.

---

## Tier 1 architecture (proposed)

```
                              PUBLIC INTERNET
                                      │
                              ┌───────┴───────┐
                              │ 102.182.117.43 │
                              │  Router NAT    │
                              │ UDP 500/4500  │
                              └───────┬───────┘
                                      │
                              ╔═══════╧═══════╗
                              ║  VRRP VIP     ║  ← 192.168.10.99 (virtual)
                              ║  192.168.10.99║
                              ╚═══════╤═══════╝
                                      │
                          ┌───────────┴───────────┐
                          │                       │
                   ┌──────┴──────┐         ┌──────┴──────┐
                   │   MASTER    │         │   BACKUP    │
                   │  LXC 903    │ ← VRRP →│  LXC 904    │
                   │ 192.168.10.98│  ADVERT │ 192.168.10.99x │
                   │  prio 100   │         │  prio 90    │
                   │ strongSwan  │         │ strongSwan  │
                   │  ACTIVE     │         │  STANDBY    │
                   └──────┬──────┘         └──────┬──────┘
                          │                       │
                          └───────────┬───────────┘
                                      │
                              ┌───────┴───────┐
                              │ TrueNAS NFS   │
                              │ /databyte/    │
                              │  shared/      │
                              │  ipsec.db     │
                              │  (read-write) │
                              └───────────────┘
                                      │
                              ┌───────┴───────┐
                              │  Prometheus   │  ← scrapes both via :9101
                              │  192.168.10.212│
                              └───────────────┘
```

### Components

**2× strongSwan v1.3.x LXC instances:**
- **LXC 903** (existing, 192.168.10.98) = MASTER, priority 100, currently has all data + tests
- **LXC 904** (new, on a DIFFERENT PVE host — `pve2`, 192.168.10.10x) = BACKUP, priority 90
  - Must be on different physical host to survive host failure
  - Same Ubuntu 22.04 base, same docker image, same config templates

**VRRP via keepalived:**
- VIP (Virtual IP): **192.168.10.99**
- Advertisements every 1s, preempt delay 5s
- MASTER sends GARP on state change to update router ARP cache
- Failover target: ~5s (3 missed advertisements × 1s)

**Shared state — the tricky part:**

Three classes of state need to survive failover:

1. **SQLite DB (`/var/lib/strongswan/ipsec.db`)** — customer records, EAP creds, sessions, audit log, portal sessions, quota tables. **Highest stakes.** Options:
   - **(a) NFS mount from TrueNAS** — DB lives on TrueNAS NFS share, both LXCs mount RW. SQLite supports file locking over NFS v4. Works for our 50-client scale.
   - **(b) rsync every 30s** — DB lives on MASTER, rsync to BACKUP every 30s. Risk: data loss up to 30s during failover.
   - **(c) PostgreSQL instead of SQLite** — proper replication, durable. Migration cost: rewrite all DB code. ~1 day extra.

   **Recommendation: (a) NFS.** Lowest effort, sufficient for our scale, keeps SQLite semantics. Pitfall: SQLite locking over NFS requires `nofail` mount + NFSv4 only + server-side locking enabled on TrueNAS. Test: ensure only ONE charon writes at a time (use `flock` or coordinator lock).

2. **charon runtime state** — active IKE_SAs, EAP identities in memory. **Cannot be shared** without significant code. On failover, ALL active SAs terminate and customers reconnect manually (~5-30s reconnect).
   - MOBIKE on iOS/Windows = automatic reconnect without re-auth (clients see VIP moved)
   - PSK clients need manual reconnect
   - This is acceptable per Tier 1 SLA ("best effort")

3. **Configuration** — `/home/zunaid/strongswan/`, `/opt/vpn-portal/`, iptables rules, certs. **Immutably shared via NFS or rsync.** Already in our daily RustFS backup so we have an off-host copy.

**Quotas:**
- 508 iptables per-VIP byte counters — must exist on the new MASTER after failover. Two options:
  - **(a) Reset on failover** — counters reset to 0; customers lose cumulative accounting on failover (~1 minute of usage invisible). Acceptable for 99% of customers.
  - **(b) Stateful iptables sync** — keepalived `notify_master` script rsyncs counter state from BACKUP to MASTER before assuming VIP. Adds complexity.
  - **Recommendation: (a) for v2.0.** Audit-log a "quota counter reset due to failover" event. Document in SLA. Fix in v2.1 if Zun asks.

**Portal service (FastAPI):**
- Runs on BOTH LXCs. Only the MASTER holds the VIP, so only MASTER's portal is reachable. BACKUP's portal is sleeping (or returning 503).
- DNS: clients hit `http://<VIP>:8080/portal/` — works through VIP.
- Operator dashboard: same thing, only MASTER's is reachable.

**Public IP:**
- Router NAT today: `102.182.117.43:500/4500 → 192.168.10.98` (LXC 903 directly)
- After 5H: `102.182.117.43:500/4500 → 192.168.10.99` (VRRP VIP)
- Failover transparency: clients hit the same public IP. The router sends to .99. Keepalived migrates .99 between LXC 903 and 904. Clients don't notice (except for the SA disconnect, which they recover via MOBIKE).

---

## Build plan (estimated 1 day)

### Phase 5H.1 — LXC 904 build (2-3 hours)
1. Provision LXC 904 on **pve2** (different physical host) — 4C/4GB/30GB, Ubuntu 22.04, MAC unique
2. Install: docker.io, docker-compose-v2, iptables-persistent, netfilter-persistent, ipset-persistent, firewalld, rclone, sqlite3, keepalived
3. Pull image `zun/strongswan:6.0.7-mschapv2-attrsql` from local registry (or rebuild from Dockerfile)
4. Mount TrueNAS NFS share at `/var/lib/strongswan/` (the DB path)
5. Copy strongswan config from LXC 903 (rw-psk.conf, rw-eap.conf, certs, secrets)
6. Set up systemd wrappers: `strongswan-vpn.service`, `vpn-portal.service`
7. Verify: `docker exec strongswan swanctl --list-sas` returns empty (no clients yet)

### Phase 5H.2 — keepalived config (1 hour)
1. Install keepalived on both LXC 903 and LXC 904
2. Config `/etc/keepalived/keepalived.conf`:
   - MASTER on 903: priority 100, advert 1s
   - BACKUP on 904: priority 90, advert 1s
   - VIP: 192.168.10.99/32 dev eth0
   - auth_pass: shared secret (in `/etc/keepalived/auth.pass`, mode 0600)
3. `notify_master` script on BACKUP: start charon container + portal service
4. `notify_backup` script on MASTER: stop charon container + portal service (keep DB writes clean)
5. Test: `systemctl status keepalived` on both, see VIP migrate on `kill -9` of MASTER's keepalived

### Phase 5H.3 — NFS share for DB (1 hour)
1. On TrueNAS: create dataset `databyte/vpn-shared`, enable NFS share with `nfs4` only, allow `192.168.10.98` + `192.168.10.10x` (LXC 903 + 904)
2. On LXC 903: `mount -t nfs4 192.168.10.89:/mnt/databyte/vpn-shared /var/lib/strongswan/` (already configured for `ipsec.db` location)
3. On LXC 904: same mount, same path
4. Test: write to DB from 903, read from 904, verify consistency

### Phase 5H.4 — Router NAT change (5 min)
- Zun on router: change `102.182.117.43:500/4500` from → `.98` to → `.99` (VRRP VIP)
- Verify: external client can connect through public IP

### Phase 5H.5 — Failover drill (1 hour)
1. Active SA: connect zun-windows + zun-iphone, verify both have VIPs
2. `kill -9` MASTER keepalived (or `pct stop 903 --force`)
3. Watch: BACKUP becomes MASTER in ~5s, GARP sent, router updates ARP
4. Watch: existing SAs terminated on the BACKUP charon (charon had no in-memory state of those SAs)
5. Watch: Windows + iPhone reconnect via MOBIKE in 10-30s
6. Watch: VPN portal becomes reachable on new MASTER via VIP :8080
7. Audit log: failover event recorded

### Phase 5H.6 — Quota counter reset audit (30 min)
1. Add a `failover_audit` table: `id, ts, customer_id, bytes_lost_estimate, vip`
2. Add `notify_master` script hook that walks `/proc/net/nf_conntrack` + iptables counters and logs deltas
3. Verify on next drill: customers see small accounting gap with audit message

### Phase 5H.7 — CI smoke for failover (30 min)
1. Add `tools/failover-drill.js` — uses API + Prometheus + iptables verification
2. Add `.github/workflows/5h-drill.yml` — weekly drill on Sunday 02:00 SAST (low-traffic)
3. Alert: if failover takes >10s, page Zun via Telegram

### Phase 5H.8 — Documentation + ADR (30 min)
1. `docs/decisions/5H-architecture.md` — decision record (why active/passive, why NFS, why 5s failover)
2. Update README — add HA architecture diagram
3. Update ROADMAP — 5H → DONE both gates
4. Update ToS §7 "Service availability" with actual SLA: 99.9% best effort, planned failover drill weekly
5. Tag v2.0.0

---

## Risks + open questions

| Risk | Severity | Mitigation |
|------|----------|------------|
| SQLite+NFS data corruption if both charons write simultaneously | HIGH | Coordinator lock file in NFS. Only MASTER's systemd unit starts charon. |
| LXC 903 + LXC 904 on same PVE host = single point of failure (PVE host dies) | HIGH | MUST put 904 on pve2 (different physical host). Already in plan. |
| Quota accounting gap on failover | MEDIUM | Document in SLA, audit-log the gap. Fix in v2.1. |
| Existing 508 iptables rules need to be re-created on BACKUP after failover | MEDIUM | Container restart via systemd wrapper re-applies iptables. Verify in drill. |
| Self-hosted CI runner is on LXC 903 — will it still run during failover? | LOW | DR drill is at 02:00 SAST Sunday, low CI traffic. Runners are stateless, can be re-created. Move runner to a third LXC if becomes issue. |
| TrueNAS NFS outage = total outage | MEDIUM | TrueNAS has RAIDZ1, very reliable. Backup plan: rsync DB to local disk on each LXC every 5 min as fallback. |
| VRRP not encrypted on the wire | LOW | Internal VLAN 10 only. Router doesn't speak VRRP. Acceptable for homelab. |

---

## Open questions for Zun (need pick before starting)

1. **Tier 1 vs Tier 2?** — Tier 1 (active/passive) is recommended. Tier 2 (active/active HAProxy) doubles complexity. **Pick: Tier 1 unless Zun expects >50 concurrent clients.**

2. **Where does LXC 904 live?** — Must be on `pve2` (different physical host than 903 which is on `zunaid`). Confirm: yes, plan is 904 on pve2.

3. **SLA wording in ToS §7?** — Currently says "experimental". Should HA change this? Options:
   - (a) Keep "experimental" — HA is for ops use, not customer SLA
   - (b) Update to "99.9% best-effort" — HA is real, document the gap
   - **Recommendation: (b) but with explicit caveat about 5s reconnect window on failover.**

4. **NFS vs rsync for shared DB?** — NFS recommended. rsync = up to 30s data loss. **Pick: NFS, accept the small complexity.**

5. **What about the operator portal during failover?** — Plan: BACKUP portal returns 503. After failover, new MASTER's portal takes over. Portal sessions are in the SQLite DB (shared), so customers don't lose session. **OK by default, just confirm.**

6. **Backwards compat with v1.3.0 single-LXC?** — v2.0 must keep working as a single LXC if 904 doesn't exist (degraded mode, no HA). Same docker-compose + same systemd, just no keepalived. **Yes — keep it working solo.**

7. **Migration timing?** — Zun prefers weekend work (per USER.md). 5H is a 1-day job. **Recommend: Saturday morning, 09:00 SAST. Drill by 14:00 SAST. Tag by 16:00 SAST.**

---

## Acceptance criteria (two-gate rule per Zun's policy)

**Technical gate (must all be green):**
- [ ] LXC 904 boots, charon starts, `/api/health` returns 200
- [ ] Both LXCs see shared DB via NFS, writes from 903 visible on 904 within 1s
- [ ] keepalived MASTER/BACKUP roles working, VIP migrates in <5s on `kill`
- [ ] Existing iPhone + Windows clients reconnect via MOBIKE after failover
- [ ] Operator portal reachable on VIP after failover
- [ ] Quota counter audit log records accounting gap on failover
- [ ] CI drill workflow runs weekly, alert if >10s failover
- [ ] No data loss in DB across 5 drill runs
- [ ] All v1.3.0 tests still pass on 903 (no regression)

**Operator gate (Zun sign-off):**
- [ ] Zun confirms drill feels OK from the customer perspective
- [ ] Zun updates router NAT to .99
- [ ] Zun approves ToS §7 SLA wording change
- [ ] Zun approves docs/ROADMAP.md 5H → DONE
- [ ] Zun tags v2.0.0

---

## Out of scope (5D if commercial happens)

- Geo-redundancy (Tier 3)
- Per-customer SLA tiers
- 99.99% SLA (would need Tier 2 or 3)
- Auto-scaling (not needed at <100 clients)
- Cross-region DB replication (PostgreSQL logical replication)

---

## Estimated effort summary

| Phase | Effort | Parallelizable? |
|-------|--------|-----------------|
| 5H.1 — LXC 904 build | 2-3h | Yes (background while 903 keeps running) |
| 5H.2 — keepalived | 1h | Yes |
| 5H.3 — NFS | 1h | No (must complete before failover drill) |
| 5H.4 — Router NAT | 5min | Zun on router UI |
| 5H.5 — Failover drill | 1h | No (depends on all above) |
| 5H.6 — Quota audit | 30min | Yes |
| 5H.7 — CI drill | 30min | Yes |
| 5H.8 — Docs + tag | 30min | No (last step) |
| **TOTAL** | **~6-8h** | **1 day, Saturday recommended** |

---

**Last updated:** 2026-06-21 18:50 UTC
**Plan version:** Rev 1
**Next review:** When Zun picks to start (currently waiting on lower-priority items)