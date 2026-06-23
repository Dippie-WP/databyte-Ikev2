# OPS-LOG-MANAGEMENT — VPN Portal Log Retention

> **Added 2026-06-23 (LOW #3 polish).** Documents what logs the portal generates,
> where they live, how long they're kept, and how to recover space if needed.

## Quick reference

| Log | Path | Owner | Rotated by | Retention |
|---|---|---|---|---|
| **Portal (gunicorn)** | journald (`vpn-portal.service`) | systemd-journal | journald default (size-based) | 4 GB max, 15% free space cap |
| **nginx access** | `/var/log/nginx/vpn-portal.access.log` | root:adm | `/etc/logrotate.d/nginx` (Debian) | rotate daily, keep 14 |
| **nginx error** | `/var/log/nginx/vpn-portal.error.log` | root:adm | `/etc/logrotate.d/nginx` (Debian) | rotate daily, keep 14 |
| **fail2ban** | `/var/log/fail2ban.log` | root:adm | `/etc/logrotate.d/fail2ban` (Debian) | rotate weekly, keep 4 |
| **AIDE reports** | `/var/log/aide/aide-report-YYYYMMDDTHHMMSSZ.txt` | `_aide:adm` | `aide-report.sh` (manual rotation, keeps last 12) | 12 reports ≈ 3 months at weekly cadence |
| **AIDE JSON** | `/var/log/aide/aide-report-YYYYMMDDTHHMMSSZ.json` | `_aide:adm` | `aide-report.sh` | 12 (same cadence) |

## What is NOT logged

- **No plaintext passwords.** Operator + portal logins only log the failure
  mode + IP, not the attempted password.
- **No customer traffic content.** Per-VIP byte counters only (in
  `/var/lib/strongswan/attr-sql` SQLite tables), used for quota billing.
- **No full request bodies.** nginx access log has UA + path + status, not
  POST bodies.

## journald tuning (recommended)

The portal log lives in journald. Default Debian journald has no size limit
and can fill `/var/log` if a runaway process spams logs. The Xneelo VPS
shows 942 MB used so far (acceptable, but worth tightening).

To set a hard cap (recommended for production):

```ini
# /etc/systemd/journald.conf
[Journal]
SystemMaxUse=500M            # hard cap for /var/log/journal
SystemKeepFree=1G            # ensure 1 GB free on the disk
MaxRetentionSec=90day        # drop entries older than 90 days
```

Then:

```bash
sudo systemctl restart systemd-journald
sudo journalctl --vacuum-size=500M
sudo journalctl --verify
```

⚠️ **Apply only after a quiet period** — restarting journald while it's
busy can cause transient logging gaps. Off-peak hour recommended.

## nginx logrotate (Debian default — no change)

`/etc/logrotate.d/nginx` ships with Debian and rotates `/var/log/nginx/*.log`
daily with keep-14. Our JSON access log rotates correctly because it ends
in `.log`.

## fail2ban logrotate (Debian default — no change)

`/etc/logrotate.d/fail2ban` ships with Debian and rotates
`/var/log/fail2ban.log` weekly with keep-4. The current
`/var/log/fail2ban.log` shows ~30 KB — adequate for 24h of bans.

## AIDE rotation (custom — `aide-report.sh`)

The AIDE report runner (`host/aide/aide-report.sh`) handles its own
rotation:

```bash
RETENTION_COUNT=12  # keep last 12 weekly reports
```

Manual cleanup if needed:

```bash
# List reports
ls -la /var/log/aide/

# Drop old ones (keeps last 12)
cd /var/log/aide
ls -t aide-report-*.txt | tail -n +13 | xargs -r rm --
ls -t aide-report-*.json | tail -n +13 | xargs -r rm --
```

## Manual cleanup commands

If disk fills up unexpectedly:

```bash
# 1. Journald
sudo journalctl --vacuum-size=200M          # shrink to 200 MB
sudo journalctl --vacuum-time=30days        # drop entries older than 30 days

# 2. nginx (force rotate now)
sudo logrotate -f /etc/logrotate.d/nginx

# 3. fail2ban
sudo logrotate -f /etc/logrotate.d/fail2ban

# 4. AIDE
cd /var/log/aide
ls -t aide-report-*.{txt,json} | tail -n +4 | xargs -r rm --   # keep last 3 only
```

## How to alert on log growth

No alert configured by default. If disk usage is a concern, add to
monitoring (Prometheus node_exporter `node_filesystem_avail_bytes`):

```yaml
# Alert: less than 1 GB free on /var/log
- alert: LogDiskSpaceLow
  expr: node_filesystem_avail_bytes{mountpoint="/var/log"} < 1073741824
  for: 5m
  labels:
    severity: warning
```

## Lessons

### #77 — portal logs go to journald, not a file

When `systemctl status` shows the gunicorn worker output, that's journald.
Don't `grep /var/log/vpn-portal/*` — there are no files. Use
`journalctl -u vpn-portal`.

### #78 — AIDE reports are owned by `_aide`, not root

Manual cleanup of `/var/log/aide/` requires either `sudo` or being in the
`adm` group. Forgetting this leaves "Permission denied" errors that look
like real failures.

---

**Last updated:** 2026-06-23 (Misha) — Initial documentation. LOW #3 polish.
