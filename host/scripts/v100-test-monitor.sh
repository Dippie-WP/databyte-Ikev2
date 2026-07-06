#!/bin/bash
# 100MB Cap End-to-End Test Monitor
# Tracks Zun's new test customer (id=87, name=zun-100mb-test) as he
# burns through 100MB and the quota hard-cut fires.
#
# Refreshes every 10s. Reports when state changes.
# Press Ctrl+C to exit.

CUSTOMER_ID=87
CUSTOMER_NAME="zun-100mb-test"
EAP_IDENTITY="zun-100mb-test-iphone"
QUOTA_BYTES=104857600
PORTAL=https://myvpn.databyte.co.za
COOKIE=/tmp/portal-cookies-100mb-test.txt
LOG=/tmp/100mb-test-monitor.log

# Login first (mirrors portal creds of admin)
curl -sk -X POST -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"At7S7rKtJqzSbOBqJymWv19iY_ImOfKs"}' \
    "$PORTAL/api/login" -c "$COOKIE" >/dev/null 2>&1

# Color codes
R='\033[0;31m'  # red
G='\033[0;32m'  # green
Y='\033[1;33m'  # yellow
B='\033[0;34m'  # blue
N='\033[0m'     # no color

# State tracking
LAST_PCT=-1
LAST_SAS=""
LAST_CUT=""

echo "=== 100MB CAP E2E TEST MONITOR ===" | tee -a "$LOG"
echo "customer_id=$CUSTOMER_ID name=$CUSTOMER_NAME eap=$EAP_IDENTITY" | tee -a "$LOG"
echo "quota_bytes=$QUOTA_BYTES (100 MiB)" | tee -a "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S %Z') starting..." | tee -a "$LOG"
echo | tee -a "$LOG"

while true; do
    TS=$(date '+%Y-%m-%d %H:%M:%S')

    # Portal data: customer detail (data_used_bytes, over_quota, current_session)
    CUST_JSON=$(curl -sk -b "$COOKIE" "$PORTAL/api/customers/$CUSTOMER_ID" 2>/dev/null)
    if [ -z "$CUST_JSON" ]; then
        echo -e "${R}[$TS] portal API unreachable, retrying...${N}"
        sleep 10
        continue
    fi
    USED=$(echo "$CUST_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('data_used_bytes',0))" 2>/dev/null || echo 0)
    OVER=$(echo "$CUST_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('over_quota',False))" 2>/dev/null || echo False)
    SESSION=$(echo "$CUST_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('current_session',{}).get('sa_state') or 'offline')" 2>/dev/null || echo "offline")

    # Charon SAs (via docker exec on VPS)
    SA_OUT=$(ssh vpn-prod-01-root 'docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas 2>&1' 2>/dev/null)
    SAS_FOR_USER=$(echo "$SA_OUT" | grep -c "$EAP_IDENTITY" || true)
    ESTABLISHED=$(echo "$SA_OUT" | grep -c "ESTABLISHED.*$EAP_IDENTITY" || true)

    # iptables byte counter via quota-monitor logs (nft quota counter)
    METER=$(ssh vpn-prod-01-root 'nft list meter ip filter client_src 2>/dev/null | grep -E "10\.99\.0\.2|10\.99\.0\.[0-9]+" | head -5' 2>/dev/null)
    # Show outbound bytes (this VIP's meter counter)
    METER_OUT=$(ssh vpn-prod-01-root "nft list meter ip filter client_dst 2>/dev/null" 2>/dev/null | head -20)

    # quota-monitor audit_log latest 5 cut-related events
    AUDIT=$(curl -sk -b "$COOKIE" "$PORTAL/api/admin/audit?limit=5" 2>/dev/null | \
            python3 -c "
import json,sys
try:
    data=json.load(sys.stdin)
    for r in data.get('rows',[]):
        if r.get('target_id')==$CUSTOMER_ID or 'cut' in r.get('action',''):
            print(r.get('ts'), r.get('action'), r.get('payload','')[:120])
except: pass
" 2>/dev/null)

    # Compute derived state
    PCT=$((USED * 100 / QUOTA_BYTES))
    FREE=$((QUOTA_BYTES - USED))
    FREE_MB=$(python3 -c "print(f'{${FREE}/1048576:.2f}')" 2>/dev/null || echo "?")

    # Color the percentage
    if [ "$PCT" -ge 100 ]; then PCT_COLOR=$R
    elif [ "$PCT" -ge 80 ]; then PCT_COLOR=$Y
    else PCT_COLOR=$G; fi

    # Emit report only on state change OR every 5th iteration (live debug)
    ITER=$((ITER + 1))
    EVERY_5TH=$((ITER % 5 == 0))
    CHANGED=0
    if [ "$LAST_PCT" != "$PCT" ] || [ "$LAST_CUT" != "$AUDIT" ]; then CHANGED=1; fi

    if [ "$CHANGED" = "1" ] || [ "$EVERY_5TH" = "1" ]; then
        printf "${B}[$TS]${N} %-20s ${PCT_COLOR}%3d%%${N} (%d / %d bytes, %s MiB free)  over=%s  SAs=%d(%d established)  session=%s\n" \
               "$CUSTOMER_NAME" "$PCT" "$USED" "$QUOTA_BYTES" "$FREE_MB" "$OVER" "$SAS_FOR_USER" "$ESTABLISHED" "$SESSION"
        if [ -n "$METER" ] && [ "$EVERY_5TH" = "1" ]; then
            printf "         nft meter: %s\n" "$METER"
        fi
        if [ -n "$AUDIT" ]; then
            echo "$AUDIT" | sed 's/^/         audit: /'
        fi
        LAST_PCT=$PCT
        LAST_CUT="$AUDIT"
    fi

    # Detection: when cut fires, BIG ALERT + quit
    if [ "$OVER" = "True" ]; then
        echo
        echo -e "${R}🚨🚨🚨 CUT FIRED 🚨🚨🚨${N}"
        echo "[$TS] customer $CUSTOMER_NAME hit 100% — over_quota=True"
        echo "radcheck should now have DISABLED- marker:"
        ssh vpn-prod-01-root 'export MYSQL_PWD=$(grep -v "^#" /root/.mariadb-radius-pw | head -1); mariadb -u radius -h 127.0.0.1 radius -e "SELECT * FROM radcheck WHERE username = \"'"$EAP_IDENTITY"'\";"' 2>&1 | sed 's/^/   /'
        echo "current SAs (should be 0 after SA terminate):"
        echo "$SA_OUT" | grep -E "ESTABLISHED|'"$EAP_IDENTITY"'" || echo "   (none)"
        echo
        echo -e "${G}Cut lifecycle complete. Customer locked out.${N}"
        echo "Customer should NOT be able to reconnect. If he can, that's a bug."
        # Stay running for 60 more seconds to catch any re-reconnect attempt
        for i in 1 2 3 4 5 6; do
            sleep 10
            RECONNECT=$(ssh vpn-prod-01-root 'docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas 2>&1' 2>/dev/null | grep -c "$EAP_IDENTITY" || true)
            if [ "$RECONNECT" -gt "0" ]; then
                echo -e "${R}⚠️⚠️⚠️ RECONNECT DETECTED — '$EAP_IDENTITY' has $RECONNECT SA(s)${N}"
                echo "Expected: 0. This is a BUG."
            else
                printf "${G}[%s] still locked out ✓${N}\n" "$(date '+%H:%M:%S')"
            fi
        done
        exit 0
    fi

    sleep 10
done
