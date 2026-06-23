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
