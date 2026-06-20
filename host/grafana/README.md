# host/grafana/

Grafana dashboards for the strongSwan VPN gateway stack.

## dashboards/

JSON exports of dashboards built against the local Prometheus
(`http://192.168.10.212:9090`, datasource uid `prometheus-default`).

### strongswan-quota (5C.3)

11-panel dashboard for the 5B quota layer:

| # | Panel | What |
|---|-------|------|
| 1 | Active Leases (stat) | `vpn_active_lease_count` |
| 2 | Active Customers (stat) | `sum(vpn_customer_is_active)` |
| 3 | Customers Over Quota (stat) | `sum(vpn_customer_over_quota)` |
| 4 | Exporter Scrape Errors (stat) | `vpn_exporter_scrape_errors_total` |
| 5 | Per-Customer Data Used (timeseries) | `vpn_customer_data_used_bytes` |
| 6 | Quota Utilization (%) (timeseries) | `100 * used / limit` |
| 7 | Customer Roster (table) | joined table of customers, used, limit, %, status |
| 8 | Active Leases - live traffic (table) | per-VIP in/out bytes |
| 9 | Live Throughput (timeseries) | `rate(lease_bytes_in/out_total[5m])` |
| 10 | Alerts Recorded (table) | `vpn_alerts_total` |
| 11 | Audit Log Activity (timeseries) | `vpn_audit_log_total` by actor/action |

Source data: `vpn-quota-exporter` on LXC 903, port 9102.

### Existing dashboards (pre-5C.3, kept for compatibility)

- `strongswan-v1-2` (12 panels, 5A.9) — system-level: charon uptime, IKE_SA count, plugins, worker threads. Source: `ipsec-exporter` :9101.
- `strongswan-vpn` (12 panels, 5A.9) — per-SA bandwidth: per-client download/upload, cumulative MB. Source: `ipsec-exporter` :9101.

## Install / reimport

```bash
API_KEY=$(cat /root/.openclaw/.grafana/api_key)
python3 -c "
import json
dash = json.load(open('host/grafana/dashboards/strongswan-quota.json'))
payload = {'dashboard': dash, 'message': 'reimport (5C.3)', 'overwrite': True}
json.dump(payload, open('/tmp/dash-payload.json', 'w'))
"
curl -X POST -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     --data-binary @/tmp/dash-payload.json \
     http://192.168.10.212:3000/api/dashboards/db
```

Dashboard URL after import:
`http://192.168.10.212:3000/d/strongswan-quota/`
