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

## Install — per-lineage `renew_hook` (as deployed on JHB 2026-10-24 14:42 UTC)

```bash
sudo install -m 0755 deploy-hook.sh /usr/local/sbin/databyte-strongswan-deploy-hook.sh
```

Then bind it to the lineage that owns this host's IKEv2 identity, in that
lineage's renewal conf **only**:

```
# /etc/letsencrypt/renewal/<SITE_FQDN>.conf  ->  [renewalparams]
renew_hook = /usr/local/sbin/databyte-strongswan-deploy-hook.sh
```

**Do not** put the hook in `/etc/letsencrypt/renewal-hooks/deploy/`. That
directory is **global**: certbot runs everything in it after *any* successful
renewal on the host, so a renewal of an unrelated lineage would install the
wrong certificate into the strongswan x509 directory and reload charon with it
— on the live JHB host that very thing happened on 2026-09-03, and charon held
`CN=adminer.databyte.co.za` as the IKEv2 server identity for ~48 seconds.

certbot 4.0.0 supports the per-lineage form natively: `_internal/hooks.py`
`renew_hook()` runs the directory hooks first and then `config.renew_hook`, and
`renewal.py` restores `renew_hook` from the conf's `[renewalparams]` on every
renewal — `storage.write_renewal_config()` preserves it when certbot rewrites
the conf.

**Live state on `vpn-prod-01` (JHB, 2026-10-24 14:42 UTC):** hook at
`/usr/local/sbin/databyte-strongswan-deploy-hook.sh`, `/etc/letsencrypt/renewal-hooks/deploy/`
is EMPTY, `renew_hook` set in `myvpn.databyte.co.za.conf` only. `adminer.databyte.co.za.conf`
has no `renew_hook`, so it can never touch strongswan.

This repo copy additionally carries the **per-host lineage guard** (see below)
for hosts that carry more than one lineage. The guard runs BEFORE any file is
written, compares the lineage NAME, and exits 0 on a foreign lineage.

## Manual run (test only)

```bash
sudo RENEWED_LINEAGE=/etc/letsencrypt/live/myvpn.databyte.co.za \
     RENEWED_DOMAINS=myvpn.databyte.co.za \
     /usr/local/sbin/databyte-strongswan-deploy-hook.sh
```

On a host using the guarded copy, `SITE_FQDN` must resolve first (it reads
`/etc/default/site-cert-watch` if the variable is unset), otherwise the hook
refuses with exit 1 rather than installing a certificate it cannot attribute
to a site.

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
