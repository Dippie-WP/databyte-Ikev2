# Quick start (gateway-only self-host)

A complete end-to-end deploy from a fresh Linux box. Single-operator setup — you host the server, you use the server, you administer it. **Gateway-only** — no FreeRADIUS, no MariaDB, no portal. Useful for learning, prototyping, or running your own personal VPN.

> **For the full production stack** (FreeRADIUS + MariaDB + FastAPI portal + Telegram bot), see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — the "Build from scratch" section is the canonical guide.

Assumes you have:
- A Linux server (Debian/Ubuntu) with Docker installed
- A public IP (static, or dynamic with DDNS)
- Access to your router to forward UDP 500 + UDP 4500
- A DNS name pointing to your public IP (e.g., `vpn.example.com`)
- Root on the server

**Total time:** ~30 min on a fresh host, ~10 min if you've done it before.

---

### 1. Clone the repo

```bash
git clone https://github.com/<your-org>/<your-vpn-repo>.git
cd <your-vpn-repo>
```

### 2. Generate your certs

The image doesn't ship certs — you generate your own CA and server cert.

```bash
# Replace vpn.example.com with your actual hostname
SERVER_ID=vpn.example.com bash scripts/gen-certs.sh
```

This creates:
- `docker/swanctl/x509ca/strongswan-ca.crt.pem` (CA cert — **give this to your clients**)
- `docker/swanctl/x509/server.crt.pem` (server cert, 1y validity)
- `docker/swanctl/private/server-key.pem` (server private key, mode 600)
- `docker/swanctl/private/strongswan-ca-key.pem` (CA private key, mode 600)

> **Note:** Uses RSA-2048 for broad client compatibility (including older iOS versions). Cert is 1y expiry; rotate manually.

### 3. Set your admin password

Copy the EAP secrets template and edit it:

```bash
cp docker/swanctl/conf.d/rw-eap.conf.template docker/swanctl/conf.d/rw-eap.conf
$EDITOR docker/swanctl/conf.d/rw-eap.conf
```

Example secrets block at the bottom of the file:
```ini
secrets {
  eap-<your-username> {
    id = <your-username>
    secret = "<choose-a-strong-password>"
  }
  ike-psk {
    id = vpn.example.com
    secret = "<generate-a-32-byte-base64-key>"  # openssl rand -base64 32
  }
}
```

You also need to set the **server identity** in `rw-eap.conf` to match your cert:
```ini
connections {
  rw-eap {
    local {
      id = vpn.example.com  # must match your cert's CN/SAN
      cert = /etc/swanctl/x509/server.crt.pem
    }
    ...
  }
}
```

### 4. Build the image (~5 min on first build)

```bash
bash scripts/build-image.sh
# Optional: tag a specific version
# bash scripts/build-image.sh <your-namespace>/strongswan:6.0.7-mschapv2-attrsql
```

### 5. Apply host network config (one-time, needs root)

```bash
# IP forwarding
sudo cp host/sysctl.d/99-strongswan.conf /etc/sysctl.d/
sudo sysctl --system

# iptables (MASQUERADE + FORWARD for 10.99.0.0/24)
sudo cp host/iptables/rules.v4.template /etc/iptables/rules.v4
sudo systemctl restart netfilter-persistent

# Docker iptables persistence (watchdog — auto-recovers if rules drop)
# Note: verify the unit's ExecStart in your fork — role varies by version
sudo cp host/systemd/strongswan-iptables-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strongswan-iptables-watchdog.service
```

**On your router:** forward UDP 500 and UDP 4500 from your public IP to the Docker host's LAN IP.

### 6. Start the container

```bash
cd docker
docker compose --profile vpn up -d
```

Watch the logs:
```bash
docker logs -f strongswan
```

You should see: `loaded plugins: ... attr-sql ... sqlite ... eap-mschapv2 ...` and `charon (N) started` (N = number of worker threads, typically 16).

### 7. Seed the DB with your first user

After charon has initialized the schema (on first run, ~5 sec):

```bash
# Compute NTLM hash of your password
PASSWORD='<choose-a-strong-password>'
HASH=$(echo -n "$PASSWORD" | iconv -t UTF-16LE | openssl dgst -md4 -provider legacy -provider default -hex | awk '{print toupper($NF)}')

# Seed the DB (10.99.0.50 is the first IP in the 10.99.0.0/24 pool — adjust if your pool is different)
USERNAME=<your-username> VIP=10.99.0.50 NTLM_HASH=$HASH bash scripts/seed-db.sh
```

### 8. Reload secrets in charon (no restart needed)

```bash
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds
```

### 9. Connect from a client

#### Android
1. Install **strongSwan VPN Client** from Play Store
2. Import `docker/swanctl/x509ca/strongswan-ca.crt.pem` (email it to yourself, tap to install)
3. Add VPN profile:
   - Gateway: `vpn.example.com`
   - Type: IKEv2 EAP (Username/Password)
   - Username: `<your-username>` (lowercase)
   - Password: the secret you set in step 3
4. Connect — should get VIP 10.99.0.50 (or whatever you seeded)

#### iPhone / iPad (iOS 18+)

**Use the strongSwan iOS app** ([App Store link](https://apps.apple.com/app/strongswan-vpn-client/id1453698374)). iOS native VPN Settings + `.mobileconfig` is broken for EAP-MSCHAPv2 on iOS 18+ (iOS sends EAP identity, server sends MSCHAPV2 challenge, iOS never responds). The strongSwan app is the official client with working EAP-MSCHAPv2.

Setup:
1. Install **strongSwan VPN Client** from the App Store (free)
2. Open the app → tap **+** to add a profile
3. **Server:** your VPN hostname (e.g. `vpn.example.com`)
4. **Username:** the user you seeded (lowercase)
5. **Password:** the secret from step 3
6. **CA certificate:** import the `strongswan-ca.crt.pem` (air-drop it, or download from a URL you host)
7. **Server identity (advanced / settings cog):** must match the server cert CN/SAN, e.g. `vpn.example.com`. If the app auto-fills the IP, **change it** — charon matches on IDr.
8. Tap **Save** → flip the toggle

If the app says "trust this CA": enable it. If "no proposal chosen": the server expects AES-256/SHA2-256/DH14 (strongSwan app defaults — should just work).

#### Windows
1. **PowerShell as Admin:**
   ```powershell
   Add-VpnConnection -Name "MyVPN" -ServerAddress "vpn.example.com" `
     -TunnelType IKEv2 -AuthenticationMethod EAP `
     -RememberCredential
   Set-VpnConnectionIPsecConfiguration -Name "MyVPN" `
     -DHGroup Group14 -PfsGroup PFS2048 `
     -IntegrityCheckMethod SHA256 -EncryptionMethod AES256
   ```
2. **Install CA to LocalMachine Trusted Root** (NOT CurrentUser):
   ```powershell
   Import-Certificate -FilePath "strongswan-ca.crt.pem" `
     -CertStoreLocation "Cert:\LocalMachine\Root"
   ```
3. Network & Internet settings → VPN → MyVPN → Connect

#### Linux
Use NetworkManager-strongswan-gnome, or charon-cmd for CLI testing.

### 10. Verify it works

```bash
# On the Docker host, check active SAs
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas

# Check your client has the right VIP
ip addr show  # on the client, look for 10.99.0.50 on the tun0/ppp0 interface

# Test connectivity
ping 1.1.1.1     # should work
curl https://icanhazip.com  # should show your SERVER's public IP, not your client's
```

### Adding more users (after initial deploy)

```bash
# Generate NTLM hash for new password
HASH=$(echo -n '<new-user-password>' | iconv -t UTF-16LE | openssl dgst -md4 -provider legacy -provider default -hex | awk '{print toupper($NF)}')

# Seed DB
USERNAME=<new-username> VIP=10.99.0.51 NTLM_HASH=$HASH bash scripts/seed-db.sh

# Add to secrets block in docker/swanctl/conf.d/rw-eap.conf:
#   eap-<new-username> { id = <new-username>, secret = "<new-user-password>" }

# Reload (no restart)
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds
```

### Updating the image (after a new release)

```bash
git pull
bash scripts/build-image.sh
cd docker && docker compose --profile vpn up -d --force-recreate
```

### Rollback

```bash
# Roll back to previous image tag
docker tag <your-namespace>/strongswan:6.0.7-mschapv2-attrsql <your-namespace>/strongswan:6.0.7-mschapv2-attrsql.bak
docker tag <your-namespace>/strongswan:6.0.7-mschapv2-attrsql.previous <your-namespace>/strongswan:6.0.7-mschapv2-attrsql
cd docker && docker compose --profile vpn up -d --force-recreate
```

For HA rollback (multiple instances + load balancer), use a Floating IP on your cloud provider rather than two A records — two A records would round-robin clients unpredictably, breaking VPN sessions.

---

**Hand-curated 2026-10-17 for generic use.** This doc is part of the generic template — all technical claims verified, all databyte-specific references removed. For the full production stack with FreeRADIUS + MariaDB + portal + bot, see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).
