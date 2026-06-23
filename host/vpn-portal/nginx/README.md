# databyte VPN Portal — nginx config (CP4 + CP5)

Production nginx site config for vpn-prod-01 (154.65.110.44).

## Files

- **`vpn-portal.conf`** — Site config, installed at `/etc/nginx/sites-enabled/vpn-portal`
  - HTTP → HTTPS 301 redirect on :80
  - HTTPS on :443 with Cloudflare Origin Cert (15y)
  - TLS 1.2 + 1.3 only (modern Mozilla intermediate ciphers)
  - HTTP/2 enabled
  - Full security headers: HSTS (2y preload), CSP (strict, no inline), X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, COOP
  - Rate limits: 5r/m for login, 60r/m for API
  - Connection limit: 20 per IP
  - Static assets at `/static/` — 7-day immutable cache
  - Cert distribution at `/certs/` — 24-hour cache (CP5)
  - Unix socket proxy → /run/vpn-portal/gunicorn.sock

- **`portal-limits.conf`** — HTTP-scope directives, installed at `/etc/nginx/conf.d/portal-limits.conf`
  - `limit_req_zone` + `limit_conn_zone` declarations
  - `log_format portal` (JSON-friendly, parseable by Promtail/Loki later)

## Install (manual, one-time)

```bash
scp vpn-portal.conf vpn-prod-01:/tmp/
scp portal-limits.conf vpn-prod-01:/tmp/

ssh vpn-prod-01 'sudo bash'
sudo mv /tmp/vpn-portal.conf /etc/nginx/sites-available/vpn-portal
sudo mv /tmp/portal-limits.conf /etc/nginx/conf.d/portal-limits.conf
sudo chmod 644 /etc/nginx/sites-available/vpn-portal /etc/nginx/conf.d/portal-limits.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/vpn-portal /etc/nginx/sites-enabled/vpn-portal
sudo mkdir -p /var/log/nginx /var/www/acme
sudo nginx -t
sudo systemctl reload nginx
```

## Cert storage (one-time, not in repo)

```bash
# /etc/ssl/cloudflare/databyte.co.za.crt (mode 644, root:root)
# /etc/ssl/cloudflare/databyte.co.za.key (mode 600, root:root)
# Generated in Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate
```

## Companion systemd drop-in (one-time)

`/etc/systemd/system/vpn-portal.service.d/runtime-dir.conf`:
```ini
[Service]
# Allow nginx (www-data) to traverse /run/vpn-portal/ to reach gunicorn.sock
# Socket file itself remains mode 0777; only directory traversal needs loosening
RuntimeDirectoryMode=0755
```

## Verification

```bash
# From VPS (or anywhere with Cloudflare proxy access):
curl -skI https://myvpn.databyte.co.za/api/health
curl -skI https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem
curl -skI https://myvpn.databyte.co.za/static/app.js
# Expect: HSTS + CSP + X-Frame + X-Content + Referrer + Permissions + COOP headers
```

## Operational notes

- **Cloudflare Edge Only:** Origin Cert is for Cloudflare → Origin. The Xneelo firewall should restrict 443 to Cloudflare IP ranges only — not direct internet. Random public IPs (e.g. OC host) will be blocked; this is intentional.
- **Cert renewal:** Origin Cert is 15y (expires 2041-06-19). Track manually — Cloudflare does NOT send expiration notifications.
- **Cookie flags:** When HTTPS is live, `/etc/vpn-portal.env` must have `COOKIE_SECURE=true`. Portal emits `Secure; HttpOnly; SameSite=lax; Path=/; Max-Age=28800`.
- **No HSTS preload yet:** Submit to https://hstspreload.org only after 1+ week of stable operation.
