# host/ssl — ISRG Root CA certs for LE chain verification

## Why this exists

Debian 13's `ca-certificates` package (version 20250419) does NOT include
Let's Encrypt's `ISRG Root X1` or `ISRG Root X2`. This is because Mozilla
removed X1 from default trust after January 2025 (in line with the LE
"Y" hierarchy transition), and Debian tracks Mozilla.

For **IKEv2** this doesn't matter — clients verify the server cert against
their own trust stores (iOS/Android/Windows/macOS all have ISRG X1/X2).

But for **on-server operations** that touch the LE chain — `openssl verify`,
charon's OCSP/CRL fetches, `curl` against LE-protected APIs, the revocation
monitor — the missing roots cause `error 20 at 0 depth lookup: unable to get
local issuer certificate`.

## Files

| File | Purpose |
|------|---------|
| `ISRG_Root_X1.crt` | ISRG Root X1 (self-signed, expires 2035). Source: Debian 13 `/etc/ssl/certs/ISRG_Root_X1.pem`, validated against letsencrypt.org canonical source — exact byte match. |
| `ISRG_Root_X2.crt` | ISRG Root X2 (self-signed, expires 2040). Source: Debian 13 `/etc/ssl/certs/ISRG_Root_X2.pem`, SHA-256 fingerprint matches the published `69:72:9B:8E:15:A8:6E:FC:17:7A:57:AF:B7:17:1D:FC:64:AD:D2:8C:2F:CA:8C:F1:50:7E:34:45:3C:CB:14:70`. |
| `SHA256SUMS` | File integrity hashes (`openssl dgst -sha256`). Cert fingerprints are in the header comments. |
| `install-isrg-roots.sh` | Idempotent installer — copies the certs to `/usr/local/share/ca-certificates/` and runs `update-ca-certificates --fresh`. |

## Install

```bash
sudo /opt/strongswan-vpn-gateway/host/ssl/install-isrg-roots.sh
```

Then verify:

```bash
# LE leaf verifies via chain.pem + trust store:
openssl verify \
    -CAfile /etc/ssl/certs/ca-certificates.crt \
    -untrusted /etc/letsencrypt/live/myvpn.databyte.co.za/chain.pem \
    /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem
# Expected: "...: OK"

# Symlinks exist:
ls -l /etc/ssl/certs/ISRG_Root_X*.pem
```

## Renewal tracking

ISRG Root X1 expires **2035-06-04**.
ISRG Root X2 expires **2040-09-17**.
By the time they expire, we'll have moved to whatever the next LE hierarchy
is. No tracking required.
