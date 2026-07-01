# DEPLOYMENT — step by step on a new host

This walks through deploying the strongSwan gateway on a fresh LXC. **The live production deployment is on the Xneelo VPS (`vpn-prod-01`, 154.65.110.44, `myvpn.databyte.co.za`) — NOT on any LXC.** LXC 903 (192.168.10.98, `vpn-gateway`) is the local LAN lab; LXC 902 (192.168.10.212, `myservices`) hosts the monitoring stack only (Grafana / Prometheus / Paperless / Dockhand). Use this guide for a new LXC, new host, or recovery.

> **For the Xneelo VPS production deployment ( Johannesburg, myvpn.databyte.co.za):**
> Use **[VPS-XNEELO-DEPLOY.md](./VPS-XNEELO-DEPLOY.md)** instead — it has the one-shot `bootstrap-xneelo.sh` script that does everything in 15-25 min, plus Cloudflare DNS setup.
>
> This generic DEPLOYMENT.md is the reference for manual/debug steps.

## 0. Prerequisites

- A Debian/Ubuntu LXC with: 1+ CPU, 1GB+ RAM, 1GB disk free
- Internet egress (for `apt-get`, `wget strongswan.org`, `rclone`)
- Docker installed (`curl -fsSL https://get.docker.com | sh`)
- `rclone` installed (`apt install rclone` or https://rclone.org/install/)
- `sqlite3` (`apt install sqlite3`) — for DB ops
- Public IP for the LXC, with UDP 500 + 4500 forwarded from the router
- A DNS name pointing to the public IP (e.g., `vpn.example.com`) — needed for the server cert SAN. If you don't have one, use the public IP and skip the SAN.

## 1. LXC host setup (one-time)

### 1.1 Sysctl

```bash
sudo cp host/sysctl.d/99-strongswan.conf /etc/sysctl.d/
sudo sysctl --system
# Verify:
sysctl net.ipv4.ip_forward  # should be 1
```

### 1.2 firewalld

```bash
sudo apt install firewalld
sudo cp host/firewalld/zones/trusted.xml /etc/firewalld/zones/
sudo firewall-cmd --reload
sudo firewall-cmd --zone=trusted --list-all
# Should show:
#   sources: 10.99.0.0/24
#   masquerade: yes
```

If you DON'T use firewalld, you can use the iptables rules in `host/iptables/rules.v4.template` instead — they cover everything (MASQ + MSS clamp + ACCEPT).

### 1.3 iptables (MSS clamp — CRITICAL for 5G)

> **Backend:** `iptables-legacy` (not iptables-nft). Debian 13 defaults to nft; `scripts/bootstrap-xneelo.sh` line 188-194 pins alternatives to legacy before Step 8 loads any rules. Don't switch — charon VICI + bandwidth-monitor depend on legacy semantics.

```bash
sudo apt install iptables-persistent
# Netfilter asks to save current rules — say yes
sudo cp host/iptables/rules.v4.template /etc/iptables/rules.v4
sudo systemctl enable netfilter-persistent
sudo systemctl restart netfilter-persistent
# Verify the MSS rule is loaded:
sudo iptables -t mangle -L FORWARD -n -v | grep TCPMSS
# Should show: TCPMSS tcp ... TCPMSS set 1260
```

**If you skip this step, 5G clients will see TCP handshakes complete but responses time out (ERR_TIMED_OUT).** See ISSUES-LOG §5A.7.

## 2. Project checkout

```bash
cd ~  # or wherever
git clone https://github.com/Dippie-WP/strongswan-vpn-gateway.git
cd strongswan-vpn-gateway
```

## 3. Generate certs

```bash
SERVER_ID=vpn.example.com bash scripts/gen-certs.sh
# (or omit SERVER_ID for default vpn.homelab.local)
```

Outputs:
- `docker/swanctl/x509ca/strongswan-ca.crt.pem` — install on clients
- `docker/swanctl/x509/server.crt.pem` — used by charon
- `docker/swanctl/private/*-key.pem` — server key (mode 600)

## 4. Create swanctl configs

```bash
cd docker/swanctl/conf.d
cp rw-eap.conf.template rw-eap.conf
cp rw-psk.conf.template rw-psk.conf

# Edit rw-eap.conf:
#   - Uncomment the `secrets { eap-USERNAME { id = USERNAME; secret = "..."; } }` block
#   - Set your username + password

# Edit rw-psk.conf:
#   - Uncomment the `secrets { ike-psk { ...; secret = "..."; } }` block
#   - Set your PSK
```

Generate strong secrets:

```bash
openssl rand -base64 16   # for password
openssl rand -base64 32   # for PSK
```

## 5. Build the image

```bash
cd /path/to/strongswan-vpn-gateway
bash scripts/build-image.sh
# Or with a custom tag:
bash scripts/build-image.sh zun/strongswan:6.0.7-mschapv2-attrsql-v1.2.1
```

Build time: ~5 min. Output: `zun/strongswan:6.0.7-mschapv2-attrsql`.

## 6. Start the container

```bash
cd docker
docker compose --profile vpn up -d
# Wait ~10 sec for charon to start

# Verify:
docker ps --filter name=strongswan
docker logs strongswan --tail 30
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-pools
```

The first call to `swanctl --list-pools` initializes the SQLite DB schema. Without it, the DB dir looks empty.

## 7. Seed the first user

```bash
# Generate NTLM hash from password (or skip — charon will do it on first use)
PASSWORD='mySecret123'
NTLM_HASH=$(echo -n "$PASSWORD" | iconv -t utf-16le | openssl md4 -binary | xxd -p -c 32)
echo "NTLM hash: $NTLM_HASH"

# Seed (replace zun/10.99.0.50 with your username/VIP)
USERNAME=zun VIP=10.99.0.50 bash scripts/seed-db.sh

# Verify:
sqlite3 /var/lib/strongswan/ipsec.db "SELECT * FROM identities;"
sqlite3 /var/lib/strongswan/ipsec.db "SELECT * FROM addresses;"
```

## 8. Configure backup (optional, recommended)

```bash
# 1. Configure rclone
rclone config
# Add: name=rustfs, type=s3, provider=Other, endpoint=http://YOUR_TRUENAS:30293,
#       access_key_id, secret_access_key, region=us-east-1,
#       force_path_style=true, no_check_bucket=true

# 2. Create the bucket (one time)
rclone mkdir rustfs:open-claw-push

# 3. Install the backup script
sudo cp scripts/strongswan-db-backup.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/strongswan-db-backup.sh
echo "0 3 * * * root /usr/bin/flock -n /var/run/strongswan-db-backup.lock /usr/local/bin/strongswan-db-backup.sh" \
  | sudo tee /etc/cron.d/strongswan-db-backup
sudo chmod 644 /etc/cron.d/strongswan-db-backup
```

## 9. Client setup

### Android (works)

1. Install **strongSwan VPN Client** from Play Store
2. Copy the CA cert (`strongswan-ca.crt.pem`) to the phone (e.g., via Telegram)
3. Install: **Settings → Security → Encryption & credentials → Install a certificate → CA certificate**
4. Open strongSwan app → Add profile:
   - Server: `vpn.example.com` (your SERVER_ID)
   - VPN type: IKEv2 EAP
   - Username: `zun`
   - Password: (from rw-eap.conf)
   - CA certificate: select the installed one
5. Connect. Verify you get a VIP in the 10.99.0.0/24 range, and `https://ifconfig.me` shows the server's public IP.

### Windows 10/11 (works, EAP-MSCHAPv2)

Windows has a built-in IKEv2 client — no app install required. Two scripts
ship in the repo to make it one-shot:

| Script | Purpose |
|---|---|
| `scripts/setup-windows-vpn.ps1` | Full setup: install CA cert, create VPN connection, configure crypto, connect. **First-time use.** |
| `scripts/connect-databyte-vpn.ps1` | Idempotent: recreate connection + reconnect. **Re-runs any time.** Profile XML pre-configures EAP-MSCHAPv2 (no dialog at first connect) and ForceTunnel (no split tunneling). |
| `scripts/strongswan-ca.crt.pem` | Bundled CA cert (must be installed before first connect — see #43). |

**One-shot operator setup (Windows PowerShell 5.1+, as Administrator):**

```powershell
# Pull the three files from the OC host (or copy them via your channel)
Invoke-WebRequest http://OC_HOST:8888/connect-databyte-vpn.ps1 -OutFile connect-databyte-vpn.ps1
Invoke-WebRequest http://OC_HOST:8888/strongswan-ca.crt.pem -OutFile strongswan-ca.crt.pem
Invoke-WebRequest http://OC_HOST:8888/README-windows-vpn.md -OutFile README-windows-vpn.md

# Run the script (it installs the CA cert, creates the connection, configures crypto, and connects)
.\connect-databyte-vpn.ps1
```

The script bakes in operator credentials (`zun-operator` / EAP secret), the
server FQDN (`myvpn.databyte.co.za`), crypto match (AES256/SHA256/Group14/ECP384),
and the EAP-MSCHAPv2 profile XML. Re-running it is safe — it recreates the
connection and reconnects.

**Verify end-to-end (Windows):**

```powershell
# 1. Get-VpnConnection shows the tunnel up
Get-VpnConnection -Name 'Databyte VPN' | Select-Object Name, ConnectionStatus

# 2. Public IP is the VPS, not your ISP
(Invoke-WebRequest -Uri 'https://ifconfig.me' -UseBasicParsing).Content

# 3. Cap is enforced (download to public iperf3 target)
iperf3.exe -c iperf.angolacables.co.ao -p 9200 -t 30
# Expected: ~17 Mbps (cap minus XFRM overhead). See #49 — VPS-local tests bypass the cap.
```

**Known issues** (full list in 5D-BANDWIDTH-LIMITING.md lessons):

- **#43**: silent hang if CA cert not in Trusted Root CAs — the script handles this via `Import-Certificate`.
- **#47**: split tunneling on by default — handled via `<RoutingPolicyType>ForceTunnel</RoutingPolicyType>` in profile XML.
- **#48**: PowerShell 5.1 parser error on `} catch {` — script tested on Windows 10/11 stock 5.1.

For the bundled README, see `scripts/README-windows-vpn.md`.

### iOS (broken, see ISSUES-LOG)

The native IKEv2 client silently fails cert validation. Workaround for now: use **strongSwan app** (paid, ~$5) with PSK profile. Fix coming in v1.3 with Let's Encrypt cert.

### Friend (PSK)

Same as Android, but use the PSK profile (no cert needed, no cert install).

## 10. Verify end-to-end

```bash
# From LXC 902
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas

# Should show:
#   rw-eap: #1, ESTABLISHED, IKEv2, ...
#     remote 'zun' @ <phone_ip>[port] [10.99.0.50]
#     ...

# After client connects to https://ifconfig.me
curl --interface 10.99.0.50 --max-time 10 https://ifconfig.me
# Should return the LXC's public IP
```

> **For per-user bandwidth limits (Phase 5D):**
> See [5D-BANDWIDTH-LIMITING.md](./5D-BANDWIDTH-LIMITING.md) — the
> `bandwidth-monitor` daemon enforces per-user bandwidth caps (tc + iptables).
> On Xneelo, this is installed as part of `bootstrap-xneelo.sh` step 17.

## 11. Rollback

If v1.2 is broken, rollback to v1.1 (PSK only, no VIP pin) in 3 min:

```bash
bash scripts/rollback-v1.1.sh
# Type 'yes' to confirm
```

The DB is bind-mounted, not in the image — your data is preserved.

## 12. Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `swanctl --list-sas` shows nothing | SA not established | Check phone logs, charon filelog |
| Phone connects, but `ifconfig.me` shows phone's IP, not server's | MASQ missing | Check firewalld trusted zone has `<masquerade/>` |
| `ERR_TIMED_OUT` on first browse attempt | MSS clamp missing | Apply iptables rules.v4 (5A.7) |
| Phone connects, VPN shows OK, but no traffic flows | install_virtual_ip=yes default | Verify `00-virtual-ip.conf` is bind-mounted (5A.6) |
| Stuck in CONNECTING | Network reachability | Check router port-forwards, public IP |
| iana.org / Cloudflare sites time out, others work | 5G MTU | MSS clamp 1260 (5A.7) |
| Windows: IKEv2 connects but iperf3 returns 0 bytes | CA cert not in Trusted Root CAs | `Import-Certificate` (see #43) |
| Windows: VPN connects but `ifconfig.me` shows ISP IP, not VPS | Split tunneling on | Profile XML `ForceTunnel` (see #47) |
| Windows: PowerShell parse error on `} catch {` | PS 5.1 parser quirk | Put `catch` on its own line (see #48) |
| VPS-local iperf3 (`-c 127.0.0.1`) shows unshaped speed | Cap only engages on FORWARD | Test via public target, e.g. `iperf.angolacables.co.ao:9200` (#49) |
| Port 9102/8080/3000 unreachable from public internet on Xneelo | Edge firewall blocks non-22 TCP | Add Security Group rule in Xneelo control panel (#50) |
