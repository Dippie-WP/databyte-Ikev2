# strongswan-vpn-gateway

Personal strongSwan EAP VPN gateway. For per-user VIP pinning, attr-sql + SQLite, server-cert + EAP-MSCHAPv2 with PSK fallback. v1.2 lock-in, both gates green.

## What this is

A self-hosted IKEv2 VPN gateway running in a Docker container on an LXC host. Single-tenant homelab use today; structured to make multi-tenant / commercial per-user billing straightforward later (Phase 5B–5D roadmap).

- **Image:** `zun/strongswan:6.0.7-mschapv2-attrsql` (custom build; not pushed to a registry)
- **StrongSwan version:** 6.0.7 (CVE-2026-47895 patched)
- **Auth:** Server-cert + EAP-MSCHAPv2 (primary) and PSK (fallback for iOS, friends)
- **Pool:** 10.99.0.0/24 with per-user sticky VIPs via attr-sql + SQLite
- **Deployed at:** LXC 902 (192.168.10.212, on pve2 in Cape Town homelab)
- **Backed up to:** `rustfs:/open-claw-push/strongswan-db/` (daily + ISO-week slots, 14d/8w retention)
- **Public endpoint:** 102.182.117.43, router forwards UDP 500/4500

## What's where

| Path | What's in it |
|---|---|
| `docker/` | The container: Dockerfile, docker-compose, swanctl configs, strongswan.d overrides, in-image `start.sh` |
| `host/` | The LXC host: sysctl, iptables, firewalld zone, optional nftables service |
| `scripts/` | Operate-time: cert gen, DB seed, image build, daily backup, rollback |
| `docs/` | ROADMAP, ARCHITECTURE, DEPLOYMENT, ISSUES-LOG, SESSION-HISTORY |
| `examples/` | Client profiles: Android `.sswan`, iOS `.mobileconfig` template (iOS path is broken; see issues) |

## Quick start (new host)

See `docs/DEPLOYMENT.md`. Headline:

```bash
# 1. Drop in real certs + secrets (NOT in git)
cd docker/swanctl
# Use scripts/gen-certs.sh to generate, or copy from existing host

# 2. Build the image
bash scripts/build-image.sh

# 3. Apply host config (one time)
sudo cp host/sysctl.d/99-strongswan.conf /etc/sysctl.d/
sudo cp host/firewalld/zones/trusted.xml /etc/firewalld/zones/
sudo cp host/iptables/rules.v4.template /etc/iptables/rules.v4
sudo sysctl --system
sudo firewall-cmd --reload
sudo systemctl restart netfilter-persistent

# 4. Bring up
cd docker
docker compose --profile vpn up -d

# 5. Verify
docker logs strongswan --tail 30
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas
```

## Status

| Phase | Description | Gate |
|---|---|---|
| **5A** | Foundation: conn config, user+pool+VIP pin, public-path test, reconnect, rollback, install_virtual_ip fix, MSS clamp | ✅ **GREEN (both gates)** |
| 5B | Quota layer (nftables accounting + monitor + alerts) | ⏳ Pending sign-off on 5A |
| 5C | Surface (status FastAPI + Grafana dashboard) | ⏳ Pending 5B |
| 5D | Commercial (pricing, payment-triggered reset) | 🔒 Shelved |

## Versions

- **v1.2** (this): EAP + attr-sql + sticky VIPs, public-path tested on 5G
- **v1.1** (rollback target, still in registry): PSK + EAP, no VIP pinning

## Critical known limitations

1. **iOS .mobileconfig** silently fails cert validation. iOS path shelved to v1.3.
2. **5G IP rotation** causes brief MOBIKE gaps. Workaround: rekey_time is 24h, will shorten in 5B.
3. **MSS clamp at 1260** required for 5G carriers. Lives in `host/iptables/rules.v4`. Forgetting this → iana.org-style timeouts.
4. **No monitoring yet.** Plain `swanctl --list-sas` is the only operational view. (5C adds the surface.)

## License

None declared. Personal project.

## Maintainer

Zun (@zuzu172 on Telegram, github Dippie-WP). Built with Misha.
