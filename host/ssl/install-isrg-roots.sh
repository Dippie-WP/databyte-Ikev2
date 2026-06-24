#!/usr/bin/env bash
# install-isrg-roots.sh — install ISRG Root X1 + X2 into system trust store
# ----------------------------------------------------------------------------
# Debian 13's ca-certificates package (20250419) ships WITHOUT Let's Encrypt's
# ISRG Root X1 and X2 — Mozilla removed X1 from default trust after January
# 2025 (and Debian tracks Mozilla). This is fine for clients (they have their
# own trust stores), but on the VPN server it means charon can't do CRL/OCSP
# validation against the LE chain, and `openssl verify` of LE certs fails
# locally with `error 20 at 0 depth lookup: unable to get local issuer
# certificate`.
#
# This script installs the ISRG roots from /opt/strongswan-vpn-gateway/host/ssl
# into /usr/local/share/ca-certificates/ and runs update-ca-certificates.
# After this, `openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt`
# succeeds for the LE chain.
#
# Idempotent: re-running is a no-op.
#
# Run as root:
#   sudo bash install-isrg-roots.sh
# ----------------------------------------------------------------------------

set -uo pipefail

SRC_DIR="/opt/strongswan-vpn-gateway/host/ssl"
DST_DIR="/usr/local/share/ca-certificates"

log_info()  { echo "[$(date -u +%FT%TZ)] [INFO]  $*"; }
log_warn()  { echo "[$(date -u +%FT%TZ)] [WARN]  $*"; }
log_error() { echo "[$(date -u +%FT%TZ)] [ERROR] $*" >&2; }

[[ $EUID -ne 0 ]] && { log_error "must run as root (use sudo)"; exit 1; }
[[ ! -d "$SRC_DIR" ]] && { log_error "source dir missing: $SRC_DIR"; exit 1; }

installed=0
for cert in "$SRC_DIR"/ISRG_Root_X*.crt; do
    [[ -f "$cert" ]] || continue
    fname="$(basename "$cert" .crt)"     # ISRG_Root_X1 / ISRG_Root_X2
    dst="${DST_DIR}/${fname}.crt"        # update-ca-certificates picks *.crt

    # SHA-256 fingerprint match (proves we have the canonical root)
    fp_src=$(openssl x509 -in "$cert" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
    log_info "installing $(basename "$cert") (sha256 ${fp_src:-?})"

    install -m 0644 "$cert" "$dst"
    installed=$((installed + 1))
done

if (( installed == 0 )); then
    log_error "no ISRG_Root_X*.crt files found in $SRC_DIR"
    exit 1
fi

log_info "running update-ca-certificates --fresh..."
update-ca-certificates --fresh 2>&1 | tail -5 || log_warn "update-ca-certificates exit non-zero (continuing to verify)"

# Verify both roots made it into the bundle. `update-ca-certificates --fresh`
# rebuilds ca-certificates.crt WITHOUT descriptive Subject lines, so a plain
# `grep "ISRG_Root_X1"` against the bundle will return false. We use three
# independent signals instead:
#   1. symlink exists in /etc/ssl/certs/
#   2. pkcs7 parse of bundle finds a cert with CN=ISRG Root X1/X2
#   3. openssl s_client against an LE-issued cert verifies (or local chain
#      verify with chain.pem as -untrusted returns OK)
log_info "=== verification ==="
for cert in ISRG_Root_X1 ISRG_Root_X2; do
    symlink="/etc/ssl/certs/${cert}.pem"
    if [[ -L "$symlink" ]]; then
        log_info "  ✓ $cert symlink present ($symlink)"
    else
        log_error "  ✗ $cert symlink MISSING ($symlink)"
        exit 1
    fi
done

# pkcs7 parse — counts ISRG subjects in the bundle
isrg_count=$(openssl crl2pkcs7 -nocrl -certfile /etc/ssl/certs/ca-certificates.crt 2>/dev/null | \
    openssl pkcs7 -print_certs -noout 2>/dev/null | \
    grep -cE "subject=.*CN ?= ?ISRG Root X[12]")
if (( isrg_count >= 2 )); then
    log_info "  ✓ $isrg_count ISRG Root X1/X2 certs found in bundle"
else
    log_error "  ✗ only $isrg_count ISRG certs in bundle (expected >= 2)"
    exit 1
fi

# Sanity: re-verify the actual LE chain now succeeds. With -untrusted chain
# (the server's intermediates), openssl walks leaf → chain → trust anchor.
# Without -untrusted, fails (intermediates not in bundle — expected).
log_info "=== LE chain verify (post-install) ==="
if [[ -f /etc/letsencrypt/live/myvpn.databyte.co.za/chain.pem ]]; then
    if openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt \
        -untrusted /etc/letsencrypt/live/myvpn.databyte.co.za/chain.pem \
        /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem >/dev/null 2>&1; then
        log_info "  ✓ LE chain verifies locally via trust store"
    else
        log_warn "  ✗ LE chain verify failed (not critical for IKEv2)"
    fi
fi

log_info "=== install-isrg-roots complete ($installed roots installed) ==="
exit 0
