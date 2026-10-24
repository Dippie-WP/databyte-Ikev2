# cert-monitor — TLS Certificate Expiry Monitoring

Monitor TLS cert expiry for the VPN portal, alerting at 90/60/30/14/7 days
remaining.

## Files

- **`cert-expiry-check.sh`** — Bash script. Checks:
  - `/etc/ssl/cloudflare/databyte.co.za.crt` (Cloudflare Origin CA, 15y)
  - `/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/strongswan-ca.crt.pem`
    (StrongSwan CA, 10y)

  Exit codes:
  - `0` — all certs > 90 days remaining
  - `1` — one or more certs in WARN window (< 90 days)
  - `2` — one or more certs in CRITICAL window (< 30 days)

- **`cert-expiry-check.service`** — systemd oneshot
- **`cert-expiry-check.timer`** — weekly timer (Sun 03:00 UTC)

## Install

```bash
sudo install -m 0755 cert-expiry-check.sh /usr/local/bin/
sudo install -m 0644 cert-expiry-check.service /etc/systemd/system/
sudo install -m 0644 cert-expiry-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cert-expiry-check.timer
```

## Verify

```bash
# Manual one-shot run
sudo /usr/local/bin/cert-expiry-check.sh

# Check next scheduled run
sudo systemctl list-timers cert-expiry-check.timer

# View last run output
sudo journalctl -u cert-expiry-check.service --no-pager
```

## Alerting

The script does NOT send external alerts by default. To wire it into your
monitoring stack:

### Prometheus (via node_exporter textfile collector)

Add to `cert-expiry-check.sh`:

```bash
TEXTFILE_DIR=/var/lib/prometheus/node-exporter
mkdir -p "$TEXTFILE_DIR"
cat > "$TEXTFILE_DIR/cert_expiry_days.prom" <<EOF
# HELP cert_expiry_days Days until TLS cert expires
# TYPE cert_expiry_days gauge
cert_expiry_days{label="origin_ca"}      $days_left_origin
cert_expiry_days{label="strongswan_ca"}  $days_left_strongswan
EOF
```

Then alert:

```yaml
- alert: TlsCertExpiringSoon
  expr: cert_expiry_days < 30
  for: 1h
  labels: { severity: critical }
```

### Telegram bot

Add to the script:

```bash
if (( EXIT_CODE >= 2 )); then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=⚠️ VPN cert expiring in <30 days: $label"
fi
```

## Lessons

### #79 — long-lived certs still need monitoring

A 15-year Cloudflare Origin CA cert is easy to forget. The cert might be
valid for 15 years, but:

- Cloudflare may deprecate the Origin CA root in 5-10 years, forcing a
  re-issue.
- A bug in `cert-expiry-check.sh` (wrong path, parse error) could silently
  skip checks.

Always check explicitly, never trust "it should be fine".

---

## site-cert-watch — per-site watcher + auto-renewal activator

Added 2026-10-24 for the per-site VPN model (TKT-041). The existing `cert-expiry-check.sh` watches two
long-lived CA certs and knows nothing about the LE certificate. Under the per-site model the LE
certificate IS the site's IKEv2 server identity, so it needs its own watcher.

### Files

- **`site-cert-watch.sh`** — per-site watcher. Three jobs:
  1. **WATCH** — expiry of this site's LE cert and this site's strongswan CA.
  2. **MATCH** — the cert strongswan is actually SERVING (`x509/server.pem`, which is what
     `certs = server.pem` loads) must be byte-identical to this lineage's cert on disk. Catches a deploy
     hook that failed to install the renewed cert, and catches a renewal on a DIFFERENT lineage
     overwriting the identity.
  3. **ACTIVATE** — ensure auto-renewal is armed: `certbot.timer` enabled and active, a renewal config
     present for this lineage, and — when the cert is inside the renewal window — an actual targeted
     renewal, then re-verification.

  Exit codes: `0` ok · `1` WARN · `2` CRIT.

- **`site-cert-watch.service`** — systemd oneshot, reads `SITE_FQDN` from `/etc/default/site-cert-watch`.
- **`site-cert-watch.timer`** — 4x daily (01:00, 07:00, 13:00, 19:00 UTC), deliberately offset from
  `certbot.timer` (00:00 / 12:00 UTC) so a run lands AFTER a certbot run and can verify its result.

### Per-site configuration

```
# /etc/default/site-cert-watch
SITE_FQDN=za-cpt-01-vpn.databyte.co.za
```

The same file is read by the certbot deploy hook, so the site identity is configured in exactly one place.
Without it the hook refuses to install any certificate (exit 1) — failing closed is deliberate.

### Environment overrides

| Variable | Default | Meaning |
|---|---|---|
| `SITE_FQDN` | — (required) | this site's canonical public name |
| `SITE_LINEAGE` | `$SITE_FQDN` | certbot lineage name, if the host names it differently |
| `CHECK_ONLY` | `0` | `1` = report only: never enables a timer, never renews |
| `ENABLE_TIMER` | `1` | `0` = report the timer state but never enable it |
| `WARN_DAYS` | `30` | LE cert warn window |
| `CRIT_DAYS` | `14` | LE cert critical window |
| `RENEW_WINDOW_DAYS` | `30` | inside this window the watcher attempts a targeted renewal |

### Install

```bash
sudo install -m 0755 site-cert-watch.sh /usr/local/bin/
sudo install -m 0644 site-cert-watch.service /etc/systemd/system/
sudo install -m 0644 site-cert-watch.timer /etc/systemd/system/
sudo printf 'SITE_FQDN=<SITE_FQDN>\n' > /etc/default/site-cert-watch
sudo systemctl daemon-reload
sudo systemctl enable --now site-cert-watch.timer
```

### Verify

```bash
# report only, no mutation
sudo SITE_FQDN=<SITE_FQDN> CHECK_ONLY=1 /usr/local/bin/site-cert-watch.sh

sudo /usr/local/bin/site-cert-watch.sh          # full run
sudo journalctl -u site-cert-watch.service --no-pager
sudo systemctl list-timers site-cert-watch.timer
```
