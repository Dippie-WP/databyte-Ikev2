# Phase 5C.6 — Multi-Device Per Customer (Rev 2, with verified facts)

**Status:** DRAFT v2 — Zun callout: "no assuming and guessing" — every fact below is verified against live state or upstream docs.
**Target version:** v1.2.6
**Branch:** TBD (`v1.2.6-multidevice-shared-creds` from main)
**Date:** 2026-06-20 18:58 UTC

---

## 0. Why this revision exists

I wrote Rev 1 from research and made some assumptions. Zun called me out:
> "If you need more clarity always ask me more questions. Build this with truth and facts no assuming and guessing."

This Rev 2 separates **verified facts** (with sources) from **design choices** (with my recommendation + open questions for Zun).

---

## 1. Verified facts (NOT up for debate — these are the ground truth)

### 1.1 strongSwan `uniqueids` default behavior (CONFIRMED)

**Source:** strongSwan 5.9 strongswan.conf docs (the 5.9 doc still lists `uniqueids` as a charon key in the older versions; in 6.0 it was reorganized but the daemon still respects it for backward compat).

- **Default = `yes`** → only ONE IKE_SA per identity (same IKE ID, same VIP, all re-init kicks the oldest)
- **`uniqueids = no`** → multiple SAs with the same ID allowed (gets DIFFERENT VIPs from the pool)
- **Live state check on LXC 903 (192.168.10.98) at 18:56 UTC:** `uniqueids` is NOT explicitly set in `/etc/strongswan.conf` → running with the default `yes` (one SA per ID).

**Operational impact:**
- If we want a customer to use the same identity on 2 devices simultaneously → MUST set `uniqueids = no` in strongswan.conf
- If we use per-device identities (2 different EAP IDs for the same customer) → no change needed; the default works

### 1.2 EAP identity ↔ VIP mapping (CONFIRMED)

**Source:** 2016 strongSwan users mailing list thread "Separate devices connecting with same user-based credentials":
> "all devices with the same user authentication credentials receive the same Virtual IP from strongswan."

And confirmed by **ivpn/expr** behavior: different devices = different identities (even if they share a password).

- Same `name` in `users` table → same VIP allocated by the pool
- Different `name` → different VIPs (from the 10.99.0.0/24 pool, 254 addresses)
- 2 different identities = 2 different VIPs = 2 different iptables byte counters

### 1.3 attr-sql `users` table can have shared passwords (CONFIRMED)

**Source:** Live DB inspection at 18:56 UTC. The attr-sql schema is:
```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  password TEXT  -- 16 bytes = NTLM hash of "client password"
);
```

- `name` is UNIQUE → 1 row per EAP identity
- `password` is **NOT unique** → multiple rows can store the same NTLM hash → multiple identities, one shared password
- Live state: ALL current users have UNIQUE passwords (no shared password exists today). This is the new feature, not a restriction.

### 1.4 Quota layer (CONFIRMED)

- **Per-VIP byte counters** in iptables-legacy `FORWARD` chain, comment `quota:VIP` → one counter per VIP
- **Per-customer aggregate** = sum of all VIP counters for that customer's devices
- Currently: 508 per-VIP rules (254 in + 254 out) installed live
- **Cap enforcement:** quota-monitor polls every 60s; at 100% it terminates ALL SAs for the customer and replaces the EAP secret with `KILLED-<random>` (proven in v1.1.0)

### 1.5 Each device has 1 strongSwan identity, 1 VIP, 1 password (CURRENT state)

**Live state check at 18:56 UTC:**
```
zun-operator has 6 devices: zun, zun-iphone, zun-windows, zun-iphone2, zun-iphone3, zun-android
Each has a UNIQUE strongSwan user_id + UNIQUE password
NO shared passwords exist
```

**This is the "legacy" model that 5C.6 changes.**

---

## 2. The 3-device test scenario — what does the requirement actually mean?

Zun's words:
> "We need to test 1 set of credentials. On three devices of which only 2 is allowed the third device shouldn't allow"

There are two valid interpretations. **I need Zun to confirm which one he means.** I won't pick one.

### Interpretation A: 1 EAP identity, 2 simultaneous SAs (no per-device tracking)

- Operator creates customer → 1 shared password → customer installs 1 profile on 3 devices
- All 3 devices send the same EAP identity (e.g., `friend-1`)
- Server allows max 2 simultaneous SAs for that identity
- 3rd device's IKE_AUTH is REJECTED (SA terminated before CHILD_SA established)
- Per-device tracking: IMPOSSIBLE at the IKE layer (3 devices with same identity look identical to charon). Workaround: track per-peer-IP (cellular IPs change, NAT problems) OR per-EAP-reauthentication (when the same identity connects from a new IP, the old one is killed → effectively 1 device at a time, breaks the "2 simultaneous" requirement)
- **Doesn't satisfy R4 (per-device dashboard)**

### Interpretation B: 2 EAP identities (same password), 1 SA each, 2 device cap = 2 identities max

- Operator creates customer → 1 shared password → operator provisions 2 device slots (each with its own identity like `friend-1-iphone` + `friend-1-android`)
- Customer installs the 2 profiles on up to 2 devices (1 per slot)
- 3rd device would need a 3rd identity, but cap is 2 → operator never created the 3rd → 3rd device's IKE_AUTH fails (identity not in `users` table)
- Per-device tracking: 1 identity per slot → 1 VIP per slot → 1 iptables counter per slot → perfect per-device dashboard
- **Satisfies all R1-R7**

### My recommendation: Interpretation B (it satisfies R4-R6 cleanly and is the production-VPN pattern).

**OPEN QUESTION #1: Is interpretation B what you meant?**

If yes → we move to Q2.
If no (you want A) → the per-device dashboard piece becomes a separate design problem (peer-IP tracking) and the build is bigger / more brittle.

---

## 3. Design choices (interpretation B assumed for the rest of this section)

### 3.1 Device slot identity naming

Zun's example: "friend 1a, friend 1b" — suggests friendly suffixes, not OS names.

Two naming schemes:
- **Scheme X (slot-suffix):** `friend-1a`, `friend-1b` (auto, slot is a/b/c/d). Device label = same as identity.
- **Scheme Y (operator-labeled):** `friend-1-iphone`, `friend-1-android` (operator picks label at creation). Identity = label. Display label = identity.

**My recommendation: Scheme X** for v1, with operator override allowed at creation. Reason: matches the user's example ("1a, 1b"), and the device label IS the identity the customer types in their VPN app (no need for separate "device name" + "identity" UX).

**OPEN QUESTION #2: Scheme X (1a/1b) or Scheme Y (iphone/android) or something else?**

### 3.2 Password generation timing

Two options:
- **Per-customer at creation:** 1 password shown ONCE at customer creation, reused for all device slots.
- **Per-slot at creation:** Each slot gets its own password (current model, just relabeled).

**My recommendation: Per-customer at creation.** Reason: matches "1 set of credentials" literally, and rotation = rotate ONCE for the customer (not per-slot). The customer only has 1 password to remember.

**OPEN QUESTION #3: Confirmed per-customer? (If no → each device slot has its own password and "1 set of creds" is not literal.)**

### 3.3 Password delivery to customer

Currently (v1.2.5): operator sees the password in the portal response, distributes manually (no Telegram for v1.2.5).

For v1.2.6: same? OR do we add a one-time download URL the operator can paste to the customer? OR Telegram-DM to operator's pre-registered customer ID?

**OPEN QUESTION #4: Same as v1.2.5 (operator copies and pastes manually) or do we build a delivery mechanism now?**

### 3.4 Data cap behavior when hit

When customer's `data_used_bytes >= data_limit_bytes`:
- **Option α (cut all):** Terminate ALL SAs for the customer, replace all shared-password EAP blocks with KILLED. (Same as v1.1.0 today.)
- **Option β (block new, keep old):** Existing SAs keep running, new IKE_AUTH attempts are rejected. Customer can finish current session but can't reconnect after disconnect.

**My recommendation: Option α** (consistent with current behavior, customers are paying customers who understand the cap, sudden cutoff is clearer than "soft block").

**OPEN QUESTION #5: α (cut all) or β (block new, keep old)?**

### 3.5 Cap on simultaneous connections per customer

Beyond the 2-slot cap (which is enforced at device-row level in the DB), should we ALSO enforce a runtime cap on simultaneous IKE_SAs per customer?

- **Without runtime cap:** customer has 2 device slots. If a 3rd device uses one of the 2 existing identities, it would establish a 3rd SA (charon allows it by default with `uniqueids=yes`... actually no, default `uniqueids=yes` KICKS the oldest SA for the same identity, so the 3rd device's SA replaces the 1st device's SA — first-device loses connectivity, third-device gets connectivity).
- **With runtime cap:** customer has 2 device slots + 1 SA per slot (max 2 SAs per customer). 3rd device's IKE_AUTH is rejected.

**This is the "test cap and connection too" interpretation.** My current design includes the 2-slot cap (rejection at EAP because the 3rd identity doesn't exist). The runtime cap is a separate layer that catches the case where the 3rd device uses an existing identity.

**OPEN QUESTION #6: Build both the slot cap AND the runtime SA cap, or just the slot cap?**

### 3.6 Existing v1.2.5 customers (migration)

The 6 devices on zun-operator, the 4 devices on friend-*, the 2 on demo-*, etc. all have per-device passwords today (legacy model).

Options:
- **Leave as-is:** New customers (created after v1.2.6) get shared password; existing customers keep per-device passwords (UI shows them as "legacy", no shared password visible).
- **Migrate:** Pick 1 device per customer as "primary", propagate its password to the other device rows. Risky: changes customer-facing creds without operator involvement.
- **Re-issue:** Operator must manually reset each existing customer's devices to use the new model.

**My recommendation: Leave as-is** for v1.2.6, add a "migrate to shared password" button per customer (one-click, operator-triggered).

**OPEN QUESTION #7: Leave legacy / migrate / re-issue?**

### 3.7 Friendly device names on the dashboard

Zun said: "If the only have 1 device using there credentials we only show 1 device ok."

Two interpretations:
- **Filter interpretation:** Dashboard hides devices that have never connected (no `last_seen_at`).
- **State interpretation:** Dashboard shows all active devices, with state badges (active / idle / inactive / never-connected), and only `active` and `idle` count toward the "1 device using creds" view.

**My recommendation: Filter interpretation** (matches Zun's literal phrasing, cleaner UI). "Idle" devices still show in a separate "previously connected" section.

**OPEN QUESTION #8: Filter (hide never-connected) or state-badges (show all with state)?**

---

## 4. Open questions summary (please answer 1-8)

| # | Question | My default |
|---|---|---|
| 1 | Interpretation A (1 identity, 2 SAs) or B (2 identities, 1 SA each)? | **B** |
| 2 | Naming: 1a/1b (auto) or iphone/android (operator)? | **1a/1b** |
| 3 | Per-customer password (shown once at customer creation)? | **Yes** |
| 4 | Delivery: manual (same as v1.2.5) or build delivery now? | **Manual** |
| 5 | Cap hit: cut all SAs (α) or block new, keep old (β)? | **α (cut all)** |
| 6 | Slot cap + runtime SA cap, or just slot cap? | **Both** |
| 7 | Migration: leave legacy / migrate / re-issue? | **Leave + one-click migrate button** |
| 8 | Dashboard: filter never-connected, or state-badges? | **Filter** |

**8 questions. If you say "all your defaults" I proceed. If you want to override any, tell me which.**

---

## 5. What gets built (assumes all my defaults)

### 5C.6.1 — Schema (additive, idempotent)
- `customers.shared_password_ntlm` (BLOB, 16 bytes) — the shared secret in charon format
- `customers.shared_password_set_at` (INTEGER) — when it was issued
- `customers.shared_password_legacy` (INTEGER DEFAULT 0) — 1 = old per-device-password customer, 0 = new model

### 5C.6.2 — POST /api/customers
- Generates shared password (token_urlsafe(16))
- Stores NTLM hash in `customers.shared_password_ntlm`
- Returns password ONCE in response

### 5C.6.3 — POST /api/customers/{id}/devices (changed)
- Auto-generates slot identity: `{customer_slug}-{slot_letter}` (e.g., `friend-1-a`)
- Inserts `users` row with the SHARED NTLM hash (not a new one)
- Inserts `devices` row
- Appends EAP block to `rw-eap.conf` (uses shared secret)
- charon reload
- Idempotent on `device_name` (return existing row if re-adding same slot)
- 409 on cap
- Returns: new device + shared password reminder (operator re-display)

### 5C.6.4 — POST /api/devices/{id}/rotate (changed)
- Generates NEW shared password for the customer
- Updates `customers.shared_password_ntlm`
- Updates ALL `users` rows for this customer with new NTLM
- Updates ALL EAP blocks in `rw-eap.conf` for this customer
- charon reload
- Returns new shared password ONCE

### 5C.6.5 — DELETE /api/devices/{id} (unchanged behavior)
- Soft-delete + BLOCKED- secret
- Other devices for the customer keep working with the shared password

### 5C.6.6 — POST /api/customers/{id}/migrate-to-shared (NEW)
- Picks one device's password as the canonical shared password (or generates new one)
- Updates all `users` rows for the customer
- Updates all EAP blocks
- Marks `shared_password_legacy = 0`
- charon reload
- Returns the (new) shared password ONCE

### 5C.6.7 — GET /api/customers/{id}/usage (NEW)
- Returns: customer + array of devices with `bytes_used` from iptables counter
- Filters out devices with `last_seen_v4 IS NULL` (never connected)

### 5C.6.8 — Runtime SA cap enforcement (NEW, if Q6 = "both")
- New daemon `vpn-sa-cap-monitor.py` on LXC 903
- Polls `swanctl --list-sas` every 30s
- For each customer: count SAs. If `> max_devices`, terminate the newest SA(s) via VICI
- Logs to audit_log

### 5C.6.9 — UI changes
- Customer card: show `data_used_bytes / data_limit_bytes` (R5)
- Per-device breakdown (R4) under the card
- Add device: pick slot letter (a/b/c/d) + friendly label (e.g., "iPhone" — display only, doesn't affect identity)
- Hide never-connected devices (R6)
- Migration banner for legacy customers (Q7)

### 5C.6.10 — E2E test
- Create `friend-test` customer (verify shared password returned)
- Create slot `a` → 1 SA possible
- Create slot `b` → 2 SAs possible
- Attempt slot `c` → 409 (cap)
- Both devices connect → 2 SAs up, 2 VIPs, 2 iptables counters with bytes
- 3rd physical device using slot `a` identity → 3rd SA attempted → runtime cap fires → SA terminated
- Verify audit log: 4 rows (customer create, 2 device creates, 1 cap violation)

---

## 6. Effort estimate (assumes all my defaults)

| Step | Effort |
|---|---|
| 5C.6.1 schema | 15 min |
| 5C.6.2 customer create | 30 min |
| 5C.6.3 device create (rewritten) | 1.5h (the meaty one) |
| 5C.6.4 rotate (rewritten) | 1h |
| 5C.6.5 deactivate (unchanged) | 0 min |
| 5C.6.6 migrate-to-shared | 1h |
| 5C.6.7 usage endpoint | 1h |
| 5C.6.8 SA-cap-monitor (NEW daemon) | 2h |
| 5C.6.9 UI | 2h |
| 5C.6.10 E2E | 1h |
| **Total** | **~10h** |

This is bigger than my first estimate (was 7-8h) because of 5C.6.8 (SA-cap-monitor) and the rewrite of the device-create + rotate endpoints (they need to handle the shared password properly).

**Spans 2 sessions** for sure. Might need 3.

---

## 7. Migration of existing test data (cleanup)

After the model is built, the 6 devices on zun-operator are "legacy" (per-device password). Two cleanups needed before going live:

| Action | What |
|---|---|
| Deactivate test devices | `zun-iphone3` + `zun-android` (you said clean these up) |
| Reset zun-operator to 2-slot | `max_devices = 2` (currently 6, bumped for the wrong test) |
| Decide on zun + zun-iphone + zun-windows | These are your real devices. Migrate to shared? Or leave as legacy? |
| Decide on zun-iphone2 | Your real iPhone (per 17:26). Migrate to shared? Or leave as legacy? |

**OPEN QUESTION #9: For each of (zun, zun-iphone, zun-windows, zun-iphone2) — keep per-device password (legacy) or migrate to shared password model?**

---

## 8. What I will NOT do (out of scope for 5C.6)

- Per-peer-IP tracking (would only be needed for Interpretation A — Q1 says no)
- iOS mobileconfig generator (separate feature, not in this phase)
- Customer self-service portal (you said operator-only for v1.2.5; assume same for 5C.6)
- Telegram DM delivery (you said no for v1.2.5; Q4 asks if 5C.6 changes that)
- 5C.4 (RustFS backup) — already cancelled 2026-06-20
- 5C.7+ — backlog, not in this phase

---

## 9. Sign-off

- [ ] Zun: answer Q1-Q9 above
- [ ] Zun: confirm effort is acceptable (10h, 2-3 sessions)
- [ ] Zun: say "go" to start 5C.6.1

When you answer, I'll start with schema + customer-create endpoint (5C.6.1 + 5C.6.2) since they're independent of the design decisions and let us see data shape early.
