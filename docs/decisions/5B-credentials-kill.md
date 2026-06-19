# Phase 5B.3 — "Kill credentials" at 100% (decision 2026-06-19, CORRECTED)

**Decision (Zun, 13:57 UTC):** When a customer hits 100% quota, we kill their
credentials. The next IKE_SA auth attempt fails because the EAP-MSCHAPv2
password no longer matches.

## CORRECTION (2026-06-19 14:08 UTC) — important

**The strongSwan `attr-sql` plugin stores ATTRIBUTES, not credentials.** Auth
is performed by the `eap-mschapv2` plugin, which reads from `swanctl.conf`'s
`secrets { ... }` block.

| Source of truth | Used for | Where |
|----------------|----------|-------|
| `swanctl.conf` `secrets { eap-* { secret = "..." } }` | **EAP-MSCHAPv2 auth (this is what charon checks against)** | `/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf` on LXC 903 host, bind-mounted as `/etc/swanctl/conf.d/rw-eap.conf` (READ-ONLY) in the strongSwan container |
| DB `users.password` (NTLM hash) | **attr-sql metadata only — not used in auth path** | `/var/lib/strongswan/ipsec.db` (bind-mounted) |

The 5A work put data in BOTH places (DB for attr-sql metadata, conf for charon
auth). To kill a customer, we MUST mutate the conf + reload charon, NOT just
the DB. Confirmed by:
- `swanctl --load-creds` shows only conf-defined secrets (3 zun + 2 demo
  after this commit; demo-phone and demo-laptop did NOT appear until I
  added them to the conf)
- [strongSwan attr-sql docs](https://docs.strongswan.org/docs/latest/plugins/attr-sql.html)
  describe attr-sql as for "attributes" only; the `sql` plugin (NOT loaded
  in our setup) is the one that does DB-backed auth

## What this means for "kill credentials at 100%"

To kill a customer's credentials, the quota-monitor at 100% will:

1. **Terminate active IKE_SAs** for the customer via VICI
   (`swanctl --terminate-sae --ike <id>`)
2. **Edit `/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf` on LXC host**
   - Replace the customer's `secret = "..."` with a random unguessable value
     (e.g. `secret = "KILLED-$(uuidgen)"` or `secret = "KILLED-$(date +%s)"`)
   - Or comment out the entire `eap-* { ... }` block
3. **Reload charon** via `swanctl --uri=tcp://127.0.0.1:4502 --load-creds`
4. **Set `customers.over_quota = 1`** in DB
5. **Log to `alerts` table**
6. **Send Telegram DM to customer + Zun**

The 100% cut is immediate AND permanent (until Zun extends them).

## What the customer experiences

| Phase | Connection | Auth attempt | Data flow |
|-------|------------|--------------|-----------|
| 0–80% | OK | OK | OK |
| 80% alert | OK | OK | OK (warning DM sent) |
| 100% cut | **Terminated** (existing IKE_SA torn down by quota-monitor) | **Fails** (EAP secret no longer matches) | Blocked |

**On re-connect attempt:** customer enters same creds → IKE_SA_INIT succeeds
(server cert validates) → IKE_AUTH fails at EAP-MSCHAPv2 verify → client
shows auth error. No way to log back in until Zun extends them.

**On re-purchase:** Zun runs `extend_customer.sh <customer> <tier>` →
generates new password → mutates `rw-eap.conf` (replaces the secret) →
`--load-creds` → clears `over_quota=0` and `data_used_bytes=0` → customer
is unblocked.

## Files added/modified (5B prep)

| File | Status | Purpose |
|------|--------|---------|
| `quota/seed_demo_creds.sh` | NEW | Generates random passwords, NTLM-hashes them, writes to DB `users.password` (for attr-sql metadata) and saves plaintext to `/root/.demo_vpn_creds` (mode 600) |
| `quota/update_rw_eap_conf.py` | NEW | Adds `eap-* { secret = "..." }` blocks to host-side `rw-eap.conf`, then `swanctl --load-creds` |
| `docker/swanctl/conf.d/rw-eap.conf` | PULLED into repo | Now version-controlled (with demo creds for testing — to be removed before 5B.3 sign-off) |
| `docs/decisions/5B-credentials-kill.md` | NEW | This document |

## Files to add (5B.3)

| File | Purpose |
|------|---------|
| `quota/lib_counters.py` | Parse iptables output → `{vip: (out, in)}` |
| `quota/lib_db.py` | SQL: load customers, update data_used_bytes, log alerts |
| `quota/lib_vici.py` | `swanctl --uri=tcp://127.0.0.1:4502 --list-sas` + `--terminate-sae` via docker exec |
| `quota/lib_telegram.py` | Telegram DM via `requests.post` |
| `quota/quota-monitor.py` | Main 60s loop, ties everything together |
| `quota/extend_customer.sh` | Zun-runs CLI for unlocking a customer after payment |
| `quota/kill_customer.sh` | quota-monitor helper: invalidates conf secret, reloads charon, sets over_quota=1 |
| `quota/test_quota_monitor.sh` | Unit smoke tests |

## Gating for 5B.3 sign-off

5B.3 is GREEN when all of these pass:

- [ ] 5B.3.1 monitor starts cleanly in `--once` mode (prints state, exits 0)
- [ ] 5B.3.2 monitor runs as daemon (systemd) for 1h+ without crash
- [ ] 5B.3.3 `data_used_bytes` updates correctly for active SA
- [ ] 5B.3.4 80% alert fires ONCE per threshold per customer (no spam)
- [ ] 5B.3.5 100% cut: SA terminated, conf secret replaced + reloaded,
       over_quota=1
- [ ] 5B.3.6 Reconnect attempt after cut → fails at IKE_AUTH (EAP-MSCHAPv2)
- [ ] 5B.3.7 `extend_customer.sh` unblocks customer, re-auth succeeds

Only then do we sign off 5B.3, commit on v1.1-quota-db, and consider merging to main.
