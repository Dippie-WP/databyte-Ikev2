#!/usr/bin/env bash
# ============================================================================
# test-customer-facing-commands.sh
# ============================================================================
# Structural anti-lie test: verifies that EVERY command/instruction the
# portal gives to a customer ACTUALLY WORKS when the customer runs it.
#
# Why this exists:
#   Misha has shipped "Windows installer one-liner" twice and claimed it
#   worked both times. Both times the customer-facing command failed:
#     v1.5.0 → 'iex (irm URL?slug=X&token=***)' broke in PS 5.1 (AmpersandNotAllowed)
#     v1.6.0 → customer URL returned 404 (.ps1 not deployed to nginx path)
#     v1.6.1 → base64 padding math wrong for token lengths divisible by 4
#   In every case I verified server/UI/test outputs and never ran the
#   actual command the customer would paste.
#
# What this script does:
#   1. POSTs to installer-token endpoint to get the 3-line powershell_cmd
#   2. Parses out the curl URL and the -t token
#   3. curl GET the .ps1 — verifies HTTP 200, content size, Decode-PackedToken present
#   4. Runs the .ps1 in pwsh with the -t token (Linux pwsh, not Windows PS5.1,
#      but tests: parser accepts the command, token decodes, /api/installer
#      endpoint reachable)
#   5. Verifies script prints "Fetching customer credentials via installer token"
#      and NOT "No installer token - using hardcoded test creds (lab mode)"
#   6. Verifies powershell_cmd does not contain "url?" with "&" outside the
#      canonical 3-line block (PS 5.1 safety check)
#   7. (Future) iOS mobileconfig, Android APK link, Linux nmcli line, macOS .mobileconfig
#
# Usage:
#   tools/test-customer-facing-commands.sh                # smoke test (live portal)
#   tools/test-customer-facing-commands.sh --strict       # fail on any warning
#   PORTAL_BASE=https://other:port tools/test-customer-facing-commands.sh
#
# Exit codes:
#   0 = all customer-facing commands work
#   1 = at least one customer-facing command would fail
#   2 = setup error (can't login, can't reach portal, etc.)
#
# Zun's framing: "did you even dry run and audit your work look at this shit"
# This script is the audit. Run it before claiming shipped.
# ============================================================================

set -uo pipefail

# --- defaults ---
PORTAL_BASE="${PORTAL_BASE:-https://vpn-portal.databyte.co.za}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-At7S7rKtJqzSbOBqJymWv19iY_ImOfKs}"
STRICT=0
PWSH_BIN="${PWSH_BIN:-pwsh}"
CURL_BIN="${CURL_BIN:-curl}"

# --- args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --strict) STRICT=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1"; exit 2 ;;
    esac
done

PASS=0
FAIL=0
WARN=0

ok()   { echo -e "  \033[32mPASS\033[0m  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  \033[31mFAIL\033[0m  $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "  \033[33mWARN\033[0m  $1"; WARN=$((WARN+1)); }

# --- preflight ---
echo "=== test-customer-facing-commands.sh ==="
echo "portal: $PORTAL_BASE"
echo "strict: $STRICT"
echo ""

# 0. portal reachable
if ! $CURL_BIN -skf "${PORTAL_BASE}/api/health" >/dev/null 2>&1; then
    fail "portal /api/health unreachable"
    exit 2
fi
ok "portal reachable"

# 1. login
echo ""
echo "=== Test 1: Windows PowerShell installer (3-line block) ==="
LOGIN_RESP=$($CURL_BIN -sk -i -X POST "${PORTAL_BASE}/api/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" 2>&1)
COOKIE=$(echo "$LOGIN_RESP" | grep -i "set-cookie" | head -1 | sed 's/Set-Cookie: //I' | cut -d';' -f1)
if [[ -z "$COOKIE" ]]; then
    fail "login failed (no cookie)"
    exit 2
fi
ok "logged in as admin"

# 2. create fresh customer
CUST_NAME="smoketest$(date +%s)"
CUST_RESP=$($CURL_BIN -sk -X POST "${PORTAL_BASE}/api/customers" \
    -H "Content-Type: application/json" \
    -b "$COOKIE" \
    -d "{\"name\":\"$CUST_NAME\",\"display_name\":\"SmokeTest\",\"tier_name\":\"tier_5gb\",\"device_name\":\"laptop\",\"device_type\":\"Windows\"}" 2>&1)
CUST_ID=$(echo "$CUST_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('customer',{}).get('id',''))" 2>/dev/null)
if [[ -z "$CUST_ID" ]]; then
    fail "customer creation failed: $CUST_RESP"
    exit 2
fi
ok "created customer $CUST_NAME (id=$CUST_ID)"

# 3. installer-token
TOKEN_RESP=$($CURL_BIN -sk -X POST "${PORTAL_BASE}/api/customers/$CUST_ID/installer-token" \
    -H "Content-Type: application/json" \
    -b "$COOKIE" 2>&1)
PS_CMD=$(echo "$TOKEN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('powershell_cmd',''))" 2>/dev/null)
if [[ -z "$PS_CMD" ]]; then
    fail "installer-token response missing powershell_cmd"
    exit 2
fi
ok "got installer-token response"

# 4. PS5.1 safety: no raw '&' in the URL portion of the curl line
#    (the & on the & $env:TEMP line is fine — call operator on its own line)
CURL_URL=$(echo "$PS_CMD" | grep -oP "curl\.exe -o \\\$env:TEMP\\\\setup\.ps1 '[^']+'" | head -1 | sed -n "s/.*'\([^']*\)'.*/\1/p")
if [[ -n "$CURL_URL" ]]; then
    if echo "$CURL_URL" | grep -q '&'; then
        fail "PS5.1 safety: curl URL contains '&' which breaks PS 5.1 parser: $CURL_URL"
    else
        ok "PS5.1 safety: curl URL has no '&'"
    fi
else
    warn "could not extract curl URL from powershell_cmd"
fi

# 5. THE BUG WE CAUGHT TWICE: curl the URL, verify HTTP 200
echo ""
echo "  checking: $CURL_URL"
HTTP_CODE=$($CURL_BIN -sk -o /tmp/test-ps1-content.ps1 -w "%{http_code}" "$CURL_URL" 2>&1)
if [[ "$HTTP_CODE" == "200" ]]; then
    ok "URL serves HTTP 200 ($(wc -c < /tmp/test-ps1-content.ps1) bytes)"
else
    fail "URL returns HTTP $HTTP_CODE (this is the bug we caught yesterday — .ps1 not deployed to nginx path)"
fi

# 6. verify script has Decode-PackedToken (the canonical fix)
if grep -q "Decode-PackedToken" /tmp/test-ps1-content.ps1 2>/dev/null; then
    ok "downloaded script has Decode-PackedToken helper"
else
    fail "downloaded script is missing Decode-PackedToken helper (wrong/old version served)"
fi

# 7. verify padding math is correct (the OTHER bug we caught)
if grep -q "(4 - \$Packed.Length % 4) % 4" /tmp/test-ps1-content.ps1 2>/dev/null; then
    ok "downloaded script has correct base64 padding math"
else
    fail "downloaded script has WRONG base64 padding math (causes 400 Bad Request for tokens divisible by 4)"
fi

# 8. THE REAL TEST: extract -t token, run script in pwsh
PACKED_TOKEN=$(echo "$PS_CMD" | grep -oP 'setup\.ps1 -t \K[A-Za-z0-9_\-]+' | head -1)
if [[ -z "$PACKED_TOKEN" ]]; then
    fail "could not extract -t token from powershell_cmd"
else
    ok "extracted -t token (length=${#PACKED_TOKEN})"
    
    # Run the actual customer command in pwsh
    if command -v "$PWSH_BIN" >/dev/null 2>&1; then
        PSH_OUTPUT=$($PWSH_BIN -NoProfile -File /tmp/test-ps1-content.ps1 -t "$PACKED_TOKEN" 2>&1 | head -50)
        
        # Check for the success indicator
        if echo "$PSH_OUTPUT" | grep -q "Fetching customer credentials via installer token"; then
            ok "script reaches 'Fetching customer credentials' step (token decoded successfully)"
        else
            fail "script never reaches credential fetch — token decode or API call broken"
            echo "    first 10 lines of output:"
            echo "$PSH_OUTPUT" | head -10 | sed 's/^/      /'
        fi
        
        # Check for the "lab mode" fallback (means token was lost)
        if echo "$PSH_OUTPUT" | grep -q "No installer token - using hardcoded test creds"; then
            fail "script fell back to LAB MODE — -t token was not recognized"
        else
            ok "script did NOT fall back to lab mode"
        fi
        
        # Check for parser error
        if echo "$PSH_OUTPUT" | grep -q "AmpersandNotAllowed"; then
            fail "PS parser error: AmpersandNotAllowed — & in URL still present"
        else
            ok "no PS parser error"
        fi
        
        # Check for the customer name in the success output
        if echo "$PSH_OUTPUT" | grep -q "$CUST_NAME"; then
            ok "script fetched creds for the correct customer ($CUST_NAME)"
        else
            warn "could not verify customer name in output (may be irrelevant for this step)"
        fi
    else
        warn "pwsh not available — skipping script execution test"
    fi
fi

# --- summary ---
echo ""
echo "=== SUMMARY ==="
echo -e "  \033[32mPASS: $PASS\033[0m"
echo -e "  \033[31mFAIL: $FAIL\033[0m"
echo -e "  \033[33mWARN: $WARN\033[0m"
echo ""

if [[ $FAIL -gt 0 ]]; then
    echo -e "\033[31m=== CUSTOMER-FACING FLOW IS BROKEN — DO NOT CLAIM SHIPPED ===\033[0m"
    exit 1
elif [[ $STRICT -eq 1 && $WARN -gt 0 ]]; then
    echo -e "\033[33m=== STRICT MODE — WARNINGS COUNTED AS FAILURES ===\033[0m"
    exit 1
else
    echo -e "\033[32m=== ALL CUSTOMER-FACING COMMANDS WORK ===\033[0m"
    exit 0
fi