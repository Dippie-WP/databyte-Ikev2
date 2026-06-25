#!/usr/bin/env bash
# smoke.sh — VPN portal + strongSwan API-layer smoke test (Layer 4 of testing plan)
#
# Runs every 6h on LXC 903 (lab). Catches drift that L1 pytest can't simulate
# (live portal auth, live charon creds, quota endpoint with real DB).
#
# Checks
# ------
# 1. portal-health   GET <PORTAL_URL>/api/customers (or /api/health if added later)
#                    returns 200
# 2. customer-login  POST /api/portal/login for each test customer in creds file
#                    returns 200 + sets cookie
# 3. customer-me     GET /api/portal/me with cookie returns 200 + correct customer
# 4. customer-quota  GET /api/customers with cookie returns own row with quota fields
# 5. swanctl-creds   docker exec strongswan swanctl --list-creds | grep -c "^eap-"
#                    count >= EXPECTED_CRED_COUNT
#
# Usage
# -----
#   # One-shot (CI / on-demand)
#   ./scripts/smoke.sh
#
#   # Override config
#   PORTAL_URL=http://192.168.10.98:8080 \
#   CREDS_FILE=/root/.demo_vpn_creds \
#   ./scripts/smoke.sh
#
#   # Cron / systemd timer
#   /usr/local/bin/smoke.sh   # wrapped by systemd, output → journald
#
# Exit codes
# ----------
# 0   All checks passed
# 1   One or more checks failed
# 2   Setup error (portal unreachable, creds file missing, etc.)

set -uo pipefail

# --- Config (override via env) -------------------------------------------
PORTAL_URL="${PORTAL_URL:-http://192.168.10.98:8080}"
CREDS_FILE="${CREDS_FILE:-/root/.demo_vpn_creds}"
SWANCTL_CONTAINER="${SWANCTL_CONTAINER:-strongswan}"
EXPECTED_CRED_COUNT="${EXPECTED_CRED_COUNT:-3}"   # demo-phone + demo-laptop + operator (or test customer)
TIMEOUT="${TIMEOUT:-10}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"     # leave empty to skip telegram alerts
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# --- Counters -------------------------------------------------------------
PASS=0
FAIL=0
FAILURES=()

log()   { echo "[$(date -u +%H:%M:%SZ)] $*"; }
err()   { echo "[$(date -u +%H:%M:%SZ)] ❌ $*" >&2; }

pass()  { PASS=$((PASS+1)); log "  ✅ $1"; }
fail()  { FAIL=$((FAIL+1)); FAILURES+=("$1"); err "FAIL: $1${2:+ — $2}"; }

# --- Telegram alert on failure --------------------------------------------
telegram_alert() {
    local msg="$1"
    if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
        log "Telegram not configured — skipping alert"
        return 0
    fi
    curl -sS --max-time 10 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=${msg}" \
        -d "parse_mode=Markdown" >/dev/null 2>&1 \
        || err "Telegram alert failed (non-fatal)"
}

# --- Parse creds file (seed_demo_creds.sh format) --------------------------
# Format:
#   demo-phone
#     Username: demo-phone
#     Password: abc123
#
#   demo-laptop
#     Username: demo-laptop
#     Password: xyz789
#
# Output: populates CUST_NAME and CUST_PASSWORD arrays
declare -a CUST_NAME=()
declare -a CUST_PASSWORD=()
parse_creds_file() {
    if [[ ! -r "$CREDS_FILE" ]]; then
        err "Creds file not readable: $CREDS_FILE"
        return 1
    fi
    local current_name=""
    while IFS= read -r line; do
        # Skip comments + blank lines
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        # Indented "Password: X" line — associate with current name
        if [[ "$line" =~ ^[[:space:]]*Password:[[:space:]]+(.+)$ ]]; then
            if [[ -n "$current_name" ]]; then
                CUST_PASSWORD+=("${BASH_REMATCH[1]}")
                current_name=""  # consumed; next non-indented line is next customer
            fi
            continue
        fi
        # Indented "Username: X" line — skip (redundant with the name line)
        [[ "$line" =~ ^[[:space:]]*Username: ]] && continue
        # Non-indented line = new customer name
        if [[ "$line" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            current_name="$line"
            CUST_NAME+=("$current_name")
        fi
    done < "$CREDS_FILE"

    if [[ ${#CUST_NAME[@]} -eq 0 ]]; then
        err "No customers parsed from $CREDS_FILE"
        return 1
    fi
    if [[ ${#CUST_NAME[@]} -ne ${#CUST_PASSWORD[@]} ]]; then
        err "Mismatched count: ${#CUST_NAME[@]} names vs ${#CUST_PASSWORD[@]} passwords"
        return 1
    fi
    log "Parsed ${#CUST_NAME[@]} test customer(s) from $CREDS_FILE"
}

# --- Check 1: Portal health -----------------------------------------------
check_portal_health() {
    local code
    code=$(curl -sS --max-time "$TIMEOUT" -o /dev/null -w "%{http_code}" \
                "${PORTAL_URL}/api/customers" 2>/dev/null) || code="000"
    if [[ "$code" == "200" || "$code" == "401" ]]; then
        # 401 = portal up but auth required (expected for unauth request)
        pass "portal-health ($code)"
    else
        fail "portal-health" "got HTTP $code"
    fi
}

# --- Check 2-4: Customer login + /me + /api/customers ---------------------
check_customer_flow() {
    local i name pw cookie_file login_code me_code cust_code
    for i in "${!CUST_NAME[@]}"; do
        name="${CUST_NAME[$i]}"
        pw="${CUST_PASSWORD[$i]}"
        cookie_file=$(mktemp)

        # 2. Login
        login_code=$(curl -sS --max-time "$TIMEOUT" -o /dev/null -w "%{http_code}" \
            -c "$cookie_file" \
            -X POST "${PORTAL_URL}/api/portal/login" \
            -H "Content-Type: application/json" \
            -d "{\"identity\":\"${name}\",\"password\":\"${pw}\"}" 2>/dev/null) || login_code="000"

        if [[ "$login_code" != "200" ]]; then
            fail "customer-login[$name]" "HTTP $login_code"
            rm -f "$cookie_file"
            continue
        fi
        pass "customer-login[$name]"

        # 3. /api/portal/me
        me_code=$(curl -sS --max-time "$TIMEOUT" -o /dev/null -w "%{http_code}" \
            -b "$cookie_file" "${PORTAL_URL}/api/portal/me" 2>/dev/null) || me_code="000"
        if [[ "$me_code" != "200" ]]; then
            fail "customer-me[$name]" "HTTP $me_code"
            rm -f "$cookie_file"
            continue
        fi
        pass "customer-me[$name]"

        # 4. /api/customers — verify customer can see own row with quota fields
        cust_body=$(curl -sS --max-time "$TIMEOUT" -b "$cookie_file" \
            "${PORTAL_URL}/api/customers" 2>/dev/null)
        cust_code=$(curl -sS --max-time "$TIMEOUT" -o /dev/null -w "%{http_code}" \
            -b "$cookie_file" "${PORTAL_URL}/api/customers" 2>/dev/null) || cust_code="000"
        if [[ "$cust_code" != "200" ]]; then
            fail "customer-quota[$name]" "HTTP $cust_code"
            rm -f "$cookie_file"
            continue
        fi
        # Body must mention customer's name AND quota-related field.
        # JSON may or may not have whitespace around colons — match loosely.
        if ! grep -qE "\"name\":[[:space:]]*\"${name}\"" <<<"$cust_body"; then
            fail "customer-quota[$name]" "name '${name}' not in /api/customers response"
            rm -f "$cookie_file"
            continue
        fi
        if ! grep -qE '"data_limit_bytes"|"data_used_bytes"' <<<"$cust_body"; then
            fail "customer-quota[$name]" "no quota fields in response"
            rm -f "$cookie_file"
            continue
        fi
        pass "customer-quota[$name]"

        rm -f "$cookie_file"
    done
}

# --- Check 5: swanctl --list-creds -----------------------------------------
check_swanctl_creds() {
    local count
    if ! command -v docker >/dev/null 2>&1; then
        fail "swanctl-creds" "docker not in PATH"
        return
    fi
    count=$(docker exec "$SWANCTL_CONTAINER" \
                swanctl --uri=tcp://127.0.0.1:4502 --list-creds 2>/dev/null \
                | grep -c "^eap-" || true)
    if [[ "$count" -ge "$EXPECTED_CRED_COUNT" ]]; then
        pass "swanctl-creds ($count loaded, expected ≥ $EXPECTED_CRED_COUNT)"
    else
        fail "swanctl-creds" "got $count, expected ≥ $EXPECTED_CRED_COUNT"
    fi
}

# --- Main ------------------------------------------------------------------
main() {
    log "smoke.sh starting — portal=$PORTAL_URL creds=$CREDS_FILE"

    if ! parse_creds_file; then
        err "Setup error: cannot parse creds file"
        exit 2
    fi

    check_portal_health
    check_customer_flow
    check_swanctl_creds

    echo "================================================================"
    log "Result: $PASS passed, $FAIL failed"
    if [[ $FAIL -gt 0 ]]; then
        err "FAILURES:"
        for f in "${FAILURES[@]}"; do
            err "  - $f"
        done
        telegram_alert "🚨 *VPN smoke FAILED* (LXC 903)

Passed: $PASS
Failed: $FAIL
Failures:
$(printf '  - %s\n' "${FAILURES[@]}")

Portal: $PORTAL_URL
Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        exit 1
    fi
    echo "✅ All checks passed"
    exit 0
}

main