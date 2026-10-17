# Architecture

> Generic architecture for a self-hosted IKEv2 VPN stack (strongSwan + FreeRADIUS + MariaDB + FastAPI portal + Telegram bot). Suitable for any Debian 13 VPS (any provider). A **Reference deployment** section below shows one real production setup as a worked example — substitute your own values everywhere.
>
> **Re-verified against repo HEAD: 2026-10-17** (Dockerfile, docker-compose.yml, swanctl config, strongswan.d overlays, host/systemd unit files, Debian trixie package availability, daloRADIUS install method).
> **Reference deployment values: verified-live 2026-10-15** by the operator of the source deployment (not independently re-verified in this session — no prod access).

---

## Reference deployment (worked example: Databyte VPN)

This is one real production deployment of the architecture. Values shown here are specific to that deployment — treat the column as illustrative, not prescriptive.

| Aspect | Databyte value | Your deployment |
|---|---|---|
| **Server hostname** | `vpn-prod-01` | `<your-hostname>` |
| **Cloud** | Any Debian 13 VPS (this deployment runs on a generic cloud VPS) | Same |
| **Public IPv4** | 154.65.110.44 | `<your-public-ip>` |
| **Tailscale** (admin back-door, optional) | 100.64.212.47 | `<your-tailscale-ip>` |
| **OS** | Debian 13.7 (trixie) | Same |
| **strongSwan image** | `zun/strongswan:6.0.7-mschapv2-attrsql` (re-tag for your namespace) | `<your-namespace>/strongswan:6.0.7-mschapv2-attrsql` |
| **charon container** | charon 6.0.7, UDP/500 + UDP/4500 on 0.0.0.0 + [::] | Same |
| **VICI socket** | TCP 127.0.0.1:4502 (loopback) | Same |
| **Portal** | FastAPI v2.2.1 | Your version |
| **Bot** | Telegram bot v2.5.0 | Your version |
| **MariaDB** | 11.8.6-MariaDB-0+deb13u1, `radius` DB = 45 tables | Your version |
| **FreeRADIUS** | active, UDP/1812 (auth) + 1813 (acct) + 18120 (coa) + 3799 (DAE) | Same ports |
| **VIP pool** | 10.99.0.0/24 | Your pool |
| **LE cert domains** | `vpn.example.com`, `admin.example.com` | Your domains |
| **Backups** | kopia → `backup.example.com:443` (⚠ 502 Bad Gateway, PARKED) | Your backup target |

**Live row counts (Databyte, 2026-10-15):** `radcheck=28`, `radpostauth=2,301`, `radacct=1,168`, `customers=12`, `users=12`, `devices=11`, `operators=1`.

---

## Architecture overview

```
[5G phone / iPhone / Windows / macOS]
        │  (UDP 500, UDP 4500, ESP)
        ▼
[Internet] ──► [vpn-gateway: <your-public-ip>, Debian 13 trixie]
                     │
                     ├── [charon container (strongswan 6.0.7, --network host, base: ubuntu:24.04)]
                     │     ├── EAP-MSCHAPv2 ──► eap-radius plugin ──► FreeRADIUS UDP/1812+1813
                     │     ├── DAE listener: 127.0.0.1:3799 (charon side)
                     │     ├── Quota meters: nftables `inet quota_table`
                     │     └── VICI socket: 127.0.0.1:4502
                     │
                     ├── [FreeRADIUS 3.x]
                     │     ├── radcheck / radusergroup (auth)
                     │     ├── radacct (interim accounting, every 300s)
                     │     └── CoA listener: UDP/3799
                     │
                     ├── [MariaDB 11.x, 127.0.0.1:3306, db `radius`]
                     │     ├── 7 RADIUS-protocol tables
                     │     └── ~38 daloRADIUS-mirrored business tables
                     │           (customers, users, devices, tiers,
                     │            installer_tokens, purchases, alerts, audit_log)
                     │
                     ├── [portal (FastAPI)]  — systemd
                     │     └── customer self-service + operator admin (https://vpn.example.com)
                     │
                     ├── [admin bot (Telegram)] — bot-polling.service
                     │     └── /sessions, /customers, /logs
                     │
                     ├── [daloRADIUS admin UI, 127.0.0.1:8000]
                     │     └── operator-only, loopback
                     │
                     ├── [nginx] — TLS termination (LE ECDSA)
                     │     └── vpn.example.com, admin.example.com
                     │
                     ├── [quota-monitor.service]
                     │     ├── 60s poll
                     │     ├── reads nftables counters per VIP
                     │     └── at 100%: FreeRADIUS radcheck DISABLE + CoA → charon
                     │
                     └── [kopia → backup.example.com:443]
```

**Important:** the strongSwan charon container's base image is `ubuntu:24.04` (verified 2026-10-17 in `docker/Dockerfile`), NOT the host OS. The host runs Debian 13; the container runs Ubuntu. The two are independent.

---

## What runs where

Single Debian 13 VPS hosting all services. **18 systemd services** running in the reference deployment (full list with ExecStart + backup coverage varies by deployment — see the RUNBOOK in your fork).

| Service | Purpose | Notes |
|---|---|---|
| `strongswan` (Docker) | charon 6.0.7 EAP-MSCHAPv2 gateway | `--network host`, 6 bind mounts (see `docker/docker-compose.yml`) |
| `freeradius` | RADIUS auth/acct/CoA | Connects to MariaDB on 127.0.0.1:3306 |
| `mariadb` | Identity + portal business data | `radius` DB |
| `portal.service` | FastAPI, customer + operator UI | systemd-managed |
| `bot-polling.service` | Telegram bot | Sole polling source — never run a second instance (HTTP 409 Conflict) |
| `nginx` | TLS termination | LE ECDSA certs via certbot |
| `quota-monitor.service` | Reads nftables counters, enforces quota | 60s poll |
| `strongswan-iptables-watchdog.service` | Auto-reload swanctl on config change | Polls `docker/swanctl/conf.d/` every 3s, runs `swanctl --load-all` on change |
| `prometheus` + exporters | Metrics | strongswan_exporter, quota-exporter, node-exporter |
| `alertmanager` | Alert routing | User-mode |
| `apache2` | daloRADIUS admin UI reverse proxy | Loopback-only |
| `ttyd` | Web shell | Operator-only |
| `backup-relay` | Forwards alerts | |
| `zerotier-one` (or Tailscale) | Backup admin path | |
| `bandwidth-monitor` (legacy LXC) | Old daemon, may be on stale `.bak` path | Migrate to `quota-monitor.service` |
| `cron` | Daily DB backup | Legacy, mostly superseded by kopia |
| `dockhand-bridge` | Internal bridge | |
| `mariadb` (also under strongswan stack) | Identity store | See MariaDB row above |

### Listening ports (reference deployment)

- **TCP:** 22, 80, 443, 2384, 3306, 4502, 5355, 7681, 8000, 9101, 9102, 9999
- **UDP:** 500, 4500, 1812, 1813, 18120, 3799, 5355, 9993, 44062, 51820

**Only the following need to be open inbound from the internet:** UDP 500, UDP 4500, ESP (proto 50), TCP 22, TCP 80, TCP 443. All RADIUS (1812/1813/18120/3799) and MariaDB (3306) are loopback-only.

---

## Data flow (single connection)

1. Client opens IKEv2 to `vpn.example.com` (UDP/500, UDP/4500, ESP)
2. charon negotiates IKE + EAP-MSCHAPv2
3. `eap-radius` plugin forwards auth to FreeRADIUS on UDP/1812 (charon → 127.0.0.1:1812)
4. FreeRADIUS looks up `radcheck` in MariaDB → password verify (MSCHAPv2)
5. FreeRADIUS returns Accept with `Framed-IP-Address` from the VIP pool, sticky per `users.assigned_vip`
6. charon assigns VIP, installs dynamic SA, starts accounting (`accounting = yes`, `accounting_interval = 300` per `docker/strongswan.d/10-eap-radius.conf`, re-verified 2026-10-17)
7. **nftables** counter `quota_<vip>` in `inet quota_table` increments per packet
8. `quota-monitor` polls counters every 60s, joins VIP → user → customer, updates `customers.data_used_bytes`
9. At 80% → log alert row in `alerts` table
10. At 100% → set `customers.over_quota=1`, flip `radcheck.Value` to `DISABLED`, send RFC 5176 Disconnect-Request to charon on UDP/3799 (DAE) → charon terminates the SA
11. `radpostauth` row on every auth; `radacct` interim-updates every 300s (live in reference deployment: ~2,300 + ~1,200 rows after 6 d of v2.2.0)

---

## Why these specific design choices

### FreeRADIUS as identity source (NOT charon-local SQLite)
Single source of truth for customer identity, used by both the VPN auth path AND the portal business data. daloRADIUS mirrors the same MariaDB tables, so operator UI, portal UI, and VPN auth all see the same data. SQLite at `/var/lib/strongswan/ipsec.db` holds ONLY charon-internal state (addresses, ike_sas, pools).

### nftables (NOT iptables-legacy) for quota counters
nftables named counters survive `nft flush ruleset`, have proper named-meter semantics, and don't have the per-VIP-rules-lost-on-flush bug class that iptables-legacy has. Production migrated in Phase 7.5 (2026-07-09).

### Quota via FreeRADIUS CoA (NOT charon SA kill)
RFC 5176 Disconnect-Request over UDP/3799 cleanly terminates the SA AND disables the FreeRADIUS identity in one operation. Re-auth is blocked because the FreeRADIUS identity is DISABLED. charon's DAE listener (configured in `docker/strongswan.d/10-eap-radius.conf`, verified 2026-10-17) accepts the CoA on 127.0.0.1:3799. Cleaner than rewriting `rw-eap.conf` + `swanctl --load-all`.

### `install_virtual_ip = no` + `install_routes = yes` for gateways
**Verified 2026-10-17 in `docker/strongswan.d/00-virtual-ip.conf`.** charon's DEFAULT `install_virtual_ip = yes` is a CLIENT default. For a GATEWAY, the VIP belongs to the REMOTE client, not the gateway. Setting `install_virtual_ip = no` + `install_routes = yes` makes charon install the route to the remote client without adding the VIP as a local address. This is the single most important charon override for gateway deployments.

### charon container uses `ubuntu:24.04` base, not host Debian
**Verified 2026-10-17 in `docker/Dockerfile`.** The Dockerfile builds strongSwan 6.0.7 from source on Ubuntu 24.04, with `--enable-eap-mschapv2 --enable-eap-radius --enable-attr-sql --enable-sqlite` (upstream strongswan lacks these). CVE-2026-47895 fixed by pinning VERSION=6.0.7. `libsqlite3-0` must be reinstalled after `apt-get autoremove` (build lesson). The host runs Debian 13, the container runs Ubuntu — they're independent.

### FastAPI portal + Telegram bot (two operator surfaces)
- **Portal** is the customer-facing UI + operator admin via web browser. systemd-managed, restartable independently of VPN.
- **Bot** is operator-only, Telegram-based, runs as `bot-polling.service`. Sole polling source — never run a second instance (HTTP 409 Conflict from Telegram).
- Both read/write the same MariaDB `radius` DB.

### Single Debian 13 VPS (any provider)
Simpler than multi-host for current scale. HA via backup + DR runbook, not live failover. If scale requires it, the portal can move to a second VPS with shared MariaDB.

### strongSwan image built fresh per host (NOT pushed to registry)
Build is ~5 min, image is tied to a specific strongSwan version. Pushing to a registry adds operational overhead with no real benefit at this scale. `ghcr.io` can be added later if a 3rd host makes the build time matter.

### `docker compose --profile vpn` safety guard
**Verified 2026-10-17 in `docker/docker-compose.yml`.** The compose file has `profiles: [vpn]`, which means plain `docker compose up -d` does NOT start the container. Must use `docker compose --profile vpn up -d` to actually start. This prevents accidental starts from a stale or wrong checkout.

---

## Quota layer

nftables table `inet quota_table` holds named counters per VIP. The `quota-monitor` Python daemon (60s poll):

1. Reads nftables counters for every active VIP
2. Joins VIP → `users.assigned_vip` → `devices.customer_id` → `customers.tier_id` / `data_limit_bytes`
3. Computes delta since last poll, adds to `customers.data_used_bytes`
4. At 80% → writes alert row
5. At 100% → `customers.over_quota=1`, FreeRADIUS `radcheck` value flipped to `DISABLED`, RFC 5176 Disconnect-Request (UDP/3799) → charon terminates the SA

**No iptables-legacy. No SQLite quota tables. Single source of truth: MariaDB `radius` DB.**

---

## Key architecture shifts (historical)

| Was (v1.x, LXC lab design) | Is now (current prod design) | When |
|---|---|---|
| SQLite at `/var/lib/strongswan/ipsec.db` (charon identity) | MariaDB `radius` DB with daloRADIUS-mirrored business tables | 2026-07-12 |
| iptables-legacy per-VIP byte counters in FORWARD | nftables named counters in `inet quota_table` | 2026-07-09 |
| charon's local `users`/`pools`/`leases` (SQLite) | FreeRADIUS `radcheck` (MariaDB), charon `eap-radius` proxies | 2026-07-06 |
| Quota via iptables counter + charon SA kill | Quota via nftables counter + RADIUS DISABLE + RFC 5176 CoA | 2026-07-06 |
| Self-signed CA | Let's Encrypt ECDSA | 2026-07+ |
| No Telegram bot | Telegram bot for operator commands (`/sessions` `/customers` `/logs`) | 2026-10 |
| No automatic swanctl config reload | `strongswan-iptables-watchdog.service` polls every 3s | 2026-10 |

The reference deployment in this repo is current prod. An LXC 903 lab design is preserved separately as a historical reference (see `docs/ARCHITECTURE-v1.x-LEGACY.md` in the source repo fork).

---

## Build from scratch

**Re-verified against source repo HEAD: 2026-10-17.** Replace all `<placeholder>` values with your own before running.

### Prerequisites

#### Server
- Any Debian 13 VPS (any cloud provider) with a public IPv4
- Sizing: per your customer count and bandwidth — no hard minimum specified (this deployment runs comfortably on a small VPS with ~12 customers)
- Root SSH access
- Tailscale installed (admin back-door; optional but recommended)

#### Domain + DNS
- Apex domain on a DNS provider with an API (Cloudflare is convenient; any provider with a certbot plugin works)
- DNS A records:
  - `vpn.example.com` → `<your-public-ip>`
  - `admin.example.com` → `<your-public-ip>`
- DNS provider API token with `Zone.DNS:Edit` scope (Cloudflare) or equivalent

#### Network / firewall
- **Inbound open from internet:** UDP 500, UDP 4500, ESP (proto 50) — IKEv2; TCP 22 — SSH; TCP 80, 443 — HTTP/HTTPS (LE challenge + portal)
- **Outbound open:** HTTPS for apt, pip, docker pulls
- **Loopback-only (no inbound needed):** FreeRADIUS (1812/1813/18120/3799), MariaDB (3306), VICI (4502), charon DAE (3799)

#### Software (apt) — verified against packages.debian.org 2026-10-17
- `strongswan-pki`, `strongswan-swanctl`, `libcharon-extauth-plugins` (host tools; charon itself runs in Docker)
- `freeradius`, `freeradius-mysql`, `freeradius-utils`
- `mariadb-server`
- `nginx`, `certbot`, `python3-certbot-dns-cloudflare`
- `nftables`
- `docker.io`, `docker-compose-plugin`
- `git`, `python3`, `python3-pip`, `python3-venv`
- `prometheus-node-exporter`, `prometheus`, `alertmanager` (optional)
- `apache2` (for daloRADIUS)
- `ufw`, `fail2ban`

#### Source repos
- This repo (fork/clone it; name doesn't matter)
- Separate repo for the Telegram bot (a generic bot framework that talks to your MariaDB)
- daloRADIUS: `https://github.com/lirantal/daloradius.git` (cloned during install, not an apt package)

#### Secrets to have ready at deploy time
- LE account email
- DNS provider API token (Zone.DNS:Edit)
- MariaDB root password
- MariaDB `radius` user password
- FreeRADIUS ↔ charon shared secret (32+ hex chars recommended)
- Telegram bot token (from @BotFather)
- Telegram chat_id (your chat)

### Install methods

#### 1. Base system
```
apt update && apt upgrade -y
apt install -y ufw fail2ban

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 500/udp
ufw allow 4500/udp
ufw enable

# Tailscale (optional admin back-door)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

#### 2. MariaDB
```
apt install -y mariadb-server
systemctl enable --now mariadb
mysql_secure_installation

# Modern syntax: CREATE USER + GRANT as separate statements
mysql -e "CREATE DATABASE radius CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER 'radius'@'localhost' IDENTIFIED BY '<radius-password>';"
mysql -e "GRANT ALL PRIVILEGES ON radius.* TO 'radius'@'localhost';"
mysql -e "FLUSH PRIVILEGES;"

# Clone the repo (replace /opt/vpn-repo with your preferred checkout path)
git clone https://github.com/your-org/your-vpn-repo.git /opt/vpn-repo
```

#### 3. FreeRADIUS (provisioning via the repo's script)
```
apt install -y freeradius freeradius-mysql freeradius-utils

# Apply operator overlay via the repo's provision script
# Handles clients.conf, mods-enabled, sites-enabled, schema migrations
bash /opt/vpn-repo/host/freeradius/provision-freeradius.sh

systemctl enable --now freeradius

# Verify (default FreeRADIUS test user is `test`, secret `testing123`)
radtest test test 127.0.0.1 0 testing123
# → should return Access-Accept
```

#### 4. strongSwan (charon in Docker)
**Re-verified 2026-10-17 against `docker/Dockerfile` and `docker/docker-compose.yml`.** Build + start via docker compose, not raw `docker run`.

```
apt install -y docker.io docker-compose-plugin
systemctl enable --now docker

# Clone strongSwan config subtree
cd /opt/vpn-repo
git checkout main   # or your branch

# Build the image
cd docker
docker build -t your-namespace/strongswan:6.0.7-mschapv2-attrsql -f Dockerfile .

# ⚠ `profiles: [vpn]` in docker-compose.yml means plain `docker compose up -d` does NOT start it.
# Must use `--profile vpn` to actually start the container (safety guard).
docker compose --profile vpn up -d

# The compose file bind-mounts 6 paths from the host into the container:
#   ./swanctl → /etc/swanctl (ro)
#   ./strongswan.d/{debug,attr-sql,00-virtual-ip,10-eap-radius}.conf (4 individual file mounts, ro)
#   /var/log/charon-log-host/charon.log → /var/log/charon-filog.log (rw)
#   /var/lib/strongswan → /var/lib/strongswan (SQLite DB, for charon-internal state)
```

The 4 individual `strongswan.d/*.conf` files are required (verified in `docker/docker-compose.yml`):
- `00-virtual-ip.conf` — `install_virtual_ip = no` + `install_routes = yes` (critical for gateway)
- `10-eap-radius.conf` — `eap-radius` plugin → FreeRADIUS 127.0.0.1:1812+1813, DAE on 127.0.0.1:3799, `accounting = yes`, `accounting_interval = 300`
- `attr-sql.conf` — charon attr-sql plugin for per-VIP leases
- `debug.conf` — charon debug logging

#### 5. swanctl config
Source: this repo at `docker/swanctl/`. Structure:
- `swanctl.conf` — main config (includes `conf.d/*.conf`, defines the VIP pool)
- `conf.d/rw-eap.conf.template` — rendered to `rw-eap.conf` at deploy (EAP secrets; references FreeRADIUS for MSCHAPv2, NO local secrets)
- `conf.d/rw-psk.conf.template` — rendered to `rw-psk.conf` (PSK fallback)
- `conf.d/README.md` — operator notes
- `x509/`, `x509ca/`, `private/` — cert stores

The `swanctl.conf` includes a pool (verified 2026-10-17):
```
include conf.d/*.conf

pools {
  rw-pool {
    addrs = 10.99.0.0/24
    dns   = 1.1.1.1, 8.8.8.8
  }
}
```
Substitute your own VIP pool and DNS servers.

Reload after edit (or just restart the container):
```
docker compose --profile vpn restart strongswan
```

#### 6. nginx + Let's Encrypt
**Re-verified 2026-10-17.** nginx vhost config is NOT in this repo (no `host/nginx/` dir). Either:
- (a) Hand-create `/etc/nginx/sites-enabled/vpn.example.com` as a reverse proxy to the portal (uvicorn/gunicorn socket)
- (b) Use the portal's built-in server (FastAPI can serve TLS directly via uvicorn)

The reference deployment uses option (a) with a uvicorn/gunicorn socket that nginx proxies to.

```
apt install -y nginx certbot python3-certbot-dns-cloudflare

# Get ECDSA certs via DNS-01 (Cloudflare plugin, auto-renews)
mkdir -p ~/.secrets && chmod 700 ~/.secrets
echo 'dns_cloudflare_api_token = <cloudflare-token>' > ~/.secrets/cloudflare.ini
chmod 600 ~/.secrets/cloudflare.ini
certbot certonly --dns-cloudflare --dns-cloudflare-credentials ~/.secrets/cloudflare.ini \
  -d vpn.example.com -d admin.example.com

systemctl enable --now nginx
```

#### 7. Portal (FastAPI)
```
cd /opt/vpn-repo/host/portal
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env  # set DATABASE_URL=mysql+pymysql://radius:<pw>@127.0.0.1/radius

# systemd unit (in this repo at host/systemd/portal.service)
cp /opt/vpn-repo/host/systemd/portal.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now portal
```

#### 8. Admin bot (Telegram, separate repo)
```
mkdir -p /opt/admin-bot
cd /opt/admin-bot
git clone <your-admin-bot-repo> .
git checkout <your-version-tag>
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env  # set DATABASE_URL + TELEGRAM_BOT_TOKEN

# systemd unit
cp systemd/bot-polling.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bot-polling
```

Sole polling source — never run a second instance (HTTP 409 Conflict from Telegram).

#### 9. daloRADIUS
**daloRADIUS is NOT a single apt package.** Install requires a stack of packages + a GitHub clone. **Re-verified against official docs 2026-10-17** ([daloradius INSTALL.debian.md](https://github.com/lirantal/daloradius/blob/master/doc/install/INSTALL.debian.md)):

```
apt --no-install-recommends install apache2 php libapache2-mod-php \
  php-mysql php-zip php-mbstring php-common php-curl \
  php-gd php-db php-mail php-mail-mime mariadb-client freeradius-utils rsyslog
git clone https://github.com/lirantal/daloradius.git /opt/daloradius
chown -R www-data:www-data /opt/daloradius
systemctl enable --now apache2
```

In daloRADIUS operators panel → settings, point at MariaDB `radius` DB.

#### 10. quota-monitor (Python daemon)
Canonical unit (per `host/systemd/README.md` in the repo). Note: the `host/systemd/quota-monitor.service` file in the reference repo points at `/home/zunaid/strongswan/quota/quota-monitor.py` — that's the LXC 903 lab path. For a fresh Debian 13 VPS deploy, point ExecStart at your actual checkout:
```
cp /opt/vpn-repo/quota/quota-monitor.py /opt/quota-monitor/
# Edit the ExecStart= line in /etc/systemd/system/quota-monitor.service to match your install path
cp /opt/vpn-repo/host/systemd/quota-monitor.service /etc/systemd/system/
$EDITOR /etc/systemd/system/quota-monitor.service
systemctl daemon-reload
systemctl enable --now quota-monitor
```

#### 11. strongswan-iptables-watchdog
File was renamed from `strongswan-watchdog.service` → `strongswan-iptables-watchdog.service`. (Legacy name visible in some older docs.)
```
cp /opt/vpn-repo/host/systemd/strongswan-iptables-watchdog.service /etc/systemd/system/strongswan-watchdog.service
cp /opt/vpn-repo/host/systemd/strongswan-iptables-watchdog.sh /opt/strongswan-iptables-watchdog.sh
chmod +x /opt/strongswan-iptables-watchdog.sh
systemctl daemon-reload
systemctl enable --now strongswan-watchdog
```
Polls `docker/swanctl/conf.d/` every 3s, runs `swanctl --load-all` on change.

#### 12. nftables quota_table
The ruleset is applied by a shell script, not a `.conf` file. **Re-verified 2026-10-17.**
```
bash /opt/vpn-repo/quota/install_quota_rules.sh
systemctl enable --now nftables-vpn  # /opt/vpn-repo/host/systemd/nftables-vpn.service

nft list ruleset | grep quota
# → table inet quota_table { ... }
```

---

## Integration: how to make them talk

### Startup order
1. **MariaDB** up first — everyone reads/writes it
2. **FreeRADIUS** up next — needs MariaDB; charon depends on it via eap-radius
3. **charon (Docker)** up — needs FreeRADIUS reachable on UDP/1812+1813
4. **nginx** up — TLS termination for portal
5. **Portal** up — reads MariaDB
6. **bot-polling** up — reads MariaDB, talks to Telegram
7. **quota-monitor** up — reads nftables, writes MariaDB, sends CoA to charon
8. **strongswan-iptables-watchdog** up — watches swanctl config dir, reloads charon on change
9. **daloRADIUS** up — operator admin UI on 127.0.0.1:8000

### Key config links between services

| From | To | How |
|---|---|---|
| charon | FreeRADIUS | shared secret in `docker/strongswan.d/10-eap-radius.conf` ↔ `/etc/freeradius/3.0/clients.conf` (FreeRADIUS expects charon at 127.0.0.1) |
| charon (DAE) | FreeRADIUS (CoA listener) | UDP/3799 loopback, second shared secret |
| FreeRADIUS | MariaDB | `/etc/freeradius/3.0/mods-enabled/sql` with `radius` user creds |
| Portal | MariaDB | `app.py` reads `DATABASE_URL` from `.env` |
| bot-polling | MariaDB | same `DATABASE_URL` env var |
| bot-polling | Telegram | `TELEGRAM_BOT_TOKEN` in `.env` |
| quota-monitor | nftables | reads `nft list ruleset` every 60s |
| quota-monitor | MariaDB | writes to `customers.data_used_bytes`, `radcheck.Value` (DISABLE at 100%) |
| quota-monitor | charon (DAE) | sends RFC 5176 Disconnect-Request to `127.0.0.1:3799` |
| strongswan-iptables-watchdog | charon | polls `docker/swanctl/conf.d/` every 3s, runs `docker compose --profile vpn restart strongswan` on change |

### End-to-end verification

Run these in order after install:

```
# 1. MariaDB
mysqladmin ping -u root -p
mysql -u radius -p<radius-password> -e "SELECT COUNT(*) FROM radius.radcheck;"

# 2. FreeRADIUS
systemctl status freeradius
radtest test test 127.0.0.1 0 testing123
# → rad_recv: Access-Accept

# 3. charon
docker ps | grep strongswan
docker exec strongswan swanctl --stats
# → no errors, UDP/500 + UDP/4500 listening

# 4. Add a test user
mysql -u radius -p<radius-password> radius -e \
  "INSERT INTO radcheck (UserName, Attribute, Value) VALUES ('testuser', 'Cleartext-Password', 'testpass');"

# 5. From a client device, connect to vpn.example.com with testuser / testpass
docker exec strongswan swanctl --list-sas
# → shows the new SA with assigned VIP from your pool

# 6. radpostauth + radacct rows
mysql -u radius -p<radius-password> radius -e \
  "SELECT * FROM radpostauth WHERE username='testuser' ORDER BY id DESC LIMIT 1;"
# → Access-Accept
mysql -u radius -p<radius-password> radius -e \
  "SELECT * FROM radacct WHERE UserName='testuser' ORDER BY radacctid DESC LIMIT 1;"
# → live accounting, interim-update

# 7. nftables counter incrementing
nft list ruleset | grep quota
# → table inet quota_table with named counters

# 8. Portal
curl -k https://vpn.example.com/
# → 200 OK

# 9. Bot
# Send /sessions in Telegram
# → returns active sessions from radacct

# 10. Quota enforcement (manual trigger)
# Set customers.data_used_bytes = tier limit for testuser
# Wait 60s for quota-monitor poll
# → charon terminates the SA via RFC 5176 CoA; client disconnects
```

End-to-end working = client can connect, auth against FreeRADIUS via charon, get a VIP, traffic flows, counters increment, portal shows data, bot responds to `/sessions`, quota enforces at 100%.

---

## Re-verification log (this session)

**2026-10-17** — Re-verified against the source repo HEAD:

| Item | Source verified | Status |
|---|---|---|
| Repo file structure (`docker/`, `host/`, `quota/`, `docs/`, `scripts/`, `examples/`, `ops/`, `tracker/`) | GitHub API contents listing | ✅ |
| `docker/Dockerfile` base image + build flags | Raw file fetch | ✅ ubuntu:24.04 base, VERSION=6.0.7, `--enable-eap-mschapv2 --enable-eap-radius --enable-attr-sql --enable-sqlite` |
| `docker/docker-compose.yml` profiles + bind mounts + healthcheck | Raw file fetch | ✅ `profiles: [vpn]`, 6 bind mounts, `cap_add: NET_ADMIN, NET_RAW`, `restart: unless-stopped`, healthcheck on VICI socket |
| `docker/swanctl/swanctl.conf` pool config | Raw file fetch | ✅ `addrs = 10.99.0.0/24`, `dns = 1.1.1.1, 8.8.8.8` |
| `docker/strongswan.d/10-eap-radius.conf` | Raw file fetch | ✅ `server = 127.0.0.1`, `auth_port = 1812`, `acct_port = 1813`, `accounting = yes`, `accounting_interval = 300`, DAE on `127.0.0.1:3799` |
| `docker/strongswan.d/00-virtual-ip.conf` | Raw file fetch | ✅ `install_virtual_ip = no`, `install_routes = yes` |
| `host/systemd/quota-monitor.service` ExecStart | Raw file fetch | ⚠ Points at LXC 903 path (`/home/zunaid/strongswan/...`) — edit ExecStart for a fresh VPS deploy |
| Debian trixie package availability | packages.debian.org | ✅ `strongswan-pki`, `strongswan-swanctl`, `libcharon-extauth-plugins`, `python3-certbot-dns-cloudflare` all in trixie |
| daloRADIUS install method | daloradius INSTALL.debian.md | ✅ Not an apt package — install requires a stack of packages + GitHub clone |
| Reference deployment live-state values | Source doc (verified-live 2026-10-15 by operator) | ⚠ NOT independently re-verified in this session — no prod access |

Items in the reference deployment table (server hostname, public IP, Tailscale IP, LE domains, backup target, row counts) are **not independently re-verified** in this session. They are taken from the existing source doc, which states they were verified-live by the operator on 2026-10-15. Treat as illustrative example, not as current ground truth.
