# VPS-XNEELO-DEPLOY — Databyte VPN Production Deploy (Xneelo VPS)

| Field | Value |
|---|---|
| Document ID | DAT-OPS-XNEELO-DEPLOY-001 |
| Revision | v1.0.0 |
| Date (SAST) | 2026-10-15 |
| Author | Misha 🐻 (Zun's assistant) |
| Authoritative source of facts | `ssh root@vpn-prod-01` + `git ls-remote origin` + `scripts/bootstrap-xneelo.sh` (live read 2026-10-15) + Xneelo control panel + Cloudflare dashboard |
| ISO 9001:2015 | Document Control: this is internal — distribution restricted to operators |
| Verification | Each step is independently verifiable; assumes operator has access to Xneelo control panel + Cloudflare + Tailscale + 1Password + OC |
| Live fact-check | 2026-10-15 06:50 UTC (08:50 SAST) — verified against `vpn-prod-01` running portal **v2.2.1** (git HEAD `8e02a6e`) + bot **v2.5.0** (HEAD `01ad543`) on 45-table MariaDB `radius` |
| Companion docs | [`RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0](./RUNBOOK-DR-REBUILD-AND-HA.md) (DR on a fresh VPS from kopia) · [`ARCHITECTURE.md`](./ARCHITECTURE.md) (live state + architecture) · [`QUICKSTART.md`](./QUICKSTART.md) (personal-use gateway-only path) |

---

## 0. Purpose

Canonical deploy procedure for the Databyte VPN stack on a fresh Xneelo VPS (Debian 13). Covers:

- **The one-shot `scripts/bootstrap-xneelo.sh`** (15-25 min) — installs the base stack: Docker, strongSwan image, operator user, fail2ban, iptables MSS clamp, LE cert generation, basic rclone backup, smoke test.
- **Post-script steps** (§4, ~30-45 min) — bring a fresh install to **production parity** with `vpn-prod-01`: MariaDB + FreeRADIUS + VPN Portal + Telegram bot + daloRADIUS + quota/bandwidth monitors + kopia server + Tailscale + the 7 strongswan bind mounts.

> **Critical caveat:** `bootstrap-xneelo.sh` predates the Phase 4E / Phase 5 / v2.x architecture shifts. It installs a **personal-use gateway-only** stack (charon-local SQLite, attr-sql, iptables-legacy). The script's own internal step counters are also inconsistent (some say "Step X/16", some say "Step X/19") — the header claims 19 steps; the `info` lines mix both. Don't trust the counters, trust the header.

> **For DR on a fresh VPS** (rebuilding from kopia after total loss), use **`docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0** §2.3 instead — that doc covers the 24-step kopia-restore path. This doc is **greenfield only**.

> **For personal use** (you only, no customers), the script alone is sufficient — skip §4 entirely.

---

## 1. Prerequisites

### 1.1 VPS — provision in Xneelo control panel first

| Item | Spec / note |
|---|---|
| Provider | Xneelo (South African DC) |
| OS | **Debian 13 (trixie)** — required (charon 6.0.7 + Python 3.13 venv + systemd-resolved behavior all assume trixie) |
| vCPU | 2 minimum; 4 recommended for prod (charon + portal + MariaDB + 2 monitors + node_exporter + ttyd + ...) |
| RAM | 3.8 GB minimum (vpn-prod-01 baseline); 4 GB recommended |
| Disk | 10 GB minimum (vpn-prod-01 uses 9.7 GB with 2.2 GB free); 20 GB recommended for headroom |
| Public IPv4 | 1 static address (Xneelo assigns per-VPS) |
| Public IPv6 | Optional, native dual-stack supported |
| Inbound ports to open at Xneelo Security Group | **22/tcp** (SSH), **80/tcp** (HTTP→HTTPS), **443/tcp** (HTTPS), **500/udp** (IKE), **4500/udp** (NAT-T). **Nothing else public.** All RADIUS/MariaDB/CoA ports stay on loopback. |
| Hostname | `vpn-prod-XX.databyte.co.za` (XX = ordinal; current prod is `vpn-prod-01`) |
| Tailscale | Install via `tailscale up --authkey=<reusable-key>` after first boot. See §4.11. |

### 1.2 Off-server secrets — from `RUNBOOK-DR-REBUILD-AND-HA.md` §0.5

These 10 items are **NOT in kopia** (kopia only backs up the file system). Have them ready **BEFORE** you start:

| # | Secret | Where |
|---|---|---|
| 1 | Kopia repository password | 1Password "Databyte → VPS kopia" + paper |
| 2 | Cloudflare DNS API token | 1Password "Databyte → Cloudflare" + paper |
| 3 | Cloudflare account login | 1Password |
| 4 | GitHub SSH key with `Dippie-WP/databyte-Ikev2` access | 1Password (backup) |
| 5 | Tailscale auth key (reusable) | https://login.tailscale.com/admin/settings/keys |
| 6 | Xneelo / cloud provider login | 1Password |
| 7 | MariaDB root password | (set during §4.1) |
| 8 | FreeRADIUS clients.conf shared secret | default `testing123` for localhost; document any change |
| 9 | `/var/spool/cron/crontabs/debian` content | capture from existing host: `crontab -u debian -l` |
| 10 | LE private keys for `myvpn.*` | kopia via `/etc/letsencrypt`; **only matters if kopia password also lost** |

If any of items 1-6 are inaccessible: deploy stops at §4.8 (kopia connect) or §4.7 (LE cert renewal). **Treat as load-bearing.**

### 1.3 Cloudflare DNS — A records to create

TTL **300s** (or 60s if planning HA in §3 of runbook):

| Name | Type | Value | Purpose |
|---|---|---|---|
| `vpn-prod-XX.databyte.co.za` | A | `<new VPS IPv4>` | management hostname (matches /etc/hostname) |
| `vpn-portal.databyte.co.za` | A | `<new VPS IPv4>` | VPN portal HTTPS (customer self-service) |
| `myvpn.databyte.co.za` | A | `<new VPS IPv4>` | strongSwan IKEv2 server cert SAN |
| `adminer.databyte.co.za` | A | `<new VPS IPv4>` | (optional) Adminer DB UI; cert covers via `certonly` |
| `kop.databyte.co.za` | A | `<kopia server IPv4>` | kopia backup endpoint (if running your own server) |

### 1.4 Local machine

- OC (OpenClaw) or laptop with `ssh` + `scp` access to OC
- The bootstrap script lives in the repo (`scripts/bootstrap-xneelo.sh`)
- A populated `.env.xneelo` (see `cp .env.xneelo.example .env.xneelo && vim .env.xneelo`)

---

## 2. Pre-flight (~5 min)

Run these on **OC** (not the new VPS) BEFORE first SSH to the new host:

```bash
# 1. Verify kopia password (kopia server still reachable)
ssh root@vpn-prod-01 'sudo -u debian -H bash -lc \
  "KOPIA_PASSWORD=$(cat /home/debian/.kopia-password) kopia repository status 2>&1"' | head -10

# 2. Verify GitHub SSH key works
ssh -T git@github.com  # expect "Hi Dippie-WP!"

# 3. Verify DNS records current at registrar
dig +short vpn-prod-XX.databyte.co.za
dig +short vpn-portal.databyte.co.za
dig +short myvpn.databyte.co.za

# 4. Verify new VPS SSH is reachable
ssh root@NEW_VPS_IP 'echo OK'

# 5. Verify Cloudflare / registrar account logged in (for DNS-01 + cert renewal)
```

---

## 3. The bootstrap script — `scripts/bootstrap-xneelo.sh`

### 3.1 Copy + populate the env file

```bash
# ON OC, in the repo root:
cp .env.xneelo.example .env.xneelo
vim .env.xneelo
# Required vars (the script auto-generates OPERATOR_PASSWORD, DEMO_CUSTOMER_PASSWORD,
# FRIEND_PSK if empty, and writes them back to .env.xneelo):
#   PUBLIC_IPV4, SERVER_ID, OPERATOR_USER, OPERATOR_PASSWORD, DEMO_CUSTOMER_USER, DEMO_CUSTOMER_VIP
```

### 3.2 Run the script

```bash
# ON OC:
scp -i ~/.ssh/id_ed25519 .env.xneelo scripts/bootstrap-xneelo.sh root@NEW_VPS_IP:/tmp/
ssh -i ~/.ssh/id_ed25519 root@NEW_VPS_IP
bash /tmp/bootstrap-xneelo.sh 2>&1 | tee /tmp/bootstrap.log
```

Time: **15-25 minutes** (network-dependent). 19 logical steps per the script header.

### 3.3 What the 19 steps do

| # | Step | What it does | Time |
|---|---|---|---|
| 1 | `apt update` + install base packages | Docker, rclone, sqlite3, unattended-upgrades, fail2ban, rkhunter, iptables-persistent, sudo, git. Switches `iptables` to `iptables-legacy` (Debian 13 ships iptables-nft by default) | 2 min |
| 2 | Disable root SSH login | `PermitRootLogin no` in `/etc/ssh/sshd_config` + `systemctl reload sshd` | <1 min |
| 3 | Create operator user (`zunaid`) | `useradd -m -G sudo`, copies root's `authorized_keys` to zunaid, sets passwordless sudo via `/etc/sudoers.d/90-zunaid` | <1 min |
| 4 | Enable unattended security upgrades | `dpkg-reconfigure unattended-upgrades`, sets auto-reboot to false | <1 min |
| 5 | Configure fail2ban (SSH jail) | 3 retries / 1h window → 24h ban | <1 min |
| 6 | Configure rkhunter | Daily scan cron + DB update | <1 min |
| 7 | Apply sysctl hardening | `ip_forward=1` + reject ICMP redirects + rp_filter=1 | <1 min |
| 8 | Apply iptables MSS clamp + VPN FORWARD rules | `/etc/iptables/rules.v4` with: MSS clamp 1260, INPUT DROP with explicit ACCEPT (loopback, VICI 4502, VPN subnet, ICMP, SSH, IKE, established), FORWARD DROP→ACCEPT for VPN subnet, MASQUERADE 10.99.0.0/24. **Includes VICI port 4502** (fix for charon start-script hang) | <1 min |
| 9 | Clone project repo | `git clone https://github.com/Dippie-WP/databyte-Ikev2.git /opt/strongswan-vpn-gateway` | <1 min |
| 10 | Generate strongSwan CA + server certs | `bash scripts/gen-certs.sh SERVER_ID=$SERVER_ID` (RSA-2048, 1y, PKCS#1 v1.5 — iOS 18+ compat) | <1 min |
| 11 | Configure `rw-eap.conf` + `rw-psk.conf` | Copy from templates, uncomment `secrets { eap-USERNAME { ... } }` and `secrets { ike-psk { ... } }` blocks | <1 min |
| 12 | Build Docker image | `bash scripts/build-image.sh` → `zun/strongswan:6.0.7-mschapv2-attrsql` (≈5 min on first build) | 5 min |
| 13 | Start container | `docker compose --profile vpn up -d` | <1 min |
| 14 | Seed first users in SQLite (v1.x era) | Inserts EAP identity + sticky VIP into `/var/lib/strongswan/ipsec.db` (charon-local SQLite). **Pre-Phase-4E pattern; superseded by FreeRADIUS for prod — see §4.1** | <1 min |
| 15 | Install bandwidth-monitor systemd service | Per-user tc classes + iptables (v1.x iptables-legacy) | <1 min |
| 16 | Configure rclone remote for RustFS backup | `rclone config` (interactive) — sets up the S3-compatible endpoint | <1 min |
| 17 | Install DB backup cron | `/etc/cron.d/strongswan-db-backup` at 03:00 UTC daily | <1 min |
| 18 | (not visible in script as 18 — likely part of 16/17 grouping) | — | — |
| 19 | Smoke test | `docker exec strongswan swanctl --list-sas` to confirm container started | <1 min |

> **Note on step counter drift:** the script's `info` lines mix `/16` and `/19` (e.g., "Step 4/19", "Step 5/16", "Step 7/19", "Step 8/16"). The header says 19. Don't trust the counters — trust the header.

### 3.4 What the script does NOT do (the prod-parity gap)

| Missing | Why it matters | Fix in §4 |
|---|---|---|
| **MariaDB install + schema** | No `radius` DB → no FreeRADIUS auth → no customers | §4.1 |
| **FreeRADIUS operator overlay** | `radacct` chain silently broken → no accounting | §4.1 |
| **daloRADIUS** | Apache daloRADIUS admin UI not deployed | §4.2 |
| **VPN Portal** | `https://vpn-portal.databyte.co.za/` returns 502 | §4.3 |
| **Telegram bot** | No `/sessions`, `/customers`, `/logs` Telegram admin | §4.4 |
| **quota-monitor.service** | Customer data caps not enforced | §4.5 |
| **ipban** | No IP ban for abusive customers | §4.6 |
| **LE cert via certbot** | `myvpn.databyte.co.za` uses self-signed → iOS native fails | §4.7 |
| **Kopia server setup + restore** | No backup; rebuild is impossible | §4.8 |
| **Cloudflare DNS-01 creds** | LE cert can't renew past 90 days | §4.9 |
| **Tailscale node identity** | No ops tunnel | §4.11 |
| **The 7 strongswan bind mounts** | Container starts but `swanctl --list-sas` is empty (silent fail) | §4.10 |
| **charon-log-host dir + log file** | `docker run` aborts on the charon.log bind | §4.10 |
| **`strongswan` group (GID 1002)** | `/var/lib/strongswan/ipsec.db` is unreadable in container | §4.10 |
| **logrotate for charon.log** | (operational, deprioritized — log bounded by container lifetime) | §4.12 |
| **`/var/spool/cron/crontabs/debian` backup cron** | No daily kopia backup | §4.8 |
| **Cloudflare API token for DNS-01** | LE cert can't renew post-2026-12-02 | §4.9 |

**Total post-script time: ~30-45 min** to reach production parity.

---

## 4. Post-script steps (~30-45 min for prod parity)

> Run these AFTER `bootstrap-xneelo.sh` completes and the smoke test passes. Steps are independent unless noted; recommended order is top-to-bottom.

### 4.1 MariaDB + FreeRADIUS + daloRADIUS (10 min)

```bash
# 1. Install
apt install -y freeradius freeradius-mysql mariadb-server mariadb-client
systemctl enable --now freeradius mariadb

# 2. Bootstrap radius DB
mariadb -e "CREATE DATABASE IF NOT EXISTS radius;
            CREATE USER IF NOT EXISTS 'radius'@'localhost' IDENTIFIED BY 'radiuspw';
            GRANT ALL ON radius.* TO 'radius'@'localhost';
            CREATE USER IF NOT EXISTS 'portal'@'127.0.0.1' IDENTIFIED BY 'portalpw';
            GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER, REFERENCES ON radius.* TO 'portal'@'127.0.0.1';"
# CHANGE THESE PASSWORDS before running in prod — record in 1Password

# 3. Apply FreeRADIUS schema + operator overlay
cd /opt/strongswan-vpn-gateway
bash scripts/deploy-freeradius.sh
# This applies stock schema + the host/freeradius/ overlay (provision-freeradius.sh).
# See host/freeradius/README.md for the overlay design.

# 4. Verify
systemctl is-active freeradius mariadb
radtest testing password 127.0.0.1 0 testing123
# expect "Received Access-Accept"
mariadb -uroot radius -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius';"
# expect 45 (matches vpn-prod-01 baseline, 2026-10-15)

# 5. Install daloRADIUS (admin UI on 127.0.0.1:8000)
cd /tmp && git clone --depth 1 https://github.com/lirantal/daloradius.git
mv daloradius /var/www/daloradius
cp /var/www/daloradius/app/common/includes/daloradius.conf.php.sample \
   /var/www/daloradius/app/common/includes/daloradius.conf.php
$EDITOR /var/www/daloradius/app/common/includes/daloradius.conf.php
# Set: host=127.0.0.1, user=radius, pass=<from above>. NOTE: password length limit 14.
a2enmod php8.3 && systemctl restart apache2
curl -ksS http://127.0.0.1:8000/operators/login.php | head -5
# expect "daloRADIUS :: Login"
```

### 4.2 VPN Portal (5 min)

```bash
# Clone + install (NOT done by bootstrap-xneelo.sh)
git clone https://github.com/Dippie-WP/databyte-Ikev2.git /opt/vpn-portal
cd /opt/vpn-portal
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Apply DB schema (use the migration scripts in order)
bash apply_portal_schema.sh
bash apply_migration_v2.0.sh
bash apply_migration_v2.0_session_tracking.sh
# apply_customers_extensions.sh if upgrading from a pre-v2.0 portal

# Generate admin password hash
HASH=$(.venv/bin/python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_ADMIN_PASSWORD', bcrypt.gensalt()).decode())")
cat > /etc/vpn-portal.env <<EOF
ADMIN_USER=admin
ADMIN_PASS_HASH=$HASH
SESSION_SECRET=$(openssl rand -hex 32)
VPN_HOST=127.0.0.1
DB_HOST=127.0.0.1
DB_NAME=radius
DB_USER=portal
DB_PASS=portalpw
TELEGRAM_BOT_TOKEN=<from 1Password>
EOF
chmod 600 /etc/vpn-portal.env

# Install systemd unit
cp systemd/vpn-portal.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vpn-portal

# Verify
curl -sS http://127.0.0.1:8080/api/health | jq
# expect: {"status":"ok", "db_ok":true, "db_customers":N, "charon_ok":true}
```

### 4.3 Telegram bot (included in portal deploy)

The bot (`/opt/vpn-portal/bot.py`) runs as a **separate systemd service**:

```bash
# Install bot-polling.service (referenced by HOT-261 as sole polling source)
cp systemd/bot-polling.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bot-polling

# Verify
systemctl status bot-polling
# expect: active (running) since ...
journalctl -u bot-polling -n 20
# expect: "Application started" / no HTTP 409 Conflict
```

**Bot version** lives in `vpn-admin-bot` repo (separate version stream from portal). To update: see `prod-vpn-bot-publish` skill.

### 4.4 daloRADIUS (admin UI on 127.0.0.1:8000)

Already done in §4.1 step 5. Just verify it works.

### 4.5 quota-monitor + bandwidth-monitor (5 min)

```bash
# Pre-create strongswan group (GAP #10 from RUNBOOK §1.6)
groupadd -g 1002 strongswan
id vpn-portal >/dev/null && usermod -aG strongswan vpn-portal
mkdir -p /var/lib/strongswan
chown root:strongswan /var/lib/strongswan
chmod 775 /var/lib/strongswan

# Fix bandwidth-monitor.service ExecStart path (GAP #14 from RUNBOOK §1.6)
sed -i 's|/home/zunaid/strongswan/quota/bandwidth-monitor.py|/opt/strongswan-vpn-gateway/quota/bandwidth-monitor-nft.py|' \
    /etc/systemd/system/bandwidth-monitor.service
# Also fix quota-monitor (canonical location is /opt)
sed -i 's|/home/zunaid/strongswan/quota/quota-monitor.py|/opt/strongswan-vpn-gateway/quota/quota-monitor.py|' \
    /etc/systemd/system/quota-monitor.service

# Install quota-monitor.service (not in bootstrap-xneelo.sh)
cp /opt/strongswan-vpn-gateway/quota/quota-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now quota-monitor bandwidth-monitor
systemctl status quota-monitor bandwidth-monitor
# expect: both active (running)
```

### 4.6 ipban (1 min)

```bash
# Pre-installed at /opt/ipban (via kopia restore or fresh clone)
systemctl enable --now ipban
systemctl status ipban
# expect: active (running)
```

### 4.7 Let's Encrypt cert (5 min)

```bash
# DNS-01 first (avoids HTTP-01 rate limits when scaling)
cat > /etc/letsencrypt/cloudflare.ini <<EOF
dns_cloudflare_api_token = <FROM 1PASSWORD>
EOF
chmod 600 /etc/letsencrypt/cloudflare.ini

# Issue cert covering all 3 SANs (single cert, single renewal)
certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d myvpn.databyte.co.za \
  -d vpn-portal.databyte.co.za \
  -d adminer.databyte.co.za \
  --agree-tos --no-eff-email \
  -m operator@databyte.co.za

# Nginx vhost
cp /opt/vpn-portal/nginx/vpn-portal.conf /etc/nginx/sites-available/
ln -sf /etc/nginx/sites-available/vpn-portal.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Verify
curl -I https://vpn-portal.databyte.co.za/api/health
# expect: HTTP/2 200
```

### 4.8 Kopia server + restore (10 min)

```bash
# Install kopia client
curl -sSfL https://kopia.io/signing-key.asc | gpg --dearmor -o /usr/share/keyrings/kopia-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/kopia-keyring.gpg] https://packages.kopia.io/apt/ stable main" \
  > /etc/apt/sources.list.d/kopia.list
apt update && apt install kopia

# Connect to existing repo (kopia server on kop.databyte.co.za)
sudo -u debian -H bash -c "
  export KOPIA_PASSWORD=\$(cat /home/debian/.kopia-password)
  kopia repository connect server \
    --url=https://kop.databyte.co.za:443 \
    --username=debian \
    --password=\$KOPIA_PASSWORD
  kopia snapshot list --all
"
# expect: list of all paths from existing host

# Restore the 6 critical paths (per RUNBOOK §2.3 steps 8-13)
kopia restore /opt/vpn-portal --destination-path /opt/vpn-portal
kopia restore /opt/strongswan-vpn-gateway --destination-path /opt/strongswan-vpn-gateway
kopia restore /etc --destination-path /etc
kopia restore /var/lib --destination-path /var/lib
kopia restore /home/debian --destination-path /home/debian
kopia restore /usr/local --destination-path /usr/local

# Capture debian crontab (off-server backup — RUNBOOK §0.5 item #9)
crontab -u debian -l > /home/debian/.kopia-cron.tab
crontab -u debian /home/debian/.kopia-cron.tab
# expect: "0 0 * * * /home/debian/local/bin/kopia-backup-all"
```

### 4.9 Cloudflare DNS-01 creds

Already done in §4.7. Verify `/etc/letsencrypt/cloudflare.ini` is `chmod 600` and contains the API token.

### 4.10 The 7 strongswan bind mounts (CRITICAL — silent failure if missed)

The `bootstrap-xneelo.sh` runs the strongswan container but **does NOT apply the operator overlay bind mounts**. Without them, charon starts but `swanctl --list-sas` is empty (silent fail — no auth).

```bash
# Pre-create the charon-log-host dir + log file (GAP #11)
mkdir -p /var/log/charon-log-host
touch /var/log/charon-log-host/charon.log
chown -R root:root /var/log/charon-log-host

# Stop the existing container (created by bootstrap script)
docker stop strongswan && docker rm strongswan

# Re-run with the exact 7 binds (copied from live vpn-prod-01)
docker run -d --name strongswan \
  --network host \
  --restart unless-stopped \
  --cap-add CAP_NET_ADMIN \
  --cap-add CAP_NET_RAW \
  --health-cmd "swanctl --uri=tcp://127.0.0.1:4502 --stats >/dev/null && ss -ltn | grep -q :4502" \
  --health-interval 30s --health-timeout 5s --health-retries 3 --health-start-period 30s \
  -v /opt/strongswan-vpn-gateway/docker/swanctl:/etc/swanctl:ro \
  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/debug.conf:/etc/strongswan.d/debug.conf:ro \
  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/attr-sql.conf:/etc/strongswan.d/attr-sql.conf:ro \
  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/00-virtual-ip.conf:/etc/strongswan.d/00-virtual-ip.conf:ro \
  -v /opt/strongswan-vpn-gateway/docker/strongswan.d/10-eap-radius.conf:/etc/strongswan.d/10-eap-radius.conf:ro \
  -v /var/log/charon-log-host/charon.log:/var/log/charon-host.log:rw \
  -v /var/lib/strongswan:/var/lib/strongswan:rw \
  zun/strongswan:6.0.7-mschapv2-attrsql

# Verify immediately
docker ps --filter name=strongswan
# expect: Up X minutes (healthy)
docker exec strongswan ss -ltn | grep 4502
# expect: 127.0.0.1:4502 LISTEN
docker exec strongswan cat /etc/strongswan.d/10-eap-radius.conf | grep -c eap-radius
# expect: >= 1 (bind mount hit, customer identity source live)
```

### 4.11 Tailscale (2 min)

```bash
# Install
curl -fsSL https://tailscale.com/install.sh | sh

# Auth with reusable key
tailscale up --authkey=<FROM 1PASSWORD>

# Verify
tailscale status
# expect: vpn-prod-XX listed with Tailscale IP (e.g., 100.64.x.x)
```

### 4.12 logrotate for charon.log (operational, deprioritized)

```bash
cat > /etc/logrotate.d/charon-log-host <<'EOF'
/var/log/charon-log-host/charon.log {
  daily missingok rotate 7 compress delaycompress notifempty create 0644 root root
}
EOF
systemctl reload logrotate
# logrotate.timer is hourly; config will fire daily.
```

---

## 5. Verification (Definition of Done)

Run **all 10 checks** in order. Deploy is officially successful only when all 10 pass.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Portal HTTP health | `curl -sk https://vpn-portal.databyte.co.za/api/health` | `{"status":"ok","db_ok":true,"db_customers":N,"charon_ok":true}` |
| 2 | Public DNS resolves | `dig +short vpn-portal.databyte.co.za` | `<new VPS IPv4>` |
| 3 | LE cert valid | `curl -I https://vpn-portal.databyte.co.za` | HTTP/2 200, no cert warning |
| 4 | MariaDB customers present | `mariadb -uroot radius -e 'SELECT COUNT(*) FROM customers;'` | matches the count from kopia-restored host (live vpn-prod-01: 11) |
| 5 | FreeRADIUS local test | `radtest testing password 127.0.0.1:18120 0 testing123` | `Received Access-Accept` |
| 6 | strongswan container healthy | `docker ps --filter name=strongswan --format '{{.Status}}'` | `Up X minutes (healthy)` |
| 7 | charon VICI listening | `docker exec strongswan ss -ltn \| grep 4502` | `127.0.0.1:4502 LISTEN` |
| 8 | charon loaded eap-radius plugin | `docker exec strongswan cat /etc/strongswan.d/10-eap-radius.conf \| grep -c eap-radius` | ≥ 1 |
| 9 | IKE_SA from a real client | From any test device, connect to `myvpn.databyte.co.za`. Then on new VPS: `docker exec strongswan swanctl --list-sas` | IKE_SA appears with `ESTABLISHED` state |
| 10 | Both monitors + bot running | `systemctl status quota-monitor bandwidth-monitor bot-polling --no-pager` | All `active (running)` |

**Bonus check:** `mariadb -uroot radius -e 'SELECT COUNT(*) FROM radpostauth;'` should grow by ~customer-connect-count over the first hour.

---

## 6. Off-server secret capture

After deploy, update the off-server secret checklist (see `RUNBOOK-DR-REBUILD-AND-HA.md` §0.5) with:

- New VPS Tailscale IP
- New MariaDB root password (set in §4.1)
- New `TELEGRAM_BOT_TOKEN` (set in §4.2)
- The new `/var/spool/cron/crontabs/debian` content (captured in §4.8)
- Updated Cloudflare DNS A records (TTL 300s confirmed)

Verify access quarterly as part of §4.3 of the runbook.

---

## 7. Hard rules (LOCKED)

These rules are enforced in `RUNBOOK-DR-REBUILD-AND-HA.md` §5 and apply equally here:

1. **Portal business data lives in MariaDB** `radius` (Phase 4E 2026-07-12) — NOT in SQLite. SQLite at `/var/lib/strongswan/ipsec.db` holds only charon-internal state.
2. **`uniqueids=no`** for fail-safe HA (per strongSwan docs).
3. **MOBIKE must be enabled** in `rw-eap.conf` — required for client roaming AND for HA failover.
4. **Never `kopia restore /etc` blindly on a different host** — overwrites ALL of /etc including network interfaces, sshd_config, hostname. Always back up new host's `/etc` first.
5. **DNS TTL must be ≤300s** for HA cutover to work.
6. **MariaDB replication is NOT a backup** — Galera replicates state, but `DROP DATABASE` replicates too.
7. **strongSwan HA plugin requires kernel patches** — not a feature you can flip on. Option B (DNS round-robin + MOBIKE) sidesteps this.
8. **`bot-polling.service` is the SOLE Telegram polling source** (HOT-261) — never start a second `bot.py` instance.
9. **`bandwidth-monitor.service` `ExecStart` MUST point at `/opt/.../bandwidth-monitor-nft.py`** — NEVER `/home/zunaid/...` (`.bak` graveyard). Apply the sed in §4.5.

---

## 8. References

| Reference | Use |
|---|---|
| [`docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0](./RUNBOOK-DR-REBUILD-AND-HA.md) | DR rebuild from kopia on fresh VPS — 24-step procedure, 75-135 min RTO |
| [`docs/ARCHITECTURE.md`](./ARCHITECTURE.md) (v2.2.1 fact-check 2026-10-15) | Live prod state + architecture shifts since v1.x |
| [`docs/QUICKSTART.md`](./QUICKSTART.md) | Personal-use gateway-only path (no RADIUS) |
| [`docs/DEPLOYMENT.md`](./DEPLOYMENT.md) | Generic LXC deploy (less relevant for Xneelo VPS — kept for lab context) |
| [`docs/CLOUDFLARE-DNS.md`](./CLOUDFLARE-DNS.md) | Required DNS records + Cloudflare API token scope |
| [`scripts/bootstrap-xneelo.sh`](../scripts/bootstrap-xneelo.sh) | The one-shot script (per-step header inside file) |
| [`host/freeradius/README.md`](../host/freeradius/README.md) | FreeRADIUS operator overlay design |
| `prod-vpn-bot-edit` skill (workshop) | Editing the Telegram bot |
| `prod-vpn-bot-publish` skill (workshop) | Publishing bot changes to both repos + CI |
| [`strongSwan docs — High Availability`](https://docs.strongswan.org/docs/5.9/features/highAvailability.html) | HA option B (DNS round-robin + MOBIKE) |

---

## 9. Change log

| Rev | Date | Author | Notes |
|---|---|---|---|
| v1.0.0 | 2026-10-15 06:50 UTC / 08:50 SAST | Misha 🐻 | **Initial release.** Created to satisfy the broken `VPS-XNEELO-DEPLOY.md` references in `DEPLOYMENT.md`, `QUICKSTART.md`, and `SESSION-HISTORY.md` (file was referenced but did not exist). Documents the live `scripts/bootstrap-xneelo.sh` (19 steps per header, step counter drift in script internal `info` lines noted in §3.3) + the 16 post-script steps in §4 needed to reach production parity with `vpn-prod-01`. Verified against live `vpn-prod-01` running portal v2.2.1 (git HEAD `8e02a6e`) + bot v2.5.0 (HEAD `01ad543`) on 45-table MariaDB `radius` DB. RTO 15-25 min script + 30-45 min post-script = ~1 h for full prod-parity deploy. |
