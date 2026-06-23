# AIDE File Integrity Monitoring

Monitors critical portal files for unauthorized changes. Run weekly via systemd timer.

## Files in this dir

| File | Destination | Purpose |
|------|-------------|---------|
| `aide.conf.d/databyte.conf` | `/etc/aide/aide.conf.d/databyte.conf` | What to monitor (extends default `/etc/aide/aide.conf`) |
| `aide-report.sh` | `/usr/local/bin/aide-report.sh` | Wrapper script: runs aide, rotates reports, emits JSON |
| `aide-check.service` | `/etc/systemd/system/aide-check.service` | oneshot service that calls the script |
| `aide-check.timer` | `/etc/systemd/system/aide-check.timer` | weekly timer (Sun 02:30 UTC) |

## Install

```bash
# Install aide package
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y aide aide-common

# Copy files
sudo install -m 0644 -o root -g root aide.conf.d/databyte.conf /etc/aide/aide.conf.d/databyte.conf
sudo install -m 0755 -o root -g root aide-report.sh /usr/local/bin/aide-report.sh
sudo install -m 0644 -o root -g root aide-check.service /etc/systemd/system/aide-check.service
sudo install -m 0644 -o root -g root aide-check.timer /etc/systemd/system/aide-check.timer

# Enable timer
sudo systemctl daemon-reload
sudo systemctl enable --now aide-check.timer
```

## First run (initialise baseline)

```bash
sudo /usr/local/bin/aide-report.sh
# On first run this creates /var/lib/aide/aide.db (the baseline).
# Subsequent runs compare against this baseline.
```

## Verify timer

```bash
systemctl list-timers aide-check.timer
# Should show next run time and last run
```

## Review reports

```bash
# Latest human-readable report
sudo less /var/log/aide/aide-report-latest.txt

# Latest machine-parseable JSON
sudo cat /var/log/aide/aide-report-latest.json | jq .

# All reports (oldest first)
sudo ls -la /var/log/aide/aide-report-*.txt
```

## What's monitored

| Path | Why |
|------|-----|
| `/opt/vpn-portal/` | Portal app code — must match repo |
| `/opt/vpn-portal/www/` | Static assets + HTML shells |
| `/etc/nginx/sites-available/vpn-portal` | nginx vhost config |
| `/etc/nginx/conf.d/portal-limits.conf` | rate limits + cache map |
| `/etc/systemd/system/vpn-portal.service.d/` | systemd drop-ins |
| `/etc/vpn-portal.env` | secrets (perms only — never rehash on every run) |
| `/etc/ssl/cloudflare/` | Origin CA cert + key |
| `/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/` | strongSwan CA cert |
| `/var/lib/strongswan/ipsec.db` | DB presence (not content — it changes constantly) |

## Responding to alerts

When AIDE reports changes, classify them:

1. **Expected (you just deployed)** — update baseline:
   ```bash
   sudo mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
   # Done. Next run will compare against the new baseline.
   ```

2. **Unexpected (investigation needed)** — DO NOT update baseline. Read
   the report, figure out what changed, who did it, when. If it's an
   attack: rotate secrets, check logs, restore from backup.

3. **False positive (e.g. log file got picked up)** — update the config
   in `aide.conf.d/databyte.conf` to exclude the path, then update
   baseline.

## Tuning

To add a new path to monitor, edit `aide.conf.d/databyte.conf` and add:

```
/path/to/monitor Full
```

then update baseline:

```bash
sudo mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
```
