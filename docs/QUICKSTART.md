## Quick start (per-host / customer self-host)

A complete end-to-end deploy from a fresh Linux box. Single-operator setup — you host the server, you use the server, you administer it.

> **For the Databyte production deployment (the architecture this repo currently runs on — FreeRADIUS + MariaDB):** see [`docs/VPS-XNEELO-DEPLOY.md`](docs/VPS-XNEELO-DEPLOY.md). The per-host setup below is the **gateway-only** path without RADIUS — useful for learning, prototyping, or running your own personal VPN.

Assumes you have:
- A Linux server (Debian/Ubuntu) with Docker installed
- A public IP (static, or dynamic with DDNS)
- Access to your router to forward UDP 500 + UDP 4500
- A DNS name pointing to your public IP (e.g., `vpn.example.com`)
- Root on the server

**Total time:** ~30 min on a fresh host, ~10 min if you've done it before.

### 1. Clone the repo

```bash
git clone https://github.com/Dippie-WP/databyte-Ikev2.git
cd databyte-Ikev2
```

### 2. Generate your certs (5 sec)

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

> **Note:** The script default uses RSA-2048 for the server cert (changed 2026-06-19 — ECDSA P-256 was rejected by iOS 18+ IKEv2). Signature is PKCS#1 v1.5 with sha256 (RSASSA-PSS was tried but iOS 18 silently rejected it — rolled back to PKCS#1 v1.5). Cert is 1y expiry; rotate manually.

### 3. Set your admin password

The default `rw-eap.conf.template` has an `eap-zun` user. Edit it to your identity and set a password:

```bash
cp docker/swanctl/conf.d/rw-eap.conf.template docker/swanctl/conf.d/rw-eap.conf
$EDITOR docker/swanctl/conf.d/rw-eap.conf
```

Example secrets block at the bottom of the file:
```ini
secrets {
  eap-yourname {
    id = yourname
    secret = "YourStrongPassword2026!"
  }
  ike-psk {
    id = vpn.example.com
    secret = "jzm+7IIsL+8lXwktTn8M5+kV4VTM2L1KjAotUQtKMyc="  # generate your own
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
# bash scripts/build-image.sh zun/strongswan:6.0.7-mschapv2-attrsql
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

You should see: `loaded plugins: ... attr-sql ... sqlite ... eap-mschapv2 ...` and `charon (16) started`.

### 7. Seed the DB with your first user

After charon has initialized the schema (on first run, ~5 sec):

```bash
# Compute NTLM hash of your password
PASSWORD='YourStrongPassword2026!'
HASH=$(echo -n "$PASSWORD" | iconv -t UTF-16LE | openssl dgst -md4 -provider legacy -provider default -hex | awk '{print toupper($NF)}')

# Seed the DB (VIP 10.99.0.50 — first IP in the pool)
USERNAME=yourname VIP=10.99.0.50 NTLM_HASH=$HASH bash scripts/seed-db.sh
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
   - Username: `yourname` (lowercase — see CORR-2026-07-11-026 in CHANGELOG)
   - Password: `YourStrongPassword2026!`
4. Connect — should get VIP 10.99.0.50

#### iPhone / iPad (iOS 18+)

**Use the strongSwan iOS app** ([App Store link](https://apps.apple.com/app/strongswan-vpn-client/id1453698374)) — iOS native VPN Settings + `.mobileconfig` is **fundamentally broken for EAP-MSCHAPv2** on iOS 18+ (iOS sends EAP identity, server sends MSCHAPV2 challenge, iOS never responds). The strongSwan app is the official client and has a working EAP-MSCHAPv2 implementation.

Setup:
1. Install **strongSwan VPN Client** from the App Store (free)
2. Open the app → tap **+** to add a profile
3. **Server:** your public IP or hostname (e.g. `vpn.example.com`)
4. **Username:** the strongSwan user you seeded (lowercase — e.g. `yourname`)
5. **Password:** the secret you set in `docker/swanctl/conf.d/rw-eap.conf`
6. **CA certificate:** import the `strongswan-ca.crt.pem` (e.g. air-drop it, or download from a URL you host)
7. **Server identity (advanced / settings cog):** must match the server cert CN/SAN, e.g. `vpn.example.com`. If the app auto-fills the IP, **change it** — charon matches on IDr.
8. Tap **Save** → flip the toggle

If the app says "trust this CA": enable it. If "no proposal chosen": the server expects AES-256/SHA2-256/DH14 (default in the strongSwan app — should just work).

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

> **For Databyte customers:** the canonical Windows installer is `setup-databyte-vpn-<customer>-<device>.ps1` (baked by operator from the customer portal). See `docs/DAT-VPN-EXT-WIN-001.md`.

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
HASH=$(echo -n 'NewUserPassword' | iconv -t UTF-16LE | openssl dgst -md4 -provider legacy -provider default -hex | awk '{print toupper($NF)}')

# Seed DB
USERNAME=alice VIP=10.99.0.51 NTLM_HASH=$HASH bash scripts/seed-db.sh

# Add to secrets block in docker/swanctl/conf.d/rw-eap.conf:
#   eap-alice { id = alice, secret = "NewUserPassword" }

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
docker tag zun/strongswan:6.0.7-mschapv2-attrsql zun/strongswan:6.0.7-mschapv2-attrsql.bak
docker tag zun/strongswan:6.0.7-mschapv2-attrsql.previous zun/strongswan:6.0.7-mschapv2-attrsql
cd docker && docker compose --profile vpn up -d --force-recreate
```

For HA rollback (multiple instances + LB), see `docs/PLAN-5H-HA-LB.md`.

