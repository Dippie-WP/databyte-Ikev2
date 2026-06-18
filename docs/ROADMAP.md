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

## 5B — Quota layer — ⏳ Pending operator sign-off on 5A

**Goal:** Per-user data quota, 80% Telegram alert, 100% disconnect.

| Step | What | Notes |
|---|---|---|
| 5B.1 | nftables accounting rules (`strongswan-quota.nft`) | outline in `runbooks/v1.2-nftables-accounting-outline.md` (RustFS) |
| 5B.2 | `quota-monitor.py` (nftables counters → SQLite → Telegram) | reads nftables counters |
| 5B.3 | 80% alert + 100% disconnect test | 100% triggers `swanctl --terminate --ike` |

## 5C — Surface — ⏳ Gated on 5B green

**Goal:** Operator dashboard + monitoring integration.

| Step | What |
|---|---|
| 5C.1 | Status FastAPI app (bcrypt + rate-limit + itsdangerous) |
| 5C.2 | Grafana `vpn-quota` dashboard |
| 5C.3 | Backup verify (RustFS) |

## 5D — Commercial — 🔒 Shelved

**Goal:** Multi-tenant billing, payment-triggered reset, customer-facing messages.

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
