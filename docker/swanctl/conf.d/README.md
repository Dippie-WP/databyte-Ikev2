# swanctl/conf.d/

Connection config files. swanctl.conf `include`s everything in this dir.

## Files

- `rw-eap.conf.template` — Primary: EAP-MSCHAPv2 + server cert
- `rw-psk.conf.template` — Fallback: PSK only (iOS — currently broken, see ISSUES-LOG)

## Deploy-time setup

The `.conf.template` files in git are STRUCTURE only. They have:

- `connections { ... }` blocks — committed (auth method, proposals, etc.)
- Commented-out `secrets { ... }` blocks — REPLACE per host

To deploy on a new host:

```bash
# 1. Copy templates to live configs
cp rw-eap.conf.template rw-eap.conf
cp rw-psk.conf.template rw-psk.conf

# 2. Edit each: uncomment the secrets block, set real values
$EDITOR rw-eap.conf
$EDITOR rw-psk.conf

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
  `.gitignore` so they never get committed
- secrets blocks must stay inside this file; the `secrets {}` syntax
  in the top-level `swanctl.conf` doesn't get loaded by `include conf.d/*.conf`
