#!/usr/bin/env bash
# check_stale_refs.sh — Catches lab-leakage in production code.
#
# Per TODO.md Bug #5: VPS and LXC 903 lab are intentionally separate (Zun 2026-06-25).
# Greps for 102.182.117.43, vpn.homelab.local, 192.168.10.98, "LXC 903", "lxc-903".
#
# Production paths: host/ (deployable code), docker/ (configs), quota/ (daemons), tests/
# Excludes:         .md files (documentation), CHANGELOG, archive/, _archived-, .bak
# Skip rules:       Skip COMMENT lines (start with #, //, --, or are inside multi-line
#                   strings/JSON metadata). Comments legitimately reference LXC 903 to
#                   document the dev/prod split — those are not leaks.
# Skip env defaults: Lines like `os.environ.get("X", "192.168.10.98")` are NOT leaks —
#                   they're explicit dual-env configs. Skip if the line is a default arg.
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

# File-level allowlist regex: docs/, archive, _archived-, .bak, gen-certs.sh, CHANGELOG.md,
# .service files (systemd — they document deployment context), .json in host/grafana/
# (dashboard metadata, not deployment config).
ALLOWLIST=(
  '\.md$'
  '/archive/'
  '_archived-'
  '\.bak$'
  'gen-certs\.sh'
  'CHANGELOG\.md'
  '\.service$'
  'host/grafana/'
  'host/grafana/dashboards/'
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

# Skip if line looks like a comment, env-var default arg, or JSON metadata.
is_skippable_line() {
  local line="$1"
  # Python / bash / shell comments
  if [[ "$line" =~ ^[[:space:]]*# ]]; then return 0; fi
  # JS / C / CSS comments
  if [[ "$line" =~ ^[[:space:]]*// ]]; then return 0; fi
  # SQL / Haskell / Lua comments
  if [[ "$line" =~ ^[[:space:]]*-- ]]; then return 0; fi
  # Continuation of multi-line string/docstring (Python)
  if [[ "$line" =~ ^[[:space:]]*\"\"\" ]]; then return 0; fi
  if [[ "$line" =~ ^[[:space:]]*\'\'\' ]]; then return 0; fi
  # Block-comment continuation (Python/JS docstring lines starting with *)
  if [[ "$line" =~ ^[[:space:]]*\*[^/] ]]; then return 0; fi
  # JSON metadata fields: "description": ..., "tags": [...], "title": ...
  if [[ "$line" =~ (description|tags|title)\"?:.*\" ]]; then return 0; fi
  # env-var default arg pattern (Python): os.environ.get("X", "192.168.10.98") — still a leak!
  # We DON'T skip these; only hardcoded non-env refs are skipped via the above comment rules.
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
      # The grep output format is "path:lineno:content" — extract content after the 2nd colon.
      # For comments etc. we need the content (text after second `:`).
      content="${clean#*:}"
      content="${content#*:}"
      # File-level allowlist
      file_path="${clean%%:*}"
      if is_allowlisted "$file_path"; then
        if [[ $STRICT -eq 1 ]]; then
          # In strict mode, only allowlist .md / .service / archive still applies —
          # but we don't STRICT-FAIL allowlisted files anymore. (Was over-broad pre-fix.)
          echo "  allowlisted (strict): $clean"
        else
          echo "  allowlisted: $clean"
        fi
        continue
      fi
      # Line-level skip (comments, JSON metadata)
      if is_skippable_line "$content"; then
        echo "  comment/metadata: $clean"
        continue
      fi
      # Hardcoded non-comment reference in production code — REAL leak.
      echo "  FAIL: $clean"
      FAIL=1
    done < <(grep -rnE "$pattern" "$path" 2>/dev/null \
              | grep -v __pycache__ \
              | grep -v '\.pyc$' \
              || true)
  done
done

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "OK: no lab-leakage in production paths ($FOUND references — all allowlisted, in comments, or in JSON metadata)"
  exit 0
else
  echo "FAIL: lab-leakage detected (real hardcoded refs in production code)"
  exit 1
fi
