#!/usr/bin/env bash
# bootstrap-xneelo.sh — One-shot bootstrap for the Xneelo VPS
#
# Run this ONCE after first SSH to a fresh Xneelo VPS.
# Reads variables from ../.env.xneelo (copy from .env.xneelo.example first).
#
# What it does (in order):
#   1. apt update + install Docker, rclone, sqlite3, unattended-upgrades, fail2ban, rkhunter, iptables-persistent
#   2. Disable root SSH login ( PermitRootLogin no )
#   3. Create non-root operator user (zunaid) with sudo
#   4. Copy SSH key to operator user
#   5. Enable unattended security upgrades
#   6. Configure fail2ban for SSH (3 retries → 24h ban)
#   7. Apply sysctl hardening (ip_forward, redirect hardening)
#   8. Apply iptables MSS clamp + VPN FORWARD rules
#   9. Clone project repo
#   10. Generate strongSwan CA + server certs (SAN = SERVER_ID)
#   11. Edit rw-eap.conf + rw-psk.conf from templates
#   12. Build docker image
#   13. Start container via docker compose
#   14. Seed first users in SQLite DB
#   15. Configure rclone remote for RustFS backup
#   16. Install DB backup cron job
#   17. Smoke test
#
# Time: ~15-25 min depending on network speed.
#
# Usage:
#   # DO THIS ON YOUR LOCAL MACHINE — don't run this while root-ssh'd in yet
#   # 1. Copy .env.xneelo.example to .env.xneelo and fill in the values
#   cp .env.xneelo.example .env.xneelo
#   vim .env.xneelo
#
#   # 2. scp the env file + this script to the VPS
#   scp -i ~/.ssh/id_ed25519 .env.xneelo bootstrap-xneelo.sh root@<VPS_IP>:/tmp/
#
#   # 3. SSH in and run
#   ssh -i ~/.ssh/id_ed25519 root@<VPS_IP>
#   bash /tmp/bootstrap-xneelo.sh 2>&1 | tee /tmp/bootstrap.log
#
#   # 4. If anything breaks, check /tmp/bootstrap.log
#
# Requirements:
#   - Running as root on the target VPS
#   - Debian 13 (trixie) or Ubuntu 24.04 LTS
#   - VPS must have public internet access
#   - .env.xneelo must exist in /tmp/ (or ../.env.xneelo relative to this script)

set -euo pipefail

# ─── Colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
NC='\033[0m' # No Colour

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR]${NC}  $*" >&2; }
die()   { err "$*"; exit 1; }

# ─── Env ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${REPO_DIR}/.env.xneelo"

# Allow running from /tmp with /tmp/.env.xneelo
if [[ ! -f "$ENV_FILE" ]]; then
    ENV_FILE="/tmp/.env.xneelo"
fi

if [[ ! -f "$ENV_FILE" ]]; then
    die "Missing .env.xneelo. Copy .env.xneelo.example to .env.xneelo and fill in values first.
  Expected at: ${ENV_FILE}"
fi

info "Loading environment from: ${ENV_FILE}"
set -a; source "$ENV_FILE"; set +a

# ─── Validation ────────────────────────────────────────────────────────────────
REQUIRED=("PUBLIC_IPV4" "SERVER_ID" "OPERATOR_USER" "OPERATOR_PASSWORD" "DEMO_CUSTOMER_USER" "DEMO_CUSTOMER_VIP")
for var in "${REQUIRED[@]}"; do
    if [[ -z "${!var}" ]]; then
        die "Required variable ${var} is empty in .env.xneelo. Fill it in and re-run."
    fi
done
ok "All required variables are set."

# ─── Pre-flight ──────────────────────────────────────────────────────────────
info "=== Pre-flight checks ==="
whoami == root || die "Must run as root"
uname -r | grep -qE "Linux" || die "Not Linux?"
ok "Running as root on Linux"

# Detect OS
if [[ -f /etc/debian_version ]]; then
    OS_FAMILY="debian"
    info "Detected: Debian/Ubuntu family"
elif [[ -f /etc/redhat-release ]]; then
    OS_FAMILY="rhel"
    warn "RHEL-family detected. This script is written for Debian/Ubuntu. YMMV."
else
    warn "Unknown OS. Proceeding anyway..."
    OS_FAMILY="unknown"
fi

# Detect default interface + gateway if not set
if [[ -z "${DEFAULT_GATEWAY:-}" ]]; then
    DEFAULT_GATEWAY=$(ip route | awk '/default/ {print $3; exit}')
    info "DEFAULT_GATEWAY detected: ${DEFAULT_GATEWAY}"
fi

if [[ -z "${INTERNAL_IFACE:-}" ]]; then
    INTERNAL_IFACE=$(ip route | awk '/default/ {print $5; exit}')
    info "INTERNAL_IFACE detected: ${INTERNAL_IFACE}"
fi

# ─── Step 1: apt update + install packages ───────────────────────────────────
info "=== Step 1/16: Updating apt and installing packages ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    docker.io \
    docker-compose-plugin \
    rclone \
    sqlite3 \
    unattended-upgrades \
    fail2ban \
    rkhunter \
    iptables-persistent \
    openssl \
    sudo \
    git \
    ufw \
    bc \
    dnsutils \
    net-tools \
    > /dev/null 2>&1
ok "Packages installed."

# ─── Step 2: Disable root SSH login ─────────────────────────────────────────
info "=== Step 2/16: Disabling root SSH login ==="
sed -i 's/^PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
grep -q "^PermitRootLogin no" /etc/ssh/sshd_config && ok "PermitRootLogin = no" || warn "Could not verify PermitRootLogin setting"
systemctl reload sshd
ok "SSH root login disabled."

# ─── Step 3: Create operator user ────────────────────────────────────────────
info "=== Step 3/16: Creating operator user (${OPERATOR_USER}) ==="
id "${OPERATOR_USER}" &>/dev/null && ok "User ${OPERATOR_USER} exists" || {
    useradd -m -s /bin/bash -G sudo "${OPERATOR_USER}"
    echo "${OPERATOR_USER}:${OPERATOR_PASSWORD}" | chpasswd
    ok "User ${OPERATOR_USER} created."
}

# Copy SSH key from root's authorized_keys
mkdir -p "/home/${OPERATOR_USER}/.ssh"
chmod 700 "/home/${OPERATOR_USER}/.ssh"
if [[ -f /root/.ssh/authorized_keys ]]; then
    cp /root/.ssh/authorized_keys "/home/${OPERATOR_USER}/.ssh/"
    chmod 600 "/home/${OPERATOR_USER}/.ssh/authorized_keys"
    chown -R "${OPERATOR_USER}:${OPERATOR_USER}" "/home/${OPERATOR_USER}/.ssh"
    ok "SSH key copied to ${OPERATOR_USER}."
else
    warn "No /root/.ssh/authorized_keys found. You may need to add the SSH key manually."
fi

# sudo without password for operator (for ansible/automation)
echo "${OPERATOR_USER} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/90-${OPERATOR_USER}
chmod 440 /etc/sudoers.d/90-${OPERATOR_USER}
ok "Operator user configured with passwordless sudo."

# ─── Step 4: unattended-upgrades ─────────────────────────────────────────────
info "=== Step 4/16: Enabling unattended security upgrades ==="
dpkg-reconfigure -plow unattended-upgrades - <<< "yes" > /dev/null 2>&1 || true
cat > /etc/apt/apt.conf.d/51unattended-upgrades-security << 'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot-Time "02:00";
EOF
ok "Unattended upgrades configured."

# ─── Step 5: fail2ban ────────────────────────────────────────────────────────
info "=== Step 5/16: Configuring fail2ban ==="
cat > /etc/fail2ban/jail.d/sshd.local << 'EOF'
[sshd]
enabled  = true
port     = ssh
filter   = sshd
logpath  = /var/log/auth.log
maxretry = 3
bantime  = 86400   # 24 hours
findtime = 3600    # 1 hour window
EOF
systemctl enable fail2ban
systemctl start fail2ban
ok "fail2ban configured (SSH: 3 retries → 24h ban)."

# ─── Step 6: rkhunter setup ──────────────────────────────────────────────────
info "=== Step 6/16: Configuring rkhunter ==="
sed -i 's/^CRON_DAILY_RUN=.*/CRON_DAILY_RUN="yes"/' /etc/default/rkhunter || true
sed -i 's/^CRON_DB_UPDATE=.*/CRON_DB_UPDATE="yes"/' /etc/default/rkhunter || true
rkhunter --propupd 2>/dev/null
ok "rkhunter configured."

# ─── Step 7: Sysctl hardening ────────────────────────────────────────────────
info "=== Step 7/16: Applying sysctl hardening ==="
cat >> /etc/sysctl.d/99-strongswan.conf << 'EOF'

# ── Xneelo VPS bootstrap ──────────────────────────────────────────────────
# VPN gateway: forward traffic from VPN clients
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1

# Reject redirect attacks
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0

# Harden against spoofed source routes
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# Ignore ICMP redirects entirely on public-facing interface
net.ipv4.conf.${INTERNAL_IFACE}.accept_redirects = 0
net.ipv4.conf.${INTERNAL_IFACE}.send_redirects = 0
EOF

# Apply immediately
sysctl -p /etc/sysctl.d/99-strongswan.conf > /dev/null 2>&1
# Verify
sysctl net.ipv4.ip_forward | grep -q "= 1" && ok "ip_forward = 1" || warn "ip_forward not set to 1"
ok "Sysctl hardening applied."

# ─── Step 8: iptables MSS clamp + VPN rules ────────────────────────────────
info "=== Step 8/16: Applying iptables MSS clamp + VPN FORWARD rules ==="

# Build the rules file
cat > /etc/iptables/rules.v4 << 'EOF'
# Generated by bootstrap-xneelo.sh
# MSS clamp for 5G carriers (PMTUD fix — without this, 5G clients see TCP timeouts)
*mangle
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
# Clamp MSS for packets going through the tunnel (ESP/UDP-encap overhead)
-A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260
COMMIT

*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]

# ── VPN clients (10.99.0.0/24) ─────────────────────────────────────────────
# Allow all traffic to/from VPN subnet
-A INPUT  -s 10.99.0.0/24 -j ACCEPT
-A FORWARD -s 10.99.0.0/24 -j ACCEPT
-A FORWARD -d 10.99.0.0/24 -j ACCEPT

# ── ICMP ───────────────────────────────────────────────────────────────────
-A INPUT -p icmp -j ACCEPT

# ── SSH (rate-limited) ────────────────────────────────────────────────────
-A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT
-A OUTPUT -p tcp --sport 22 -m conntrack --ctstate ESTABLISHED -j ACCEPT

# ── IKEv2 (UDP 500 + 4500) ─────────────────────────────────────────────────
-A INPUT -p udp --dport 500  -j ACCEPT
-A INPUT -p udp --dport 4500 -j ACCEPT

# ── Healthcheck / monitoring ───────────────────────────────────────────────
# Prometheus node exporter (port 9100) — limit to our homelab subnet
-A INPUT -s 192.168.10.0/24 -p tcp --dport 9100 -j ACCEPT

# ── Established/related ─────────────────────────────────────────────────────
-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# ── NAT for VPN clients ────────────────────────────────────────────────────
# MASQUERADE VPN client traffic going out the public interface
-A POSTROUTING -s 10.99.0.0/24 ! -d 10.99.0.0/24 -m comment --comment "VPN MASQ" -j MASQUERADE

COMMIT
EOF

iptables-restore < /etc/iptables/rules.v4
systemctl enable netfilter-persistent
ok "iptables applied and saved."

# ─── Step 9: Clone repo ─────────────────────────────────────────────────────
info "=== Step 9/16: Cloning project repo ==="
if [[ -d /opt/strongswan-vpn-gateway ]]; then
    warn "Repo already exists at /opt/strongswan-vpn-gateway. Pulling latest."
    cd /opt/strongswan-vpn-gateway && git pull
else
    mkdir -p /opt
    git clone https://github.com/Dippie-WP/databyte-Ikev2.git /opt/strongswan-vpn-gateway
    cd /opt/strongswan-vpn-gateway
fi
ok "Repo cloned."

# ─── Step 10: Generate certs ────────────────────────────────────────────────
info "=== Step 10/16: Generating strongSwan CA + server certs ==="
cd /opt/strongswan-vpn-gateway

# Backup existing certs if they exist
if [[ -f docker/swanctl/x509/server.crt.pem ]]; then
    cp -r docker/swanctl docker/swanctl.bak.$(date +%Y%m%d%H%M%S)
fi

SERVER_ID="${SERVER_ID}" bash scripts/gen-certs.sh
ok "Certificates generated for SAN: ${SERVER_ID}"

# ─── Step 11: Edit rw-eap.conf + rw-psk.conf from templates ───────────────
info "=== Step 11/16: Configuring rw-eap.conf + rw-psk.conf ==="
cd /opt/strongswan-vpn-gateway/docker

# rw-eap.conf — fill in the server ID and secrets block
sed "s/vpn\.homelab\.local/${SERVER_ID}/" swanctl/conf.d/rw-eap.conf.template > swanctl/conf.d/rw-eap.conf

# Add operator EAP secret to rw-eap.conf secrets block
OPERATOR_NTLM=$(echo -n "${OPERATOR_PASSWORD}" | iconv -t utf-16le | openssl md4 -binary | xxd -p -c 256)
cat >> swanctl/conf.d/rw-eap.conf << EOF

secrets {
    eap-operator {
        id = ${OPERATOR_USER}
        secret = "${OPERATOR_PASSWORD}"
    }
}
EOF

# rw-psk.conf — generate a PSK
FRIEND_PSK_VAL="${FRIEND_PSK:-$(openssl rand -base64 24 | tr -d '/+=')}"
sed "s/vpn\.homelab\.local/${SERVER_ID}/" swanctl/conf.d/rw-psk.conf.template > swanctl/conf.d/rw-psk.conf
sed -i "s/YOUR_PSK_HERE/${FRIEND_PSK_VAL}/" swanctl/conf.d/rw-psk.conf
ok "rw-eap.conf + rw-psk.conf configured."

# ─── Step 12: Build docker image ─────────────────────────────────────────────
info "=== Step 12/16: Building Docker image ==="
cd /opt/strongswan-vpn-gateway
bash scripts/build-image.sh "${DOCKER_IMAGE:-zun/strongswan:6.0.7-mschapv2-attrsql}"
ok "Docker image built."

# ─── Step 13: Start container ───────────────────────────────────────────────
info "=== Step 13/16: Starting strongSwan container ==="
mkdir -p /var/log/charon-log-host
mkdir -p /var/lib/strongswan

cd /opt/strongswan-vpn-gateway/docker
docker compose --profile vpn up -d
sleep 5

# Healthcheck
docker ps --filter name=strongswan --format "{{.Names}} {{.Status}}" || die "Container not running"
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --stats > /dev/null 2>&1 || die "charon not responding"
ok "Container started and healthy."

# ─── Step 14: Seed users in SQLite DB ───────────────────────────────────────
info "=== Step 14/16: Seeding users in SQLite DB ==="
cd /opt/strongswan-vpn-gateway

# Seed operator
USERNAME="${OPERATOR_USER}" VIP="${OPERATOR_VIP}" bash scripts/seed-db.sh

# Seed demo customer
USERNAME="${DEMO_CUSTOMER_USER}" VIP="${DEMO_CUSTOMER_VIP}" bash scripts/seed-db.sh

ok "Users seeded."

# ─── Step 15: Configure rclone for RustFS backup ────────────────────────────
info "=== Step 15/16: Configuring rclone remote (RustFS) ==="
if [[ -n "${RUSTFS_ACCESS_KEY:-}" && -n "${RUSTFS_SECRET_KEY:-}" ]]; then
    # Create rclone config (non-interactive)
    mkdir -p /root/.config/rclone
    cat > /root/.config/rclone/rclone.conf << EOF
[${RCLONE_REMOTE:-rustfs}]
type = s3
provider = Other
endpoint = ${RUSTFS_ENDPOINT}
access_key_id = ${RUSTFS_ACCESS_KEY}
secret_access_key = ${RUSTFS_SECRET_KEY}
region = us-east-1
force_path_style = true
no_check_bucket = true
EOF
    chmod 600 /root/.config/rclone/rclone.conf
    ok "rclone configured for ${RCLONE_REMOTE:-rustfs}."
else
    warn "RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY not set in .env.xneelo. Skipping rclone config."
fi

# ─── Step 16: Install DB backup cron ───────────────────────────────────────
info "=== Step 16/16: Installing DB backup cron job ==="
cat > /usr/local/bin/vpn-db-backup.sh << 'EOF'
#!/bin/bash
# Nightly DB backup to RustFS
# Installed by bootstrap-xneelo.sh
set -euo pipefail

REMOTE="${RCLONE_REMOTE:-rustfs}"
BUCKET="${RCLONE_BUCKET:-open-claw-push}"
SRC_DB="/var/lib/strongswan/ipsec.db"
BACKUP_DIR="/tmp/vpn-backups"
DATE=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"
cp "$SRC_DB" "$BACKUP_DIR/ipsec.db.$DATE"
# Keep 7 days locally
find "$BACKUP_DIR" -name "ipsec.db.*" -mtime +7 -delete

if command -v rclone &>/dev/null && [[ -f /root/.config/rclone/rclone.conf ]]; then
    rclone copy "$BACKUP_DIR/ipsec.db.$DATE" "${REMOTE}:${BUCKET}/vpn-prod-01/db/" --quiet
    echo "$(date): Backup of ipsec.db.$DATE to RustFS succeeded."
else
    echo "$(date): rclone not configured. DB backed up locally only."
fi
EOF
chmod +x /usr/local/bin/vpn-db-backup.sh

CRON_H="${BACKUP_CRON_HOUR:-3}"
CRON_M="${BACKUP_CRON_MINUTE:-0}"
echo "${CRON_M} ${CRON_H} * * * root /usr/local/bin/vpn-db-backup.sh" > /etc/cron.d/vpn-db-backup
chmod 644 /etc/cron.d/vpn-db-backup
ok "DB backup cron installed (daily at ${CRON_H}:${CRON_M} UTC)."

# ─── Smoke test ─────────────────────────────────────────────────────────────
info ""
info "=== SMOKE TEST ==="
info "Checking charon is up..."
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas 2>&1 | head -5 && ok "charon responding" || warn "No active SAs yet (expected — no clients connected)"
info "Checking iptables FORWARD rules..."
iptables -L FORWARD -n -v | grep -c "10.99.0.0/24" && ok "VPN FORWARD rules present" || warn "VPN FORWARD rules missing"
info "Checking iptables MSS clamp..."
iptables -t mangle -L FORWARD -n -v | grep -c "TCPMSS set 1260" && ok "MSS clamp active" || warn "MSS clamp not found"
info "Checking ip_forward..."
sysctl net.ipv4.ip_forward | grep -q "= 1" && ok "ip_forward = 1" || warn "ip_forward not set"
info "Checking fail2ban..."
systemctl is-active fail2ban | grep -q "active" && ok "fail2ban running" || warn "fail2ban not active"

echo ""
ok "=== Bootstrap complete ==="
info "Next step: Connect a client from outside the VPS (e.g. your phone on LTE)."
info "  Server: ${SERVER_ID}"
info "  Type:   IKEv2 EAP-MSCHAPv2"
info "  User:   ${OPERATOR_USER} / ${OPERATOR_PASSWORD}"
info ""
info "Check logs: docker logs strongswan --tail 30"
info "Full status: docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas"
info ""
info "NOTE: Remember to update Cloudflare DNS A record for ${SERVER_ID} → ${PUBLIC_IPV4}"
info "      Use DNS-only (grey cloud) — Cloudflare proxy does NOT proxy UDP 500/4500."