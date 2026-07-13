# Phase 5B — Architecture decision record (2026-06-19)

The quota layer. Documents all major design choices and the lessons that
came out of the 3 end-to-end test runs.

## TL;DR

We measure data usage with iptables-legacy per-VIP byte counters in the
FORWARD chain. Every 60 seconds, a Python daemon (`quota-monitor.py`)
reads those counters, looks up the customer in the DB, and increments
their `data_used_bytes`. At 80% it logs a warning. At 100% it
terminates the IKE_SA, kills the customer's EAP secret in
`rw-eap.conf` (replace with `KILLED-<random>`), reloads charon, and
marks the customer `over_quota=1`. Re-auth is blocked because the
secret is now dead.

## What was decided

### Decision 1: iptables-legacy counters, not nftables

**Why iptables-legacy:**
- The LXC host already has netfilter-persistent + iptables-legacy as the
  firewalld backend. Adding nftables-native as a second stack would
  duplicate complexity.
- The per-VIP ACCEPT rule pattern is straightforward in iptables-legacy.
- We have 254 outbound + 254 inbound rules (508 total) — well within
  iptables' scalability limits.

**Why NOT nftables (deferred to v1.3):**
- nftables named counters persist across `nft flush ruleset` reloads.
  iptables-legacy `restore` does NOT preserve byte counters (5B.6 bug).
- A migration would be ~2-3h of work and prevent future 5B.6-style bugs.
- Noted in v1.3 backlog.

**Tradeoff accepted:** any future re-apply of `rules.v4` (by the
watchdog, by an admin, by an external system) will reset all counters.
The 5B.6 fix narrowed the watchdog case statement to actual container
lifecycle events to prevent routine counter wipes. For belt-and-braces,
we have session sidecar (`/var/run/quota-monitor.session`) so the daemon
can re-baseline cleanly on counter reset.

### Decision 2: Per-VIP rules, not per-customer or per-network

**Why per-VIP:**
- The pool is 10.99.0.0/24 (253 usable IPs). 254 rules × 2 directions
  (in/out) = 508 rules. Cheap.
- iptables has no concept of "customer." VIP IS the customer identifier
  from the network perspective.
- Resolution chain (VIP → leases → users → devices → customers) happens
  in the daemon, not in iptables.

**Why NOT per-customer (impossible):** iptables can't query the
strongSwan DB on every packet. iptables doesn't know about user
identities. Only IPs and ports.

**Why NOT per-network (10.99.0.0/24 only):** we'd lose per-customer
granularity. The whole point of quota is to know WHO used how much.

### Decision 3: ACCEPT (not RETURN) in FORWARD

**Why ACCEPT:**
- iptables-legacy is first-match. We want to count bytes and let the
  packet through.
- The default policy is ACCEPT, so ACCEPT in our rule does what we want
  (counts and passes through to the rest of the chain — which is empty
  for these rules).
- RETURN would re-evaluate subsequent rules; we have no subsequent rules
  that would do anything different, but it's wasteful.

**Edge case:** if the default FORWARD policy were ever changed to DROP,
our rules would need to be `-j ACCEPT` (which they are) and we would
intentionally ACCEPT before any DROP happens. Tested with
`iptables-legacy -P FORWARD DROP` as a sanity check: traffic flows
through per-VIP rules correctly.

### Decision 4: 60s poll, not real-time hooks

**Why 60s:**
- Quota enforcement is not a packet-by-packet concern. Customers don't
  notice if their cut happens within 60s of crossing 100%.
- Polling is simple. No need for netlink subscriptions, no need to
  maintain state across daemon restarts.
- The 60s window is small enough that a customer pushing 100 MB/min
  accumulates ~1.6 MB between polls — well within the 100 MiB cap
  tolerance (1.6%).

**Why not faster (10s, 5s):**
- CPU usage scales with poll frequency. 60s is comfortable.
- iptables-legacy counter reads via `iptables-legacy -L FORWARD -nvx`
  take ~50ms on a 508-rule chain. At 5s polls, that's 1% CPU. At 1s
  polls, 5% CPU. 60s is essentially free.

**Why not slower (5min, 1h):**
- Customers pushing 100 MB/min would accumulate 500 MB between polls.
  The cut would be very late and very large.

### Decision 5: 80% warn + 100% hard cut (no soft cap, no throttle)

**Why 80% warn:**
- Gives the customer a "you've used 80%" notice before the cut. They
  can pause, plan to buy more, or finish what they're doing.
- 80% is industry standard (cellular data plans, cloud egress).
- One warn per cycle, fired via `alerts` table, no spam.

**Why 100% hard cut (no soft cap):**
- Zun's explicit decision (5B Q&A 2026-06-19 13:08 UTC): "no soft cap,
  no throttle, hard cut at 100%."
- Simpler implementation: no `tc` qdisc, no `--limit` rate limiting, no
  bandwidth shaping. Just count and cut.
- Bandwidth throttling can be added in a later phase if needed (5G is
  already variable, so throttling may not add much value).

**Why no calendar cycle:**
- 5B Q&A confirmed: per-purchase cycle, no rolling window. Customer pays
  for a tier (e.g. 5 GB), gets 5 GB, when 5 GB is used they're cut. To get
  more, they buy more (manual extension by Zun).
- Manual extension means Zun runs `extend_customer.sh <customer>
  <tier>` after payment — no automatic payment integration (5D).

### Decision 6: Kill credentials in conf, not DB

**Why conf (rw-eap.conf), not DB (users.password):**
- The `attr-sql` plugin stores ATTRIBUTES, not credentials. The
  `eap-mschapv2` plugin reads from `swanctl.conf`'s `secrets { ... }`
  block for auth.
- See ADR `5B-credentials-kill.md` for the full explanation.
- Bottom line: killing the DB password does NOT block auth. Killing
  the conf password DOES block auth.

**Why this is clean:**
- `swanctl --load-creds` reloads conf without restarting charon. No
  service interruption for other customers.
- The original conf is backed up to `.backups/rw-eap.conf.bak-quotamon-<epoch>`.
  To restore: `cp` the backup back, `swanctl --load-creds`.

**Why not "revoke" via OCSP/CRL:**
- Server cert is the same for all customers. Per-customer certs are
  5D (commercial). For 5B, conf-secret-kill is the right level of
  granularity.

### Decision 7: Operator bypass via is_operator flag

**Why not "unlimited tier" in the catalog:**
- Zun's explicit decision (5B Q&A 2026-06-19 13:08 UTC): "no unlimited
  tier in the catalog." Zun's account has operator bypass instead.
- Cleaner separation: "customer catalog" is for paying customers only.
  Zun's account is admin-class, not customer-class.
- Future-proofing: if scope ever changes to multi-tenant, the operator
  flag is still right.

**Implementation:**
- `customers.is_operator` column (BOOLEAN, default 0)
- quota-monitor checks `is_operator=1` and skips quota logic entirely
  (no DB writes, no log spam)
- Bypass applies to ALL quota checks: data accumulation, 80% warn,
  100% cut.

### Decision 8: 2 simultaneous connections per customer

**Why 2 (not 1, not 3, not unlimited):**
- 5B Q&A 2026-06-19 13:08 UTC. Zun's reasoning: "phone + laptop is
  realistic; phone + laptop + tablet is overkill."
- 1 connection is too restrictive (can't use phone and laptop on the
  same trip).
- Unlimited defeats the purpose (one customer could share with friends).

**How enforced:**
- The `devices` table allows multiple rows per customer (1 per device).
  Each device = 1 strongSwan user.
- iptables layer doesn't enforce connection count (we don't have a
  reliable way to map SA ↔ customer at iptables level).
- 5C web page could enforce this if needed (count active SAs per
  customer via the daemon, block 3rd connection).

### Decision 9: Session sidecar for delta computation

**Why a sidecar file (`/var/run/quota-monitor.session`):**
- quota-monitor needs to know "how many bytes since last poll" to
  compute the delta that should be added to `data_used_bytes`.
- Storing the last counter value in memory works until daemon restart
  — then we lose the baseline.
- Sidecar persists across daemon restarts. On restart, daemon reads
  the sidecar, uses the stored values as baseline, and continues.

**Why not store in DB:**
- DB writes are slower than file writes. We poll every 60s.
- Sidecar is per-VM, doesn't need cross-host sync.
- If the LXC dies completely, the sidecar dies too, and we re-baseline
  (which is what the 5B.6 fix already handles via the "counter went
  backwards" warning).

**Sidecar format:** JSON dict, one entry per customer: `{customer_name:
  {last_counter: int, last_seen_at: int}}`. Updated on every poll.

## The 5B.6 lesson: iptables-legacy counter fragility

**The bug (2026-06-19 19:48 UTC, found via Zun's "you lie" screenshot):**

`strongswan-iptables-watchdog.service` re-applied `iptables-restore` on
every docker container event, including `exec_create`/`exec_start`/
`health_status*`. These fired on:

- Every Prometheus scrape (30s)
- Every quota-monitor poll (60s)
- Every `swanctl --list-sas` from the exporter
- Every `swanctl --load-creds` from quota-monitor cut

Each re-apply **reset all 508 per-VIP byte counters to 0**. Zun saw 140
MB in iOS app but only 22 MB in daemon. Math: 60s daemon poll + 30s
Prometheus scrape + health checks = ~6 counter resets per minute.
Daemon's 60s poll always read the counter within seconds of a reset.

**The fix:**

Narrowed watchdog case statement to only match on actual container
lifecycle events:
```bash
case "$action" in
  start|restart|unpause|die|stop|kill|oom)  # NOT exec_create, NOT health_status*
    sleep 1
    $RULES_BIN $RULES
    ;;
esac
```

**The general lesson:**

iptables-legacy `restore` does NOT preserve byte counters. Any
production iptables-counter-based accounting must ensure that
`iptables-restore` is called only when truly needed. nftables named
counters don't have this problem.

**Future-proofing:**

- Document this gotcha in `host/systemd/README.md`
- Add a verification test in the watchdog service (after 3 docker exec
  calls, counter should have accumulated, not been reset)
- Plan migration to nftables for v1.3 (~2-3h work)

## Test methodology and results

3 end-to-end runs with real iOS app traffic, all cut correctly:

| Run | Time | Connect → cut | Peak | Final | Notes |
|---|---|---|---|---|---|
| #1 | 17:42 UTC | n/a (synthetic pre-set) | n/a | 104.8% | First cut, pre-set DB to 100 MiB + 1 byte |
| #2 | 19:44 UTC | 8 min | 22 MB/min | 104.8% | First REAL cut (exposed 5B.6 bug) |
| #3 | 19:56 UTC | 2:23 | 144 MB/min | 158.0% | Zun pushed hard |
| #4 | 23:26 UTC | 1:06 | 140 MB/min | 158.0% | Zun: "Beautiful the app automatically logged me off" |

**How the test works (Option A — real traffic):**

1. Reset test bed:
   - `sqlite3 ... "UPDATE customers SET data_used_bytes=0, over_quota=0"`
   - Restore EAP secret in `rw-eap.conf` from latest backup
   - `docker exec swanctl --load-creds`
   - `iptables-legacy -Z FORWARD` (zero counters)
   - `rm /var/run/quota-monitor.session`
2. Zun opens strongSwan iOS app, taps connect
3. Daemon polls every 60s, accumulates `data_used_bytes` from iptables counter deltas
4. At 80% → log alert (Zun could enable Telegram DM here in 5C.3)
5. At 100% → terminate SA, kill conf secret, reload charon
6. iOS app sees the SA die, shows "Disconnected" or retry loop
7. Subsequent re-auth attempts fail at EAP-MSCHAPv2 (secret is dead)

**Why this proves the design:**

- The cut is real (iOS app actually disconnects, can't reconnect)
- The cut is automatic (no operator intervention needed)
- The cap is enforced (no way to bypass without modifying the server)
- The kill is permanent (re-auth blocked until Zun extends the customer)
- The accounting is correct (DB + iptables counter + actual iOS app data
  all agree after the watchdog fix)

## What was NOT decided (deferred)

- **Customer web page (5C.1)** — UI for customers to see their usage
- **Admin web page (5C.2)** — UI for Zun to manage customers/tiers/devices
- **Telegram bot (5C.3)** — automated DMs at 80% + 100%
- **Grafana vpn-quota dashboard (5C.4)** — per-customer usage charts
- **Backup verify (5C.3)** — verify the daily DB backup actually went to RustFS
- **Payment integration (5D)** — auto-reset on payment
- **Multi-device allocation (5D)** — 2 devices per customer, 2 GB on phone
  + 1 GB on laptop, etc. (5B is shared pool, no per-device caps)
- **HA + LB (5H)** — 2x v1.1 + keepalived VRRP for ~5s failover

All gated on 5B sign-off and Zun approval per the two-gate rule.

---

## Update note (2026-07-13, v2.2.0 doc-sync — added by Misha)

This ADR was written for the LXC 903 lab design (2026-06-19). For VPS production (`vpn-prod-01`, 154.65.110.44), the quota backend was migrated to **nftables named counters** in Phase 7.5 (2026-07-09) — see `docs/ARCHITECTURE.md` "🟢 Verified-live 2026-07-13" table (this doc-sync commit). The 5B.6 fix (narrow watchdog case statement) and the kill-credentials design both remain valid as defense-in-depth. The choice of iptables-legacy over nftables for the lab was correct at the time; for production, nftables was the right call (named counters don't reset on rule reload).

The five "Future work" items at the bottom of this doc are now mostly historical:
- Admin web page → **DELIVERED** as `vpn-portal.service` (Phase 5D)
- Telegram bot at 80/100 → **⏔ NEVER BUILT** (no customer self-service; operator manual per Zun 2026-06-19)
- Grafana dashboard → **DELIVERED** as `host/grafana/dashboards/strongswan-quota.json` (5C.3)
- Backup verify → ⛔ **CANCELLED 2026-06-20** — replaced by PBS full-host backup (DR runbook §1.4)
- Payment integration → ⛔ **NEVER PLANNED** (operator-only model)
- Multi-device → ⛔ **5C.5 REVERTED 2026-06-20** + 5C.6 SHELVED (1-customer-1-device model)
- HA + LB → ⏳ **NOT STARTED** (last-last phase per Zun 2026-06-20)
