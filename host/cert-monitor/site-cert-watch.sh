#!/usr/bin/env bash
# site-cert-watch.sh
# ----------------------------------------------------------------------------
# Per-site certificate watcher + auto-renewal activator.
#
# Why this exists (TKT-041, per-host cert model):
#   A site's LE certificate is not only its HTTPS cert - it IS that site's
#   IKEv2 server identity, because the certbot deploy hook copies the same
#   leaf + key into strongswan's x509 directory and reloads charon. So on this
#   estate a site certificate that expires, or a renewal that does not reach
#   strongswan, takes that site's customer VPN down.
#
# Three jobs, per site:
#   1. WATCH     - expiry of this site's LE cert, and of this site's strongswan CA.
#   2. MATCH     - verify the certificate strongswan is actually SERVING is
#                  byte-identical to this lineage's cert on disk. Catches a deploy
#                  hook that failed to install the renewed cert, and catches a
#                  renewal on a DIFFERENT lineage overwriting the identity.
#   3. ACTIVATE  - make sure auto-renewal is genuinely armed and working:
#                  certbot.timer enabled+active, a renewal config present for
#                  this lineage, and - when the cert is inside the renewal
#                  window - an actual targeted renewal, then re-verification.
#
# Per-site parameterisation: SITE_FQDN (plus SITE_LINEAGE if the host names
# its lineage differently). No hardcoded site names - one script, one value per
# host, via /etc/default/site-cert-watch.
#
# Exit codes:
#   0 = all good
#   1 = WARN  - needs attention, not yet fatal
#   2 = CRIT  - expiry imminent, renewal not armed, or the served identity does
#               not match this lineage
#
# Output goes to journald. It does not send external alerts by itself - see
# README.md for the Prometheus textfile / Telegram wiring.
# ----------------------------------------------------------------------------

set -uo pipefail

# ---- per-site configuration ----------------------------------------------
# The canonical public name of THIS site, e.g. za-cpt-01-vpn.databyte.co.za.
# On the live JHB host that is myvpn.databyte.co.za.
: "${SITE_FQDN:?SITE_FQDN must be set (the canonical public name of this site)}"

# The certbot lineage name. Defaults to the FQDN, matching the prod convention
# where the lineage directory is named after the FQDN
# (/etc/letsencrypt/live/<fqdn>/). Override with SITE_LINEAGE if a host uses a
# different lineage name.
: "${SITE_LINEAGE:=${SITE_FQDN}}"

# ---- thresholds ------------------------------------------------------------
WARN_DAYS=${WARN_DAYS:-30}              # LE cert: warn inside this window
CRIT_DAYS=${CRIT_DAYS:-14}              # LE cert: critical inside this window
CA_WARN_DAYS=${CA_WARN_DAYS:-365}       # strongswan CA (10y): warn a year out
CA_CRIT_DAYS=${CA_CRIT_DAYS:-180}
RENEW_WINDOW_DAYS=${RENEW_WINDOW_DAYS:-30}   # certbot's own renewal window

# ---- switches --------------------------------------------------------------
# CHECK_ONLY=1 -> report only. Never enables a timer, never renews, never
# touches anything. This is how the script is tested, and how it should be run
# on a host you only want to observe.
CHECK_ONLY=${CHECK_ONLY:-0}
# ENABLE_TIMER=0 -> never enable certbot.timer (report its state only).
ENABLE_TIMER=${ENABLE_TIMER:-1}

# ---- paths -----------------------------------------------------------------
CERTBOT_BIN=${CERTBOT_BIN:-/usr/bin/certbot}
LETSENCRYPT_DIR=${LETSENCRYPT_DIR:-/etc/letsencrypt}
LIVE_DIR="${LETSENCRYPT_DIR}/live/${SITE_LINEAGE}"
RENEW_CONF="${LETSENCRYPT_DIR}/renewal/${SITE_LINEAGE}.conf"
CERT_PEM="${LIVE_DIR}/cert.pem"
SWANCTL_DIR=${SWANCTL_DIR:-/opt/strongswan-vpn-gateway/docker/swanctl}
# swanctl loads `certs = server.pem` (see conf.d/rw-eap.conf), so server.pem is
# the operative file. server.crt.pem is the legacy copy the hook also writes.
SERVED_CERT_PEM="${SWANCTL_DIR}/x509/server.pem"
SERVED_CERT_ALT_PEM="${SWANCTL_DIR}/x509/server.crt.pem"
SWANCTL_CA_PEM="${SWANCTL_DIR}/x509ca/strongswan-ca.crt.pem"
CERTBOT_TIMER=${CERTBOT_TIMER:-certbot.timer}

# ---- logging ---------------------------------------------------------------
log_info()  { echo "[$(date -u +%FT%TZ)] [INFO]  $*"; }
log_warn()  { echo "[$(date -u +%FT%TZ)] [WARN]  $*"; }
log_crit()  { echo "[$(date -u +%FT%TZ)] [CRIT]  $*"; }

EXIT_CODE=0
bump() { if (( $1 > EXIT_CODE )); then EXIT_CODE=$1; fi; }

# ---- helpers ---------------------------------------------------------------
sha_of() {
    [[ -f "$1" ]] || return 1
    sha256sum "$1" 2>/dev/null | cut -d' ' -f1
}

days_left_of() {
    local p="$1" end_epoch now
    end_epoch=$(openssl x509 -in "$p" -noout -enddate 2>/dev/null \
                | sed 's/^notAfter=//' | xargs -I{} date -d "{}" +%s 2>/dev/null)
    [[ -z "$end_epoch" || "$end_epoch" == "0" ]] && return 1
    now=$(date +%s)
    echo $(( (end_epoch - now) / 86400 ))
}

# ============================================================================
log_info "=== site-cert-watch start: site=${SITE_FQDN} lineage=${SITE_LINEAGE} check_only=${CHECK_ONLY} ==="

# ---- 1. this site's LE certificate ----------------------------------------
if [[ ! -f "$CERT_PEM" ]]; then
    if [[ -f "$RENEW_CONF" ]]; then
        log_crit "LE certificate missing at ${CERT_PEM} but a renewal config exists - the cert was deleted or the lineage is broken"
        bump 2
    else
        log_warn "no LE certificate for this site yet (${CERT_PEM}) and no renewal config - site not issued yet"
        bump 1
    fi
else
    DL=$(days_left_of "$CERT_PEM" || true)
    if [[ -z "$DL" ]]; then
        log_crit "cannot parse expiry from ${CERT_PEM}"
        bump 2
    elif (( DL < 0 )); then
        log_crit "LE cert EXPIRED $((-DL)) days ago - ${CERT_PEM}"
        bump 2
    elif (( DL < CRIT_DAYS )); then
        log_crit "LE cert ${DL} days remaining (critical) - ${CERT_PEM}"
        bump 2
    elif (( DL < WARN_DAYS )); then
        log_warn "LE cert ${DL} days remaining - ${CERT_PEM}"
        bump 1
    else
        log_info "LE cert ${DL} days remaining - ${CERT_PEM}"
    fi
fi

# ---- 2. this site's strongswan CA -----------------------------------------
if [[ -f "$SWANCTL_CA_PEM" ]]; then
    CA_DL=$(days_left_of "$SWANCTL_CA_PEM" || true)
    if [[ -n "$CA_DL" ]]; then
        if (( CA_DL < CA_CRIT_DAYS )); then
            log_crit "strongswan CA ${CA_DL} days remaining - ${SWANCTL_CA_PEM}"
            bump 2
        elif (( CA_DL < CA_WARN_DAYS )); then
            log_warn "strongswan CA ${CA_DL} days remaining - ${SWANCTL_CA_PEM}"
            bump 1
        else
            log_info "strongswan CA ${CA_DL} days remaining - ${SWANCTL_CA_PEM}"
        fi
    else
        log_warn "cannot parse expiry from ${SWANCTL_CA_PEM}"
        bump 1
    fi
else
    log_warn "strongswan CA not found at ${SWANCTL_CA_PEM}"
    bump 1
fi

# ---- 3. is auto-renewal armed? --------------------------------------------
if [[ ! -x "$CERTBOT_BIN" ]]; then
    log_crit "certbot not found at ${CERTBOT_BIN} - auto-renewal cannot run on this host"
    bump 2
elif [[ ! -f "$RENEW_CONF" ]]; then
    log_crit "no renewal config at ${RENEW_CONF} - certbot will NOT renew this lineage"
    bump 2
else
    log_info "renewal config present: ${RENEW_CONF}"

    TIMER_ENABLED=$(systemctl is-enabled "$CERTBOT_TIMER" 2>/dev/null || echo unknown)
    TIMER_ACTIVE=$(systemctl is-active "$CERTBOT_TIMER" 2>/dev/null || echo inactive)
    if [[ "$TIMER_ENABLED" != "enabled" ]]; then
        if (( CHECK_ONLY == 1 )) || [[ "$ENABLE_TIMER" != "1" ]]; then
            log_warn "${CERTBOT_TIMER} is '${TIMER_ENABLED}' - would enable it now (auto-renewal not armed)"
            bump 1
        else
            log_warn "${CERTBOT_TIMER} is '${TIMER_ENABLED}' - enabling it now (auto-renewal was not armed)"
            if systemctl enable --now "$CERTBOT_TIMER" 2>&1 | sed 's/^/    /'; then
                log_info "${CERTBOT_TIMER} enabled and started"
            else
                log_crit "failed to enable ${CERTBOT_TIMER}"
                bump 2
            fi
        fi
    elif [[ "$TIMER_ACTIVE" != "active" ]]; then
        if (( CHECK_ONLY == 1 )); then
            log_warn "${CERTBOT_TIMER} is enabled but '${TIMER_ACTIVE}' - would start it now"
            bump 1
        else
            log_warn "${CERTBOT_TIMER} is enabled but '${TIMER_ACTIVE}' - starting it now"
            systemctl start "$CERTBOT_TIMER" 2>&1 | sed 's/^/    /'
            log_info "${CERTBOT_TIMER} started"
        fi
    else
        log_info "${CERTBOT_TIMER} enabled and active"
    fi
fi

# ---- 4. inside the renewal window - attempt a targeted renewal ------------
NEED_RENEW=0
if [[ -f "$CERT_PEM" ]]; then
    DL=$(days_left_of "$CERT_PEM" || true)
    if [[ -n "$DL" ]] && (( DL <= RENEW_WINDOW_DAYS )); then
        NEED_RENEW=1
    fi
fi

if (( NEED_RENEW == 1 )) && [[ -x "$CERTBOT_BIN" && -f "$RENEW_CONF" ]]; then
    if (( CHECK_ONLY == 1 )); then
        log_warn "LE cert is inside the ${RENEW_WINDOW_DAYS}-day renewal window - would run a targeted renewal"
        bump 1
    else
        BEFORE_SHA=$(sha_of "$CERT_PEM" || echo "")
        BEFORE_MTIME=$(stat -c %Y "$CERT_PEM" 2>/dev/null || echo 0)
        log_info "LE cert is inside the ${RENEW_WINDOW_DAYS}-day renewal window - running a targeted renewal"
        if "$CERTBOT_BIN" renew --cert-name "$SITE_LINEAGE" \
                --non-interactive --no-random-sleep-on-renew 2>&1 | sed 's/^/    /'; then
            AFTER_SHA=$(sha_of "$CERT_PEM" || echo "")
            AFTER_MTIME=$(stat -c %Y "$CERT_PEM" 2>/dev/null || echo 0)
            if [[ "$AFTER_SHA" != "$BEFORE_SHA" || "$AFTER_MTIME" != "$BEFORE_MTIME" ]]; then
                log_info "renewal replaced the certificate on disk"
                DL=$(days_left_of "$CERT_PEM" || true)
                [[ -n "$DL" ]] && log_info "LE cert now ${DL} days remaining"
            else
                # certbot's own renew window is 30 days, so inside that band it can
                # legitimately answer "not yet due" and change nothing. That is only a
                # problem once we are inside the CRITICAL band, where the cert must
                # actually have been replaced.
                if (( DL <= CRIT_DAYS )); then
                    log_warn "renewal ran but the certificate on disk did not change, and it is inside the critical window"
                    bump 1
                else
                    log_info "renewal ran and certbot judged the cert not yet due (inside certbot's own renew window) - no change"
                fi
            fi
        else
            log_crit "targeted renewal FAILED for lineage ${SITE_LINEAGE}"
            bump 2
        fi
    fi
fi

# ---- 5. does strongswan serve THIS lineage's cert? ------------------------
if [[ -f "$CERT_PEM" && -f "$SERVED_CERT_PEM" ]]; then
    LINEAGE_SHA=$(sha_of "$CERT_PEM" || echo "")
    SERVED_SHA=$(sha_of "$SERVED_CERT_PEM" || echo "")
    if [[ -n "$LINEAGE_SHA" && "$LINEAGE_SHA" == "$SERVED_SHA" ]]; then
        log_info "served cert matches this lineage (sha ${SERVED_SHA:0:12})"
        ALT_SHA=$(sha_of "$SERVED_CERT_ALT_PEM" || echo "")
        if [[ -n "$ALT_SHA" && "$ALT_SHA" != "$LINEAGE_SHA" ]]; then
            log_warn "legacy x509/server.crt.pem does NOT match this lineage (the hook writes both copies)"
            bump 1
        fi
    else
        log_crit "SERVED cert does NOT match this lineage's cert on disk"
        log_crit "  on disk (${SITE_LINEAGE}): ${LINEAGE_SHA:-none}"
        log_crit "  served by strongswan:      ${SERVED_SHA:-none}"
        log_crit "  the deploy hook did not install this lineage's cert, or another lineage overwrote it"
        bump 2
    fi
else
    log_warn "cannot compare served cert (missing ${CERT_PEM} or ${SERVED_CERT_PEM})"
    bump 1
fi

log_info "=== site-cert-watch done: site=${SITE_FQDN} (exit ${EXIT_CODE}) ==="
exit $EXIT_CODE
