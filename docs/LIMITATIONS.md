# Known limitations

Moved out of the README front page (2026-10-30). These are current, known constraints.

## Critical

1. **charon-cmd 5.9.5** in the test environment is incompatible with the 6.0.7 server's EAP-Identity flow. Real load test deferred (requires all 3 real clients online simultaneously, or a Python VICI client).
2. **5G IP rotation** can cause a brief IKE_SA re-auth on iOS (not pure MOBIKE; functionally equivalent).
3. **MSS clamp at 1260 is required** for 5G carriers. Lives in `host/iptables/rules.v4`. Forgetting this → iana.org-style timeouts.
4. **EAP creds are in plaintext** in the `swanctl.conf` `secrets` block. Acceptable for personal use; commercial needs EAP-TLS.
5. **No CRL/OCSP.** Server cert has 1-year validity, manual rotation.
6. **charon-log** lives inside the container — must be bind-mounted to the host for log shipping (pattern in `host/strongswan/strongswan.d/debug.conf`).

See also [ARCHITECTURE.md](ARCHITECTURE.md) "Known limitations" and [5D-BANDWIDTH-LIMITING.md](5D-BANDWIDTH-LIMITING.md).

## Orphaned tags on origin

Checkout `main` — it is canonical. Some tags on origin pre-date the 2026-06-26 recovery baseline and point at older commits; treat them as orphaned and do not build from them:

- `archive-v1.8.0-removed-2026-07-01`, `archive-v1.9.0-sse-removed-2026-07-01` (point at the destructive-replay branch)
- `v2.3.0`, `v2.6.0`, `v2.7.0`–`v2.7.2`
