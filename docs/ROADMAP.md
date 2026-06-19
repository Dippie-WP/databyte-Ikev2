# ROADMAP

Phased execution per the two-gate rule: each phase is green only when (a) all its technical pass criteria are met AND (b) operator sign-off is given. No auto-promotion.

## 5A — Foundation (lock-in) — ✅ GREEN (both gates, 2026-06-18)

**Goal:** Self-hosted IKEv2 EAP-MSCHAPv2 + per-user sticky VIP. Public-path tested.

| Step | What | Status |
|---|---|---|
| 5A.1 | `rw-eap` conn config + self-signed CA + server cert (ECDSA P-256) | ✅ |
| 5A.2 | DB: `rw-pool` (10.99.0.0/24) + `zun` user + sticky VIP pin (10.99.0.50) | ✅ |
| 5A.3 | End-to-end client test (Android strongSwan app, 5G public path) | ✅ |
| 5A.4 | Reconnect test — same VIP returned | ✅ |
| 5A.5 | Rollback rehearsal (charon-cmd LAN, 30s swap, no DB loss) | ✅ |
| 5A.6 | `install_virtual_ip = no` fix (gateway mode) | ✅ |
| 5A.7 | Server-side MSS clamp at 1260 (5G PMTUD) | ✅ |

**Files added/touched:** see `SESSION-HISTORY.md`.

## 5B — Quota layer — 🟡 IN PROGRESS (kicked off 2026-06-19 13:17 UTC)

**Goal (revised 2026-06-19 13:17 UTC — single-operator + paying customers):**
- Operator account: unlimited, no data cap, bypasses all quota checks
- Customers: 2 simultaneous connections per purchase, shared quota pool, 3GB/10GB/15GB catalog
- 100% = hard cut, manual extension by operator after payment (no calendar cycle)
- Customer-facing web page: read + "buy more" CTA → DM to operator → operator sends payment link
- Customer auth: username + password (bcrypt)
- Customer notifications: Telegram DM at 80% warn + 100% cut
- Grafana: operator-only, system + all users monitoring
- Admin web page: operator manages customers/tiers/devices/quota extensions

| Step | What | Status |
|---|---|---|
| 5B.1 | **DB schema — 6 new tables (customers, tiers, devices, purchases, alerts, audit_log) + 10 indexes + seeds + systemd unit** | ✅ DONE 2026-06-19 13:30 UTC |
| 5B.2 | nftables accounting rules (`strongswan-quota.nft`) — per-VIP byte counter, no rate-limit | ⏳ Next |
| 5B.3 | `quota-monitor.py` — nftables + DB → 80% alert + 100% cut + VICI terminate | ⏳ |
| 5B.4 | systemd unit (`quota-monitor.service`) | ⏳ (placeholder file added in 5B.1) |
| 5B.5 | End-to-end test with demo-customer (100 MB tier, 80% + 100% trigger) | ⏳ |
| 5C.1 | Customer web page (FastAPI + bcrypt) | ⏳ Gated on 5B green |
| 5C.2 | Admin web page (`/admin`, customer mgmt + credential gen + quota extension) | ⏳ |
| 5C.3 | Telegram bot (vpn-bot.py — auth + buy-more relay + outbound alerts) | ⏳ |
| 5C.4 | Grafana `vpn-quota` dashboard (active SAs per customer, usage, alerts) | ⏳ |

**5B.1 deliverables (signed off in this commit):**
- `quota/quota_schema.sql` — 6 tables, 10 indexes, idempotent `IF NOT EXISTS`
- `quota/apply_quota_schema.sh` — host-side applier, idempotent, pre/post check
- `quota/seed_real_tiers.sh` — 3GB/10GB/15GB tiers
- `quota/seed_5B1.sh` — demo_100mb tier + zun-operator + demo-customer + 5 device links
- `quota/reset_demo.sh` — resets demo customer's `data_used_bytes` to 0
- `host/systemd/quota-schema.service` — oneshot at host boot
- `host/systemd/quota-monitor.service` — placeholder for 5B.3
- `host/systemd/README.md` — install instructions

**Backups:** `ipsec.db.bak-5B1-20260619-132059` retained on LXC 903 until 5B.5 green.

## 5C — Surface — ⏳ Gated on 5B green

**Goal:** Operator dashboard + monitoring integration.

| Step | What |
|---|---|
| 5C.1 | Status FastAPI app (bcrypt + rate-limit + itsdangerous) |
| 5C.2 | Grafana `vpn-quota` dashboard |
| 5C.3 | Backup verify (RustFS) |

## 5D — Commercial — 🔒 Shelved (out of scope, customer-facing bits moved to 5C)

**Status:** Zun confirmed 2026-06-19 12:30 UTC: "I'm the only one hosting the server." Single-operator only — no multi-tenant SaaS, no automated billing, no customer self-signup. The "buy more → DM to Zun → payment link" flow is manual by design.

**Original goal (if scope ever changes):** Multi-tenant billing, payment-triggered reset, customer-facing messages.

| Step | What |
|---|---|
| 5D.1 | Pricing tiers |
| 5D.2 | Audit trail (who connected, when, how much) |
| 5D.3 | Payment-triggered reset (Stripe / Paystack) |
| 5D.4 | Customer onboarding flow (signup → credentials) |
| 5D.5 | Hard VIP pinning (released=0 enforced, no lease reuse) |

## v1.3 backlog (revisit later)

- **iOS native IKEv2 + EAP** — needs Let's Encrypt cert via certbot + DNS-01 (current iOS path silently fails cert validation)
- **Customer onboarding flow** — auto-generate username + password per signup
- **CA cert auto-bundle** for Android — include CA in `.sswan` profile export
- **Phone-side UX polish** — shorter `rekey_time` (24h → 20-30m), `reauth_time` (24h → 2-3h), `charon.keep_alive = 20s`
- **Server-side defaults audit** — every `charon.*` setting reviewed for gateway vs client default
- **Cloudflare bot detection** — ifconfig.me may give `ERR_CONNECTION_CLOSED` because shared MASQ IP looks bot-like
- **5G MTU/PMTUD** — server-side MSS clamp at 1260 fixes (5A.7). May need carrier-specific tuning
