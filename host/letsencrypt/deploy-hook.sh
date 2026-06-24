#!/usr/bin/env bash
# deploy-hook.sh — Let's Encrypt deploy hook for strongSwan IKEv2 server
# ----------------------------------------------------------------------------
# Runs after a successful certbot renewal on vpn-prod-01.
#
# Why this script exists
# ----------------------
# Let's Encrypt's new "Y" certificate hierarchy (rolled out 2025-11-24) places
# 3-4 certs in fullchain.pem: leaf → intermediate (YE2) → root-ye → root-x2
# (cross-signed by X1). strongSwan charon loads only ONE certificate per
# file in /etc/swanctl/x509ca/ — see GitHub issue strongswan/strongswan#3072.
# To avoid breakage on every renewal, we split fullchain.pem into individual
# files (one cert each) before swanctl --load-creds.
#
# What it does
# ------------
#   1. Preflight: verify certbot env, source paths, docker, container up
#   2. Wipe stale LE-sourced x509ca files (keeps strongswan-ca.crt.pem)
#   3. Split /etc/letsencrypt/live/$RENEWED_LINEAGE/fullchain.pem into N files
#   4. Install leaf  → x509/server.crt.pem (legacy) AND x509/server.pem (new)
#      Install chain → x509ca/le-NN-<short-CN>.pem (NN = order, one cert each)
#      Install key   → private/server-key.pem (mode 0600)
#   5. Reload charon via swanctl --uri=tcp://127.0.0.1:4502 --load-creds
#      (no container restart — preserves existing IKE SAs)
#   6. Verify new cert is loaded (swanctl --list-certs | grep myvpn)
#
# Certbot environment (auto-set by certbot deploy-hook contract):
#   $RENEWED_DOMAINS    — space-separated list of renewed domains
#   $RENEWED_LINEAGE    — dir containing cert.pem, chain.pem, fullchain.pem,
#                         privkey.pem (e.g. /etc/letsencrypt/live/myvpn...)
#
# Install
# -------
#   sudo install -m 0755 deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/
#
# Test
# ----
#   sudo certbot renew --dry-run
#   sudo RENEWED_LINEAGE=/etc/letsencrypt/live/myvpn.databyte.co.za \
#        RENEWED_DOMAINS=myvpn.databyte.co.za \
#        /etc/letsencrypt/renewal-hooks/deploy/deploy-hook.sh
#
# Rollback
# --------
# If the hook fails after writing new certs but before charon reload, charon
# keeps using the OLD cert loaded into memory. To manually restore:
#   git -C /opt/strongswan-vpn-gateway checkout docker/swanctl/x509/server.crt.pem
#   docker restart strongswan   # forces fresh load
#
# Exit codes
# ----------
#   0 = success
#   1 = preflight failure or any step failed
# ----------------------------------------------------------------------------

set -uo pipefail

# ---- paths ----
SWANCTL_DIR="/opt/strongswan-vpn-gateway/docker/swanctl"
X509_DIR="${SWANCTL_DIR}/x509"
X509CA_DIR="${SWANCTL_DIR}/x509ca"
PRIVATE_DIR="${SWANCTL_DIR}/private"

LE_LIVE="${RENEWED_LINEAGE:?RENEWED_LINEAGE must be set by certbot}"
LE_FULLCHAIN="${LE_LIVE}/fullchain.pem"
LE_CERT="${LE_LIVE}/cert.pem"
LE_KEY="${LE_LIVE}/privkey.pem"

CONTAINER_NAME="strongswan"
SWANCTL_URI="tcp://127.0.0.1:4502"   # matches strongswan.conf vici socket

# ---- logging (matches existing scripts) ----
log_info()  { echo "[$(date -u +%FT%TZ)] [INFO]  $*"; }
log_warn()  { echo "[$(date -u +%FT%TZ)] [WARN]  $*"; }
log_error() { echo "[$(date -u +%FT%TZ)] [ERROR] $*" >&2; }

log_info "=== strongswan LE deploy-hook start ==="
log_info "RENEWED_DOMAINS=${RENEWED_DOMAINS:-<unset>}"
log_info "RENEWED_LINEAGE=${LE_LIVE}"

# ---- 1. preflight ----
for f in "$LE_FULLCHAIN" "$LE_CERT" "$LE_KEY" "$X509_DIR" "$X509CA_DIR" "$PRIVATE_DIR"; do
    if [[ ! -e "$f" ]]; then
        log_error "preflight failed: missing $f"
        exit 1
    fi
done
if ! command -v docker >/dev/null 2>&1; then
    log_error "docker not on PATH"
    exit 1
fi
if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    log_error "container $CONTAINER_NAME not running"
    exit 1
fi

# ---- 2. wipe stale LE-sourced x509ca files ----
# Files we own start with "le-". Anything else (e.g. strongswan-ca.crt.pem)
# is preserved — useful for rollback to self-signed CA if needed.
log_info "removing old LE-sourced x509ca files (le-*.pem)..."
removed=0
# nullglob ensures the glob expands to empty if no matches
shopt -s nullglob
for f in "$X509CA_DIR"/le-*.pem; do
    rm -f -- "$f"
    log_info "  removed: $(basename "$f")"
    removed=$((removed + 1))
done
shopt -u nullglob
log_info "wiped $removed old LE cert files from x509ca/"

# ---- 3. split fullchain.pem into individual PEM files ----
log_info "splitting fullchain.pem into individual PEM blocks..."
CERT_DIR=$(mktemp -d -t le-cert.XXXXXX)
trap 'rm -rf "$CERT_DIR"' EXIT

awk -v dir="$CERT_DIR" '
    /-----BEGIN CERTIFICATE-----/ {
        n++; out = sprintf("%s/cert-%02d.pem", dir, n)
        # truncate (first match starts the new file)
    }
    { print >> out }
    /-----END CERTIFICATE-----/ { close(out) }
' "$LE_FULLCHAIN"

mapfile -t cert_files < <(find "$CERT_DIR" -name "cert-*.pem" | sort)

if [[ ${#cert_files[@]} -eq 0 ]]; then
    log_error "no cert blocks extracted from fullchain.pem"
    exit 1
fi
log_info "extracted ${#cert_files[@]} cert blocks"

# ---- 4a. install leaf cert (BOTH filenames for migration) ----
leaf="${cert_files[0]}"
# Sanity: cert.pem must match first block of fullchain.pem
if ! diff -q "$LE_CERT" "$leaf" >/dev/null 2>&1; then
    log_warn "cert.pem differs from first block of fullchain.pem (proceeding anyway)"
fi
install -m 0644 "$leaf" "$X509_DIR/server.crt.pem"   # legacy name (current swanctl.conf ref)
install -m 0644 "$leaf" "$X509_DIR/server.pem"        # new canonical name (post-LE)
log_info "installed leaf cert → server.crt.pem AND server.pem"

# ---- 4b. install chain (excluding leaf) as x509ca/le-NN-<cn>.pem ----
idx=1
for cert in "${cert_files[@]:1}"; do
    # Extract a short CN label from the subject
    cn=$(openssl x509 -in "$cert" -noout -subject 2>/dev/null \
            | sed -n 's/.*CN *= *\([^,/]*\).*/\1/p' \
            | tr ' ' '-' | tr -cd 'A-Za-z0-9.-' | head -c 40)
    [[ -z "$cn" ]] && cn="unknown"

    idx_padded=$(printf "%02d" "$idx")
    target="$X509CA_DIR/le-${idx_padded}-${cn}.pem"
    install -m 0644 "$cert" "$target"
    log_info "installed chain[$idx]: $cn → $(basename "$target")"
    idx=$((idx + 1))
done

# ---- 4c. install private key (mode 0600) ----
install -m 0600 "$LE_KEY" "$PRIVATE_DIR/server-key.pem"
log_info "installed key → $PRIVATE_DIR/server-key.pem (mode 0600)"

# ---- 5. reload charon (no container restart) ----
log_info "reloading strongSwan creds in container $CONTAINER_NAME..."
if ! docker exec "$CONTAINER_NAME" swanctl --uri="$SWANCTL_URI" --load-creds; then
    log_error "swanctl --load-creds FAILED — charon still uses previous in-memory creds"
    exit 1
fi
log_info "swanctl --load-creds succeeded"

# ---- 6. verify chain loaded ----
log_info "verifying loaded certs..."
loaded=$(docker exec "$CONTAINER_NAME" swanctl --uri="$SWANCTL_URI" --list-certs --type x509 2>&1 || true)
# head -60 (not sed + head): the section header is followed by a blank line,
# then cert entries; an anchored sed range would stop at the blank and show
# nothing. head shows the full picture.
echo "$loaded" | head -60 || true

if ! echo "$loaded" | grep -q "myvpn.databyte.co.za"; then
    log_error "new LE cert NOT loaded — list-certs does not contain myvpn.databyte.co.za"
    log_error "full dump:"
    echo "$loaded" >&2
    exit 1
fi

# Count LE intermediates loaded (excluding self-signed CA which charon won't show here)
intermediates_loaded=$(echo "$loaded" | grep -cE "Let's Encrypt|ISRG Root (YE|X2)" || true)
log_info "LE intermediates loaded: $intermediates_loaded"

log_info "=== strongswan LE deploy-hook complete ==="
exit 0
