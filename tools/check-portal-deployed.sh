#!/usr/bin/env bash
# check-portal-deployed.sh — Answer: is the source code deployed to VPS right now?
#
# Compares SHA256 of source files against what's on the VPS.
# Also shows .last_deployed if present.
#
# Usage:
#   ./tools/check-portal-deployed.sh           # show all SHAs side-by-side
#   ./tools/check-portal-deployed.sh --strict  # exit non-zero on any mismatch
#
# This is the canonical "is it shipped?" check. Run it before claiming a
# portal feature is shipped. If the SHAs don't match, it's NOT shipped —
# regardless of git commits or test counts.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_DIR="${REPO_ROOT}/host/vpn-portal"
VPS_HOST="vpn-prod-01"
VPS_PORTAL_DIR="/opt/vpn-portal"
STATE_FILE="${SOURCE_DIR}/.last_deployed"

STRICT=0
for arg in "$@"; do
    if [[ "$arg" == "--strict" ]]; then
        STRICT=1
    fi
done

cd "$REPO_ROOT"

echo "═══════════════════════════════════════════════════════"
echo " PORTAL DEPLOY CHECK — source vs ${VPS_HOST}"
echo "═══════════════════════════════════════════════════════"
echo ""

# git status
echo "─── git state ───"
git_status="$(git status --porcelain)"
if [[ -n "$git_status" ]]; then
    echo "  ⚠ uncommitted changes:"
    echo "$git_status" | sed 's/^/    /'
else
    echo "  ✓ working tree clean"
fi
echo "  HEAD: $(git rev-parse HEAD)  ($(git log -1 --format='%s' HEAD))"
echo ""

# Source SHAs
echo "─── source SHAs ───"
SOURCE_PY="$(sha256sum "${SOURCE_DIR}/app.py" | awk '{print $1}')"
SOURCE_JS="$(sha256sum "${SOURCE_DIR}/www/static/app.js" | awk '{print $1}')"
SOURCE_HTML="$(sha256sum "${SOURCE_DIR}/www/index.html" | awk '{print $1}')"
printf "  %-12s %s\n" "app.py"     "$SOURCE_PY"
printf "  %-12s %s\n" "app.js"     "$SOURCE_JS"
printf "  %-12s %s\n" "index.html" "$SOURCE_HTML"
echo ""

# Deployed SHAs
echo "─── deployed SHAs (${VPS_HOST}:${VPS_PORTAL_DIR}) ───"
DEPLOYED_PY="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/app.py" | awk '{print $1}')"
DEPLOYED_JS="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/www/static/app.js" | awk '{print $1}')"
DEPLOYED_HTML="$(ssh "${VPS_HOST}" "sha256sum ${VPS_PORTAL_DIR}/www/index.html" | awk '{print $1}')"
printf "  %-12s %s\n" "app.py"     "$DEPLOYED_PY"
printf "  %-12s %s\n" "app.js"     "$DEPLOYED_JS"
printf "  %-12s %s\n" "index.html" "$DEPLOYED_HTML"
echo ""

# Compare
echo "─── comparison ───"
MISMATCH=0
# index.html is expected to differ from source after cache-bust (STEP 7 of
# deploy-portal-vps.sh bumps ?v= on the deployed copy). Only flag a mismatch
# if the source-vs-deployed delta isn't explained by .last_deployed.
for pair in "app.py:$SOURCE_PY:$DEPLOYED_PY" "app.js:$SOURCE_JS:$DEPLOYED_JS" "index.html:$SOURCE_HTML:$DEPLOYED_HTML"; do
    IFS=":" read -r name src dep <<< "$pair"
    if [[ "$src" == "$dep" ]]; then
        printf "  ✓ %-12s MATCH\n" "$name"
    elif [[ "$name" == "index.html" && -f "$STATE_FILE" ]]; then
        printf "  ⚠ %-12s DIVERGED (expected — cache-bust ?v= in .last_deployed)\n" "$name"
    else
        printf "  ✗ %-12s MISMATCH  src=%s  deployed=%s\n" "$name" "${src:0:12}" "${dep:0:12}"
        MISMATCH=1
    fi
done
echo ""

# If .last_deployed exists, also cross-check that the CURRENT deployed SHAs
# match what was recorded at deploy time. This catches "someone re-deployed
# on top without updating state" and "file modified post-deploy".
if [[ -f "$STATE_FILE" ]]; then
    echo "─── cross-check against .last_deployed ───"
    RECORDED_PY="$(grep -E '^\s+app\.py=' "$STATE_FILE" | tail -1 | cut -d= -f2 | tr -d ' ')"
    RECORDED_JS="$(grep -E '^\s+app\.js=' "$STATE_FILE" | tail -1 | cut -d= -f2 | tr -d ' ')"
    if [[ "$DEPLOYED_PY" == "$RECORDED_PY" && "$DEPLOYED_JS" == "$RECORDED_JS" ]]; then
        echo "  ✓ deployed SHAs match .last_deployed (no post-deploy tampering)"
    else
        echo "  ✗ deployed SHAs differ from .last_deployed (possible post-deploy change)"
        MISMATCH=1
    fi
    echo ""
fi

# Last deployed state
echo "─── .last_deployed ───"
if [[ -f "$STATE_FILE" ]]; then
    cat "$STATE_FILE"
else
    echo "  (no .last_deployed file — never deployed via deploy-portal-vps.sh?)"
fi
echo ""

# Verdict
echo "─── verdict ───"
if [[ $MISMATCH -eq 0 ]]; then
    if [[ -f "$STATE_FILE" ]]; then
        echo "  ✓ VERIFIED DEPLOYED — .last_deployed present, source matches deployed app.js + app.py"
        echo "    Safe to claim portal feature is shipped."
        exit 0
    else
        echo "  ⚠ SHAs match but no .last_deployed file — feature was NOT deployed via deploy-portal-vps.sh"
        echo "    Code may be on disk, but no verified deploy record exists."
        echo "    Run: ./host/scripts/deploy-portal-vps.sh \"<feature marker>\""
        if [[ $STRICT -eq 1 ]]; then exit 1; fi
        exit 0
    fi
else
    echo "  ✗ NOT DEPLOYED — source ≠ ${VPS_HOST}"
    echo "    DO NOT claim the portal feature is shipped."
    echo "    Run: ./host/scripts/deploy-portal-vps.sh \"<feature marker>\""
    if [[ $STRICT -eq 1 ]]; then
        exit 1
    fi
    exit 0
fi
