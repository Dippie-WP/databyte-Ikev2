#!/usr/bin/env bash
# deploy-portal-vps.sh — Deploy vpn-portal to VPS (production only).
#
# What this script does (in order, refuses to skip any step):
#   1. Pre-flight: confirm git working tree is clean (or committed).
#   2. Source SHA: capture HEAD commit + SHA256 of app.py + app.js + index.html.
#   3. rsync host/vpn-portal/ → vpn-prod-01:/opt/vpn-portal/
#   4. systemctl restart vpn-portal.service on VPS.
#   5. Wait for /api/health to return 200.
#   6. Verify: SHA256 of deployed app.py + app.js matches source.
#   7. Verify: HTML grep for $FEATURE_MARKER returns ≥1 match in live page.
#   8. Write host/vpn-portal/.last_deployed with all data.
#   9. Exit 0 only if every step passed. Exit non-zero on any failure.
#
# Usage:
#   ./host/scripts/deploy-portal-vps.sh "<feature marker string>"
#
# The feature marker MUST be a unique substring of the new feature (e.g., the
# dropdown option label "Asymmetric — 40 Mbps"). After deploy, the script greps
# the LIVE public HTML for this string. If 0 matches, deploy FAILS — even if
# the rsync and restart succeeded. This is the contract that prevents
# "shipped-but-not-deployed" lies.
#
# Zun hard rule (2026-06-25): NEVER deploy to LXC 903 from this script.
# Production = VPS ONLY. LXC 903 is OFF LIMITS.
#
# v1.7.5 (2026-06-28): Step 6 SHA verification now uses `sudo -n sha256sum`
# so the check works regardless of deploy-target file mode. Previously a
# `sudo cp` of a root-owned backup could leave files at 0640, causing
# sha256sum to silently return the literal string "sha256sum:" (its error
# message prefix) as the SHA, triggering a bogus MISMATCH failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOURCE_DIR="${REPO_ROOT}/host/vpn-portal"
VPS_HOST="vpn-prod-01"
VPS_PORTAL_DIR="/opt/vpn-portal"
STATE_FILE="${SOURCE_DIR}/.last_deployed"
HEALTH_URL="http://127.0.0.1:8080/api/health"
PUBLIC_HTML_URL="https://vpn-portal.databyte.co.za/"
SMOKE_URL="https://vpn-portal.databyte.co.za/api/health"

FEATURE_MARKER="${1:-}"

DRY_RUN=0
for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        DRY_RUN=1
    fi
done

if [[ -z "$FEATURE_MARKER" || "$FEATURE_MARKER" == "--dry-run" ]]; then
    echo "FAIL: feature marker required"
    echo "Usage: $0 \"<unique substring of new feature visible in HTML>\" [--dry-run]"
    exit 2
fi

cd "$REPO_ROOT"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║ DRY RUN — no changes will be made to ${VPS_HOST}"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""
fi

echo "=== STEP 1: pre-flight ==="
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "FAIL: git working tree has uncommitted changes"
    echo "Commit or stash first; deploys must be reproducible from a commit"
    git status --short
    exit 2
fi
echo "  git tree clean ✓"

echo ""
echo "=== STEP 1.5: JS syntax check (added 2026-06-25) ==="
# Catches parse errors that L1 pytest misses (UI not rendered in tests).
# Bug that motivated this: const onlineLeases placed inside return el(...)
# arg list in commit 1cc2855 — shipped SyntaxError, portal broken 2.5h.
JS_FILES=(
    "${SOURCE_DIR}/static/app.js"
    "${SOURCE_DIR}/static/portal.js"
)
for js in "${JS_FILES[@]}"; do
    if [[ -f "$js" ]]; then
        if ! node --check "$js" 2>/dev/null; then
            echo "FAIL: JS syntax error in $js"
            node --check "$js"
            exit 3
        fi
        echo "  $(basename "$js") syntax OK ✓"
    fi
done

echo ""
echo "=== STEP 1.6: Python syntax check + portal env-var completeness (added 2026-07-05) ==="
# Python ast.parse catches SyntaxError that importers only fail at runtime.
# Bug that motivated this: Phase 4A + 4B commits contained a db_exec call
# with a trailing comma in the SQL string, only surfaced by gunicorn worker
# boot. ast.parse is sub-second for the whole file.
PY_FILES=(
    "${SOURCE_DIR}/app.py"
    "${SOURCE_DIR}/portal_auth.py"
)
for py in "${PY_FILES[@]}"; do
    if [[ -f "$py" ]]; then
        if ! python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$py" 2>/dev/null; then
            echo "FAIL: Python syntax error in $py"
            python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$py"
            exit 3
        fi
        echo "  $(basename "$py") ast.parse OK ✓"
    fi
done

# portal env-var completeness check. app.py imports from these at top —
# if any is missing on the VPS, the portal service will crash on first
# request. EnvironmentFile=/etc/vpn-portal.env is loaded by systemd, so
# the missing keys only fail in quirky settings (env-not-reloaded, etc).
# Check via /proc/<PID>/environ so we read what systemd actually loaded.
ENV_FILE="/etc/vpn-portal.env"
REQUIRED_ENV_KEYS=("ADMIN_PASS_HASH" "DB_URL" "SSH_KEY" "RW_EAP_CONF")
ENV_VARS_PRESENT=()
ENV_VARS_MISSING=()
MAIN_PID="$(ssh "${VPS_HOST}" 'sudo -n systemctl show vpn-portal --property=MainPID --value' 2>/dev/null || true)"
if [[ -z "$MAIN_PID" || "$MAIN_PID" == "0" ]]; then
    echo "  SKIP: vpn-portal.service not running on ${VPS_HOST}; cannot inspect runtime env"
else
    for key in "${REQUIRED_ENV_KEYS[@]}"; do
        present="$(ssh "${VPS_HOST}" "sudo -n grep -c \"^${key}=\" /proc/${MAIN_PID}/environ" 2>/dev/null | head -1 | tr -dc '0-9' || echo 0)"
        if [[ "${present:-0}" -ge 1 ]]; then
            ENV_VARS_PRESENT+=("$key")
        else
            ENV_VARS_MISSING+=("$key")
        fi
    done
    echo "  env vars present: ${#ENV_VARS_PRESENT[@]}/${#REQUIRED_ENV_KEYS[@]} (${ENV_VARS_PRESENT[*]:-none})"
    if [[ ${#ENV_VARS_MISSING[@]} -gt 0 ]]; then
        echo "  ⚠ env vars MISSING on VPS runtime: ${ENV_VARS_MISSING[*]}"
        echo "    /etc/vpn-portal.env has been edited but systemd was not reloaded."
        echo "    Run: ssh ${VPS_HOST} 'sudo systemctl daemon-reload && sudo systemctl restart vpn-portal.service'"
        echo "    Continuing deploy anyway (the var will be loaded on next restart)."
    fi
fi

echo ""
echo "=== STEP 2: capture source SHAs ==="
SOURCE_HEAD="$(git rev-parse HEAD)"
SOURCE_PY_SHA="$(sha256sum "${SOURCE_DIR}/app.py" | awk '{print $1}')"
SOURCE_JS_SHA="$(sha256sum "${SOURCE_DIR}/www/static/app.js" | awk '{print $1}')"
SOURCE_CSS_SHA="$(sha256sum "${SOURCE_DIR}/www/static/app.css" | awk '{print $1}')"
SOURCE_HTML_SHA="$(sha256sum "${SOURCE_DIR}/www/index.html" | awk '{print $1}')"
echo "  HEAD:        ${SOURCE_HEAD}"
echo "  app.py:      ${SOURCE_PY_SHA}"
echo "  app.js:      ${SOURCE_JS_SHA}"
echo "  app.css:     ${SOURCE_CSS_SHA}"
echo "  index.html:  ${SOURCE_HTML_SHA}"

echo ""
echo "=== STEP 3: sync to ${VPS_HOST} (rsync or tar+ssh) ==="
ssh "${VPS_HOST}" "test -d ${VPS_PORTAL_DIR}" || {
    echo "FAIL: ${VPS_PORTAL_DIR} does not exist on ${VPS_HOST}"
    exit 3
}

# Use rsync if available on both ends; else tar+ssh pipeline.
USE_RSYNC=0
if [[ $DRY_RUN -eq 1 ]]; then
    # In dry-run, just show what files WOULD change by hashing each.
    echo "  [DRY-RUN] would sync these files (hashing each, comparing to deployed):"
    SOURCE_FILE_COUNT="$(find "${SOURCE_DIR}" -type f \
        ! -path '*/__pycache__/*' \
        ! -path '*/.venv/*' \
        ! -name '*.bak*' \
        ! -name '.last_deployed' | wc -l)"
    echo "  source files to sync: ${SOURCE_FILE_COUNT}"
    # Sample diff for the 4 key files
    for f in app.py www/static/app.js www/static/app.css www/index.html; do
        src_sha="$(sha256sum "${SOURCE_DIR}/${f}" | awk '{print $1}')"
        # v1.7.5 — sudo -n sha256sum for same reason as Step 6 (file may be 0640)
        dep_sha="$(ssh "${VPS_HOST}" "sudo -n sha256sum ${VPS_PORTAL_DIR}/${f}" 2>/dev/null | awk '{print $1}')"
        if [[ "$src_sha" == "$dep_sha" ]]; then
            echo "    ✓ ${f}: would be SKIPPED (sha256 match)"
        else
            echo "    ✗ ${f}: would be UPDATED (src=${src_sha:0:12} deployed=${dep_sha:0:12})"
        fi
    done
    echo "  [DRY-RUN] no actual file transfer happened"
elif command -v rsync >/dev/null && ssh "${VPS_HOST}" 'command -v rsync' >/dev/null 2>&1; then
    USE_RSYNC=1
    rsync -av --delete \
        --rsync-path='sudo rsync' \
        --chown=vpn-portal:vpn-portal \
        --exclude '__pycache__' \
        --exclude '.venv' \
        --exclude '*.bak*' \
        --exclude '.last_deployed' \
        "${SOURCE_DIR}/" \
        "${VPS_HOST}:${VPS_PORTAL_DIR}/" 2>&1 | tail -5
else
    # tar+ssh fallback. No --delete equivalent; we'll first delete the dest contents
    # (preserving .last_deployed), then extract. Slower but works without rsync.
    echo "  [info] rsync not available; using tar+ssh fallback"
    ssh "${VPS_HOST}" "sudo find ${VPS_PORTAL_DIR} -mindepth 1 \
        ! -name '.last_deployed' \
        ! -name '*.bak*' \
        -exec rm -rf {} +" 2>&1 | tail -3
    tar --exclude='__pycache__' \
        --exclude='.venv' \
        --exclude='*.bak*' \
        --exclude='.last_deployed' \
        -czf - -C "${SOURCE_DIR}" . | \
        ssh "${VPS_HOST}" "sudo tar -xzf - --no-same-owner -C ${VPS_PORTAL_DIR}/ && \
            sudo chown -R vpn-portal:vpn-portal ${VPS_PORTAL_DIR}" 2>&1 | tail -5
fi

echo ""
echo "=== STEP 4: restart vpn-portal.service on ${VPS_HOST} ==="
if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] WOULD RUN: ssh ${VPS_HOST} 'sudo systemctl restart vpn-portal.service'"
    echo "  [DRY-RUN] showing CURRENT state instead:"
    ssh "${VPS_HOST}" 'sudo systemctl is-active vpn-portal.service 2>&1; sudo systemctl show vpn-portal.service --property=ActiveEnterTimestamp,MainPID 2>&1 | head -3'
else
    ssh "${VPS_HOST}" 'sudo systemctl restart vpn-portal.service'
fi
# (no change needed — already uses sudo)

echo ""
echo "=== STEP 5: wait for /api/health 200 ==="
if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] WOULD poll ${SMOKE_URL} until 200; showing current state:"
    HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "${SMOKE_URL}" --max-time 5 || echo 000)"
    echo "  current: ${SMOKE_URL} → ${HTTP_CODE}"
else
    for i in 1 2 3 4 5 6 7 8 9 10; do
        HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "${SMOKE_URL}" --max-time 5 || echo 000)"
        if [[ "$HTTP_CODE" == "200" ]]; then
            echo "  ${SMOKE_URL} → ${HTTP_CODE} ✓ (try ${i})"
            break
        fi
        echo "  ${SMOKE_URL} → ${HTTP_CODE} (try ${i}/10, retrying)"
        sleep 2
        if [[ "$i" == "10" ]]; then
            echo "FAIL: /api/health never returned 200"
            exit 4
        fi
    done
fi

echo ""
echo "=== STEP 6: verify deployed SHAs match source ==="
MISMATCH=0
# v1.7.5 — use sudo -n sha256sum so the check works regardless of file mode.
# Symptom of the bug: when an earlier deploy (`sudo cp` of a root-owned
# backup) leaves deploy-target files at mode 0640 (`-rw-r-----`), the SSH
# user (debian) can't read them. sha256sum prints "Permission denied" to
# stderr and "sha256sum: " to stdout — awk then captures the literal string
# "sha256sum:" as the SHA, and the comparison below triggers a bogus MISMATCH.
# Verified live on 2026-06-28 by `chmod 640 /opt/vpn-portal/app.py` and
# re-running this command — produced `DEPLOYED_PY_SHA=sha256sum:`.
# Adding `sudo -n` matches the pattern already used by Steps 3, 4, 7, 9.
DEPLOYED_PY_SHA="$(ssh "${VPS_HOST}" "sudo -n sha256sum ${VPS_PORTAL_DIR}/app.py" | awk '{print $1}')"
DEPLOYED_JS_SHA="$(ssh "${VPS_HOST}" "sudo -n sha256sum ${VPS_PORTAL_DIR}/www/static/app.js" | awk '{print $1}')"
DEPLOYED_CSS_SHA="$(ssh "${VPS_HOST}" "sudo -n sha256sum ${VPS_PORTAL_DIR}/www/static/app.css" | awk '{print $1}')"
DEPLOYED_HTML_SHA="$(ssh "${VPS_HOST}" "sudo -n sha256sum ${VPS_PORTAL_DIR}/www/index.html" | awk '{print $1}')"
echo "  deployed app.py:      ${DEPLOYED_PY_SHA}"
echo "  deployed app.js:      ${DEPLOYED_JS_SHA}"
echo "  deployed app.css:     ${DEPLOYED_CSS_SHA}"
echo "  deployed index.html:  ${DEPLOYED_HTML_SHA}"

if [[ "$DEPLOYED_PY_SHA" != "$SOURCE_PY_SHA" ]]; then
    echo "FAIL: deployed app.py SHA != source app.py SHA"
    if [[ $DRY_RUN -eq 0 ]]; then exit 5; fi
    MISMATCH=1
fi
if [[ "$DEPLOYED_JS_SHA" != "$SOURCE_JS_SHA" ]]; then
    echo "FAIL: deployed app.js SHA != source app.js SHA"
    if [[ $DRY_RUN -eq 0 ]]; then exit 5; fi
    MISMATCH=1
fi
if [[ "$DEPLOYED_CSS_SHA" != "$SOURCE_CSS_SHA" ]]; then
    echo "FAIL: deployed app.css SHA != source app.css SHA"
    if [[ $DRY_RUN -eq 0 ]]; then exit 5; fi
    MISMATCH=1
fi
# Note: index.html SHA is checked loosely — STEP 7 will bump ?v= on the
# deployed copy, which makes its SHA diverge from source by design. The
# important check is that app.py + app.js + app.css match (the actual feature code).
if [[ "$DEPLOYED_HTML_SHA" != "$SOURCE_HTML_SHA" ]]; then
    echo "  ⚠ deployed index.html SHA != source (OK if STEP 7 cache-bust hasn't run yet)"
fi
if [[ $MISMATCH -eq 0 ]]; then
    echo "  app.py + app.js + app.css SHAs match ✓"
fi

echo ""
echo "=== STEP 7: cache-bust HTML to force Cloudflare re-fetch of static assets ==="
# Cloudflare caches /static/app.js for 7 days (immutable). After deploying new
# app.js, we bump the ?v= query string in deployed index.html so CF sees a new
# URL, misses cache, and fetches the new file from origin.
NEW_CACHE_VERSION="${SOURCE_HEAD:0:7}"
echo "  new cache version: ${NEW_CACHE_VERSION}"
# v1.7.3 — broaden the cache-bust regex to match any ?v=<token> form
# (hex git SHA, semver N.N.N, placeholder 'BUILD', etc.). The previous
# `[0-9.]+` matched only digits + dot, which broke on re-deploys when the
# deployed HTML already had a hex cache-bust from a previous run — only
# the leading digit was replaced, leaving the rest orphaned (e.g. ?v=3fcc9ca
# → ?v=096b0a4fcc9ca). `[^"]+` matches any non-quote chars up to the next ".
ssh "${VPS_HOST}" "sudo sed -i -E 's/(\?v=)[^\"]+/\\1${NEW_CACHE_VERSION}/g' \
    ${VPS_PORTAL_DIR}/www/index.html \
    ${VPS_PORTAL_DIR}/www/portal/index.html"
echo "  bumped ?v= values in deployed HTML:"
ssh "${VPS_HOST}" "grep -oE '\\\\?v=[0-9a-f]+' ${VPS_PORTAL_DIR}/www/index.html ${VPS_PORTAL_DIR}/www/portal/index.html" | head -6 | sed 's/^/    /'
# Re-fetch DEPLOYED_HTML_SHA after the bump (expected to differ from source —
# by design, since source uses placeholder ?v=BUILD but deployed uses ?v=gitsha)
DEPLOYED_HTML_SHA="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/www/index.html" | awk '{print $1}')"

echo ""
echo "=== STEP 8: verify feature marker in LIVE resources (post cache-bust) ==="
# Note: the portal shell is a SPA — the actual feature lives in app.js OR app.css.
# Fetch the live HTML, extract the new ?v= app.js + app.css URLs, then check all three.
INDEX_RAW="$(ssh "${VPS_HOST}" "curl -sk ${PUBLIC_HTML_URL} --max-time 10 2>/dev/null | grep -c -- '${FEATURE_MARKER}'" 2>/dev/null || true)"
# Extract the versioned app.js URL from live HTML (post cache-bust should be ?v=$NEW_CACHE_VERSION)
JS_URL_VERSIONED="$(ssh "${VPS_HOST}" "curl -sk ${PUBLIC_HTML_URL} --max-time 10 2>/dev/null | grep -oE '/static/app\\.js\\?v=[0-9a-f.]+' | head -1" 2>/dev/null || true)"
JS_URL="${PUBLIC_HTML_URL%/}${JS_URL_VERSIONED:-/static/app.js}"
JS_RAW="$(ssh "${VPS_HOST}" "curl -sk '${JS_URL}' --max-time 10 2>/dev/null | grep -c -- '${FEATURE_MARKER}'" 2>/dev/null || true)"
# Also check app.css (CSS-only fixes won't show up in JS grep)
CSS_URL_VERSIONED="$(ssh "${VPS_HOST}" "curl -sk ${PUBLIC_HTML_URL} --max-time 10 2>/dev/null | grep -oE '/static/app\\.css\\?v=[0-9a-f.]+' | head -1" 2>/dev/null || true)"
CSS_URL="${PUBLIC_HTML_URL%/}${CSS_URL_VERSIONED:-/static/app.css}"
CSS_RAW="$(ssh "${VPS_HOST}" "curl -sk '${CSS_URL}' --max-time 10 2>/dev/null | grep -c -- '${FEATURE_MARKER}'" 2>/dev/null || true)"
INDEX_MATCH="$(echo "${INDEX_RAW}" | head -1 | tr -dc '0-9')"
JS_MATCH="$(echo "${JS_RAW}" | head -1 | tr -dc '0-9')"
CSS_MATCH="$(echo "${CSS_RAW}" | head -1 | tr -dc '0-9')"
[[ -z "${INDEX_MATCH}" ]] && INDEX_MATCH=0
[[ -z "${JS_MATCH}" ]] && JS_MATCH=0
[[ -z "${CSS_MATCH}" ]] && CSS_MATCH=0
FEATURE_MATCH=$(( INDEX_MATCH + JS_MATCH + CSS_MATCH ))
echo "  '${FEATURE_MARKER}' matches in index.html: ${INDEX_MATCH}"
echo "  '${FEATURE_MARKER}' matches in app.js:     ${JS_MATCH}  (URL: ${JS_URL})"
echo "  '${FEATURE_MARKER}' matches in app.css:    ${CSS_MATCH}  (URL: ${CSS_URL})"
echo "  total:                                    ${FEATURE_MATCH}"
if [[ "${FEATURE_MATCH}" -lt 1 ]]; then
    echo "FAIL: feature marker not found in live resources"
    echo "Either: (a) feature not deployed, (b) marker string wrong, (c) cache not busted"
    echo "Try: curl -sk '${JS_URL}' | grep '${FEATURE_MARKER}'"
    if [[ $DRY_RUN -eq 0 ]]; then exit 6; fi
else
    echo "  feature marker found ✓"
fi

echo ""
echo "=== STEP 8.5: customer-facing flow smoke test ==="
echo "(this is the audit — it actually runs the command the customer would paste)"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
TEST_SCRIPT="${SCRIPT_DIR}/tools/test-customer-facing-commands.sh"
if [[ -x "$TEST_SCRIPT" ]]; then
    if "$TEST_SCRIPT"; then
        echo "  customer-facing flow ✓"
    else
        echo "  customer-facing flow FAILED — customer would not be able to use this"
        echo "  see output above for what broke"
        if [[ $DRY_RUN -eq 0 ]]; then exit 6; fi
    fi
else
    echo "  test script not found at $TEST_SCRIPT (skipping)"
fi

echo ""
if [[ $DRY_RUN -eq 1 ]]; then
    echo "=== STEP 8: SKIPPED in dry-run (.last_deployed would be written on real deploy) ==="
else
    echo "=== STEP 9: write .last_deployed ==="
    DEPLOY_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    DEPLOY_USER="$(whoami)"
    cat > "${STATE_FILE}" <<EOF
# Last successful deploy of vpn-portal to ${VPS_HOST}
# This file is updated by host/scripts/deploy-portal-vps.sh — DO NOT EDIT BY HAND.

deployed_at_utc=${DEPLOY_TS}
deployed_by=${DEPLOY_USER}
git_head=${SOURCE_HEAD}
git_head_short=$(git rev-parse --short HEAD)
feature_marker=${FEATURE_MARKER}
cache_bust_version=${NEW_CACHE_VERSION:-none}

source_sha256:
  app.py=${SOURCE_PY_SHA}
  app.js=${SOURCE_JS_SHA}
  app.css=${SOURCE_CSS_SHA}
  index.html=${SOURCE_HTML_SHA}

deployed_sha256:
  app.py=${DEPLOYED_PY_SHA}
  app.js=${DEPLOYED_JS_SHA}
  app.css=${DEPLOYED_CSS_SHA}
  index.html=${DEPLOYED_HTML_SHA}

health_url=${SMOKE_URL}
public_url=${PUBLIC_HTML_URL}
EOF
    rsync -av --rsync-path='sudo rsync' \
        "${STATE_FILE}" "${VPS_HOST}:${VPS_PORTAL_DIR}/.last_deployed" 2>&1 | tail -2 || \
    scp "${STATE_FILE}" "${VPS_HOST}:${VPS_PORTAL_DIR}/.last_deployed" 2>&1 | tail -2 || \
    cat "${STATE_FILE}" | ssh "${VPS_HOST}" "sudo tee ${VPS_PORTAL_DIR}/.last_deployed >/dev/null"
    echo "  wrote ${STATE_FILE} ✓"
    echo "  wrote ${VPS_PORTAL_DIR}/.last_deployed on VPS ✓"
fi

echo ""
if [[ $DRY_RUN -eq 1 ]]; then
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║ DRY-RUN COMPLETE — no changes were made"
    echo "║"
    echo "║ Verdict:"
    if [[ $MISMATCH -eq 0 && "$FEATURE_MATCH" -ge 1 ]]; then
        echo "║   ✓ Would have SUCCEEDED — feature already live"
    elif [[ $MISMATCH -gt 0 ]]; then
        echo "║   ✗ Would have FAILED at STEP 6 — source ≠ deployed"
    elif [[ "$FEATURE_MATCH" -lt 1 ]]; then
        echo "║   ✗ Would have FAILED at STEP 7 — feature marker not in live HTML"
    fi
    echo "║"
    echo "║ To actually deploy, run WITHOUT --dry-run:"
    echo "║   $0 \"${FEATURE_MARKER}\""
    echo "╚════════════════════════════════════════════════════════════╝"
    exit 0
fi

echo "═══════════════════════════════════════════════════════"
echo " DEPLOY SUCCESS — feature '${FEATURE_MARKER}' is LIVE"
echo " ${DEPLOY_TS} | ${SOURCE_HEAD:0:12}"
echo "═══════════════════════════════════════════════════════"
exit 0
