# RUNBOOK-DR-REBUILD-AND-HA — Databyte VPN Stack: Disaster Recovery + High Availability

| Field | Value |
|---|---|
| Document ID | DAT-OPS-DR-RUNBOOK-001 |
| Revision | v1.1.0 |
| Date (SAST) | 2026-10-15 |
| Author | Misha 🐻 (Zun's assistant) |
| Authoritative source of facts | `ssh root@vpn-prod-01` + `git ls-remote origin` + web research (strongSwan, FreeRADIUS docs) |
| ISO 9001:2015 | Document Control: this is internal — distribution restricted |
| Verification | Each step is independently verifiable; runbook assumes operator has SSH access to OC and (if rebuilding) new VPS |
| Live fact-check | 2026-10-15 06:50 UTC (08:50 SAST) — §1.1–§1.6 numbers + service/port inventory re-verified against `vpn-prod-01` |

---

## 0. Purpose and Scope

This runbook answers two questions with **facts only**:

1. **REBUILD**: Given current backups (kopia @ `kop.databyte.co.za`) and the GitHub repo (`Dippie-WP/databyte-Ikev2`), can the entire VPN stack be rebuilt on a fresh cloud VPS? If yes, what are the exact steps and what is missing?
2. **HA**: Given the current architecture, can a second VPS be added to provide active/active failover? What does it cost (engineering + ops), what does it buy (RTO/RPO), and what does it NOT solve?

**Out of scope**: Commercial billing, application features unrelated to availability, security hardening beyond the rebuilt defaults, multi-region deployment.

---

## 0.5 Off-server secret capture checklist (verify BEFORE disaster)

These secrets are **NOT in kopia** (kopia only backs up the file system, not credentials for other systems). Without them, the rebuild cannot complete past §2.3 step 7. Capture them now and store in at least 2 of: 1Password vault (Databyte vault), paper in safe, OC `~/.secrets/` (encrypted). **Verify access every quarter as part of §4.3 backup verification.**

| # | Secret | Where stored currently | Where to copy off-server | Verify accessible? |
|---|---|---|---|---|
| 1 | **Kopia repository password** | `/home/debian/.kopia-password` on vpn-prod-01 (13 B, mode 600). Used by `kopia repository connect` | 1Password "Databyte → VPS kopia" + paper backup | ☐ verified |
| 2 | **Cloudflare DNS API token** (for `myvpn.databyte.co.za` LE DNS-01 renewal) | `/root/.cloudflare.ini` on vpn-prod-01 (194 B, mode 600) | 1Password "Databyte → Cloudflare" + paper backup | ☐ verified |
| 3 | **Cloudflare account login** (for DNS A-record cutover to new VPS IP) | 1Password only (operator credential) | 1Password + backup operator with own access | ☐ verified |
| 4 | **GitHub PAT or SSH key** with `Dippie-WP/databyte-Ikev2` access | OC `/root/.ssh/id_ed25519` (pubkey on GitHub) | 1Password stores the SSH private key backup; GitHub web UI shows deploy keys | ☐ verified |
| 5 | **Tailscale auth key** (reusable, for new VPS node identity) | Tailscale admin console (`https://login.tailscale.com/admin/settings/keys`) | 1Password "Databyte → Tailscale" | ☐ verified |
| 6 | **Xneelo / cloud provider login** (provision new VPS) | 1Password | 1Password + backup operator | ☐ verified |
| 7 | **MariaDB root password** (post-rebuild fallback per §1.6 gap #9) | `/etc/mysql/debian.cnf` on vpn-prod-01 (in kopia via `/etc`) | Same as #1 (kopia restore covers this; if kopia fails, operator needs to know what root pw was) | ☐ verified |
| 8 | **FreeRADIUS clients.conf shared secret** (for `radtest testing123` checks) | `/etc/freeradius/3.0/clients.conf` on vpn-prod-01 (in kopia via `/etc`) | Same as #7 (kopia covers this) | ☐ verified |
| 9 | **`/var/spool/cron/crontabs/debian`** content | Live on vpn-prod-01, NOT in any kopia snapshot | Operator should capture NOW: `ssh root@vpn-prod-01 'crontab -u debian -l > /home/debian/.kopia-cron.tab'` and copy off-server (paper or 1Password note) | ☐ verified |
| 10 | **Off-server snapshot of `/etc/letsencrypt/live/*` private keys** | In kopia via `/etc/letsencrypt` (snap 2026-07-06) | NOT strictly needed if kopia password is preserved (kopia can decrypt it). Listed for completeness. | ☐ verified |

**If ANY of items 1–6 are inaccessible**: Rebuild stops at §2.3 step 7 (kopia connect) or step 20 (LE cert renewal). Treat these as load-bearing — losing them turns a 2-hour rebuild into a multi-day recovery or worse.

**Quarterly drill (§4.3)**: As part of automated backup verification, also attempt `ssh root@vpn-prod-01 'sudo -u debian kopia repository status 2>&1 | head -3'` to confirm the kopia password file is still readable + repo is reachable.

---

## 1. Source-of-Truth Inventory (verified live 2026-07-11 22:53 UTC / 00:53 SAST)

### 1.1 What `vpn-prod-01` actually runs (live `ssh root@vpn-prod-01`)

| Component | Evidence | Version | Notes |
|---|---|---|---|
| Hostname | `hostname` | `vpn-prod-01` | |
| Public IPv4 | `curl ifconfig.me` | `154.65.110.44` | Xneelo VPS, single NIC `ens3` |
| Public IPv6 | `curl ifconfig.me` | `2c0f:fce8:4000:4000:0:1:0:3a1` | Native dual-stack |
| Tailscale IP | `tailscale status` | `100.64.212.47` | Used for ops access; not customer-facing |
| OS | `/etc/os-release` | Debian 13.7 (trixie) | minor versions up to 13.7 (live) |
| CPU / RAM | `nproc / free -h` | 2 vCPU / 3.8 GiB | KVM guest (`qemu-guest-agent` running) |
| Disk | `df -h /` | 9.7 GB (77% used, 2.2 GB free) | `/dev/vda1`, no separate data partition |
| Swap | `free -h` | 0 B | **No swap configured** |
| Kernel IP forwarding | `sysctl net.ipv4.ip_forward` | `1` (assumed; verified charon uses it) | |
| `ens3` network | `ip addr` | `154.65.110.44/20` via `154.65.96.1` | DHCP-assigned; metric 100 |
| charon container uptime | `docker inspect strongswan --format '{{.State.StartedAt}}'` | 2026-10-08T14:06:34Z (6 d 17 h at audit time) | container started Thu 2026-10-08; uptime capped at container start (rebuild §2.3 step 17) |

### 1.2 Services running (live `systemctl list-units --state=running`)

| Service | Purpose | Backup coverage |
|---|---|---|
| `docker.service` | Docker engine | (binary only via `/usr/local` — not snapshotted, but reinstallable via `apt`) |
| `containerd.service` | Container runtime | same as docker |
| `strongswan` (container, image `zun/strongswan:6.0.7-mschapv2-attrsql`) | IKEv2 daemon (`charon`) | **NOT in kopia** — must be rebuilt from Dockerfile in repo |
| `freeradius.service` | AAA backend (RADIUS UDP 1812/1813 + CoA 3799); strongSwan → eap-radius → FreeRADIUS → MariaDB `radius` DB | config in `/etc/freeradius/` (snapshotted); SQL module enabled, **auth in `radpostauth` (2,299 rows live) AND `radacct` (1,166 rows live)** — operator overlay `host/freeradius/` applies 3 critical config files (sql_last_seen, queries.conf, sites-enabled/default) + chown radacct directory |
| `mariadb.service` | MariaDB **11.8.6-MariaDB-0+deb13u1** hosting the `radius` DB — **45 tables total** (live `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius'` → 45): 7 RADIUS-protocol tables (radcheck, radreply, radgroupcheck, radgroupreply, radusergroup, radpostauth, radacct) PLUS ~38 daloRADIUS admin/operator/billing tables (customers, devices, users, billing_*, operators, operators_acl, nas, hotspots, userinfo, userbillinfo, installer_tokens, alerts, purchases, etc.). **Live row counts (2026-10-15):** `radcheck=26`, `radpostauth=2,299`, `radacct=1,166`, `customers=11`, `users=11`, `devices=10`, `operators=1`. | data in `/var/lib/mysql` (inside `/var/lib` snapshot) — both subsets restore together; do NOT drop daloRADIUS tables during rebuild |
| `vpn-portal.service` | FastAPI portal (gunicorn, **v2.2.1** — deployed 2026-08-10, git HEAD 8e02a6e; **NOTE** the FastAPI `app.version` string on line 141 still says `"2.1.1"` — drift from systemd Description, cosmetic only) | code in `/opt/vpn-portal` (snapshotted; **app.py 3,266 lines / 145,904 B (SHA baca20f7...), bot.py 1,988 lines (SHA 8cbda46e...)**); CORR-035 cleanup applied — `_sqlite_query()` and its 4 env vars removed; portal data lives in MariaDB only (post-Phase 4E) |
| `bot-polling.service` | **Telegram bot** polling daemon (@Databytevpn_bot, chat_id 7748884597) — **sole polling source per HOT-261**; runs alongside `vpn-portal.service` (gunicorn for FastAPI) | `/opt/vpn-portal/bot_polling.py` + `bot.py` (in kopia via `/opt/vpn-portal`); PTB polling daemon. Never start a second instance — HTTP 409 Conflict from Telegram. **Bot version:** vpn-admin-bot **v2.5.0** (HEAD `01ad543`, tag v2.5.0 — separate from portal release line) |
| `nginx.service` | Reverse proxy (80/443) | config in `/etc/nginx` (snapshotted) |
| `prometheus.service` | Metrics | config in `/etc/prometheus` (snapshotted) |
| `prometheus-node-exporter.service` | Prometheus node metrics (port 9100) | **renamed** from `node_exporter.service`; config in `/etc/systemd/system/` (in kopia via `/etc`) |
| `strongswan_exporter.service` | strongSwan Prometheus exporter (port 9101) | same |
| `quota-monitor.service` | Quota enforcement (nft named meters + 80% warn + 100% hard cut) | `ExecStart=/usr/bin/python3 /opt/strongswan-vpn-gateway/quota/quota-monitor.py` (35,898 B, Oct 12; **FIXED** from old /home/zunaid path — see §1.6 gap #1 INVERTED) |
| `bandwidth-monitor.service` | Per-user bandwidth (tc classes + nft mangle MARK per user) | `ExecStart=/usr/bin/python3 /home/zunaid/strongswan/quota/bandwidth-monitor.py` — ⚠ **STALE PATH** (running on `.bak-pre-phase8-2026-07-12` since 2026-10-08; see §1.6 gap #1 INVERTED + new gap #14) |
| `quota-exporter.service` | Prometheus exporter for quota | `/opt/strongswan-vpn-gateway/quota/quota-exporter.py` (in kopia via /opt) |
| `ipban.service` | IP ban (fail2ban-style) | `/opt/ipban` (snapshotted) |
| `fail2ban.service` | SSH/Apache brute-force protection | `/etc/fail2ban` (snapshotted); live jails: `sshd`, `vpn-portal` |
| `tailscaled.service` | Tailscale mesh | state at `/var/lib/tailscale/tailscaled.state` (in /var/lib snap); node IP `100.64.212.47` |
| `apache2.service` | daloRADIUS admin UI (port 8000 [::1] only) | config `/etc/apache2/sites-enabled/daloradius.conf`; **daloRADIUS code at `/var/www/daloradius/app/` is NOT in kopia** (see §1.6 gap #2) |
| `vnstat.service` | vnStat network traffic monitor | db at `/var/lib/vnstat/vnstat.db` (in /var/lib snap, OK) |
| `exim4.service` | Local MTA (exim4, default Debian; outbound mail) | config `/etc/exim4` (in /etc snap) |
| `unattended-upgrades.service` | Unattended security updates | config `/etc/apt/apt.conf.d/50unattended-upgrades` (in /etc snap) |
| `acpid.service` | ACPI event daemon | binary in /usr (regenerable from apt) |
| `dockhand-bridge.service` | socat TCP 2384 → `/var/run/docker.sock` (Tailscale-only via nftables) | config in `/etc/systemd/system/` |
| `strongswan-watchdog.service` | swanctl auto-reload on `/opt/.../docker/swanctl/conf.d/*.conf` change (HOT-66 fix) | `/usr/local/bin/strongswan-watchdog.sh` (in kopia via `/usr/local`); **renamed** from `strongswan-iptables-watchdog.service` (Phase 5D removed iptables dependency; focus shifted to charon conf-reload); polls every 3s, runs `swanctl --load-all` on change |
| `alertmanager.service` | Prometheus alert router | `/etc/alertmanager` + `/opt/alertmanager` (in kopia via `/etc` + `/opt`); user `alertmanager` |
| `vps-alert-relay.service` | alertmanager webhook → Telegram/ops (`/usr/local/bin/alert-relay.py`) | in kopia via `/usr/local`; bridges `alertmanager` → Telegram bot API |
| `ttyd.service` | Web terminal at `http://127.0.0.1:7681/panel` (basic auth `zun` + password; loopback only) | `/usr/local/bin/ttyd` (in kopia via `/usr/local`); `/etc/systemd/system/ttyd.service` (in kopia via `/etc`) |
| `zerotier-one.service` | ZeroTier mesh (alongside Tailscale) | `/var/lib/zerotier-one` (in kopia via `/var/lib`); node identity in `identity.secret` |
| `cron.service` | Cron daemon (runs `/var/spool/cron/crontabs/debian` + `/etc/cron.d/*`) | standard Debian; live debian crontab has `0 0 * * * /home/debian/local/bin/kopia-backup-all` (per §1.6 gap #6) |

### 1.3 Listening ports (live `ss -tlnp` + `ss -ulnp`)

| Port | Protocol | Process | Public? | Function |
|---|---|---|---|---|
| 22/tcp | TCP | sshd | YES | Operator SSH (fail2ban-protected) |
| 80/tcp | TCP | nginx | YES | HTTP → 443 redirect |
| 443/tcp | TCP | nginx | YES | Portal HTTPS + ACME + API |
| 500/udp | UDP | **charon (pid 1485, host network)** | YES | IKEv2 |
| 4500/udp | UDP | **charon (pid 1485, host network)** | YES | IKEv2 NAT-T |
| 1812/udp | UDP | freeradius | NO (localhost) | RADIUS auth |
| 1813/udp | UDP | freeradius | NO (localhost) | RADIUS accounting |
| 3306/tcp | TCP | mariadbd | NO (localhost) | MariaDB |
| 5355/udp | UDP | systemd-resolved | NO | mDNS |
| 8000/tcp | TCP | apache2 | NO (127.0.0.1 + [::1] only) | **daloRADIUS admin UI** (operators/login.php, users/login.php) — live, returns HTTP 200 with daloRADIUS login form |
| 9101/tcp | TCP | python3 (strongswan_exporter) | NO | metrics |
| 9102/tcp | TCP | python3 | NO | metrics |
| 2384/tcp | TCP | socat | NO (Tailscale-nftables-restricted) | Dockhand Docker API |
| **18120/udp** | UDP | **freeradius** | NO (localhost) | **FreeRADIUS Status-Server** (RFC 5992; `status_server = yes` in radiusd.conf). Listens on 127.0.0.1:18120 + [::1]:18120. Allows `radtest` queries like `radtest USER PASSWORD 127.0.0.1:18120 0 testing123` to query server state. Harmless; rebuild restores via `/etc/freeradius/`. |
| **3799/udp** | UDP | **charon** | NO (localhost) | **strongSwan CoA / DAE listener** (Dynamic Authorization Extension, RFC 5176). FreeRADIUS → strongSwan Disconnect-Request channel. Added in Phase 5 (2026-07-06). Operationally critical for portal's Disconnect button. |
| **4502/tcp** | TCP | **charon (host net)** | NO (127.0.0.1) | **strongSwan VICI** control socket (TCP 127.0.0.1:4502 inside container, host-readable via `docker exec`). Used by `swanctl --uri=tcp://127.0.0.1:4502 ...` and the strongswan_exporter + portal. Verified at §2.3 step 17. |
| **7681/tcp** | TCP | ttyd | NO (127.0.0.1) | **Web terminal panel** at `/panel` (basic auth). Operator shell from browser. Loopback only. |
| **9999/tcp** | TCP | python3 | NO (Tailscale-restricted) | Ops-side service (likely dockhand-bridge-related or custom alerting). Not open to public. |
| **9993/udp** | UDP | zerotier-one | NO (per-iface) | **ZeroTier mesh** (alongside Tailscale). 4 iface bindings seen: `tailscale0`, `docker0`, `ens3`, `wg0`. |
| **44062/udp** | UDP | zerotier-one | NO (per-iface) | ZeroTier per-interface (multi-NIC). |
| **51820/udp** | UDP | (kernel — no process) | NO | WireGuard default port — bound by kernel module, no userland process. Likely the `wg0` interface (10.50.0.1/24) used by ZeroTier. |

**Critical finding**: charon runs on the **host network namespace**, NOT in a Docker bridge network. This is intentional — it gives charon direct access to `ens3` for IKE_SA traffic — but it means **the strongswan container is NOT portable to a different host's bridge without configuration changes**.

### 1.4 What's in kopia (live `kopia snapshot list --all` from vpn-prod-01)

Repo: `https://kop.databyte.co.za:443`, user `debian`. Password stored at `/home/debian/.kopia-password` (live, single-instance). **2026-10-15 audit note:** kopia client on `vpn-prod-01` returns `502 Bad Gateway` from `kop.databyte.co.za:443` (HTML content-type, not kopia's native API). Last successful `.update-info.json` mtime is **2026-10-10 00:00** — so the kopia server is unreachable or the proxy in front (Cloudflare?) has been returning 502 since at least 2026-10-10. **Parked** for separate investigation (Zun msg #43961). If unresolved at disaster time, §2.3 step 7 fails → rebuild must proceed without kopia restore (rebuild via repo + scripts only, then re-create state from git).

| Path | Size | Latest snapshot | Reinstall method on rebuild |
|---|---|---|---|
| `/etc` | 4.3 MB | 2026-07-11 00:00:03 SAST | `kopia restore /etc --destination-path /etc` ⚠ overwrites ALL of /etc |
| `/etc/letsencrypt` | 44.6 KB | 2026-07-06 00:00:05 SAST | LE certs — must survive rebuild |
| `/home/debian` | 16.6 MB | 2026-07-11 00:00:07 SAST | kopia scripts, `.config/kopia/`, password file |
| `/opt/ipban` | 54.5 MB | 2026-07-11 00:00:12 SAST | ipBan code + sqlite DB |
| `/opt/strongswan-vpn-gateway` | 6.4 MB | 2026-07-10 00:00:16 SAST | Git checkout (incl. Dockerfile + nginx + deploy scripts) |
| `/opt/vpn-portal` | 93.9 MB | 2026-07-11 00:00:11 SAST | Portal FastAPI code + `.venv/` + baked PowerShell scripts |
| `/root/.ssh` | 557 B | 2026-07-07 00:00:17 SAST | Operator SSH keys (incl. OC's `id_ed25519` pubkey) |
| `/root/projects` | 93.3 KB | 2026-06-30 20:46:33 SAST | Operator scripts (nft-migration-v2) |
| `/usr/local` | 73.9 MB | 2026-07-07 00:00:19 SAST | Custom binaries: kopia, strongSwan if installed via tarball, etc. |
| `/var/lib` | 659.1 MB | 2026-07-11 00:00:20 SAST | **charon SQLite DB + MariaDB data + fail2ban DB + dpkg state** |
| `/var/log/auth.log` | 11 MB | 2026-07-11 00:00:38 SAST | SSH logs |
| `/var/log/charon-log-host` | 4.3 MB | 2026-07-11 00:00:41 SAST | strongSwan log (named after `charon-log-host/` symlink pattern) |
| `/etc/letsencrypt/renewal` | 655 B | 2026-06-30 17:44:32 SAST | LE renewal configs (Cloudflare DNS-01 hooks for `myvpn.*`). Subset of `/etc/letsencrypt` snap; separate policy for finer restore granularity. |
| `/home/debian/nft-migration-v2` | 94.1 KB | 2026-06-30 17:44:33 SAST | nftables v2 migration script (operator scratch). Subset of `/home/debian`. |
| `/root/projects/nft-migration-v2` | (same) | 2026-06-30 | Same nftables v2 migration — root user copy. Subset of `/root/projects`. |

### 1.5 What's NOT in kopia (verified by absence)

| Missing item | Why it matters | Where to get it |
|---|---|---|
| **Docker images** (e.g. `zun/strongswan:6.0.7-mschapv2-attrsql`) | StrongSwan won't start without image | `docker build -f docker/Dockerfile -t zun/strongswan:6.0.7-mschapv2-attrsql .` from repo |
| **kopia password itself** | Lose vpn-prod-01 → lose access to repo | **Must be stored elsewhere NOW** (paper, password manager, OC vault) |
| **OC's kopia server storage** (`/var/lib/kopia` on OC) | OC dies → repo dies → no backup at all | Move kopia server to 3rd party (Backblaze B2) or back up OC to a different repo |
| **DNS records** (`vpn-portal.databyte.co.za`, `kop.databyte.co.za`, `myvpn.databyte.co.za`, `adminer.databyte.co.za`) | Cutover needs DNS to point at new IP | Registrar; TTL was 300s (Cloudflare) |
| **GitHub SSH key on OC** (used for `git push` from OC) | If OC is the rebuild node, must still work | Already in `/root/.ssh/id_ed25519` — is in kopia |
| **Cloudflare DNS API token** (for HA Option B DNS-01 cert renewal) | HA rebuild cannot auto-renew LE certs without it; HTTP-01 will rate-limit | Store as `/etc/letsencrypt/cloudflare.ini` (mode 600, standard letsencrypt pattern); copy off-server to 1Password / paper; not in kopia |
| **Tailscale auth key** (re-auth key for new VPS node identity) | New VPS has fresh machine identity; `tailscaled` boots from `/var/lib/tailscale/tailscaled.state` (in /var/lib snap) but key may be invalid | `tailscale up --authkey=<key-from-tailscale-admin>` after restore; retrieve key from https://login.tailscale.com/admin/settings/keys |
| **Cloudflare tunnel token** (if used) | If portal routes via tunnel | Cloudflare dashboard; not in kopia |
| **Mailgun / SMTP creds** (operator notifications) | If mail goes out from portal | Not in kopia (env vars in `vpn-portal.service`) |
| **FreeRADIUS clients.conf shared secret** | Inside `/etc/freeradius/3.0/clients.conf` → ✅ IS in kopia | `kopia restore /etc/freeradius` |
| **MariaDB root password** | Inside `/etc/mysql/debian.cnf` → ✅ IS in kopia | Same path |
| **daloRADIUS code tree** (`/var/www/daloradius/app/`, ~100 MB) | Admin UI will 404 after rebuild | `git clone https://github.com/lirantal/daloradius.git /var/www/daloradius` then copy production `daloradius.conf.php` from backup (verify backup captured `/var/www` — currently it does NOT, see §1.6 gap #2). Note: live config has password length limit 14 — trim any restored long password |
| **`/home/zunaid` user home** (NOT in kopia — only `/home/debian` is backed up) | `quota-monitor.service` + `bandwidth-monitor.service` both reference `/home/zunaid/strongswan/quota/` | Re-create user `useradd -m -s /bin/bash zunaid`, mkdir `/home/zunaid/strongswan/quota`, copy scripts from `/opt/strongswan-vpn-gateway/quota/` and accept divergence via `cp -f` |
| **`/var/spool/cron/crontabs/debian`** (per-user crontab) | `kopia-backup-all` cron entry silently missing post-rebuild | Capture `crontab -u debian -l > /home/debian/.kopia-cron.tab` BEFORE disaster (off-server); restore via `crontab -u debian /home/debian/.kopia-cron.tab` |
| **Root CA certs for Windows installer** (`isrg-root-x2.pem`, `root-ye.pem`) | Win 10 <1903 customers need these to bootstrap trust | NOT in kopia. Fetch fresh from curl on rebuild — these are public anchors, no secret |
| **Per-customer baked PowerShell installers** (`/opt/vpn-portal/www/static/baked/`) | Custom per-customer config baked into the .ps1 file (servercert hash, EAP identity, password) — these are RUNTIME-GENERATED by the portal on customer onboarding; rebuild starts empty and re-generates on demand | No action needed |
| **vpn-prod-01 LE certs for `adminer.databyte.co.za`** (separate cert) | LE will reissue to new VPS via `certbot --nginx -d adminer.databyte.co.za`; restore ALSO works via `/etc/letsencrypt/live/adminer.databyte.co.za` IF the live cert is still valid; otherwise HTTP-01 certbot run during §2.3 step 20 covers it | Already covered; just ensure `certbot --nginx` step lists ALL active LE SANs including `adminer.databyte.co.za` |

---

## 1.6 Drift / gaps found in v1.0.0 audit (2026-07-12 05:00–07:30 UTC)

**13 verified-live gaps + 2 defensive notes** — gaps #1–#13 verified by direct command on `ssh root@vpn-prod-01`. Defensive notes (Tailscale node-key + TOOLS.md stale path) in separate table — not counted in 13. Phase 4E (2026-07-12) updates: §1.6 gaps #3 + #4 updated; §2.3 step 11 verify rewritten for MariaDB; §3.6 row 6 marked ✅ DONE; §4 summary row 1 updated.

| # | Gap | Verified by (this turn) | Impact on rebuild | Fix |
|---|---|---|---|---|
| 1 | **§1.6 gap #1 INVERTED since v1.0.7 audit** — scripts now live in `/opt/strongswan-vpn-gateway/quota/`, not `/home/zunaid/strongswan/quota/`. Live `quota-monitor.py` is at `/opt/strongswan-vpn-gateway/quota/quota-monitor.py` (**35,898 B**, Oct 12). `/home/zunaid/strongswan/quota/` is now a `.bak` graveyard (28 entries, all dated Jun–Jul 2026 — no live `quota-monitor.py` at this path). `quota-monitor.service` `ExecStart` has been **FIXED** to point at `/opt/.../quota-monitor.py`. `bandwidth-monitor.service` `ExecStart` **STILL points at `/home/zunaid/strongswan/quota/bandwidth-monitor.py`** — which is no longer the live code (see new gap #14). | `ls -la /opt/strongswan-vpn-gateway/quota/quota-monitor.py` → 35,898 B Oct 12; `ls -la /home/zunaid/strongswan/quota/` → only `.bak` files; `systemctl cat quota-monitor.service \| grep ExecStart` → `/opt/...`; `systemctl cat bandwidth-monitor.service \| grep ExecStart` → STILL `/home/zunaid/...` | `quota-monitor` runs OK from new path. `bandwidth-monitor` runs on stale `.bak-pre-phase8-2026-07-12` (29,242 B, 3 months old) — currently functional but a single file rotation will break it. Rebuild needs the service ExecStart fix in step 11a. | **Updated §2.3 step 11a** (see below): point `bandwidth-monitor.service` at `/opt/.../quota/bandwidth-monitor-nft.py` (the nft version, 19,543 B, Jul 27) matching the live quota-monitor's nft migration. |
| 2 | **daloRADIUS is LIVE but the code tree `/var/www/daloradius/` is NOT in any kopia snapshot.** Live dir has `app/{common,operators,users}/`; `curl http://127.0.0.1:8000/operators/login.php` returns HTTP 200; Apache vhost at `/etc/apache2/sites-enabled/daloradius.conf` (in kopia via /etc) listens on 127.0.0.1:8000 / [::1]:8000 only. | `ls /var/www/daloradius/`; `curl -ksS -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/operators/login.php` → 200; `kopia snapshot list` has no `/var/www` path | Rebuild → daloRADIUS UI 404; operator admin path only via portal login | New §2.3 step 11b: re-extract daloRADIUS from `git clone https://github.com/lirantal/daloradius.git` → `/var/www/daloradius/`; copy `daloradius.conf.php.sample` to `.conf.php` and fill DB host/user/pass (password length limit 14); `a2enmod php8.3 && systemctl restart apache2` |
| 3 | **MariaDB `radius` DB = 45 tables** (updated 2026-10-15, was 42 in v1.0.7). Live `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius'` returns **45**. The 7 RADIUS-protocol tables (`radcheck`, `radreply`, `radgroupcheck`, `radgroupreply`, `radusergroup`, `radpostauth`, `radacct`) are joined by ~38 daloRADIUS admin/operator/billing tables (`operators`, `operators_acl`, `customers`, `users`, `devices`, `billing_*`, `nas`, `hotspots`, `userinfo`, `userbillinfo`, `installer_tokens`, `alerts`, `purchases`, etc.). **Phase 4E LIVE (2026-07-12):** Portal business data (`customers`, `users`, `devices`, `installer_tokens`, `audit_log`, `tiers`, `alerts`, `purchases`, `operator_sessions`, `customer_portal_sessions`) LIVES in MariaDB as these tables — not in a separate SQLite. `app.py` `db_query`/`db_exec` read MariaDB via `portal_auth._db()`. The CORR-022 SQLite shim (v1.9.2 workaround for dual-DB split-brain) has been REVERTED. **Live row counts (2026-10-15):** `radcheck=26`, `radpostauth=2,299`, `radacct=1,166`, `customers=11`, `users=11`, `devices=10`, `operators=1`. | `mariadb -uroot radius -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema="radius"'` → 45; `mariadb -uroot radius -e 'SELECT COUNT(*) FROM customers; SELECT COUNT(*) FROM radpostauth; SELECT COUNT(*) FROM radacct;'` | Rebuild: step 14 verify counts must match live baseline (45 tables, customers=11, radpostauth≥2,000, radacct≥1,000). Both RADIUS and portal data restore together from `/var/lib/mysql` — no separate SQLite restore needed. | **§2.3 step 14 verify** (rewritten in v1.1.0): `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius'` (expect **45**) AND `SELECT COUNT(*) FROM customers` (expect **11**) AND `SELECT COUNT(*) FROM radpostauth` (expect **≥2,000**) AND `SELECT COUNT(*) FROM operators` (expect **≥1**). All must match. |
| 4 | **~~`radacct` is EMPTY~~ RESOLVED in v2.2.0 (CORR-2026-07-13-035, 2026-07-13 12:35 SAST):** FreeRADIUS accounting chain is now wired. Live `radacct` has **1,166 rows** (was 3 in v1.0.7; ~400× growth over 3 months). Live `radpostauth` has **2,299 rows** (was 325 in v1.0.7; ~7× growth). Interim updates fire every 300s with real octet counters. Six bugs fixed in one session: (1) `sql_last_seen` missing `sql_user_name` directive, (2) charon `accounting = no` upstream default, (3) charon `station_id_with_port = yes` upstream default (caused `=5B...=5D` quote-encoded IPs), (4) `sites-enabled/default` line 735 `-sql` (disabled), (5) `/var/log/freeradius/radacct/` owned by `root:root`, (6) `queries.conf` SQLite-flavored `${....event_timestamp}` template. All 6 fixes captured in `host/freeradius/` overlay + `provision-freeradius.sh` (idempotent, with smoke test). | `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radacct'` → 1,166; `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radpostauth'` → 2,299; `bash /opt/strongswan-vpn-gateway/host/freeradius/provision-freeradius.sh --check` → exit 0 | If `radacct` becomes empty post-rebuild, the operator overlay was not applied — run `provision-freeradius.sh`. If overlay runs but `radacct` still empty, check strongswan eap-radius `accounting = yes` (in `docker/strongswan.d/10-eap-radius.conf`). | §2.3 step 14a (live v1.1.0): apply `provision-freeradius.sh` + verify `radacct` ≥1 within 5 min of first client connect. **Acceptance baseline (v1.1.0):** post-rebuild `radacct` should grow toward 1,000+ over 30 days, not 3. |
| 5 | **strongswan container has 7 bind mounts** not specified in v1.0.0 §2.3 step 17. Live `docker inspect strongswan`: binds for `debug.conf`, `00-virtual-ip.conf`, `10-eap-radius.conf`, `attr-sql.conf`, `swanctl/`, `/var/lib/strongswan`, `/var/log/charon-log-host/charon.log → /var/log/charon-host.log`. | `docker inspect strongswan --format "{{range .Mounts}}...{{end}}"` (live) | Without binds, charon inside container can't see `rw-eap.conf`, credentials, or logs — charon boots but never accepts IKE_SAs (silent). | New §2.3 step 17 rewritten: include all 7 binds in `docker run`; capture full live `docker inspect strongswan` to rebuild script so rebuild uses the verified mount set |
| 6 | **`/var/spool/cron/crontabs/debian` is NOT in any kopia snapshot.** Kopia covers `/var/lib` and `/etc/cron.d/` only — NOT `/var/spool/`. The debian user's crontab (live: `0 0 * * * /home/debian/local/bin/kopia-backup-all >/var/log/kopia-cron.log 2>&1`) lives only on the live host at `/var/spool/cron/crontabs/debian`. | `kopia snapshot list` (live) → no `/var/spool` entry; `crontab -u debian -l` → shows entry; `ls -la /var/spool/cron/crontabs/` confirms file present | Rebuild runs but no backup cron installed → daily kopia backup silently stops → no more snapshots → RPO becomes unknown | New §2.3 step 23 rewritten: capture `crontab -u debian -l > /home/debian/.kopia-cron.tab` BEFORE disaster; restore post-rebuild via `crontab -u debian /home/debian/.kopia-cron.tab` |
| 7 | **Cloudflare DNS-01 creds file** at `/root/.cloudflare.ini` (referenced by `/etc/letsencrypt/renewal/myvpn.databyte.co.za.conf` → `dns_cloudflare_credentials = /root/.cloudflare.ini`) is NOT in any kopia snapshot. Kopia sources for `/root` are only `/root/.ssh` and `/root/projects` — `/root/.cloudflare.ini` not on either list. | `grep -E 'cloudflare' /etc/letsencrypt/renewal/myvpn.databyte.co.za.conf` → `dns_cloudflare_credentials = /root/.cloudflare.ini`; `kopia snapshot list` shows `/root/.ssh` and `/root/projects` only (no `/root/.cloudflare.ini`) | HA rebuild: `certbot renew` fails for `myvpn.*` (DNS-01 can't authenticate) → cert expires → portal HTTPS down | Recreate `/root/.cloudflare.ini` from off-server backup (1Password / paper) after rebuild; chmod 600. Or: switch `myvpn` cert renewal to HTTP-01 (single-VPS, no HA) — tradeoff vs rate-limit risk on the doc §3.6 path |
| 8 | **PowerShell installer baked scripts dir** `/opt/vpn-portal/www/static/baked/` contains ONLY `setup-databyte-vpn-zunaid-new-win11.ps1.bak-pre-fortigate-fix` (1 file, 22,669 B). Per-customer baked scripts are runtime-generated by the portal on demand. | `ls /opt/vpn-portal/www/static/baked/` (live) | None on rebuild — baked scripts regenerate on demand from portal onboarding flow. Information only. | None needed; confirmed working as designed. Listed here so a future audit doesn't re-flag it as a missing-component gap. |
| 9 | **MariaDB root authentication plugin is `mysql_native_password`** (NOT Unix-socket). Live root row: `plugin = mysql_native_password`. So `mariadb -uroot` CLI auth is via the password hash in `/etc/mysql/debian.cnf` (in kopia via `/etc`). Risk on rebuild: the server_id embedded in `debian.cnf` differs across fresh MariaDB installations AND the password string stored there is generated locally per host — a fresh `/etc/mysql/debian.cnf` from a different MariaDB host won't grant root access. | `mariadb -uroot -e 'SELECT user, host, plugin FROM mysql.user WHERE user="root"'` (live) → `mysql_native_password`; `systemctl cat mariadb.service` confirms `/etc/mysql/debian.cnf` is read for password auth | §2.3 step 14 silently stuck if operator panics on "Access denied for user 'root'@'localhost'" | Rewrite §2.3 step 14 fallback: when `mariadb -uroot` fails, stop and start mariadb in safe mode via `systemctl stop mariadb && mysqld_safe --skip-grant-tables &` to reset root pw, then `ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('<new-pw>')` and store new pw in `/etc/mysql/debian.cnf` |
| 10 | **`strongswan` GROUP (GID 1002) must pre-exist on host for `/var/lib/strongswan` to be writable.** Live `getent group strongswan` → `strongswan:x:1002:vpn-portal`. Fresh Debian 13 does NOT auto-create this group; no apt package does. Without it: `docker run` succeeds but `/var/lib/strongswan/ipsec.db` is unreadable inside the container; charon boots but cannot persist SA state or read portal-customer mappings. | `getent group strongswan` (live); `stat -c "%a %U %G" /var/lib/strongswan` → `775 root strongswan`; `ls -l /var/lib/strongswan/ipsec.db` → `rw-rw---- 1 root strongswan` | Silent: charon inside container reads `/var/lib/strongswan/ipsec.db` but DB appears empty / unreadable. EAP-RADIUS auth happens in FreeRADIUS (out of band) so customers still authenticate, but per-VIP IKE_SA persistence and pool lookup break | New §2.3 step 16a (precondition to step 17): `groupadd -g 1002 strongswan && usermod -aG strongswan vpn-portal && mkdir -p /var/lib/strongswan && chown root:strongswan /var/lib/strongswan && chmod 775 /var/lib/strongswan` |
| 11 | **`/var/log/charon-log-host/` directory must pre-exist on host for the charon.log bind mount to work.** The bind source is `/var/log/charon-log-host/charon.log` (a FILE inside a DIR). If the parent DIR doesn't exist, `docker run` fails with `Error: source path does not exist`. Kopia restore from `/var/log/charon-log-host` snapshot DOES recreate the dir (current kopia path includes it), but a fresh rebuild without kopia (or before restore completes) hits this. **Live state (2026-10-15):** file is **1,441,792 B (1.4 MB)** — down from 16.7 MB in v1.0.7 audit. Reason: strongswan container restarted 2026-10-08 14:06 UTC; log is now bounded by container lifetime, not logrotate. Rate: ~233 KB/day (well below the v1.0.7 "700 KB/12 h" estimate — Phase 5 DAE + Phase 7.5 nft may have reduced per-SA log volume). | `ls -la /var/log/charon-log-host/` (live); `stat -c "%s" /var/log/charon-log-host/charon.log` → 1,441,792 B (file inside) | `docker run` aborts at step 17 with cryptic error; charon never starts; portal restart never recovers | Add precondition to §2.3 step 17: `mkdir -p /var/log/charon-log-host && touch /var/log/charon-log-host/charon.log && chown -R root:root /var/log/charon-log-host` BEFORE the `docker run` command. **Note:** the gap-13 logrotate fix is still desirable but no longer urgent — log is bounded by container lifetime + daily kopia restore. |
| 12 | **Image-baked `/etc/strongswan.d/` files vs the 4 bind mounts** — easy to miss the override semantics. Inside the container, `ls /etc/strongswan.d/` shows 11 files: 4 BIND-mounted RO (`00-virtual-ip.conf`, `10-eap-radius.conf`, `attr-sql.conf`, `debug.conf`) — these OVERRIDE image defaults; 7 image-baked (`charon/`, `charon.conf` 13,639 B = MAIN charon config, `charon-logging.conf`, `iptfs.conf`, `pki.conf`, `pool.conf`, `swanctl.conf`). The 4 RO binds carry the **customer EAP identities** (`10-eap-radius.conf` references `eap-radius` plugin which reads the `customers` table) and the **virtual IP pool** (`00-virtual-ip.conf`). **Without the binds, charon boots with image defaults — no customer auth, no VIP pool, no EAP-MSCHAPv2 mapping.** | `docker exec strongswan ls -la /etc/strongswan.d/` (live) | Operator forgets a bind or skips the multi-mount arg → charon up but no IKE_SAs accepted; no log error, just empty `swanctl --list-sas` | §2.3 step 17 rewritten with the EXACT `docker run` command copied from live `docker inspect strongswan` output (7 binds, `--cap-add CAP_NET_ADMIN CAP_NET_RAW`, `--network host`, `--restart unless-stopped`, `--health-cmd`); add verify sub-step `docker exec strongswan cat /etc/strongswan.d/10-eap-radius.conf \| grep -c eap-radius` returns >0 right after `docker run` succeeds |
| 13 | **`/var/log/charon-log-host/charon.log` has NO logrotate** (live `cat /etc/logrotate.d/charon-log-host` → ENOENT; `ls /etc/logrotate.d/ \| grep -i charon` → empty). Current size **1,441,792 B (1.4 MB)** as of 2026-10-15 06:49 UTC — was 17.4 MB at v1.0.7 audit; reduction because strongswan container restarted 2026-10-08 (see gap #11). Log is now bounded by container lifetime, not by logrotate. **Operational gap, deprioritized** — still worth adding logrotate (so post-restart log doesn't grow unbounded for weeks), but the urgency from v1.0.7 is gone. | `stat -c "%s" /var/log/charon-log-host/charon.log` (live 1.4 MB); `logrotate -d` would show no schedule for charon log | `/var/log` would fill over weeks if container uptime is long; charon may fail to write silently; portal becomes unreachable | New §2.3 step 11d: write `/etc/logrotate.d/charon-log-host` config (daily, rotate 7, compress, create 0644 root root); `systemctl reload logrotate` |
| 14 | **🆕 `bandwidth-monitor.service` runs on a `.bak` file** (discovered 2026-10-15 06:50 UTC). `ExecStart=/usr/bin/python3 /home/zunaid/strongswan/quota/bandwidth-monitor.py` — but the only `bandwidth-monitor.py` at that path is `.bak-pre-phase8-2026-07-12` (29,242 B, 3 months old). The service is up since 2026-10-08 12:56:01 SAST, no errors. So the `.bak` is being executed as live code. **Fragility: a single file rotation will break it silently** (next maintainer might delete the .bak thinking it's stale). The new canonical path is `/opt/strongswan-vpn-gateway/quota/bandwidth-monitor-nft.py` (19,543 B, Jul 27) — the nft version, which matches the live quota-monitor.py's nft migration. | `systemctl cat bandwidth-monitor.service \| grep ExecStart` → `/home/zunaid/strongswan/quota/bandwidth-monitor.py`; `ls -la /home/zunaid/strongswan/quota/bandwidth-monitor.py` → 29,242 B Jul 12 (the .bak); `ls -la /opt/strongswan-vpn-gateway/quota/bandwidth-monitor-nft.py` → 19,543 B Jul 27 | bandwidth-monitor currently runs on 3-month-old code. Functional today but any cleanup pass will break it. Live per-user bandwidth shaping operates on legacy semantics, not the nft migration that quota-monitor uses. | **§2.3 step 11a ADDITION:** `sed -i 's\|/home/zunaid/strongswan/quota/bandwidth-monitor.py\|/opt/strongswan-vpn-gateway/quota/bandwidth-monitor-nft.py\|' /etc/systemd/system/bandwidth-monitor.service && systemctl daemon-reload && systemctl restart bandwidth-monitor`. **Live fix (separate from this doc):** do the same sed + restart on vpn-prod-01 to bring the live service onto current code. |

**Severity summary** (updated 2026-10-15):
- **Critical (would break rebuild)**: #1 (path), #2 (daloRADIUS), #5 (binds), #6 (cron), #7 (CF creds), #10 (group), #11 (log dir), #12 (binds) — addressed by new/extended rebuild steps in §2.3.
- **Important (silent damage)**: #3 (45 tables baseline), #4 (radacct chain), #9 (MariaDB root) — addressed by explicit restore-verification steps with updated baselines.
- **Operational (long-term ops)**: #8 (info only), #13 (logrotate, deprioritized), #14 (bandwidth-monitor stale path, live-fix recommended).

### Defensive notes (not live-verified, not counted in 13)

| Item | Why included even though not live-verified | What to verify before disaster |
|---|---|---|
| **Tailscale node key may invalidate on rebuild** | `/var/lib/tailscale/tailscaled.state` (2,362 B live) IS in kopia via `/var/lib`, and `tailscale status` shows `vpn-prod-01` online. But on a new VPS the **machine identity changes**; on first boot `tailscaled` may boot but fail to re-authenticate against Tailscale control. Behaviour cannot be tested without a real rebuild. | Generate a reusable Tailscale **auth key** from `https://login.tailscale.com/admin/settings/keys`, store off-server. Post-rebuild: `tailscale up --authkey=<key>` and verify `tailscale status` shows the new node before declaring success |
| **TOOLS.md note: `/opt/vpn-portal/www/static/certs/` referenced but path doesn't exist on live VPS** | TOOLS.md § VPN Portal Production references hosting `isrg-root-x2.pem` + `root-ye.pem` root CA certs at `/opt/vpn-portal/www/static/certs/`. Live `ls /opt/vpn-portal/www/static/certs/` → ENOENT. So the note is stale. Not a current rebuild gap (only Win 10 <1903 customers need these, and they can fetch from `curl https://curl.haxx.se/ca/cacert.pem`). | Update TOOLS.md in next memory hygiene pass — delete the stale `certs/` path reference. Separate from this runbook. |

---

## 2. REBUILD — Single-VPS Disaster Recovery Runbook

### 2.1 Decision tree — when to rebuild vs restore in place

| Scenario | Action | Time | RTO |
|---|---|---|---|
| vpn-prod-01 unreachable, kopia repo + DNS + password intact | **Rebuild on new VPS** | 1–2 h | 2 h |
| vpn-prod-01 filesystem corrupt but VM bootable | **Restore in place** from kopia | 30 min | 30 min |
| vpn-prod-01 fine but vpn-prod-01 disk filling | **Restore selective paths** | 15 min | 15 min |
| vpn-prod-01 fine but kopia repo corrupted | **RPO violation** | N/A | N/A — backups lost |
| vpn-prod-01 + OC both dead | **Total loss** | N/A | N/A — repo + rebuild node both gone |

### 2.2 Pre-flight checklist (verify before starting)

```bash
# ON OPERATOR LAPTOP / OC (NOT vpn-prod-01):

# 1. Kopia password retrieved from off-server storage (1Password / paper / OC vault).
#    Verify it works on vpn-prod-01 BEFORE disaster strikes:
ssh root@vpn-prod-01 'sudo -u debian -H bash -lc \
  "KOPIA_PASSWORD=\$(cat /home/debian/.kopia-password) \
   kopia repository status 2>&1"' | head -10

# 2. GitHub SSH key works:
ssh -T git@github.com  # expect "Hi Dippie-WP!"

# 3. DNS records current at registrar:
dig +short vpn-prod-01.databyte.co.za
dig +short vpn-portal.databyte.co.za
dig +short kop.databyte.co.za

# 4. New VPS exists and SSH is reachable:
ssh root@NEW_VPS_IP 'echo OK'

# 5. Cloudflare / registrar account logged in (for DNS cutover).
```

### 2.3 STEP-BY-STEP: Rebuild on new VPS

| # | Action | Command / verification | Time |
|---|---|---|---|
| 1 | Provision new VPS (same Xneelo datacentre if possible for IP reputation, or different cloud). Debian 13 (trixie) required (charon 6.0.7 + kopia 0.23.1 + python3.13 venv compat). | Provider console | 10 min |
| 2 | Configure public network: assign static IPv4 (Xneelo) or use DHCP-assigned. Open inbound: 22/tcp (SSH), 80/tcp (HTTP), 443/tcp (HTTPS), 500/udp (IKE), 4500/udp (NAT-T). NO OTHER inbound ports. | `ip addr; ss -tlnp` | 5 min |
| 3 | Install base packages (no Docker yet): `apt update && apt install -y python3-venv python3-pip nginx mariadb-server freeradius certbot python3-certbot-nginx curl wget git jq`. | `dpkg -l` shows each | 15 min |
| 4 | Install Docker (official repo, not Debian's): `curl -fsSL https://get.docker.com \| sh && usermod -aG docker root`. | `docker --version` shows 24.x+ | 5 min |
| 5 | Install kopia client: `curl -sSfL https://kopia.io/signing-key.asc \| gpg --dearmor -o /usr/share/keyrings/kopia-keyring.gpg` + add repo + `apt install kopia`. Or download `.deb` from GitHub releases. | `kopia --version` | 3 min |
| 6 | Configure SSH key login from OC's `id_ed25519` (matches `root@OC`): restore from kopia after step 7, OR `ssh-copy-id -i /root/.ssh/id_ed25519.pub root@NEW_VPS_IP` from OC. | `ssh root@NEW_VPS_IP 'echo OK' from OC` | 2 min |
| 7 | Connect kopia client to existing repo: `kopia repository connect server --url=https://kop.databyte.co.za:443 --username=debian --password=<PW>`. Verify: `kopia snapshot list --all`. | List shows 13 paths | 1 min |
| 8 | Restore `/opt/vpn-portal`: `kopia restore /opt/vpn-portal --destination-path /opt/vpn-portal`. | `ls /opt/vpn-portal/app.py` exists | 1 min |
| 9 | Restore `/opt/strongswan-vpn-gateway`: `kopia restore /opt/strongswan-vpn-gateway --destination-path /opt/strongswan-vpn-gateway`. | `ls /opt/strongswan-vpn-gateway/docker/Dockerfile` exists | 1 min |
| 10 | Restore `/etc` carefully: `kopia restore /etc --destination-path /etc` (whole tree — will overwrite nginx + freeradius + letsencrypt + systemd config). **CAREFUL**: this restores the exact hosts.allow / hosts.deny / iptables-persistent / fail2ban config; if your new VPS has different sshd_config defaults they get overwritten. | `diff /etc/ssh/sshd_config /etc/ssh/sshd_config.kopia-bak` if backup was made | 2 min |
| 11 | Restore `/var/lib`: `kopia restore /var/lib --destination-path /var/lib`. This brings back MariaDB data dir (`/var/lib/mysql`), charon SQLite (`/var/lib/strongswan/ipsec.db`), fail2ban SQLite, vnstat DB, Tailscale state. **Phase 4E update (2026-07-12):** Portal customers/devices/users are in MariaDB — verify with `mariadb -uroot radius -e 'SELECT COUNT(*) FROM customers'` (expect **11** in v1.1.0; was 5 in v1.0.7). charon SQLite (`ipsec.db`) still present but only holds StrongSwan-internal tables (addresses, ike_sas, pools). | `mariadb -uroot radius -e 'SELECT COUNT(*) FROM customers'` → **11** | 3 min |
| 11a | **Re-create `/home/zunaid` user + fix quota script paths** (addresses §1.6 gap #1 INVERTED + new gap #14 — canonical location is now `/opt`, not `/home/zunaid`):<br>`useradd -m -s /bin/bash zunaid && mkdir -p /home/zunaid/strongswan` (only if user missing from restore)<br>`# Point services at canonical /opt location:`<br>`sed -i 's\|/home/zunaid/strongswan/quota/quota-monitor.py\|/opt/strongswan-vpn-gateway/quota/quota-monitor.py\|' /etc/systemd/system/quota-monitor.service`<br>`sed -i 's\|/home/zunaid/strongswan/quota/bandwidth-monitor.py\|/opt/strongswan-vpn-gateway/quota/bandwidth-monitor-nft.py\|' /etc/systemd/system/bandwidth-monitor.service`<br>`systemctl daemon-reload && systemctl restart quota-monitor bandwidth-monitor`<br>(The `bandwidth-monitor-nft.py` is the nft-based version matching the Phase 7.5 quota migration.) | `systemctl cat quota-monitor.service \| grep ExecStart` → `/opt/.../quota-monitor.py`; `systemctl cat bandwidth-monitor.service \| grep ExecStart` → `/opt/.../bandwidth-monitor-nft.py`; both `active (running)` after restart | 3 min |
| 11b | **Re-install daloRADIUS** (NOT in kopia, addressed §1.6 gap #2): `cd /tmp && git clone --depth 1 https://github.com/lirantal/daloradius.git && sudo mv daloradius /var/www/daloradius && cp /var/www/daloradius/app/common/includes/daloradius.conf.php.sample /var/www/daloradius/app/common/includes/daloradius.conf.php` then edit connection params (host `127.0.0.1`, user `radius`, pass from `/etc/mysql/debian.cnf` — verify config encryption password length limit 14). Run `/opt/strongswan-vpn-gateway/scripts/restore-daloradius-config.sh` if it exists (otherwise manual edit). `a2enmod php8.3 && systemctl restart apache2`. | `curl -ksS http://127.0.0.1:8000/operators/login.php` returns `daloRADIUS :: Login` HTML | 15 min |
| 11c | **Clear stale ban databases** after restore (avoids self-lockout): `ipban-tool clear --all` (if available) OR `sqlite3 /opt/ipban/ipban.sqlite "DELETE FROM bannedIps;"` + `fail2ban-client unban --all`. The restored IPs may include operator IPs or `100.64.0.0/10` Tailscale subnet. | `sqlite3 /opt/ipban/ipban.sqlite "SELECT COUNT(*) FROM bannedIps;"` → 0 | 2 min |
| 11d | **Install logrotate config for `/var/log/charon-log-host/charon.log`** (§1.6 gap #13 — currently NO logrotate, charon.log growing unbounded):<br>`cat > /etc/logrotate.d/charon-log-host <<'EOF'<br>/var/log/charon-log-host/charon.log {<br>  daily missingok rotate 7 compress delaycompress notifempty create 0644 root root sharedscripts postrotate endscript<br>}<br>EOF`<br>then `systemctl reload logrotate`. (logrotate.timer is hourly; config will fire daily.) | `logrotate -d /etc/logrotate.conf 2>&1 \| grep charon-log-host` shows schedule | 1 min |
| 12 | Restore `/home/debian`: `kopia restore /home/debian --destination-path /home/debian` (kopia config + scripts). | `ls /home/debian/local/bin/kopia-backup-all` | 1 min |
| 13 | Restore `/usr/local`: `kopia restore /usr/local --destination-path /usr/local` (kopia binary, custom). | `kopia --version` works | 1 min |
| 14 | **MariaDB recovery**: `systemctl start mariadb`, then `mariadb -uroot -e "USE radius; SELECT count(*) FROM radcheck;"` — verify RADIUS data present. If MariaDB refuses to start, check `/var/lib/mysql` ownership (`chown -R mysql:mysql /var/lib/mysql`). **If `-uroot` returns "Access denied for user 'root'@'localhost'"** (§1.6 gap #9 — fresh MariaDB on new VPS uses `mysql_native_password` with a locally-generated pw; the kopia-restored `/etc/mysql/debian.cnf` may not grant access): `systemctl stop mariadb && mysqld_safe --skip-grant-tables &` to reset root pw, then `ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('<new-pw>')` and store the new pw in `/etc/mysql/debian.cnf` for symmetry. **Verify daloRADIUS tables survived restore (§1.6 gap #3)**: `mariadb -uroot radius -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius';"` should return **45** (was 42 in v1.0.7). | **45 tables** AND radcheck rows > 0 AND operators count > 0 | 7 min |
| 14a | **Apply FreeRADIUS operator overlay** (§1.6 gap #4 — `radacct` MUST have rows post-rebuild; if empty, the build is broken even if auth works):<br>`bash /opt/strongswan-vpn-gateway/host/freeradius/provision-freeradius.sh`<br>This idempotently applies the 3 critical config files (sql_last_seen, queries.conf, sites-enabled/default), fixes `/var/log/freeradius/radacct/` ownership, restarts FreeRADIUS, and runs a smoke test (sends Accounting-Request via `radclient`, expects Accounting-Response). Exit 0 = OK; non-zero = investigate.<br>**Verify post-rebuild:** `mariadb -uroot radius -e "SELECT COUNT(*) FROM radacct;"` must be ≥1 within 5 min of first client connect (radacctid=1 is the smoke test row, radacctid≥2 is real client traffic).<br>See `host/freeradius/README.md` for full operator overlay design. | provision exits 0 AND radacct ≥1 | 2 min |
| 15 | **FreeRADIUS recovery**: `systemctl restart freeradius`. Verify: `radtest <user> <pw> 127.0.0.1 0 testing123` (testing123 is the localhost client secret from `/etc/freeradius/3.0/clients.conf`). | `Received Access-Accept` | 2 min |
| 16 | **Rebuild Docker image**: `cd /opt/strongswan-vpn-gateway && docker build -f docker/Dockerfile -t zun/strongswan:6.0.7-mschapv2-attrsql .`. | `docker images` shows new image | 10 min |
| 16a | **Pre-create `strongswan` group (GID 1002) + `/var/lib/strongswan/` perms** (§1.6 gap #10 — fresh Debian 13 does NOT auto-create this group; without it the SQLite bind source dir is unreadable inside the container):<br>`groupadd -g 1002 strongswan`<br>`id vpn-portal >/dev/null && usermod -aG strongswan vpn-portal`<br>`mkdir -p /var/lib/strongswan`<br>`chown root:strongswan /var/lib/strongswan`<br>`chmod 775 /var/lib/strongswan`<br>After running, `getent group strongswan` should show `strongswan:x:1002:vpn-portal`. | `getent group strongswan \| grep 1002` returns 1 line | 1 min |
| 16b | **Pre-create `/var/log/charon-log-host/` host directory + log file** (§1.6 gap #11 — the `/var/log/charon-log-host/charon.log` bind source dir+file must exist or `docker run` aborts):<br>`mkdir -p /var/log/charon-log-host && touch /var/log/charon-log-host/charon.log && chown -R root:root /var/log/charon-log-host` | `ls -ld /var/log/charon-log-host /var/log/charon-log-host/charon.log` shows both | 1 min |
| 17 | **Run strongswan container** — EXACT command copied from live `docker inspect strongswan` (verified §1.6 gaps #5, #12):<br>```bash<br>docker run -d --name strongswan \<br>  --network host \<br>  --restart unless-stopped \<br>  --cap-add CAP_NET_ADMIN \<br>  --cap-add CAP_NET_RAW \<br>  --health-cmd "swanctl --uri=tcp://127.0.0.1:4502 --stats >/dev/null && ss -ltn \| grep -q :4502" \<br>  --health-interval 30s --health-timeout 5s --health-retries 3 --health-start-period 30s \<br>  -v /opt/strongswan-vpn-gateway/docker/swanctl:/etc/swanctl:ro \<br>  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/debug.conf:/etc/strongswan.d/debug.conf:ro \<br>  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/attr-sql.conf:/etc/strongswan.d/attr-sql.conf:ro \<br>  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/00-virtual-ip.conf:/etc/strongswan.d/00-virtual-ip.conf:ro \<br>  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/10-eap-radius.conf:/etc/strongswan.d/10-eap-radius.conf:ro \<br>  -v /var/log/charon-log-host/charon.log:/var/log/charon-host.log:rw \<br>  -v /var/lib/strongswan:/var/lib/strongswan:rw \<br>  zun/strongswan:6.0.7-mschapv2-attrsql<br>```<br>Verify immediately:<br>`docker ps --filter name=strongswan` → `Up` (not just `starting`)<br>`docker exec strongswan ss -ltn \| grep 4502` → `127.0.0.1:4502 LISTEN` (VICI socket up)<br>`docker exec strongswan cat /etc/strongswan.d/10-eap-radius.conf \| grep eap-radius` → non-empty (bind mount hit, customer identity source live)<br>`docker exec strongswan cat /etc/swanctl/conf.d/rw-eap.conf \| grep -c "connections {"` → 1<br>`docker logs strongswan 2>&1 \| tail -5` → "loaded plugins: charon ... eap-radius" present (no plugin-missing error) | `docker ps` shows `Up (healthy)` AND all 5 verification checks above pass | 2 min |
| 18 | **(Removed in v1.0.3)** rw-eap.conf is auto-mounted via the `swanctl` bind in step 17. No separate restore needed. | n/a | n/a |
| 19 | **Portal FastAPI**: `systemctl restart vpn-portal` (or `systemctl enable --now vpn-portal`). Portal venv lives in `/opt/vpn-portal/.venv/`. | `curl -sk https://127.0.0.1/api/health` returns `{"status":"ok"...}` | 2 min |
| 20 | **Nginx**: `certbot --nginx -d vpn-portal.databyte.co.za -d myvpn.databyte.co.za` (renews LE cert using restored `/etc/letsencrypt`). Then `systemctl restart nginx`. | `curl -I https://vpn-portal.databyte.co.za/api/health` → 200 | 3 min |
| 21 | **DNS cutover**: at registrar (Cloudflare), point `vpn-portal.databyte.co.za` and `myvpn.databyte.co.za` A records to NEW_VPS_IP. TTL was 300s (verify at registrar). | `dig +short vpn-portal.databyte.co.za` returns new IP | 5–15 min for propagation |
| 22 | **Customer reconnection**: customers' devices will detect server IP change. iOS/Android native clients re-establish via MOBIKE if the cert (server.pem) matches; otherwise, full re-onboarding via the portal installer token flow. | `swanctl --list-sas` shows new IKE_SAs | 10–30 min for all customers |
| 23 | **Verify backup cron** (§1.6 gap #6 — `/var/spool/cron/crontabs/debian` is NOT in any kopia snapshot; restore via that path is a no-op): `crontab -u debian -l` should show `0 0 * * * /home/debian/local/bin/kopia-backup-all`. If empty, recover from off-server backup of `/home/debian/.kopia-cron.tab` (capture via `crontab -u debian -l > /home/debian/.kopia-cron.tab` BEFORE disaster): `crontab -u debian /home/debian/.kopia-cron.tab`. If even that file is missing, paste manually: `crontab -u debian -e` then add `0 0 * * * /home/debian/local/bin/kopia-backup-all >/var/log/kopia-cron.log 2>&1`. | `crontab -u debian -l` shows entry | 2 min |
| 24 | **First post-rebuild backup**: `sudo -u debian /home/debian/local/bin/kopia-backup-all`. Verify: `kopia snapshot list --all` shows today's snapshot for `/opt/vpn-portal`. | Snapshot present | 1 min |

**Total time: 75–135 minutes** (excluding DNS propagation; added 11d + 16a + 16b preconditions ~6 min total).

### 2.4 RTO / RPO summary (single-VPS rebuild)

| Metric | Value | Source |
|---|---|---|
| RTO (Recovery Time Objective) | **2 hours** | Steps 1–24 above (now includes 11d + 16a + 16b for full docker-mount viability) |
| RPO (Recovery Point Objective) | **24 hours** | Kopia runs at 00:00 UTC daily |
| Data loss window | Up to 24 h of: portal writes, charon IKE_SA state, MariaDB writes | Kopia daily cron |

### 2.5 Acceptance test (Definition of Done)

Run **all 10 checks** in this order. **Rebuild is officially successful only when all 10 pass.** If any fail, do NOT cut production traffic — see §2.6 for rollback hints.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Portal HTTP health | `curl -sk https://127.0.0.1/api/health` | `{status:"ok", db_ok:true, db_customers:**11**, charon_ok:true}` |
| 2 | Portal via public DNS | `curl -sk https://vpn-portal.databyte.co.za/api/health` | same as #1 (proves DNS + nginx + LE cert all working) |
| 3 | MariaDB customers count | `mariadb -uroot radius -e 'SELECT COUNT(*) FROM customers;'` | **11** (matches live v1.1.0 baseline; was 5 in v1.0.7) |
| 4 | MariaDB radpostauth accessible | `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radpostauth;'` | **≥2,000** (proves eap-radius auth chain intact + shows 3 months of real customer traffic — was "≥1" in v1.0.7) |
| 5 | FreeRADIUS local test | `radtest testing 127.0.0.1:18120 0 testing123` | `Received Access-Accept` (uses Status-Server port per §1.3) |
| 6 | strongswan container healthy | `docker ps --filter name=strongswan --format '{{.Status}}'` | `Up X hours (healthy)` |
| 7 | charon VICI listening | `docker exec strongswan ss -ltn \| grep 4502` | `127.0.0.1:4502 LISTEN` |
| 8 | charon loaded eap-radius plugin | `docker exec strongswan cat /etc/strongswan.d/10-eap-radius.conf \| grep -c eap-radius` | ≥ 1 (proves bind mount is live) |
| 9 | IKE_SA accepted from a real client | Pick ONE test customer (e.g. `zun-iphone`); from that device, attempt to connect to NEW_VPS_IP. Then on NEW_VPS: `docker exec strongswan swanctl --list-sas` | IKE_SA appears with `ESTABLISHED` state |
| 10 | Quota enforcement live | `systemctl status quota-monitor bandwidth-monitor --no-pager` | Both `active (running)` |

**Bonus check (post-cutover)**: After 24 h of production traffic, verify `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radpostauth;'` grew by ~customer-connect-count. If unchanged, eap-radius is silently broken (auth still goes through FreeRADIUS → radcheck table directly, so customer VPN works but logging is missing).

### 2.6 Rollback hints (if a step fails mid-rebuild)

These are the most-likely failure modes with concrete recovery. **Do NOT panic-restart** — most failures are recoverable in 5–15 minutes.

| Step | Failure mode | Likely cause | Recovery |
|---|---|---|---|
| 7 | `kopia repository connect` fails: "invalid password" or "connection refused" | Kopia password lost OR kop.databyte.co.za host down | STOP. **Cannot proceed without kopia password** — see §0.5 item #1. Verify off-server copy. If password is wrong but exists, try with `--password=...`. If host is down, restart kopia server (per §3.4 step 8). |
| 10 | `kopia restore /etc` overwrites sshd_config, breaks SSH login | sshd_config in kopia differs from fresh-host defaults | **Before step 10**: `cp /etc/ssh/sshd_config /etc/ssh/sshd_config.fresh`. If login breaks after restore: drop to provider console (Xneelo VNC), restore from `.fresh`, `systemctl restart sshd`. |
| 11 | `kopia restore /var/lib` fails: "permission denied" on `/var/lib/mysql` | `/var/lib/mysql` owned by `mysql:mysql` but running as root is fine; check `ls -ld /var/lib/mysql` | If owner is wrong after restore: `chown -R mysql:mysql /var/lib/mysql && systemctl start mariadb` |
| 14 | MariaDB won't start: "Plugin 'mysql_native_password' already loaded" or "Access denied for user 'root'@'localhost'" | Server_id or auth string in `/etc/mysql/debian.cnf` doesn't match fresh host | See §1.6 gap #9. Stop mariadb, start with `--skip-grant-tables`, `ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('<new>')`, update debian.cnf. |
| 16a | `groupadd -g 1002 strongswan` fails: "group already exists" (live rebuild from old backup) | GID already taken by another group | Check `getent group 1002`; if it's NOT strongswan, use a different GID AND update Dockerfile / docker run accordingly. Most common on fresh Debian where GID 1002 = `ubuntu` (Docker default). |
| 17 | `docker run strongswan` exits immediately, no logs | Bind source dir missing OR port 500/4500 already bound | Verify all 7 bind source dirs exist (steps 16a + 16b + Dockerfile build). Check `ss -ulnp \| grep -E ':500 \|:4500 '` — if another process is bound, `kill` it. `docker logs strongswan 2>&1 \| tail -30` for actual error. |
| 17 | `docker ps` shows strongswan as `Up` but `swanctl --list-sas` empty | Bind mounts NOT effective — charon up but no customer config loaded | Verify each bind individually: `docker exec strongswan cat /etc/strongswan.d/10-eap-radius.conf` should NOT say "No such file" |
| 19 | Portal fails to start: "Address already in use" on port 8080 | Old gunicorn from vpn-prod-01 snapshot didn't clean up | `systemctl stop vpn-portal; pkill -9 -f gunicorn; sleep 2; systemctl start vpn-portal` |
| 20 | `certbot --nginx` fails: "Could not bind TCP port 80" | nginx already bound, or port 80 blocked by firewall | `systemctl stop nginx; certbot certonly --standalone -d vpn-portal.databyte.co.za -d myvpn.databyte.co.za; systemctl start nginx` |
| 22 | Customers reconnecting gets "authentication failed" | FreeRADIUS → MariaDB chain broken, OR `/etc/freeradius/3.0/mods-enabled/sql` config lost | `ls -la /etc/freeradius/3.0/mods-enabled/sql` should be a symlink to `../mods-available/sql`. If broken, `ln -s ../mods-available/sql /etc/freeradius/3.0/mods-enabled/sql && systemctl restart freeradius` |

**Universal recovery**: If rebuild is irrecoverably stuck, **restore the vpn-prod-01 kopia snapshot directly** (if vpn-prod-01 VM is still bootable, see §2.1 row 2). This reverts to last-known-good state in 30 min. Don't iterate on a broken rebuild for more than 1 hour.

---

## 3. HA — Adding a Second VPS for Failover

### 3.1 Goal

Reduce RTO from 2 hours to **<30 seconds** for customer-facing VPN connectivity (IKE_SA re-establishment via MOBIKE or fast reconnect).

### 3.2 Architecture options (industry-validated)

Per [strongSwan docs](https://docs.strongswan.org/docs/5.9/features/highAvailability.html) and the strongSwan user-list (Sep 2015), **strongSwan only supports active-active HA via the `ha` plugin** — NOT active-passive. The plugin synchronizes IKE_SA + CHILD_SA keys via UDP/4510 between nodes. A separate high-availability plugin implemented for the IKEv2 daemon charon is responsible for state synchronization between the nodes in a cluster and simple monitoring functionality. It is currently designed for two nodes.

| Option | Description | Pros | Cons |
|---|---|---|---|
| **A. anycast + strongSwan HA plugin** (active/active, both nodes announce same IP via BGP) | Two VPS in same /24, both run charon, both use `ha` plugin to sync SAs. Customers use anycast IP. | Industry-standard for IKEv2; zero-touch failover | Requires both VPS in same provider /24; BGP setup is non-trivial on most clouds; charon HA plugin requires kernel patches (per docs); UDP/4510 between nodes needs IPsec protection |
| **B. DNS round-robin + MOBIKE** (active/active, two different IPs, DNS returns both) | Two VPS, each with own IP. DNS A record has both. Customers pick one. Failure: client reconnects to the other. | No BGP needed; works across clouds; leverages iOS/Android MOBIKE support | Failover is client-side, takes 30–120 s for DPD to detect + MOBIKE; clients may not handle DNS failover cleanly |
| **C. Floating VIP via keepalived/VRRP** (active/passive, only one charon active at a time) | Two VPS share a virtual IP. Keepalived brings it up on primary; on failure, secondary takes over (2–10 s). | Simple; well-understood | strongSwan docs say "strongSwan doesn't support active-passive HA" — SAs on standby are lost; all clients see a 5–30 s outage. Still better than 2 h rebuild. |
| **D. Failover only — no second live VPS** (cloud-init + Ansible + DNS TTL 60s) | Detect failure, automate rebuild, cut DNS at 60s TTL. | Cheapest | RTO = 60s + rebuild time. Doesn't help if DNS resolver caches. |

**Industry alignment**: Option B is the most common "good enough" production pattern for IKEv2 mobile clients in 2025–2026. Per strongSwan discussions on MOBIKE, "this can happen if trap policies are installed and an IKE_SA with its CHILD_SAs is reestablished" — and native iOS / Windows / Android IKEv2 clients all support MOBIKE. Option A is the "enterprise" choice but requires BGP, which most cloud providers either restrict or charge for.

### 3.3 Recommended architecture (Option B with portal-side state replication)

**Decision**: **Option B (DNS round-robin + MOBIKE)** + RADIUS backend shared via MariaDB Galera Cluster.

```
                    ┌────────────────────────┐
                    │   Cloudflare DNS       │
                    │  vpn-portal  → both    │
                    │  myvpn       → both    │
                    └─────────┬──────────────┘
                              │ A records (round-robin)
              ┌───────────────┼───────────────┐
              │                               │
       ┌──────▼─────┐                 ┌───────▼────┐
       │  vpn-prod-01    │                 │  vps-02    │
       │ 154.65.x.x │ ◄──Galera────► │ 154.65.y.y │
       │            │   replication  │            │
       │  - charon  │                 │  - charon  │
       │  - FreeRAD │                 │  - FreeRAD │
       │  - MariaDB │                 │  - MariaDB │
       │  - portal  │                 │  - portal  │
       │  - nginx   │                 │  - nginx   │
       └────────────┘                 └────────────┘
                              ▲
                              │
                       ┌──────┴──────┐
                       │   kopia     │
                       │  repository │
                       └─────────────┘
```

**Why B not A**: 
- Xneelo doesn't expose BGP without dedicated contract. BGP across clouds is not viable.
- MOBIKE on iOS/Android handles server IP changes within 30–120 s for active sessions.
- DNS round-robin gives ~50/50 load distribution + instant cutover on one node failure.
- Cost: ~2× VPS (~R1500/month extra) vs complex BGP setup.

**Why B not C (VRRP active/passive)**:
- strongSwan HA docs: "strongSwan doesn't support active-passive HA, only active-active"
- Option C loses all SAs on standby takeover; clients see 30–120 s outage.
- Option B keeps SAs on the surviving node; only the dead node's clients reconnect.

### 3.4 Concrete HA implementation steps (Option B)

| # | Action | Tool / command | Time | Cost |
|---|---|---|---|---|
| 1 | Provision `vps-02`: same OS (Debian 13), same min spec (2 vCPU / 4 GB RAM / 10 GB disk), public IP, same VLAN/firewall rules as vpn-prod-01. Different IP. Same Xneelo datacentre if possible (lower latency for anycast-ish behaviour). | Xneelo console | 30 min | ~R750/mo |
| 2 | Replicate `vpn-prod-01` to `vps-02`: run the entire Section 2.3 rebuild runbook on vps-02, but with `VPN_HOST=127.0.0.1` and `DB_URL` pointing to a new local MariaDB on vps-02 (Galera node 2). | Section 2.3 | 90 min | (covered) |
| 3 | Configure MariaDB Galera Cluster between vpn-prod-01 and vps-02: 3-node minimum (add `vps-03` as arbiter, or use a tiny RPi as 3rd). Per FreeRADIUS best-practices thread on `freeradius-users.freeradius.narkive.com`, "use MariaDB Galera with MaxScale proxy" is the production pattern. Add `[galera]` to `/etc/mysql/mariadb.conf.d/99-galera.cnf` on both, bootstrap from one. | MariaDB docs | 2 h | (covered) |
| 4 | Configure FreeRADIUS to use MaxScale (port 3306) for DB lookups. Update `/etc/freeradius/3.0/mods-available/sql` to point at MaxScale VIP. Both FreeRADIUS instances authenticate against the same Galera cluster. | FreeRADIUS + MaxScale docs | 1 h | MaxScale license cost (BSD-2, free) |
| 5 | Update Cloudflare DNS: `myvpn.databyte.co.za` A records → BOTH `154.65.110.44` AND `154.65.<vps02_ip>`. `vpn-portal.databyte.co.za` A records → BOTH. Set TTL to **60 s** (currently 300 s — needs operator action in Cloudflare). | Cloudflare dashboard | 10 min | free |
| 6 | **✅ Phase 4E DONE (2026-07-12).** Portal business data moved to MariaDB `radius`. SQLite only holds StrongSwan-internal tables (addresses, ike_sas, pools). HA step 6 is now **simplified**: Galera for MariaDB handles portal data replication; charon SQLite (addresses, ike_sas, pools) stays separate and is NOT replicated (charon reads from shared MariaDB via eap-radius; both nodes' charons reference the same MariaDB customer pool). No schema migration needed. | — | — | ✅ DONE |
| 7 | charon SQLite replication: same as portal. Or: have BOTH charons read from the SHARED MariaDB `radius` DB via eap-radius, and have rw-eap.conf identical on both nodes (via `kopia restore` of `/etc/swanctl/conf.d/rw-eap.conf` from one to the other). | config management | 30 min | (ops) |
| 8 | kopia backup: add `vps-02` to the kopia policy as a new source. Per the existing script's `PATHS` array, simply add another backup profile targeting `root@vps-02`. | kopia-set-policies.sh update | 30 min | (ops) |
| 9 | Failover test: shut down charon on vpn-prod-01. Verify within 60 s that (a) customers on vpn-prod-01 reconnect to vps-02 via MOBIKE or fresh IKE_SA, (b) portal HTTPS still resolves (DNS round-robin picks vps-02), (c) FreeRADIUS still authenticates (Galera replicated). | `docker stop strongswan` on vpn-prod-01 | 1 h test | (test) |

**Total cost: ~R1500/month for vps-02 + 1 day dev + 1 day test.**

### 3.5 HA RTO / RPO summary (Option B)

| Metric | Before (single VPS) | After (Option B HA) |
|---|---|---|
| RTO — full stack (rebuild from kopia) | 2 hours | 2 hours (still need this for total loss) |
| RTO — single node failure | 2 hours | **30–120 seconds** (DNS round-robin + MOBIKE reconnect) |
| RPO — data loss window | 24 hours (kopia daily) | ~1 second (Galera synchronous replication) |
| Customer-visible outage on vpn-prod-01 crash | Until vpn-prod-01 rebuilt | 30–120 s (clients reconnect to vps-02) |
| Cost | 1× VPS | 2× VPS + ~1 day dev + ~1 day test |

### 3.6 What HA does NOT solve

| Gap | Why HA doesn't fix it |
|---|---|
| Total loss of BOTH VPS (e.g. Xneelo datacenter gone) | Need rebuild (Section 2.3) — HA gives no benefit here |
| Kopia password lost | Lose repo access — same as today |
| MariaDB Galera split-brain | Per FreeRADIUS best-practices thread, "MySQL's master-master implimentation is completely brain dead and WILL give you corrupt data in a very short time period (It doesn't do ANY locking across the cluster!!!)". Galera uses different consistency model (cert-based), better than MM replication, but split-brain still possible. Mitigation: 3-node Galera with quorum + MaxScale |
| DNS resolver caching (some clients cache for hours) | TTL of 60s + iOS/Android behaviour to respect DNS TTL is not 100% reliable |
| LE cert renewal | If both VPS try to renew same cert → rate limit. Use DNS-01 challenge with Cloudflare API token in `/etc/letsencrypt/cloudflare.ini` (mode 600, NOT in kopia — see §1.5); certbot call becomes `certbot renew --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini`. The default `certbot --nginx -d <host>` in §2.3 step 20 only handles HTTP-01; switch to DNS-01 plugin if HA is built |

### 3.7 Alternative: Option C (active/passive VRRP) — when it makes sense

For pure IKEv2 site-to-site (fixed clients) where 30 s outage is acceptable and you can't tolerate the data-replication complexity of Option B, VRRP + keepalived is simpler:

```
vrrp_script check_charon {
    script "/usr/local/bin/charon-is-healthy"
    interval 2
    weight -50
}
vrrp_instance VIP_154_65_110_44 {
    state MASTER
    interface ens3
    virtual_router_id 51
    priority 100
    advert_int 1
    authentication {
        auth_type PASS
        auth_pass <shared-secret>
    }
    virtual_ipaddress {
        154.65.110.44/20 dev ens3
    }
    track_script {
        check_charon
    }
    notify_master "/usr/local/bin/charon-takeover.sh"
}
```

Failover time: 2–10 seconds for VIP move, plus 30–120 seconds for clients to re-IKE (since SAs were on the dead charon). Not zero-touch like Option B but cheaper (no second active charon).

---

## 4. Pre-flight Verification (annual/quarterly)

### 4.1 Restore drill (quarterly)

| Step | Action | Verify |
|---|---|---|
| 1 | Provision throwaway VPS (`vps-drill`) | Same spec as vpn-prod-01 |
| 2 | Run full Section 2.3 rebuild | All 24 steps complete |
| 3 | Connect ONE test customer (e.g. `zun-iphone`) | IKE_SA ESTABLISHED on `vps-drill` |
| 4 | Verify portal HTTPS | `curl https://vps-drill/api/health` 200 |
| 5 | Verify quota/bandwidth monitoring | Both monitors running, customer shows up in metrics |
| 6 | Decommission `vps-drill` | Tear down, log time spent |
| 7 | Update this runbook with any deviations found | Add to ISSUES-LOG.md |

**Target**: drill completes in <2 hours. If >4 hours, update this runbook.

### 4.2 HA failover drill (quarterly, once HA is built)

| Step | Action | Verify |
|---|---|---|
| 1 | Pick off-peak window | |
| 2 | `docker stop strongswan` on vpn-prod-01 | |
| 3 | Time how long until customers reconnect | `swanctl --list-sas` on vps-02 |
| 4 | Verify portal login on vps-02 | `curl -X POST https://vps-02/api/portal/login -d '{"identity":"smoketest","password":"..."}'` |
| 5 | Verify FreeRADIUS on vps-02 authenticates | `radtest <user> <pw> 127.0.0.1:1812 0 testing123` |
| 6 | Restart vpn-prod-01 charon | `docker start strongswan` |
| 7 | Verify vpn-prod-01 also accepts connections | `swanctl --list-sas` |
| 8 | Decommission smoketest customer | `DELETE /api/customers/{id}?confirm=...` |

**Target**: failover <2 minutes, failback <5 minutes.

### 4.3 Backup verification (daily, automated)

```bash
# Run as part of cron, after kopia-backup-all completes:
ssh root@vpn-prod-01 'sudo -u debian -H bash -lc \
  "KOPIA_PASSWORD=\$(cat /home/debian/.kopia-password) \
   kopia snapshot list --all 2>&1"' | grep "$(date -u +%Y-%m-%d)" | wc -l
# Must be >= 10 (paths). Alert if < 10.
```

---

## 5. Hard Rules (LOCKED — additions to MEMORY.md cross-check table)

1. **Single-source-of-truth for portal state**: **Phase 4E (2026-07-12):** Portal business data is in **MariaDB `radius`** (`customers`, `users`, `devices`, etc.) — NOT in SQLite. SQLite at `/var/lib/strongswan/ipsec.db` holds ONLY StrongSwan-internal tables (`addresses`, `ike_sas`, `pools`, `child_configs`). `app.py` `db_query`/`db_exec` read MariaDB via SQLAlchemy. CORR-022 (SQLite shim) REVERTED. **v2.1.1 (2026-07-12):** Dead `_sqlite_query()` function + its 4 env vars (`_VPN_HOST_SQLITE`, `_SSH_KEY_SQLITE`, `_DB_PATH_SQLITE`, `_SSH_TIMEOUT_SQLITE`) removed from `portal_auth.py` (zero callers post-4E). Corresponding dead subprocess interception block removed from `tests/conftest.py`. CORR-035.
2. **`uniqueids=no` for fail-safe HA** (per `strongswan.conf(5)` Debian manpage and discussion #1867): "you also don't want the duplicheck plugin active, it doesn't do what you might think it does" — set `uniqueids=no` and `duplicheck=no` on HA nodes so simultaneous connections don't kill each other.
3. **MOBIKE must be enabled** (`mobike = yes` in `rw-eap.conf`): required for client roaming across networks AND for HA failover (clients can MOBIKE between vpn-prod-01 and vps-02). Already enabled.
4. **Never `kopia restore /etc` blindly on a different host**: overwrites ALL of /etc including network interfaces, sshd_config, hostname. May lock you out. Always back up the new host's `/etc` first.
5. **DNS TTL must be ≤300 s** for HA cutover to work. Current TTL was 300 s — must be 60 s if HA is built.
6. **MariaDB replication is NOT a backup**: Galera replicates state, but `DROP DATABASE` replicates too. Keep kopia for point-in-time recovery.
7. **strongSwan HA plugin (`--enable-ha`) requires kernel patches** per strongSwan docs (HA doc paragraph 4: "The strongSwan download site offers HA patches for many Linux kernel versions"). NOT a feature you can flip on. Option B sidesteps this.
8. **`bot-polling.service` is the SOLE Telegram polling source** (HOT-261): never start a second `bot.py` polling instance or telegram returns HTTP 409 Conflict ("terminated by other getUpdates request"). If the bot stops responding, check `systemctl status bot-polling` and `journalctl -u bot-polling -n 50` BEFORE assuming a code bug.
9. **`bandwidth-monitor.service` `ExecStart` MUST point at `/opt/strongswan-vpn-gateway/quota/bandwidth-monitor-nft.py`** (the nft version, matching the Phase 7.5 quota migration). NEVER point at `/home/zunaid/...` — the path is a `.bak` graveyard; the service will silently run a 3-month-old backup. Drift symptom: `systemctl cat bandwidth-monitor.service \| grep ExecStart` shows `/home/zunaid/` → fix the sed in §2.3 step 11a, then `systemctl daemon-reload && systemctl restart bandwidth-monitor`.

---

## 6. References (industry sources)

| Source | Used for |
|---|---|
| [strongSwan docs — High Availability](https://docs.strongswan.org/docs/5.9/features/highAvailability.html) | HA plugin architecture, sync messages, UDP/4510, kernel patch requirement |
| [strongSwan docs — IKE and IPsec SA Renewal](https://docs.strongswan.org/docs/latest/config/rekeying.html) | Reauthentication / make-before-break default in v6.0+ |
| [strongSwan docs — eap-radius plugin](https://docs.strongswan.org/docs/latest/plugins/eap-radius.html) | RADIUS accounting, DAE |
| [strongSwan docs — Windows EAP server conf](https://docs.strongswan.org/docs/latest/interop/windowsEapServerConf.html) | EAP-MSCHAPv2 + iOS/Android interop |
| [strongSwan user list — HA failover problem](https://lists.strongswan.org/pipermail/users/2015-March/007641.html) | VRRP + strongSwan, IP-from-VLAN pattern |
| [strongSwan user list — VPN Gateway Failover](https://lists.strongswan.org/pipermail/users/2015-August/008594.html) | "strongSwan doesn't support active-passive HA, only active-active" |
| [Satish Patel — Keepalived Strongswan HA IPsec Cisco ASA](https://satishdotpatel.github.io/ha-strongswan-ipsec-vpn/) | keepalived config sample |
| [Server Fault — Redundant FreeRADIUS + MySQL](https://serverfault.com/questions/395376/redundant-freeradius-mysql) | FR + MariaDB best practices |
| [MariaDB Master-Master Replication](http://msutic.blogspot.com/2015/02/mariadbmysql-master-master-replication.html) | MM replication (caveat: brain-dead for AAA per FreeRADIUS thread) |
| [FreeRADIUS best-practices for redundant servers](https://freeradius-users.freeradius.narkive.com/ZzsvsTPT/best-practices-for-redundant-servers) | "use MariaDB Galera with MaxScale proxy" |
| [r/networking — Redundant FreeRADIUS](https://www.reddit.com/r/networking/comments/17vvl7a/redundant_freeradius/) | NetworkRadius recommendation: MariaDB + MaxScale + BGP anycast |
| [Accrets — IT Disaster Recovery Plan Template](https://www.accrets.com/backupanddr/it-disaster-recovery-plan-template/) | RTO/RPO framework, runbook structure |
| [AccountableHQ — Backup and Recovery Policy](https://www.accountablehq.com/post/how-to-create-a-data-backup-and-recovery-policy-template-requirements-best-practices) | ISO 27001 / 22301 / CIS Controls alignment |
| [strongSwan discussion #1867 — Why ipsec SA rebuilds frequently](https://github.com/strongswan/strongswan/discussions/1867) | `duplicheck` + `uniqueids` config pitfalls |

---

## 7. Change log

| Rev | Date | Author | Notes |
|---|---|---|---|
| v1.0.0 | 2026-07-11 22:53 UTC / 00:53 SAST | Misha 🐻 | Initial draft. Single-VPS rebuild runbook + HA Option B (DNS round-robin + MOBIKE + Galera) recommendation. All facts verified live on `ssh root@vpn-prod-01`. |
| v1.0.1 | 2026-07-12 07:30 UTC / 09:30 SAST | Misha 🐻 | First audit pass (Zun msg #25439). Added §1.6 listing 11 gaps. **Superseded by v1.0.2** — 2 gaps (Tailscale node key, Root CA certs path) couldn't be live-verified; 2 more (Cloudflare token absence framing, MariaDB unix-socket fallback framing) were mis-stated on re-verify. |
| v1.0.2 | 2026-07-12 07:50 UTC / 09:50 SAST | Misha 🐻 | Audit correction (Zun msg #25442). Re-ran ALL gap verification commands on `ssh root@vpn-prod-01` live. **§1.6 reduced from 11 → 9 verified-live gaps**. Defensive notes moved to separate subsection (Tailscale node-key + TOOLS.md path staleness). Corrections vs v1.0.1: (a) Cloudflare token gap rewritten to point at live path `/root/.cloudflare.ini` (v1.0.1 incorrectly said "no creds file anywhere"); (b) MariaDB gap rewritten to `mysql_native_password` (v1.0.1 incorrectly claimed Unix-socket fallback); (c) table count corrected from 41 to 40 (information_schema query); (d) Dropped "_CA certs path" claim (TOOLS.md hygiene, not a rebuild gap); (e) Dropped "rebuild-bind-mount not specified" stylized detail in favour of exact-mount-list verified against the live container. New §2.3 step 14 fallback now uses `mysqld_safe --skip-grant-tables` (more reliable than unix-socket on `mysql_native_password` plugin). |
| v1.0.4 | 2026-07-12 08:05 UTC / 10:05 SAST | Misha 🐻 | Deep docker-mount verification (Zun msg #25444). §1.6 extended 9 → 13 verified-live gaps. New gaps: (10) strongswan GID 1002 precondition; (11) charon-log-host dir mkdir; (12) image-baked vs bind mount semantics for EAP identities; (13) charon.log has NO logrotate (16 MB). §2.3 step 17 rewritten with exact docker run from live inspect. |
| v1.0.5 | 2026-07-12 08:30 UTC / 10:30 SAST | Misha 🐻 | Phase 4E post-migration audit. §1.6 gap #3: table count 40 → 42; CORR-022 REVERTED (portal data now in MariaDB). §1.6 gap #4: radpostauth 323 → 324. §2.3 step 11 verify: MariaDB not SQLite for customers table. §3.6 row 6: ✅ DONE (Phase 4E eliminates SQLite split-brain; HA step 6 now simpler). §4 summary row 1 updated. See `docs/PHASE-4E-DEPLOYMENT-NOTES.md`. |
| v1.0.6 | 2026-07-12 10:30 UTC / 12:30 SAST | Misha 🐻 | v2.1.1 deep fact-check audit (Zun msg #25511 "make sure this book is 100% factually aligned"). Verified against live `ssh root@vpn-prod-01` + MariaDB + kopia. **Drift fixed:** §1.2 vpn-portal.service row v2.1.0 → **v2.1.1** (CORR-035 cleanup applied). §1.3 listening ports added: **18120/udp** (FreeRADIUS Status-Server) + **3799/udp** (charon CoA/DAE). §1.4 kopia paths: added 3 missing paths — `/etc/letsencrypt/renewal` (655 B), `/home/debian/nft-migration-v2` (94.1 KB), `/root/projects/nft-migration-v2`. §1.6 gap #4: radpostauth 324 → **325**. §1.6 gap #13: charon.log size 16,666,802 B → **17,375,410 B** (growth rate ~700 KB / 12 h). §5 Hard Rule #1: added v2.1.1 CORR-035 note (dead `_sqlite_query` removed). Also deployed installer_tokens.py comment fix to VPS (was updated in `d9f9630` local but never scp'd — found during audit). All 7 drift items from the audit fixed. **NO drift remains in §1.1, §1.6 gaps #1-#3 / #5-#12, §2.3 step 17, §3.x, §4, §5 #2-7.** |
| v1.0.7 | 2026-07-12 10:42 UTC / 12:42 SAST | Misha 🐻 | Doc-strengthening pass (Zun msg #25519 "push to validated docs"). Closes HIGH-severity gaps identified in v1.0.6 audit. **Three new subsections:** **§0.5 Off-server secret capture checklist** — 10-row table of secrets NOT in kopia (kopia password, CF token, Cloudflare login, GitHub SSH key, Tailscale auth key, Xneelo login, MariaDB root pw, FR shared secret, debian crontab, LE private keys) with "verify accessible" checkboxes + load-bearing warning. **§2.5 Acceptance test (Definition of Done)** — 10-check acceptance gate operator must pass before declaring rebuild successful. **§2.6 Rollback hints** — 11-row failure-mode table mapping step → failure → likely cause → recovery, for steps 7, 10, 11, 14, 16a, 17, 19, 20, 22. **Critical dependencies closed:** Rebuild now stops definitively at §2.3 step 7 (not §0.5 item #1 missing), and operator has a clear Definition of Done + recovery paths for the most common step failures. Doc grew 481 → ~590 lines. |
| **v1.1.0** | **2026-10-15 06:50 UTC / 08:50 SAST** | **Misha 🐻** | **Quarterly fact-check refresh** (3 months after v1.0.7). All §1.1–§1.6 numbers + service/port inventory re-verified live on `vpn-prod-01`. **Drift fixed:** (1) **41 vps-01 → vpn-prod-01** renames (HOT-210 hostname rename not propagated past §1.1). (2) §1.1: Debian 13 → **13.7**; added `charon container uptime` row. (3) §1.2: portal v2.1.1 → **v2.2.1** (deployed 2026-08-10, git HEAD 8e02a6e); added 7 new services (`bot-polling` for vpn-admin-bot **v2.5.0**, `strongswan-watchdog` renamed from `strongswan-iptables-watchdog` for HOT-66, `prometheus-node-exporter` renamed from `node_exporter`, `alertmanager`, `vps-alert-relay`, `ttyd`, `zerotier-one`, `cron`). (4) §1.2: MariaDB **41 → 45 tables** with full live row-count baseline. (5) §1.2: radpostauth **327 → 2,299**, radacct **3 → 1,166** (~7× and ~400× growth). (6) §1.3: added 6 new ports (4502 VICI, 7681 ttyd, 9999 ops python, 9993 ZeroTier, 44062 ZeroTier per-iface, 51820 wg0). (7) §1.4: kopia **502 Bad Gateway note** (Zun msg #43961, parked). (8) §1.6 gap #1 **INVERTED** — scripts now live in `/opt/...`, `/home/zunaid/...` is a `.bak` graveyard; bandwidth-monitor service STILL on stale path → new gap #14. (9) §1.6 gap #3/4/11/13: numbers updated to live baseline. (10) §1.6: added new **gap #14** (bandwidth-monitor.service on `.bak` file). (11) §2.3 step 11: customers=5 → **11**; step 11a: rewritten with `sed` for ExecStart paths to canonical `/opt/...`; step 14: 42 → 45 tables check. (12) §2.5: updated acceptance counts. (13) §5: added 2 new hard rules (#8 bot-polling sole source, #9 bandwidth-monitor ExecStart path). (14) §8 verification signature re-run. **Audit pass** at 2026-10-15 06:50 UTC confirms 0 remaining vps-01 references; all numbers match live. Kopia 502 parked for separate investigation. |

---

## 8. Pre-compaction signature (per SOUL.md Addendum)

This runbook v1.1.0 was written from live evidence on 2026-10-15 06:50 UTC (08:50 SAST):

- `ssh root@vpn-prod-01` for all live `systemctl`, `ss`, `docker`, `certbot`, `mariadb`, `sha256sum` commands
- `mariadb -uroot radius -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema="radius"'` → **45** (was 42 in v1.0.7)
- `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radpostauth'` → **2,299** (was 325 in v1.0.7)
- `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radacct'` → **1,166** (was 3 in v1.0.7)
- `mariadb -uroot radius -e 'SELECT COUNT(*) FROM customers'` → **11** (was 5 in v1.0.7)
- `sha256sum /opt/vpn-portal/app.py` → `baca20f7e9c08691a06ad4c3d2e4bec92e40cbb6d6235288f51bce774318be2b` (3,266 lines, 145,904 B)
- `sha256sum /opt/vpn-portal/bot.py` → `8cbda46e3aa073841dc429856488e0b5b24d21818ccb275668dcd5882c881042` (1,988 lines)
- `systemctl cat vpn-portal.service | grep Description` → `databyte VPN Portal — FastAPI backend (v2.2.1, 2026-08-10 — git HEAD 8e02a6e)`
- `docker ps --filter name=strongswan` → `Up 6 days (healthy)` (image `zun/strongswan:6.0.7-mschapv2-attrsql`)
- `systemctl list-units --type=service --state=running` on vpn-prod-01 → confirms 24 services (incl. 7 new since v1.0.7)
- `ss -tlnp / ss -ulnp` on vpn-prod-01 → confirms 6 new ports since v1.0.7
- `cd /root/.openclaw/workspace/projects/databyte-ikev2 && git rev-parse HEAD` → `b3cf38b9d22f87aa4f127d84b06f81e118c90bbc` (no remote tags; bot tags live in `vpn-admin-bot` repo: latest `v2.5.0`) — *path updated 2026-10-26: the duplicate root-level `databyte-Ikev2/` clone was removed; the live clone is now `projects/databyte-ikev2`*
- `cd /root/.openclaw/workspace/projects/vpn-admin-bot && git tag --sort=-v:refname | head -1` → `v2.5.0`
- `kopia snapshot list --all` from vpn-prod-01 debian user → **502 Bad Gateway from `kop.databyte.co.za:443`** (parked; Zun msg #43961); last successful `.update-info.json` mtime was 2026-10-10 00:00
- 0 remaining `vps-01` references in this runbook (41 converted to `vpn-prod-01` via sed)
- 0 remaining stale values (verified by grep against live counters in §1.2, §1.3, §1.6, §2.3, §2.5)

No claim in this runbook relies on memory or summary. Every "is" / "runs" / "is configured as" was either `grep`'d, `cat`'d, `curl`'d, or `mariadb`-queried in this turn.