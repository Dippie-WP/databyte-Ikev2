#!/usr/bin/env bash
# cert-expiry-check.sh
# ----------------------------------------------------------------------------
# Monitor TLS certificate expiry for the VPN portal.
# Checks:
#   1. Cloudflare Origin CA cert at /etc/ssl/cloudflare/databyte.co.za.crt
#   2. StrongSwan CA cert at /opt/strongswan-vpn-gateway/docker/swanctl/x509ca/strongswan-ca.crt.pem
#
# Alerts at 90/60/30/14/7 days remaining via journald (WARN/CRITICAL).
# Exit codes:
#   0 = all certs have > 90 days remaining
#   1 = one or more certs within WARN window (< 90 days)
#   2 = one or more certs within CRITICAL window (< 30 days)
# ----------------------------------------------------------------------------

set -uo pipefail

# Thresholds can be overridden via env for testing
WARN_DAYS=${WARN_DAYS:-90}
CRIT_DAYS=${CRIT_DAYS:-30}

log_info()  { echo "[$(date -u +%FT%TZ)] [INFO] $*"; }
log_warn()  { echo "[$(date -u +%FT%TZ)] [WARN] $*"; }
log_crit()  { echo "[$(date -u +%FT%TZ)] [CRIT] $*"; }

EXIT_CODE=0

check_cert() {
    local cert_path="$1"
    local label="$2"

    if [[ ! -f "$cert_path" ]]; then
        log_warn "$label: cert file not found at $cert_path"
        EXIT_CODE=1
        return
    fi

    local expiry_epoch
    expiry_epoch=$(openssl x509 -in "$cert_path" -noout -enddate 2>/dev/null \
                   | sed 's/^notAfter=//' \
                   | xargs -I{} date -d "{}" +%s 2>/dev/null)
    if [[ -z "$expiry_epoch" || "$expiry_epoch" == "0" ]]; then
        log_warn "$label: failed to parse expiry from $cert_path"
        EXIT_CODE=1
        return
    fi

    local now_epoch
    now_epoch=$(date +%s)
    local days_left=$(( (expiry_epoch - now_epoch) / 86400 ))
    local expiry_human
    expiry_human=$(date -d "@$expiry_epoch" -u +%Y-%m-%d)

    if (( days_left < 0 )); then
        log_crit "$label: EXPIRED $((-days_left)) days ago (was $expiry_human) — $cert_path"
        EXIT_CODE=2
    elif (( days_left < CRIT_DAYS )); then
        log_crit "$label: $days_left days remaining (expires $expiry_human) — $cert_path"
        EXIT_CODE=2
    elif (( days_left < WARN_DAYS )); then
        log_warn "$label: $days_left days remaining (expires $expiry_human) — $cert_path"
        EXIT_CODE=1
    else
        log_info "$label: $days_left days remaining (expires $expiry_human) — $cert_path"
    fi
}

log_info "=== cert-expiry-check start ==="
check_cert /etc/ssl/cloudflare/databyte.co.za.crt "Origin CA (Cloudflare)"
check_cert /opt/strongswan-vpn-gateway/docker/swanctl/x509ca/strongswan-ca.crt.pem "StrongSwan CA"
log_info "=== cert-expiry-check done (exit $EXIT_CODE) ==="
exit $EXIT_CODE
