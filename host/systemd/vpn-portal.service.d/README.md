# vpn-portal.service.d — systemd drop-in overrides

Drop-in files that override /etc/systemd/system/vpn-portal.service defaults
on the VPS. Apply by copying into `/etc/systemd/system/vpn-portal.service.d/`
then `systemctl daemon-reload && systemctl restart vpn-portal`.

## Files

- **`runtime-dir.conf`** — loosens `RuntimeDirectoryMode` from `0750` (default
  in the main unit) to `0755` so the nginx worker (running as `www-data`) can
  traverse `/run/vpn-portal/` to reach `gunicorn.sock`. The socket file itself
  remains mode 0777.

- **`readwrite-paths.conf`** — adds paths to the systemd `ReadWritePaths=`
  list. Required because the main unit has `ProtectSystem=strict` which
  mounts the root filesystem read-only. Without these, the portal cannot:
  - `/var/log/vpn-portal` — write gunicorn log files
  - `/var/lib/strongswan` — read SQLite DB (charon's path, also portal reads via SSH)
  - `/var/lib/vpn-portal` — write SSH `known_hosts` (StrictHostKeyChecking=accept-new
    requires adding host keys; first-run fails with "Read-only file system"
    if this directory is not writable)

## Why drop-ins instead of editing the main unit

The main unit lives in the repo (`host/vpn-portal/systemd/vpn-portal.service`).
Drop-ins keep environment-specific overrides separate from the canonical unit,
making it easier to deploy the same unit across LXC 903 (lab) + VPS (prod) with
different hardening needs.

## Order of operations on the VPS

```bash
sudo mkdir -p /etc/systemd/system/vpn-portal.service.d
sudo cp runtime-dir.conf readwrite-paths.conf /etc/systemd/system/vpn-portal.service.d/
sudo systemctl daemon-reload
sudo systemctl restart vpn-portal
sudo systemctl cat vpn-portal | grep -E "RuntimeDirectoryMode|ReadWritePaths"
```