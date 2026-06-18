#!/usr/bin/env bash
# gen-certs.sh — Generate self-signed CA + server cert for strongSwan EAP
#
# Creates:
#   - docker/swanctl/x509ca/strongswan-ca.crt.pem    (CA cert, 10y)
#   - docker/swanctl/private/strongswan-ca-key.pem   (CA key, mode 600)
#   - docker/swanctl/x509/server.crt.pem            (server cert, 1y, ECDSA P-256)
#   - docker/swanctl/private/server-key.pem          (server key, mode 600)
#
# Output: a CA bundle (strongswan-ca.crt.pem) for client install.
#
# Usage:  cd strongswan-vpn-gateway
#         bash scripts/gen-certs.sh
#         # (or with custom server ID)
#         SERVER_ID=vpn.example.com bash scripts/gen-certs.sh
#
# Why ECDSA P-256 (not RSA 4096):
#   - Smaller certs (~1.3KB vs 2KB), faster handshake
#   - Equivalent security in practice for VPN use
#   - All modern clients support it (Android strongSwan, iOS 13+)
#   - Tested 2026-06-17

set -euo pipefail

SERVER_ID="${SERVER_ID:-vpn.homelab.local}"
CERT_DIR="docker/swanctl"

echo "=== Generating strongSwan CA + server cert for ${SERVER_ID} ==="

mkdir -p "${CERT_DIR}/x509" "${CERT_DIR}/x509ca" "${CERT_DIR}/private"

# Step 1: Generate CA key (RSA 4096, 10y)
echo "[1/4] Generating CA key (RSA 4096)..."
openssl genrsa -out "${CERT_DIR}/private/strongswan-ca-key.pem" 4096 2>/dev/null
chmod 600 "${CERT_DIR}/private/strongswan-ca-key.pem"

# Step 2: Generate self-signed CA cert (10y)
echo "[2/4] Generating self-signed CA cert (10y)..."
openssl req -x509 -new -key "${CERT_DIR}/private/strongswan-ca-key.pem" \
    -out "${CERT_DIR}/x509ca/strongswan-ca.crt.pem" \
    -days 3650 \
    -subj "/CN=strongSwan CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

# Step 3: Generate server key (ECDSA P-256)
echo "[3/4] Generating server key (ECDSA P-256)..."
openssl ecparam -name prime256v1 -genkey -noout \
    -out "${CERT_DIR}/private/server-key.pem" 2>/dev/null
chmod 600 "${CERT_DIR}/private/server-key.pem"

# Step 4: Generate server cert (1y, signed by CA)
echo "[4/4] Generating server cert (1y, signed by CA)..."
openssl req -new \
    -key "${CERT_DIR}/private/server-key.pem" \
    -out /tmp/server.csr \
    -subj "/CN=${SERVER_ID}" 2>/dev/null

openssl x509 -req \
    -in /tmp/server.csr \
    -CA "${CERT_DIR}/x509ca/strongswan-ca.crt.pem" \
    -CAkey "${CERT_DIR}/private/strongswan-ca-key.pem" \
    -CAcreateserial \
    -out "${CERT_DIR}/x509/server.crt.pem" \
    -days 365 \
    -sha256 \
    -extfile <(cat <<EOF
subjectAltName = DNS:${SERVER_ID}
extendedKeyUsage = serverAuth
keyUsage = critical, digitalSignature, keyEncipherment
EOF
) 2>/dev/null

rm -f /tmp/server.csr "${CERT_DIR}/x509ca/strongswan-ca.srl"

echo ""
echo "=== Done ==="
echo "CA cert:      ${CERT_DIR}/x509ca/strongswan-ca.crt.pem"
echo "Server cert:  ${CERT_DIR}/x509/server.crt.pem"
echo ""
echo "=== Fingerprint of CA cert (install this on clients) ==="
openssl x509 -in "${CERT_DIR}/x509ca/strongswan-ca.crt.pem" -noout -fingerprint -sha256
echo ""
echo "=== NEXT STEP ==="
echo "1. Update docker/swanctl/swanctl.conf and conf.d/rw-*.conf.template"
echo "   to use SERVER_ID = ${SERVER_ID}"
echo "2. Copy ${CERT_DIR}/x509ca/strongswan-ca.crt.pem to clients"
echo "3. Build + run: bash scripts/build-image.sh && cd docker && docker compose --profile vpn up -d"
