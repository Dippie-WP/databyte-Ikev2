#!/usr/bin/env bash
# check_stale_refs.sh — Catches lab-leakage in production code.
#
# Per TODO.md: grep for 102.182.117.43, vpn.homelab.local, 192.168.10.98.
# Runs from repo root. Exits 1 if any reference is found in production paths.
#
# Production paths: host/ (deployable code), docker/ (configs), quota/ (daemons)
# Allowlist:        docs/ (historical), README.md (may reference lab in setup
#                   examples), archive/ / _archived-* (retired files), CHANGELOG.md
#
# Usage: scripts/check_stale_refs.sh [--strict]
#   --strict: also fail on docs/ references (CI mode)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

STRICT=0
[[ "${1:-}" == "--strict" ]] && STRICT=1

# Lab-leakage patterns. Order: most specific to least.
PATTERNS=(
  'vpn\.homelab\.local'     # LXC 903 hostname
  '102\.182\.117\.43'       # Old public IP (lab router)
  '192\.168\.10\.98'        # LXC 903 IP
  'LXC ?903'                # "LXC 903" / "LXC-903"
  'lxc-903'                 # tag-style
)

# Search paths
SEARCH_PATHS=(
  "host"
  "docker"
  "quota"
  "tests"
)

# Allowlist regex: docs/, archive, _archived-, .bak, gen-certs.sh, CHANGELOG.md
ALLOWLIST=(
  '\.md$'
  '/archive/'
  '_archived-'
  '\.bak$'
  'gen-certs\.sh'
  'CHANGELOG\.md'
)

is_allowlisted() {
  local f="$1"
  for re in "${ALLOWLIST[@]}"; do
    if [[ "$f" =~ $re ]]; then
      return 0
    fi
  done
  return 1
}

FAIL=0
FOUND=0

for pattern in "${PATTERNS[@]}"; do
  echo "=== Pattern: $pattern ==="
  for path in "${SEARCH_PATHS[@]}"; do
    if [[ ! -d "$path" ]]; then continue; fi
    while IFS= read -r match; do
      FOUND=$((FOUND + 1))
      # Strip leading ./ for cleaner output
      clean="${match#./}"
      if is_allowlisted "$clean"; then
        if [[ $STRICT -eq 1 ]]; then
          echo "  STRICT-FAIL: $clean"
          FAIL=1
        else
          echo "  allowlisted: $clean"
        fi
      else
        echo "  FAIL: $clean"
        FAIL=1
      fi
    done < <(grep -rnE "$pattern" "$path" 2>/dev/null \
              | grep -v __pycache__ \
              | grep -v '\.pyc$' \
              || true)
  done
done

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "OK: no lab-leakage in production paths ($FOUND allowlisted references in docs/archive)"
  exit 0
else
  echo "FAIL: lab-leakage detected"
  exit 1
fi
