# Security Policy

## Supported Versions

This is a single-operator personal project. The **`main` branch** is the only supported version. Released tags (`v*`) are **not** patched — they are historical snapshots.

| Branch / Tag | Supported |
|--------------|-----------|
| `main` | ✅ Yes |
| `v*` tags | ❌ No (point-in-time snapshots) |
| Anything else | ❌ No |

If you depend on a tagged release, pin to the commit and follow `main` for fixes.

## Reporting a Vulnerability

**DO NOT file a public GitHub issue for security vulnerabilities.**

This project ships a production VPN gateway with cryptographic material. Public disclosure of a vuln before a fix is available puts every active customer at risk.

### Private disclosure channels

| Severity | Channel | Notes |
|----------|---------|-------|
| **Critical** (RCE, auth bypass, credential/CA/key exposure) | Telegram DM `@zunu172` | Fastest path. 24/7. |
| **High** (data leak, session hijack, quota bypass) | Telegram DM `@zunu172` | < 48h response target. |
| **Medium / Low** | Open a GitHub issue with the `security` label | OK to disclose after fix is merged. |

Include:
1. **Component affected** (charon / portal / quota-monitor / ipBan / docker / nginx / certs)
2. **Steps to reproduce** (or proof-of-concept)
3. **Impact assessment** (what can an attacker do?)
4. **Environment** (commit SHA, host type — lab 903 / VPS prod / other)

PGP key: not provided (personal project; Telegram DM is the trust anchor).

## Response Targets

| Severity | First response | Target fix | Public disclosure |
|----------|----------------|------------|-------------------|
| Critical (auth bypass, RCE, key compromise) | < 24 hours | < 7 days | After fix + 7-day grace |
| High (data leak, privilege escalation) | < 48 hours | < 14 days | After fix + 3-day grace |
| Medium | < 1 week | < 30 days | With next release notes |
| Low | < 2 weeks | Best effort | With next release notes |

## Past Incidents

| Date | Severity | Summary | Resolution |
|------|----------|---------|------------|
| 2026-06-09 | High | R2 credentials compromised in repo history | `SECURITY-TOKENS-REVOKED.md`; full purge, RustFS migration |
| 2026-06-25 | Low | GitHub PATs leaked via shell history | `SECURITY-TOKENS-REVOKED.md`; rotated + rotated again |

Full post-mortems kept in `docs/INCIDENT-*.md` after each event.

## Cryptography

| Component | Algorithm | Notes |
|-----------|-----------|-------|
| Server cert | RSA-2048 | ECDSA P-256 rejected by iOS 18+ IKEv2 — must be RSA |
| Signature | PKCS#1 v1.5 (sha256WithRSAEncryption) | RSASSA-PSS deferred to certbot migration (LE v2.x) |
| EAP creds | NTLM hash in `swanctl.conf` `secrets` block | File-based; acceptable for single-operator |
| Customer portal sessions | Argon2id (password) + SQLite opaque token | 1-hour sliding TTL (Bug #1 fix v1.3.1) |
| HTTPS (VPS) | Cloudflare Edge Cert (RSA-2048) + Origin Cert | TLS termination at CF, Origin Cert on nginx |
| DB | SQLite | File at `/var/lib/strongswan/ipsec.db` — root-only mode 0640 |
| Backup encryption | None (RustFS bucket is private) | Acceptable; bucket is single-tenant, network-isolated |

**Known limitations** (intentional, not vulnerabilities):
1. **EAP creds in plaintext** in `swanctl.conf` on the host. Acceptable for single-operator; commercial deployments need EAP-TLS with per-device client certs.
2. **No CRL/OCSP** — server cert has 1-year validity, manual rotation.
3. **No rate-limit on `/api/portal/login`** other than 5/IP/min after Bug #1 fix. Stronger limits require a WAF or fail2ban portal-login jail (see CP7 backlog).
4. **CA private key** committed to `docker/swanctl/private/strongswan-ca-key.pem` on production hosts. This is intentional (single-operator) but anyone who clones the repo and gets a working host can mint certs. **Production hosts MUST have key-permission hardening** (`chmod 600`, `chown root:root`); CI smoke check verifies perms.

## Dependencies

Security updates applied via **Dependabot** (see `.github/dependabot.yml`).

| Source | Cadence | Review SLA |
|--------|---------|------------|
| pip (host/vpn-portal/requirements.txt) | Weekly (Monday) | High/Critical < 7 days |
| GitHub Actions | Weekly | High/Critical < 7 days |
| Docker base images | Weekly | High/Critical < 7 days |

## Security Contacts

- **Project owner**: Zun (@zunu172 on Telegram, github Dippie-WP)
- **Built with**: Misha (AI coding agent, no human access)

## Acknowledgements

Thanks to the strongSwan community, Cloudflare (Origin Cert free tier), and Let's Encrypt (planned migration).

---

_Last reviewed: 2026-06-28_
