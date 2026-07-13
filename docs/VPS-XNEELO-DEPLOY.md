# VPS-XNEELO-DEPLOY — Xneelo VPS Production Deployment Runbook

> **Deploy the strongSwan VPN stack to the Xneelo Johannesburg VPS.**
> Reference: [DEPLOYMENT.md](./DEPLOYMENT.md) for generic Debian/Ubuntu.
> This doc is Xneelo-specific: panel steps, Johannesburg region, Cloudflare DNS.

**Timeline:** ~20-30 min from SSH to live VPN.

---

## Before You Start

Zun does this in the Xneelo panel before handing you SSH access:

| # | Action | Panel location | Notes |
|---|---|---|---|
| 1 | Create VPS: **Debian 13 (trixie)**, **2 vCPU / 4GB RAM / 40GB SSD**, **JHB region** | Cloud → New → Server | If only Ubuntu 24.04 LTS available, use it (both work) |
| 2 | Set hostname: `vpn-prod-01.databyte.co.za` | Cloud → Server → Settings | Visible in certs + logs |
| 3 | Add your SSH public key (`~/.ssh/id_ed25519.pub`) | Cloud → Server → Access | Avoids password SSH entirely |
| 4 | Open inbound: **22/tcp, 80/tcp, 443/tcp, 500/udp, 4500/udp** | Cloud → Firewall | Allow SSH + IKEv2 + future portal HTTPS |
| 5 | Set reverse DNS: `myvpn.databyte.co.za` → VPS public IP | Cloud → Networking | **Critical** — cert SAN needs this |
| 6 | Note the **public IPv4** and **default gateway** | Cloud → Networking | Needed for bootstrap script |
| 7 | Create Cloudflare DNS A record: `myvpn` → `<VPS_IP>`, **grey cloud** | Cloudflare Dashboard → DNS | Cert generation requires this to resolve first |

**Share with Misha via Telegram DM (NOT group):**
- VPS public IPv4
- Default gateway IP
- SSH command: `ssh root@<VPS_IP> -i ~/.ssh/id_ed25519`
- Confirm reverse DNS is set

---

## What Gets Deployed

> **🟢 Verified-live v2.2.0 (2026-07-13, commit `805ea84`):** This list was last touched 2026-07-10 and contained four stale lines (DB / Firewall / Backup / Portal version). Updated below; see CHANGELOG.md v2.2.0 entry for the 6-bug fix chain in FreeRADIUS that this release closes.

| Component | Detail |
|---|---|
| **OS** | Debian 13 (trixie) or Ubuntu 24.04 LTS |
| **Docker** | `zun/strongswan:6.0.7-mschapv2-attrsql` (pre-built, 166MB) |
| **VPN protocol** | IKEv2 + EAP-MSCHAPv2 + EAP-PSK |
| **CA** | Self-signed (ECDSA P-256 server cert, RSA 4096 CA, 10y) |
| **DB (charon-internal)** | SQLite at `/var/lib/strongswan/ipsec.db` (retained; holds charon-only tables: `addresses`, `ike_sas`, `pools`, `child_configs`, `certificates`) |
| **DB (identity + portal business)** | **MariaDB 11.x at 127.0.0.1:3306, db `radius`** — single source of truth for RADIUS-protocol tables (`radcheck`, `radreply`, `radusergroup`, `radpostauth`, `radacct`) and portal business tables (`customers`, `users`, `devices`, `installer_tokens`, `tiers`, `alerts`, `purchases`, `operator_sessions`, `customer_portal_sessions`, `audit_log`, plus ~25 daloRADIUS-mirrored). 42 tables total. Phase 4E `cb9bf69` (2026-07-12). |
| **AAA backend** | **FreeRADIUS 3.0.x** (apt package; PID 868286 live) listening 127.0.0.1:1812 (auth), 127.0.0.1:1813 (accounting), 127.0.0.1:18120 (status). CoA relay via charon UDP/3799. Charon forwards EAP-MSCHAPv2 to FreeRADIUS via `eap-radius.conf` overlay (`/opt/strongswan-vpn-gateway/docker/strongswan.d/10-eap-radius.conf`). |
| **FreeRADIUS overlay** | `host/freeradius/` directory in repo + `provision-freeradius.sh` (idempotent, drift-detectable via `--check`, with smoke test). v2.2.0 commit `805ea84`. |
| **Firewall** | **nftables** (`nftables-vpn.service` runs `nft -f /etc/nftables.conf` with `inet quota_table` for per-VIP counters; `inet filter_table` for FORWARD/MASQ/MSS-clamp 1260). iptables-legacy → nftables swap done in Phase 7.5 (2026-07-09) for persistent counters. |
| **Subnet** | `10.99.0.0/24` (VPN clients get IPs from this range; per-user sticky VIP pinned by FreeRADIUS + MariaDB) |
| **Backup** | kopia → PBS (homelab, `pbs` @ 192.168.10.84) for full-host snapshots (`/etc`, `/var/lib/mysql`, `/var/lib/strongswan`, etc.). rclone → RustFS (192.168.10.89:30293, `rustfs:` remote) only for doc push + validated docs archive. |
| **Portal** | FastAPI + gunicorn as `vpn-portal.service` (systemd, local-only port behind nginx 443). Version on VPS: **v2.2.0** (matches HEAD `805ea84`). Reads MariaDB via `portal_auth._db()`. See [`host/scripts/deploy-portal-vps.sh`](../host/scripts/deploy-portal-vps.sh) for the deploy mechanism. |

---

## Bootstrap — Run Once on First SSH

### 1. Copy env file to VPS

On your Mac, copy the filled `.env.xneelo` to the VPS:
```bash
scp -i ~/.ssh/id_ed25519 ~/.openclaw/workspace/.env.xneelo root@<VPS_IP>:/tmp/.env.xneelo
```

### 2. Run the bootstrap script

```bash
# SSH in as root (one time only)
ssh -i ~/.ssh/id_ed25519 root@<VPS_IP>

# Run the bootstrap (15-25 min, logs to /tmp/bootstrap.log)
bash /tmp/bootstrap-xneelo.sh 2>&1 | tee /tmp/bootstrap.log
```

The bootstrap script does ALL of the following automatically:

| Step | What it does | Time |
|---|---|---|
| 1 | apt update + install Docker, rclone, sqlite3, **freeradius, freeradius-mysql, mariadb-server, mariadb-client**, nftables, unattended-upgrades, fail2ban, rkhunter | 5-7 min |
| 2 | Disable root SSH login | 30 sec |
| 3 | Create operator user (zunaid) with sudo + copy SSH key | 1 min |
| 4 | Enable unattended security upgrades | 30 sec |
| 5 | Configure fail2ban (SSH: 3 retries → 24h ban) | 30 sec |
| 6 | Configure rkhunter | 30 sec |
| 7 | Apply sysctl (ip_forward, redirect hardening) | 30 sec |
| 8 | Apply **nftables** (MSS clamp 1260 in `inet filter_table` + MASQ + `inet quota_table` for per-VIP counters). nftables replaces the iptables-legacy / `iptables-persistent` setup of v1.x — see Phase 7.5 (`docs/CHANGELOG.md`) | 1 min |
| 9 | Clone project repo to `/opt/strongswan-vpn-gateway` | 2 min |
| 10 | Generate CA + server certs (SAN = myvpn.databyte.co.za) | 10 sec |
| 11 | Configure rw-eap.conf + rw-psk.conf | 1 min |
| 12 | Build Docker image | 5 min |
| 13 | Start container | 1 min |
| 14 | Apply FreeRADIUS schema + operator overlay (`bash host/freeradius/provision-freeradius.sh`); seed operator + demo customers in MariaDB `radius` DB (`/opt/vpn-portal/scripts/migrate_sqlite_to_mariadb.py` or the live migration) | 1 min |
| 15 | Configure rclone for RustFS backup (docs push only; primary backup is kopia → PBS) | 1 min |
| 16 | Install kopia backup cron + nightly charon-db backup cron (03:00 UTC = 05:00 SAST) | 30 sec |
| 17 | Install `bandwidth-monitor.service` + `quota-monitor.service` + `quota-schema.service` (uses nftables counters; reads MariaDB; interoperates with FreeRADIUS DISABLE+CoA at 100%) | 30 sec |

**Total: ~25-40 min depending on network speed.**

### 3. Verify DNS has propagated before running bootstrap

On the VPS (after bootstrap but before generating certs — the bootstrap script will fail if DNS isn't up):

```bash
ping -c 1 myvpn.databyte.co.za
# Should return the VPS public IP
```

If it doesn't resolve yet, wait 2-5 minutes and try again.

---

## Post-Bootstrap Smoke Test

### From the VPS:

```bash
# 1. Container is running
docker ps --filter name=strongswan

# 2. charon is healthy
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --stats

# 3. No active SAs yet (expected)
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas

# 4. iptables MSS clamp is active
iptables -t mangle -L FORWARD -n -v | grep TCPMSS

# 5. VPN FORWARD rules present
iptables -L FORWARD -n -v | grep 10.99.0.0

# 6. ip_forward = 1
sysctl net.ipv4.ip_forward
```

### From your phone (external test — critical):

1. Disconnect from WiFi (use LTE/5G)
2. Install **strongSwan VPN Client** from Play Store (Android) or App Store (iOS)
3. Copy `strongswan-ca.crt.pem` from the VPS to your phone:
   ```bash
   # From your Mac:
   scp -i ~/.ssh/id_ed25519 root@<VPS_IP>:/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/strongswan-ca.crt.pem ~/Downloads/
   ```
4. Send the CA cert to your phone (Telegram, AirDrop, etc.)
5. Install CA cert on phone (Android: Settings → Security → Encryption → Install certificate → CA certificate)
6. Add strongSwan profile:
   - **Server:** `myvpn.databyte.co.za`
   - **VPN type:** IKEv2 EAP-MSCHAPv2
   - **Username:** `zun` (or `zun-operator` for admin)
   - **Password:** from `.env.xneelo` → `OPERATOR_PASSWORD`
   - **CA certificate:** select the installed `strongswan-ca.crt.pem`
7. Connect
8. Open Safari → https://ifconfig.me → should show the VPS public IP

### From the VPS, verify the connection:

```bash
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas
# Should show: rw-eap #1, ESTABLISHED, IKEv2, <phone_ip>[port] [10.99.0.50]
```

### From the VPS, verify per-user bandwidth limits (Phase 5D):

```bash
# 1. Service is running
sudo systemctl is-active bandwidth-monitor
# Expected: active

# 2. tc has a per-user class for the connected phone (last octet = class minor)
sudo tc class show dev eth0 | grep -E '1:[0-9]+ ' | grep -v '1:1 \|1:ffff'
# Expected: shows 1:<VIP_last_octet> htb rate 20Mbit ceil 20Mbit

# 3. iptables has MARK rules with comment "bw:10.99.0.50" (or whatever VIP)
sudo iptables-legacy -t mangle -L PREROUTING -n -v | grep 'bw:'
sudo iptables-legacy -t mangle -L POSTROUTING -n -v | grep 'bw:'
# Expected: MARK rule with comment "bw:10.99.0.X" for the connected VIP

# 4. Real-world test: from your phone (still on VPN), run a speed test
# Expected: capped at ~18-20 Mbps (allowing for overhead). NOT saturating the VPS link.
```

If a connected user does NOT show up in steps 2-3, check the daemon:

```bash
sudo journalctl -u bandwidth-monitor -n 30 --no-pager
```

Common cause: stale swanctl VICI connection. Restart the daemon:

```bash
sudo systemctl restart bandwidth-monitor
```

---

## Files and Where They Live

| File | Location | Notes |
|---|---|---|
| SQLite DB | `/var/lib/strongswan/ipsec.db` | Host bind-mount, survives container rebuild |
| Charon logs | `/var/log/charon-log-host/charon.log` | Host bind-mount |
| CA cert (client) | `/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/strongswan-ca.crt.pem` | Install on clients |
| Server cert | `/opt/strongswan-vpn-gateway/docker/swanctl/x509/server.crt.pem` | Server-only |
| CA key | `/opt/strongswan-vpn-gateway/docker/swanctl/private/strongswan-ca-key.pem` | **Mode 600. Keep secret.** |
| Server key | `/opt/strongswan-vpn-gateway/docker/swanctl/private/server-key.pem` | **Mode 600. Keep secret.** |
| docker-compose | `/opt/strongswan-vpn-gateway/docker/docker-compose.yml` | Controls the container |
| DB backup | `/var/backups/vpn/` (local) + RustFS | Nightly cron |
| Bootstrap log | `/tmp/bootstrap.log` | Full run log |

---

## Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `gen-certs.sh` fails with "cannot resolve myvpn.databyte.co.za" | DNS A record not propagated yet | Wait 2-5 min, try again |
| Phone connects but `ifconfig.me` shows phone IP | MASQUERADE missing | Check iptables POSTROUTING rules |
| TCP sites timeout after VPN connects (5G) | MSS clamp missing | `iptables -t mangle -L FORWARD` — should show TCPMSS 1260 |
| Phone hangs at "negotiating" | Firewall blocking UDP 500/4500 | Verify Xneelo panel firewall + iptables INPUT |
| strongSwan client says "no shared key" | Wrong CA installed OR wrong EAP password | Reinstall CA cert. Check `OPERATOR_PASSWORD` matches rw-eap.conf |
| `swanctl --list-sas` empty after client connects | DB not initialized | `docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-pools` |

---

## Rollback

If something goes badly wrong:

```bash
# Stop the container
cd /opt/strongswan-vpn-gateway/docker
docker compose --profile vpn down

# Remove the container + image
docker compose down --rmi all

# Restore original OS state (Xneelo panel → Server → Rebuild)
# Then re-run from bootstrap step.
```

The DB at `/var/lib/strongswan/ipsec.db` is NOT removed by `docker compose down` — it survives on the host.

For catastrophic failure: Xneelo panel → Server → Snapshot → Restore a clean snapshot taken before bootstrap.

---

## Maintenance

| Task | Frequency | How |
|---|---|---|
| Check Docker container health | Weekly | `docker ps --filter name=strongswan` |
| Check disk space | Weekly | `df -h` |
| Review charon logs | Weekly | `tail -100 /var/log/charon-log-host/charon.log` |
| Verify backup ran | Daily (check RustFS) | `rclone ls rustfs:open-claw-push/vpn-prod-01/db/` |
| Security updates | Automatic | `unattended-upgrades` handles this |
| fail2ban bans | Weekly | `fail2ban-client status sshd` |
| Update Docker image | Monthly or on security CVE | `docker pull zun/strongswan:6.0.7-mschapv2-attrsql` then `docker compose --profile vpn up -d` |
| Rotate secrets | Quarterly | Re-run `gen-certs.sh` + reinstall CA on all clients |

---

## Security Notes for Production

This deployment is **just the VPN server**; the customer + operator portal (`myvpn.databyte.co.za` / `vpn-portal.databyte.co.za`) lives in a separate process and is covered by the portal deploy runbook.

Current state at the VPN tier (post 5D pre-launch, 2026-06-22 → 2026-06-26):

- **IKEv2 only** — UDP 500 + UDP 4500, no HTTP on this host directly
- **Public web** — portal is fronted by `myvpn.databyte.co.za` via Cloudflare proxy + nginx; portal TLS via Let's Encrypt on the portal host (certbot)
- **SSH key only** — no password authentication
- **fail2ban** blocks SSH brute force after 3 failed attempts
- **Customer data** — 40 active customers, daily backup to `rustfs:open-claw-push/strongswan-db/daily/`
- **NSA vulnerability CVE-2026-47895** is patched in strongSwan 6.0.7 (our image)

For Phase 5D — RADIUS migration (in progress 2026-07-05, replaces the SaaS billing scope):
- FreeRADIUS + daloRADIUS on prod VPS (single MariaDB, portal keeps management)
- Full plan in `../install-radius-daloradius.md` (7 phases)
- Operator-only ACL — full daloRADIUS billing engine only when second operator joins (deferred per Zun)
- Cert rotation automation, log management, uptime monitoring — ongoing (already partly in place)
- CA hierarchy: separate production CA from lab CA

---

## Next Steps After This Deploy

1. **Smoke test complete** → archived
2. **iPhone test** (your daily driver on LTE) → Verify MOBIKE works (CONFIRMED 2026-06-22 + 2026-07-04)
3. **Portal deployment** → ✅ Live since 2026-06-22 (`vpn-portal.service` on this VPS, port 8080 + nginx reverse proxy via `myvpn.databyte.co.za`); see `host/scripts/deploy-portal-vps.sh`
4. **Phase 5D — RADIUS migration** → 🟡 In progress 2026-07-05; full plan in `../install-radius-daloradius.md` (7 phases)
5. **Second VPS for HA** (Phase 5H, after 5D completes) → Keepalived + floating IP → `PLAN-5H-HA-LB.md`

---

**Last updated:** 2026-06-22 (Misha) — Initial Xneelo deployment runbook.