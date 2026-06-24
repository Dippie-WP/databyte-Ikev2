# host/letsencrypt — LE cert renewal automation for strongSwan

## Why this exists

The VPN portal uses a Cloudflare Origin Cert (no public trust, edge-only).
But the **IKEv2 server** in `strongswan` needs a publicly-trusted cert so
that iOS/Android/Windows/macOS clients can verify it natively without
needing to install our self-signed strongSwan CA on every device.

After the 2025-11-24 Let's Encrypt "Y" hierarchy transition, fullchain.pem
contains **3-4 certs** (leaf → intermediate → cross-signed root → root).
Per GitHub issue strongswan/strongswan#3072, charon loads only ONE cert per
file in `/etc/swanctl/x509ca/` — so we MUST split the chain into separate
files before `swanctl --load-creds`.

## Files

| File | Purpose |
|------|---------|
| `deploy-hook.sh` | certbot deploy hook. Runs after renewal, splits fullchain.pem into x509/x509ca, writes key, reloads charon creds. No container restart. Idempotent. |

## Install on vpn-prod-01

```bash
sudo install -m 0755 deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/
```

Certbot auto-detects anything in `/etc/letsencrypt/renewal-hooks/deploy/` and
runs it after each successful renewal with `RENEWED_LINEAGE` and
`RENEWED_DOMAINS` set.

## Manual run (test only)

```bash
sudo RENEWED_LINEAGE=/etc/letsencrypt/live/myvpn.databyte.co.za \
     RENEWED_DOMAINS=myvpn.databyte.co.za \
     /etc/letsencrypt/renewal-hooks/deploy/deploy-hook.sh
```

## Verify

```bash
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-certs --type x509 | head -30
```

Should show `subject: "CN=myvpn.databyte.co.za"` issued by `Let's Encrypt YE2`,
plus the chain certs in the CA list.

## Why we don't restart the container

`docker exec strongswan swanctl --load-creds` refreshes charon's in-memory
cert/key store from disk without dropping existing IKE SAs. The 8 active
mobile clients keep their tunnels up. Only new negotiations see the new cert.

For a full cert refresh (e.g., to clear stale entries after migrating away
from the self-signed CA), do `docker restart strongswan`.

## Cert references in swanctl.conf

Currently `conf.d/rw-eap.conf` has `certs = server.crt.pem`. The hook writes
the LE cert to **both** `server.crt.pem` (legacy, current ref) and
`server.pem` (new canonical). After v1.5.0 is verified, switch to
`certs = server.pem` and remove `cacerts = strongswan-ca.crt.pem` (no longer
needed with publicly-trusted cert).
