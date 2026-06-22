# DEPLOYMENT — step by step on a new host

This walks through deploying the strongSwan gateway on a fresh LXC. The live deployment is on LXC 902 (192.168.10.212, pve2) — that one is the reference. Use this guide for a new LXC, new host, or recovery.

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
