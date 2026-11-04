# Prod VPN — Cut the Lag (Simple Plan)

**Goal:** make video calls / YouTube smooth on the VPN by cutting **lag**, not raising speed caps.
**Host:** vpn-prod-01 · **Repo:** Dippie-WP/databyte-Ikev2 → tag **v2.7.0**
**Status:** waiting on GO. Nothing changed yet.

## The idea in one line
Lag under load = packets sitting in a big dumb queue. Give each customer a **smart, short queue** instead.

## Steps
1. **Smart queues.** Swap each customer's queue (currently a plain FIFO) for **fq_codel**. This is the main fix — it stops one person's download from lagging their own video.
2. **Fix uploads.** The upload-side shaper exists but is **not connected** — wire it up so uploads stop lagging too.
3. **Restore VoIP priority.** It broke in the September migration. Re-add: EF/DSCP mark + tc class + `copy_dscp` on the tunnel. Re-enable the service.
4. **Stop fragmentation.** Use auto MTU (clamp-to-PMTU) instead of the current hard 1260.
5. **Test.** Measure lag under load before/after. Target: **< 30 ms added, < 5 ms jitter.**
6. **Ship.** Commit → PR → merge → tag **v2.7.0**. Bot repo untouched.

## Safety
Snapshot first; every step has a one-line rollback; do it in a quiet window.

## Need from you
1. **When** — quiet window (~30–45 min, 10 customers live).
2. **How far** — just the smart queues (small, safe), or the bigger CAKE rework?
3. **MTU** — auto (recommended) or keep fixed?
4. **BBR** — skip (it doesn't help routed traffic) or add anyway?
5. **Confirm** release as **v2.7.0**.
