#!/bin/bash
# Re-apply iptables rules whenever the strongSwan container actually restarts.
# DOES NOT re-apply on docker exec (was the bug: every poll reset the per-VIP byte counters).
set -e
RULES=/etc/iptables/rules.v4
RULES_BIN=/usr/sbin/iptables-restore

# Apply rules immediately on startup
$RULES_BIN $RULES >/dev/null 2>&1 && logger -t strongswan-watchdog "initial rules applied"

# Then watch Docker events
docker events --filter container=strongswan --format "{{.Action}}:{{.Time}}" 2>/dev/null | while IFS=: read -r action time; do
  # Only re-apply on actual container lifecycle events, NOT on docker exec
  case "$action" in
    start|restart|unpause|die|stop|kill|oom)
      sleep 1  # Let Docker finish its iptables dance first
      $RULES_BIN $RULES >/dev/null 2>&1 && logger -t strongswan-watchdog "rules re-applied after $action at $time"
      ;;
  esac
done
