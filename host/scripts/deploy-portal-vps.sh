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

if [[ -z "$FEATURE_MARKER" ]]; then
    echo "FAIL: feature marker required"
    echo "Usage: $0 \"<unique substring of new feature visible in HTML>\""
    exit 2
fi

cd "$REPO_ROOT"

echo "=== STEP 1: pre-flight ==="
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "FAIL: git working tree has uncommitted changes"
    echo "Commit or stash first; deploys must be reproducible from a commit"
    git status --short
    exit 2
fi
echo "  git tree clean ✓"

echo ""
echo "=== STEP 2: capture source SHAs ==="
SOURCE_HEAD="$(git rev-parse HEAD)"
SOURCE_PY_SHA="$(sha256sum "${SOURCE_DIR}/app.py" | awk '{print $1}')"
SOURCE_JS_SHA="$(sha256sum "${SOURCE_DIR}/www/static/app.js" | awk '{print $1}')"
SOURCE_HTML_SHA="$(sha256sum "${SOURCE_DIR}/www/index.html" | awk '{print $1}')"
echo "  HEAD:        ${SOURCE_HEAD}"
echo "  app.py:      ${SOURCE_PY_SHA}"
echo "  app.js:      ${SOURCE_JS_SHA}"
echo "  index.html:  ${SOURCE_HTML_SHA}"

echo ""
echo "=== STEP 3: rsync to ${VPS_HOST} ==="
ssh "${VPS_HOST}" "test -d ${VPS_PORTAL_DIR}" || {
    echo "FAIL: ${VPS_PORTAL_DIR} does not exist on ${VPS_HOST}"
    exit 3
}
rsync -av --delete \
    --exclude '__pycache__' \
    --exclude '.venv' \
    --exclude '*.bak*' \
    --exclude '.last_deployed' \
    "${SOURCE_DIR}/" \
    "${VPS_HOST}:${VPS_PORTAL_DIR}/" 2>&1 | tail -5

echo ""
echo "=== STEP 4: restart vpn-portal.service on ${VPS_HOST} ==="
ssh "${VPS_HOST}" 'sudo systemctl restart vpn-portal.service'

echo ""
echo "=== STEP 5: wait for /api/health 200 ==="
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

echo ""
echo "=== STEP 6: verify deployed SHAs match source ==="
DEPLOYED_PY_SHA="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/app.py" | awk '{print $1}')"
DEPLOYED_JS_SHA="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/www/static/app.js" | awk '{print $1}')"
DEPLOYED_HTML_SHA="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/www/index.html" | awk '{print $1}')"
echo "  deployed app.py:      ${DEPLOYED_PY_SHA}"
echo "  deployed app.js:      ${DEPLOYED_JS_SHA}"
echo "  deployed index.html:  ${DEPLOYED_HTML_SHA}"

if [[ "$DEPLOYED_PY_SHA" != "$SOURCE_PY_SHA" ]]; then
    echo "FAIL: deployed app.py SHA != source app.py SHA"
    exit 5
fi
if [[ "$DEPLOYED_JS_SHA" != "$SOURCE_JS_SHA" ]]; then
    echo "FAIL: deployed app.js SHA != source app.js SHA"
    exit 5
fi
if [[ "$DEPLOYED_HTML_SHA" != "$SOURCE_HTML_SHA" ]]; then
    echo "FAIL: deployed index.html SHA != source index.html SHA"
    exit 5
fi
echo "  all SHAs match ✓"

echo ""
echo "=== STEP 7: verify feature marker in LIVE HTML ==="
# Fetch the public HTML, look for the feature marker.
MATCH_COUNT="$(ssh "${VPS_HOST}" "curl -sk ${PUBLIC_HTML_URL} --max-time 10 | grep -c '${FEATURE_MARKER}'" || echo 0)"
echo "  '${FEATURE_MARKER}' matches in live HTML: ${MATCH_COUNT}"
if [[ "$MATCH_COUNT" -lt 1 ]]; then
    echo "FAIL: feature marker not found in live HTML"
    echo "Either: (a) feature not deployed, (b) marker string wrong, (c) page cached"
    echo "Try: curl -sk '${PUBLIC_HTML_URL}' | head -50"
    exit 6
fi
echo "  feature marker found ✓"

echo ""
echo "=== STEP 8: write .last_deployed ==="
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

source_sha256:
  app.py=${SOURCE_PY_SHA}
  app.js=${SOURCE_JS_SHA}
  index.html=${SOURCE_HTML_SHA}

deployed_sha256:
  app.py=${DEPLOYED_PY_SHA}
  app.js=${DEPLOYED_JS_SHA}
  index.html=${DEPLOYED_HTML_SHA}

health_url=${SMOKE_URL}
public_url=${PUBLIC_HTML_URL}
EOF
# Also rsync the state file to VPS so we can check from there too
rsync "${STATE_FILE}" "${VPS_HOST}:${VPS_PORTAL_DIR}/.last_deployed" >/dev/null
echo "  wrote ${STATE_FILE} ✓"
echo "  wrote ${VPS_PORTAL_DIR}/.last_deployed on VPS ✓"

echo ""
echo "═══════════════════════════════════════════════════════"
echo " DEPLOY SUCCESS — feature '${FEATURE_MARKER}' is LIVE"
echo " ${DEPLOY_TS} | ${SOURCE_HEAD:0:12}"
echo "═══════════════════════════════════════════════════════"
exit 0
