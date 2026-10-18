# host/ssl/ — Public CA root certificates + install helpers

These are **public** certificates from Let’s Encrypt (ISRG) and helper
scripts to install them into the system trust store.

## Files

- `ISRG_Root_X1.crt` — ISRG Root X1 (intermediate cross-signed by ISRG / DST)
- `ISRG_Root_X2.crt` — ISRG Root X2 (EC, ECDSA chain)
- `install-isrg-roots.sh` — Installs both into `/usr/local/share/ca-certificates/`
  and runs `update-ca-certificates`. Required on Debian 13 (ca-certificates
  20250419+) which removed ISRG roots from default trust.

## Why this lives in the repo

The charon daemon on the VPN server performs LE certificate chain
validation when clients connect with a Let’s Encrypt server cert. Without
ISRG roots in the local trust store, `openssl verify` fails locally and
charon CRL/OCSP checks against the LE chain fail.

These certs are **public** — issued by ISRG and freely available. Safe
to commit.

## Production state (vpn-prod-01)

Installed at: `/usr/local/share/ca-certificates/ISRG_Root_X1.crt`,
`/usr/local/share/ca-certificates/ISRG_Root_X2.crt`.

Re-run `sudo bash host/ssl/install-isrg-roots.sh` after a fresh
`apt upgrade` that bumps `ca-certificates` (Debian tracks Mozilla NSS).
