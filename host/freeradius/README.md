# host/freeradius — FreeRADIUS operator overlay (v2.2.0+)

This directory tracks FreeRADIUS 3.0 config overlays that diverge from the
Debian stock defaults. Files in here are the **canonical source** for what
should be on `vps-01` — `provision-freeradius.sh` applies them idempotently.

## Why this directory exists

On 2026-07-13 we debugged six distinct bugs in one session that all combined
to "radacct is empty + devices.last_seen_v4 has bracket-encoded IPs":

1. `mods-available/sql_last_seen` was missing `sql_user_name` directive
2. Charon upstream `accounting = no` (covered in `docker/strongswan.d/`)
3. Charon upstream `station_id_with_port = yes` (covered in `docker/`)
4. `sites-enabled/default` had `-sql` (disabled) in the `accounting { }` block
5. `/var/log/freeradius/radacct/` was owned by `root:root`, freerad couldn't write
6. `mods-config/sql/main/mysql/queries.conf` had SQLite-flavored `${....event_timestamp}` template that MariaDB rejects

Only fix #2 + #3 (charon side) were committed to the repo. The other 4 fixes
were applied directly to `/etc/freeradius/3.0/` on vps-01 and never made it
to git. If vps-01 had been rebuilt that night, all 4 fixes would be lost.

**This directory + `provision-freeradius.sh` close that gap.**

## What's in here

| Path | Files in repo | Lives at (vps-01) | Why non-default |
|---|---|---|---|
| `mods-available/sql_last_seen` | full file | `/etc/freeradius/3.0/mods-available/sql_last_seen` | Has `sql_user_name = "%{User-Name}"` (otherwise post-auth UPDATE never fires) |
| `mods-config/sql/main/mysql/queries.conf` | full file | `/etc/freeradius/3.0/mods-config/sql/main/mysql/queries.conf` | 22 occurrences of `${....event_timestamp}` replaced with `FROM_UNIXTIME(UNIX_TIMESTAMP())` (SQLite → MariaDB compat) |
| `sites-enabled/default` | full file | `/etc/freeradius/3.0/sites-enabled/default` | Line 735: `-sql` → `sql` in `accounting { }` block (otherwise radacct INSERT never fires) |
| `mods-config/sql/main/mysql/queries.conf.fixed-2026-07-13` | snapshot | (NOT deployed — reference only) | Pre-fix queries.conf, kept for diff history |

## What's NOT in here (and why)

- **FreeRADIUS stock defaults** — these live in `dpkg-repack freeradius` output;
  we only track deviations. If Debian upgrades the package and our overlay
  becomes stale, `provision-freeradius.sh --check` will flag it.
- **SSL certs** (`/etc/freeradius/3.0/certs/`) — these contain the operator's
  CA, server cert, and DH params; tracked separately under `host/ssl/`.
- **Live secrets** — `clients.conf` shared secret lives in `/etc/daloRADIUS.key`
  and is captured off-server per DR runbook §0.5. We do NOT commit secrets.
- **The radacct log directory** — it's filesystem state (ownership), not a
  file. `provision-freeradius.sh` calls `chown freerad:freerad` on it.

## How to use

### Deploy for the first time (after a fresh rebuild)

```bash
ssh root@vps-01
cd /opt/strongswan-vpn-gateway  # or wherever the repo is cloned
bash host/freeradius/provision-freeradius.sh
```

The script will:
1. Sanity-check that the system has FR 3.0 installed
2. Back up current files to `/var/lib/databyte/freeradius-backup-<timestamp>/`
3. Copy each file from `host/freeradius/` to `/etc/freeradius/3.0/` (only if md5 differs)
4. Set proper ownership + mode (`chown root:freerad / chmod 640`)
5. `chown -R freerad:freerad /var/log/freeradius/radacct/` (filesystem fix)
6. Restart FreeRADIUS only if files actually changed
7. Run smoke test: send `Accounting-Request` via `radclient`, expect `Accounting-Response`

### Check drift (live vs repo)

```bash
bash host/freeradius/provision-freeradius.sh --check
```

Prints a diff table showing which files on VPS match the repo and which differ.
Exit code 0 = all match, 1 = drift detected.

### Update the overlay after fixing a new bug

1. Edit the file on vps-01: `vim /etc/freeradius/3.0/mods-available/sql_last_seen`
2. Test the fix: `systemctl restart freeradius && radtest ...`
3. Copy the working file back into repo:
   `scp root@vps-01:/etc/freeradius/3.0/mods-available/sql_last_seen host/freeradius/mods-available/sql_last_seen`
4. Update this README's "Why non-default" column with the new fix
5. Update CHANGELOG.md
6. Commit + push

### Upgrade FreeRADIUS (Debian package upgrade)

When `apt upgrade freeradius` is run, the package will replace `/etc/freeradius/3.0/`
files. Our overlay will be overwritten. After the upgrade:

```bash
bash host/freeradius/provision-freeradius.sh
```

This will restore our overlays on top of the new package files. Review the diff
(`diff -u host/freeradius/sites-enabled/default /etc/freeradius/3.0/sites-enabled/default`)
before re-applying — if Debian added new sections or comments near our edit points,
the overlay may need a refresh.

## Verification receipts (last deploy 2026-07-13)

After first run on vps-01:

```
md5sum:
  a73ef3e334d459f3b5548044e7a21504  /etc/freeradius/3.0/mods-available/sql_last_seen
  8fabf8cc952acba845bec4481039de1a  /etc/freeradius/3.0/mods-config/sql/main/mysql/queries.conf
  171b005816295f5b64e684cd56c10cbf  /etc/freeradius/3.0/sites-enabled/default

radpostauth: 327 (growing)
radacct: 3 rows (id 1=test, id 2=Windows, id 3=iPhone)
last_seen_v4: clean IP `105.174.128.86` for both active devices
```

## Authoritative references

- CORR-2026-07-13-035 in `~/self-improving/corrections.md` — full bug chain
- DR runbook `docs/RUNBOOK-DR-REBUILD-AND-HA.md` §2.3 step 14a — uses this overlay
- CHANGELOG.md v2.2.0 — release notes for this overlay
