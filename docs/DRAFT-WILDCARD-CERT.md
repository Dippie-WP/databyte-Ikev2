# WILDCARD-CERT — Databyte VPN Wildcard Certificate Issuance (MR-1)

| Field | Value |
|---|---|
| Document ID | DAT-OPS-WILDCARD-CERT-001 |
| Revision | v0.1.0 (DRAFT) |
| Date (SAST) | 2026-10-15 |
| Author | Misha 🐻 (Zun's assistant) |
| Authoritative source of facts | `ssh root@vpn-prod-01` + `/etc/letsencrypt/renewal/myvpn.databyte.co.za.conf` + `/etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem` (live read 2026-10-15) + LE + Cloudflare docs |
| ISO 9001:2015 | Document Control: this is internal — distribution restricted to operators |
| Verification | Each step is independently verifiable; assumes operator has access to `vpn-prod-01` SSH + Cloudflare dashboard + 1Password (for the API token) |
| Live fact-check | 2026-10-15 11:00 UTC — verified against `vpn-prod-01` cert path `/etc/letsencrypt/live/myvpn.databyte.co.za/` (cert SAN: `myvpn.databyte.co.za`, `vpn-portal.databyte.co.za`; expires 2026-12-02), certbot `dns-cloudflare` plugin + `/root/.cloudflare.ini` (mode 0600) configured |
| Companion docs | [`ROADMAP.md` "Multi-region rollout" subsection + MR-1 row](./ROADMAP.md) · [`VPS-XNEELO-DEPLOY.md`](./VPS-XNEELO-DEPLOY.md) (for the regional VPS deploy — MR-2) · [`CLOUDFLARE-DNS.md`](./CLOUDFLARE-DNS.md) (Cloudflare API token setup, for any new operator) · [LE DNS-01 challenge docs](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge) |
| Ticket | TKT-036 |
| Status | DRAFT — not yet executed |

---

## 0. Purpose

Issue a Let's Encrypt wildcard certificate for `*.myvpn.databyte.co.za` via certbot + Cloudflare DNS-01. The cert enables multi-region naming (e.g., `za-cpt.myvpn...`, `fr-par.myvpn...`) without re-issuing per region.

**Scope of this doc:** the certbot invocation + verification + nginx vhost preparation.
**Out of scope:** standing up regional VPSs (MR-2), per-customer `assigned_server` field in the bot (MR-3), strongSwan config per region (MR-4). Those are separate sub-tasks of the multi-region rollout (see ROADMAP).

**Why now (vs. waiting for the second server):** the cert is independent of any regional infrastructure. Issuing it now removes a future blocker; the cert just sits at `/etc/letsencrypt/live/myvpn-wildcard/` until a regional VPS needs it.

---

## 1. Prerequisites

### 1.1 Already in place (verified live 2026-10-15)

| Item | Where | Verified |
|---|---|---|
| Cloudflare API token | `/root/.cloudflare.ini` (mode 0600, 194 B) | 2026-10-15 |
| certbot `dns-cloudflare` plugin | configured in `/etc/letsencrypt/renewal/myvpn.databyte.co.za.conf` | 2026-10-15 |
| ACME v02 endpoint | `https://acme-v02.api.letsencrypt.org/directory` | 2026-10-15 |
| Existing cert for `myvpn` + `vpn-portal` | `/etc/letsencrypt/live/myvpn.databyte.co.za/` (SAN: myvpn, vpn-portal) | 2026-10-15 (valid until 2026-12-02) |
| DNS-01 propagation seconds | 30 | 2026-10-15 |

### 1.2 DNS zone requirements

The wildcard requires that `myvpn.databyte.co.za` is on the same Cloudflare zone as `databyte.co.za`. Verify:

```bash
dig +short myvpn.databyte.co.za NS
# Should show Cloudflare nameservers (e.g., aria.ns.cloudflare.com, ...)
```

### 1.3 Operator access

- SSH key to `root@vpn-prod-01`
- Cloudflare dashboard (read-only is enough)

---

## 2. Pre-flight verification

Run these BEFORE the certbot command, to confirm state is what we expect.

```bash
# 2.1 Confirm certbot config is intact
ssh root@vpn-prod-01 'cat /etc/letsencrypt/renewal/myvpn.databyte.co.za.conf | grep -E "^(authenticator|server|dns_cloudflare)"'
# Expected:
#   authenticator = dns-cloudflare
#   server = https://acme-v02.api.letsencrypt.org/directory
#   dns_cloudflare_credentials = /root/.cloudflare.ini

# 2.2 Confirm Cloudflare API token is reachable
ssh root@vpn-prod-01 'curl -sS -X GET "https://api.cloudflare.com/client/v4/user" \
  -H "Authorization: Bearer $(grep "dns_cloudflare_api_key" /root/.cloudflare.ini | cut -d"=" -f2)" \
  -H "Content-Type:application/json" | head -c 200'
# Expected: {"result":{"id":"...","email":"..."...},"success":true,...}

# 2.3 Confirm current cert is still valid
ssh root@vpn-prod-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -dates -ext subjectAltName'
# Expected: notAfter > today; SAN: DNS:myvpn.databyte.co.za, DNS:vpn-portal.databyte.co.za
```

If any of these checks fail, **stop here** and investigate before proceeding.

---

## 3. Procedure (certbot invocation)

### 3.1 Step 1: dry-run on LE staging

Validates config without issuing a real cert or hitting LE rate limits.

```bash
ssh root@vpn-prod-01 'certbot certonly --dry-run \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/.cloudflare.ini \
  --cert-name myvpn-wildcard \
  -d "*.myvpn.databyte.co.za" \
  -d "myvpn.databyte.co.za"'
```

**Expected output (last lines):**

```
Congratulations, all simulated validations passed
```

If the dry-run errors:
- `DNS problem: NXDOMAIN` — check that `myvpn.databyte.co.za` resolves in Cloudflare (and is delegated to the same zone as `databyte.co.za`)
- `Error communicating with ACME server` — check certbot version, check outbound HTTPS to `acme-staging-v02.api.letsencrypt.org`
- `Cloudflare API error` — check the API token's zone permissions (needs `Zone:DNS:Edit` for the `databyte.co.za` zone)

### 3.2 Step 2: real issuance

Only after dry-run passes. Issues the actual cert from LE production.

```bash
ssh root@vpn-prod-01 'certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/.cloudflare.ini \
  --cert-name myvpn-wildcard \
  -d "*.myvpn.databyte.co.za" \
  -d "myvpn.databyte.co.za"'
```

**Expected output (last lines):**

```
Successfully received certificate.
Certificate is saved at: /etc/letsencrypt/live/myvpn-wildcard/fullchain.pem
Key is saved at:         /etc/letsencrypt/live/myvpn-wildcard/privkey.pem
This certificate expires on 2027-01-13.
```

### 3.3 Step 3: verify the new cert

```bash
# 3.3.1 Cert path exists
ssh root@vpn-prod-01 'ls -la /etc/letsencrypt/live/myvpn-wildcard/'
# Expected: cert.pem, chain.pem, fullchain.pem, privkey.pem (symlinks to archive/)

# 3.3.2 Cert contents
ssh root@vpn-prod-01 'openssl x509 -in /etc/letsencrypt/live/myvpn-wildcard/fullchain.pem -noout \
  -issuer -dates -ext subjectAltName'
# Expected:
#   issuer=C=US, O=Let's Encrypt, CN=<staging or prod R3/R10/etc.>
#   notBefore=<today>  notAfter=<90 days from issue>
#   DNS:*.myvpn.databyte.co.za, DNS:myvpn.databyte.co.za

# 3.3.3 Cert is bound for the apex AND the wildcard
# (the wildcard SAN alone would NOT cover the apex myvpn.databyte.co.za; both SANs required)
#   - *.myvpn.databyte.co.za covers: za-cpt, za-jnb, fr-par, uk-lhr, etc.
#   - myvpn.databyte.co.za covers the apex (existing cert also covers this; redundant but harmless)

# 3.3.4 Renewal config was created
ssh root@vpn-prod-01 'cat /etc/letsencrypt/renewal/myvpn-wildcard.conf | grep -E "^(cert_name|authenticator|domains)"'
# Expected:
#   cert_name = myvpn-wildcard
#   authenticator = dns-cloudflare
#   domains = myvpn.databyte.co.za, *.myvpn.databyte.co.za
```

### 3.4 Step 4: certbot timer picks up the new cert automatically

No additional wiring needed. The certbot timer (systemd `certbot.timer` or similar) renews ALL certs in `/etc/letsencrypt/renewal/`. The new `myvpn-wildcard.conf` is auto-discovered.

```bash
# Verify the timer sees the new cert
ssh root@vpn-prod-01 'systemctl list-timers certbot*'
# Should show the next renewal time
```

The renewal will use the same DNS-01 + 30s propagation pattern as the existing cert. No intervention needed.

---

## 4. (Deferred) nginx vhost update — when a regional server actually needs the cert

This step is **NOT required for MR-1**. The cert can be issued and sit at `/etc/letsencrypt/live/myvpn-wildcard/` doing nothing. The nginx vhost change happens when MR-2 (second VPS) lands and the regional traffic needs to use the cert.

When that time comes:

```bash
# 4.1 Edit the vhost
ssh root@vpn-prod-01 'vim /etc/nginx/sites-available/vpn-portal'
# Add alongside the existing ssl_certificate line:
#   ssl_certificate     /etc/letsencrypt/live/myvpn-wildcard/fullchain.pem;
#   ssl_certificate_key /etc/letsencrypt/live/myvpn-wildcard/privkey.pem;
# Modern nginx (1.11+) handles multiple ssl_certificate lines via SNI selection.

# 4.2 Test the config
ssh root@vpn-prod-01 'nginx -t'
# Expected: syntax is ok, test is successful

# 4.3 Reload nginx
ssh root@vpn-prod-01 'systemctl reload nginx'
```

SNI behavior after the vhost change:
- Client connects to `vpn-portal.databyte.co.za` → existing cert serves it
- Client connects to `myvpn.databyte.co.za` → existing cert or wildcard cert (both cover it; nginx picks the first match)
- Client connects to `za-cpt.myvpn.databyte.co.za` → wildcard cert serves it

---

## 5. Rollback

If the cert is issued but something goes wrong (e.g., the cert is malformed, or you decided not to proceed with multi-region), revoke and remove it:

```bash
# 5.1 Revoke via certbot
ssh root@vpn-prod-01 'certbot revoke --cert-name myvpn-wildcard --reason unspecified'
# Expected: revocation successful

# 5.2 Delete the cert and renewal config
ssh root@vpn-prod-01 'certbot delete --cert-name myvpn-wildcard'
# Expected: Deleted all files relating to certificate myvpn-wildcard.

# 5.3 Verify the existing myvpn + vpn-portal cert is unaffected
ssh root@vpn-prod-01 'ls /etc/letsencrypt/live/ | grep -i myvpn'
# Expected: myvpn.databyte.co.za (no -0001, no -wildcard)
```

The rollback is safe and doesn't affect the working cert.

---

## 6. Open decisions

### 6.1 Two-cert vs one-cert (alternative)

This doc uses the **two-cert approach**: existing cert for `myvpn + vpn-portal`, NEW cert for `*.myvpn + myvpn`.

- **Pros:** minimal churn to the working cert; independent renewal cycles; easy to revoke the wildcard without touching the production cert
- **Cons:** two renewal timers, two cert files to track in nginx config

**Alternative: one unified cert** that covers `myvpn + vpn-portal + *.myvpn`. Single renewal, single cert path, but you have to `--expand` the existing cert which has a small risk of disrupting the working state.

**Recommendation:** two certs for the initial issuance (less risk). Re-evaluate unifying later if the operational overhead of two certs proves annoying.

### 6.2 When to update the nginx vhost (Section 4)

The cert can be issued and sit idle. The nginx vhost update only matters when a regional server needs to use the cert. So: issue the cert now (low risk, high value), update the vhost when MR-2 lands (zero risk, blocking).

---

## 7. References

- **TKT-036** — this ticket
- **ROADMAP.md "Multi-region rollout" subsection + MR-1 row** — parent planning context
- **VPS-XNEELO-DEPLOY.md** — for the regional VPS deploy (MR-2)
- **CLOUDFLARE-DNS.md** — Cloudflare API token setup (for the operator setting up a new token)
- **LE docs** — [Wildcard certificates will be free in 2018](https://letsencrypt.org/2017/07/06/wildcard-certificates-will-be-free-in-2018/) (2017-07-06 announcement), [DNS-01 challenge docs](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)
- **certbot docs** — [certbot dns-cloudflare plugin](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins)

---

## 8. Verification signature (post-execution)

After execution, capture this:

```text
Date executed (UTC):       ____________
Operator:                  Zun / Misha
Dry-run result:            PASS / FAIL
Real issuance result:      PASS / FAIL
Cert path:                 /etc/letsencrypt/live/myvpn-wildcard/
Cert SHA-256:              ____________
Cert notAfter:             ____________
Cert SAN:                  DNS:*.myvpn.databyte.co.za, DNS:myvpn.databyte.co.za
Renewal config created:    YES / NO
Nginx vhost updated:       YES / NO (deferred to MR-2)
Existing cert touched:     NO (verified by /etc/letsencrypt/live/ listing)
LE rate-limit consumed:    1 of 5 (this cert counts as 1)
```

---

## 9. Change log

| Rev | Date (UTC) | Author | Notes |
|---|---|---|---|
| v0.1.0 | 2026-10-15 11:15 | Misha 🐻 | DRAFT — initial procedure from TKT-036 work session. Pre-conditions all verified live against `vpn-prod-01` on 2026-10-15. Mirrors the certbot invocation from the prior session turn (chat session `322d9b8f-cfe4-45cb-8979-40495f8337aa`). |
