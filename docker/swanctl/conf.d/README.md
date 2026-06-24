# swanctl/conf.d/

Connection config files. swanctl.conf `include`s everything in this dir.

## Files

- `rw-eap.conf.template` — Primary: EAP-MSCHAPv2 + server cert (Let's Encrypt in production)
- `rw-psk.conf.template` — Fallback: PSK only (iOS — currently broken, see ISSUES-LOG)

## Deploy-time setup

The `.conf.template` files in git are STRUCTURE only. They have:

- `connections { ... }` blocks — committed (auth method, proposals, etc.)
- Commented-out `secrets { ... }` blocks — REPLACE per host

The live `rw-eap.conf` and `rw-psk.conf` (without `.template`) are
**generated per host** by `scripts/bootstrap-xneelo.sh`, which sed-replaces
the `id = "vpn.homelab.local"` placeholder with `${SERVER_ID}` and appends
real `secrets { eap-X { ... } }` blocks. They are in `.gitignore` and
should never be committed.

### Self-signed CA cert path (legacy / lab)

If you're not using Let's Encrypt (lab, air-gapped), use
`scripts/gen-certs.sh` to generate `server.crt.pem` and the strongSwan
CA, then edit `rw-eap.conf.template` to keep `certs = server.crt.pem`
and `cacerts = strongswan-ca.crt.pem` (the pre-v1.5.0 default).

### Let's Encrypt path (production, v1.5.0+)

1. Provision cert via certbot DNS-01 (see `host/letsencrypt/README.md`)
2. The `host/letsencrypt/deploy-hook.sh` writes LE cert chain into
   `x509/server.pem` + `x509ca/le-*.pem` automatically on every renewal
3. `rw-eap.conf` already references `certs = server.pem` (no `cacerts`)
   — no further config changes needed

To deploy on a new host:

```bash
# 1. Run the bootstrap (generates rw-eap.conf from template)
sudo bash scripts/bootstrap-xneelo.sh

# 2. (If using LE) install certbot + DNS-01 plugin + Cloudflare token
#    See host/letsencrypt/README.md for the full sequence

# 3. Restart strongSwan
docker compose --profile vpn restart
```

## Generating secrets

```bash
# 32-byte base64 PSK
openssl rand -base64 32

# 16-byte base64 password
openssl rand -base64 16
```

## Notes

- Live `rw-eap.conf` and `rw-psk.conf` (without `.template`) are in
  `.gitignore` — never commit them
- secrets blocks must stay inside this file; the `secrets {}` syntax
  in the top-level `swanctl.conf` doesn't get loaded by `include conf.d/*.conf`
- v1.5.0+ uses Let's Encrypt (no strongSwan CA needed). Older installs
  with `certs = server.crt.pem` + `cacerts = strongswan-ca.crt.pem`
  still work (charon still has the CA + self-signed cert on disk for
  rollback), but the recommended path going forward is LE.
