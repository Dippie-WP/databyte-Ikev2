#!/usr/bin/env bash
# check_github_parity.sh — verify local strongswan-vpn-gateway repo is in sync
# with origin. Use this anytime as a CI-style guard / operator check.
#
# Usage:
#   bash tools/check_github_parity.sh            # returns 0 if in sync, 1 if drift
#   bash tools/check_github_parity.sh --verbose  # print ahead/behind + missing tags
#
# Definition of "in sync":
#   - Local main has 0 commits ahead of origin/main
#   - Local has no dirty working-tree files (uncommitted)
#   - All local tags reachable from main exist on origin (no missing tag markers)
#
# Wire into CI / heartbeat / deploy script as needed.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Single git invocation to fetch remote tag list (one network round-trip)
REMOTE_TAGS=$(git ls-remote origin 'refs/tags/*' 2>/dev/null | awk '{print $2}' | sed 's|refs/tags/||' | sort)
LOCAL_REACHABLE=()
LOCAL_UNREACHABLE=()
while read -r tag; do
    [ -z "$tag" ] && continue
    if git merge-base --is-ancestor "$(git rev-parse "$tag")" main 2>/dev/null; then
        LOCAL_REACHABLE+=("$tag")
    else
        LOCAL_UNREACHABLE+=("$tag")
    fi
done < <(git tag -l | sort)

MISSING_ON_ORIGIN=()
for tag in "${LOCAL_REACHABLE[@]}"; do
    if ! echo "$REMOTE_TAGS" | grep -qx "$tag"; then
        MISSING_ON_ORIGIN+=("$tag")
    fi
done

AHEAD_BEHIND=$(git rev-list --left-right --count main...origin/main 2>/dev/null || echo "0 0")
AHEAD=$(echo "$AHEAD_BEHIND" | awk '{print $1}')
BEHIND=$(echo "$AHEAD_BEHIND" | awk '{print $2}')
# Filter out files that are intentionally not tracked:
#   - .last_deployed: deploy-script self-report (gitignored)
#   - .bak.* / .rej / *.swp: backup/diff/editor scratch files (transient)
#   - chatty runtime state files that aren't source
# grep -vE returns 1 when no match; `|| true` keeps set -e + pipefail happy.
DIRTY_RAW=$(git status --short)
DIRTY=$(printf '%s\n' "$DIRTY_RAW" | grep -vE '\.last_deployed$|\.bak\.[0-9]{8}(T[0-9]{6}Z)?$|\.rej$|\.swp$' | wc -l || true)

echo "=== GitHub parity check ($(date -u +%FT%TZ)) ==="
echo "Local main ahead of origin/main: $AHEAD"
echo "Local main behind origin/main:   $BEHIND"
echo "Dirty working-tree files:        $DIRTY"
echo "Local tags reachable from main:  ${#LOCAL_REACHABLE[@]}"
echo "Local tags unreachable (dangling on push): ${#LOCAL_UNREACHABLE[@]}"
echo "Reachable tags missing on origin: ${#MISSING_ON_ORIGIN[@]}"

if [ "${1:-}" = "--verbose" ]; then
    [ "${#LOCAL_UNREACHABLE[@]}" -gt 0 ] && echo "  UNREACHABLE: ${LOCAL_UNREACHABLE[*]}"
    [ "${#MISSING_ON_ORIGIN[@]}" -gt 0 ] && echo "  MISSING-ON-ORIGIN: ${MISSING_ON_ORIGIN[*]}"
    [ "$DIRTY" -gt 0 ] && git status --short | sed 's/^/    /'
fi

EXIT=0
[ "$AHEAD" != "0" ] && { echo "❌ FAIL: local main is $AHEAD commit(s) ahead of origin — push needed"; EXIT=1; }
[ "$BEHIND" != "0" ] && { echo "❌ FAIL: local main is $BEHIND commit(s) behind origin — pull needed"; EXIT=1; }
[ "$DIRTY" != "0" ] && { echo "❌ FAIL: $DIRTY dirty working-tree file(s) — commit or stash"; EXIT=1; }
[ "${#MISSING_ON_ORIGIN[@]}" -gt 0 ] && { echo "❌ FAIL: ${#MISSING_ON_ORIGIN[@]} reachable tag(s) missing on origin — push tags"; EXIT=1; }

if [ "$EXIT" = "0" ]; then
    echo "✅ PASS: local repo in perfect sync with origin (main + tags + clean tree)"
fi

exit "$EXIT"
