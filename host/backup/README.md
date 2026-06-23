# backup — VPN Portal Config + DB Backup to RustFS

Disaster-recovery backup of VPN portal secrets + DB to the LAN-attached
RustFS (S3-compatible) bucket.

## What's backed up

| File | Source | Notes |
|---|---|---|
| `vpn-portal.env` | `vpn-prod-01:/etc/vpn-portal.env` | Argon2id hashes, DB path, COOKIE_SECURE |
| `databyte.co.za.crt` | `vpn-prod-01:/etc/ssl/cloudflare/databyte.co.za.crt` | Cloudflare Origin CA cert |
| `databyte.co.za.key` | `vpn-prod-01:/etc/ssl/cloudflare/databyte.co.za.key` | Cloudflare Origin CA key (private!) |
| `ipsec.db` | `vpn-prod-01:/var/lib/strongswan/ipsec.db` | SQLite — customers, devices, sessions, audit_log |

**Total size:** ~330 KB. Negligible.

## Where it goes

```
rustfs:open-claw-push/vpn-portal-config/<YYYY-MM-DD>/
  vpn-portal.env
  databyte.co.za.crt
  databyte.co.za.key
  ipsec.db
```

## How to install

```bash
# 1. Install the script
sudo install -m 0755 backup-vpn-portal-config.sh /usr/local/bin/

# 2. Install systemd units
sudo install -m 0644 backup-vpn-portal-config.service /etc/systemd/system/
sudo install -m 0644 backup-vpn-portal-config.timer /etc/systemd/system/

# 3. Enable daily run
sudo systemctl daemon-reload
sudo systemctl enable --now backup-vpn-portal-config.timer
```

## Verify a backup

```bash
# Last run
sudo systemctl status backup-vpn-portal-config.service

# List today's backup
rclone ls rustfs:open-claw-push/vpn-portal-config/$(date -u +%Y-%m-%d)/

# Pull a backup for restore
rclone copy rustfs:open-claw-push/vpn-portal-config/2026-06-23/ /tmp/restore/
```

## Restore procedure

```bash
# 1. SSH to VPS
ssh vpn-prod-01

# 2. Pull backup files (locally first)
rclone copy rustfs:open-claw-push/vpn-portal-config/2026-06-23/ /tmp/restore/

# 3. Stop portal
sudo systemctl stop vpn-portal

# 4. Restore env
sudo cp /tmp/restore/vpn-portal.env /etc/vpn-portal.env
sudo chmod 600 /etc/vpn-portal.env
sudo chown root:root /etc/vpn-portal.env

# 5. Restore certs
sudo cp /tmp/restore/databyte.co.za.crt /etc/ssl/cloudflare/
sudo cp /tmp/restore/databyte.co.za.key /etc/ssl/cloudflare/
sudo chmod 644 /etc/ssl/cloudflare/databyte.co.za.crt
sudo chmod 600 /etc/ssl/cloudflare/databyte.co.za.key

# 6. Restore DB (atomic — backup the existing first)
sudo sqlite3 /var/lib/strongswan/ipsec.db ".backup /tmp/ipsec-pre-restore.db"
sudo cp /tmp/restore/ipsec.db /var/lib/strongswan/ipsec.db
sudo chown root:strongswan /var/lib/strongswan/ipsec.db
sudo chmod 664 /var/lib/strongswan/ipsec.db

# 7. Restart portal + charon
sudo systemctl restart vpn-portal
sudo systemctl restart charon  # or docker restart strongswan

# 8. Verify
curl -sk https://myvpn.databyte.co.za/api/health
```

## Lessons

### #81 — `sqlite3 .backup` is the only safe way to copy a live SQLite DB

Don't `cp` a SQLite DB while a process is writing to it. You WILL get
corruption (or a snapshot mid-transaction). `sqlite3 db ".backup dest"`
uses SQLite's online backup API which holds a read lock for the duration
of the copy and writes atomically.

### #82 — backup destinations need rotation too

Right now we keep one backup per day, forever. After ~3 years that's
~1000 directories, ~330 MB. Not a problem. But if secrets leak (e.g.
private key), you need a way to delete old backups. Document your
retention policy.
