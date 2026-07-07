# VPN Portal — 5C.1 Backend

FastAPI backend for the databyte VPN. Single-file MVP.

- Runs on **LXC 902 (myservices, 192.168.10.212)** on port **8080** (loopback only — proxy via Nginx PM if external)
- Reads SQLite + runs commands on **LXC 903 (vpn-gateway, 192.168.10.98)** over SSH
- Admin-only auth (bcrypt + HMAC-signed session cookie); customer web login is a 5C.2 deliverable

## Deploy

```bash
# 1. Copy code to LXC 902
ssh root@192.168.10.210 'pct push 902 /tmp/vpn-portal.tar.gz /tmp/vpn-portal.tar.gz'
# or rsync directly:
rsync -av --delete \
  /root/projects/strongswan-vpn-gateway/host/vpn-portal/ \
  root@192.168.10.210:/var/lib/lxc/902/rootfs/opt/vpn-portal/

# 2. Install on LXC 902 (via pve2)
ssh root@192.168.10.210 "pct exec 902 -- bash -c '
  set -e
  apt-get install -y python3-pip python3-venv python3-full
  python3 -m venv /opt/vpn-portal/.venv
  /opt/vpn-portal/.venv/bin/pip install -r /opt/vpn-portal/requirements.txt
  install -m 0644 /opt/vpn-portal/systemd/vpn-portal.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable vpn-portal.service
'"

# 3. Generate admin password hash and write /etc/vpn-portal.env
ssh root@192.168.10.210 "pct exec 902 -- bash -c '
  HASH=\$(/opt/vpn-portal/.venv/bin/python -c \"import bcrypt; print(bcrypt.hashpw(b\\\"CHANGE_ME\\\", bcrypt.gensalt()).decode())\")
  cat > /etc/vpn-portal.env <<EOF
ADMIN_USER=admin
ADMIN_PASS_HASH=\$HASH
SESSION_SECRET=\$(openssl rand -hex 32)
EOF
  chmod 600 /etc/vpn-portal.env
'"

# 4. Set up SSH key from 902 -> 903
ssh root@192.168.10.210 "pct exec 902 -- bash -c '
  if [ ! -f /root/.ssh/id_ed25519_vpn ]; then
    ssh-keygen -t ed25519 -N \"\" -f /root/.ssh/id_ed25519_vpn -C \"vpn-portal@902\"
  fi
  ssh-keyscan -H 192.168.10.98 >> /root/.ssh/known_hosts
'"
ssh zunaid@192.168.10.98 'sudo bash -c "
  grep -q vpn-portal@902 /root/.ssh/authorized_keys || cat >> /root/.ssh/authorized_keys <<EOF
\$(ssh root@192.168.10.210 \"pct exec 902 -- cat /root/.ssh/id_ed25519_vpn.pub\")
EOF
"'

# 5. Start
ssh root@192.168.10.210 "pct exec 902 -- systemctl start vpn-portal"

# 6. Verify
curl -sS http://192.168.10.212:8080/api/health | jq
```

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | /api/health                     | — | Liveness + DB + charon reach |
| POST | /api/login                      | — | Admin login (bcrypt + cookie) |
| POST | /api/logout                     | cookie | Clear session |
| GET  | /api/customers                  | cookie | Customer list w/ tier, used, quota, VIP |
| GET  | /api/customers/{id}             | cookie | + devices[] + alerts[] |
| GET  | /api/tiers                      | cookie | tier defs (5GB / 10GB / 20GB / demo_100MB) — Tier 1 / 2 / 3 |
| GET  | /api/quota/{customer_id}        | cookie | live used/quota + cap state |
| POST | /api/quota/{customer_id}/reset  | cookie | sqlite UPDATE, audit_log row |
| GET  | /api/vpn/sessions               | cookie | docker exec swanctl --list-sas (raw) |
| GET  | /api/vpn/pools                  | cookie | parsed pools |
| GET  | /api/security/bans              | cookie | parsed ipban-ctl list |
| GET  | /api/security/whitelist         | cookie | firewalld trusted zone sources |
| POST | /api/security/unban             | cookie | ipban-ctl unban {ip} |
| POST | /api/security/whitelist/add     | cookie | firewall-cmd --add-source {cidr} |
| GET  | /api/security/deadman           | cookie | ipban-ctl deadman status (raw) |

## Config (env vars, file `/etc/vpn-portal.env`)

| Var | Default | Notes |
|-----|---------|-------|
| `ADMIN_USER` | admin | |
| `ADMIN_PASS_HASH` | (REQUIRED) | bcrypt hash; generate with `python -c "import bcrypt; print(bcrypt.hashpw(b'YOURPASS', bcrypt.gensalt()).decode())"` |
| `SESSION_SECRET` | random per process | Set explicitly so cookies survive restarts |
| `VPN_HOST` | (REQUIRED) | `127.0.0.1` on VPS, LXC 903 IP for dev — no default (fail-fast at startup) |
| `SSH_KEY` | /root/.ssh/id_ed25519_vpn | |
| `DB_PATH` | /var/lib/strongswan/ipsec.db | On 903 |
| `SESSION_TTL` | 86400 | seconds (24h) |
| `PORT` | 8080 | uvicorn bind port (loopback) |

## Security notes

- Cookie is HttpOnly + SameSite=Lax. Add `Secure` flag if served over HTTPS.
- Session secret is HMAC-signed (no JWT lib needed; tamper-evident).
- Rate limit on /login: 5 attempts/IP/minute (in-memory; resets on restart).
- Per-customer auth is a 5C.2 deliverable. v1 = admin-only.
- SSH key (`id_ed25519_vpn`) on 902 has root@903 access. Limit by `from=192.168.10.212` in 903's authorized_keys if hardening.
- Hardcoded secrets must live in `/etc/vpn-portal.env` (mode 600), not in the repo.

## Fronting with Nginx PM (recommended before 5D)

```nginx
server {
    listen 443 ssl;
    server_name vpn.databyte.co.za;
    # ... TLS ...
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## Known gaps for 5C.2+

- `swanctl_list_sas` returns raw text. UI parses client-side for v1; structured parser later.
- `firewalld --add-source` is non-persistent (lost on reload). Persistent via `--runtime-to-permanent` or `--permanent` flag (decide which in 5C.2).
- No CSRF protection (assumes API-only, not browser-form). Add if a browser form ever POSTs directly.
- No HTTPS termination. Front with nginx-pm or traefik.
- Audit log isn't surfaced yet (read-only `/api/customers/{id}` shows alerts; add `/api/audit` if useful).
