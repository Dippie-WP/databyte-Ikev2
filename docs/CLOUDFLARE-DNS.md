# Cloudflare DNS Setup — myvpn.databyte.co.za

Cloudflare manages the `databyte.co.za` zone. All DNS changes are made in the Cloudflare dashboard.

> **Critical:** The VPN server uses UDP ports 500 and 4500 for IKEv2.
> These are **NOT** proxied by Cloudflare (Cloudflare only proxies HTTP/HTTPS on ports 80/443).
> **Use DNS-only (grey cloud) for the VPN A record** — orange cloud will block IKEv2.

---

## Records to Create / Update

### 1. VPN Server A Record (REQUIRED — before cert generation)

| Name | Type | Content | Proxy | TTL |
|------|------|---------|-------|-----|
| `myvpn` | A | `<VPS_PUBLIC_IP>` | **DNS only (grey cloud)** | Auto |

**Why grey cloud:** Cloudflare's proxy only works for HTTP/HTTPS. UDP 500/4500 (IKEv2) must go directly to the VPS IP. Grey cloud passes traffic through without proxying.

**Verification after setting:**
```bash
# From outside the VPS (e.g. your Mac)
dig +short myvpn.databyte.co.za
# Should return the VPS public IP

# From the VPS itself
curl -s https://ifconfig.me
# Should match the A record content
```

### 2. Optional: Portal A Record (for customer portal at /portal/)

After the VPN is live, when the customer portal is deployed:

| Name | Type | Content | Proxy | TTL |
|------|------|---------|-------|-----|
| `vpn-portal` or `portal` | A | `<VPS_PUBLIC_IP>` | Proxied (orange cloud) OK | Auto |

**For portal:** orange cloud is FINE because the portal is HTTPS on port 443. Cloudflare will proxy it, add HTTPS, and cache static assets.

### 3. CAA Record (optional but recommended)

Prevents accidental misissuance of certificates for `myvpn.databyte.co.za`:

| Name | Type | Content | Flag | Tag |
|------|------|---------|------|-----|
| `myvpn` | CAA | `0 issue "letsencrypt.org"` | 0 | issue |

### 4. SPF / DMARC (if you add email from this domain)

N/A for VPN-only deployment.

---

## DNS Propagation

DNS changes propagate within 30 seconds to 5 minutes typically. Cloudflare's "Cloudflare is my DNS provider" means **all** lookups for `databyte.co.za` go through Cloudflare's nameservers, so propagation is instant from their side.

**To check propagation:**
```bash
# Check what Cloudflare's nameservers resolve
dig +short myvpn.databyte.co.za @carl.ns.cloudflare.com
dig +short myvpn.databyte.co.za @lola.ns.cloudflare.com
```

**To check from your Mac (bypassing local DNS cache):**
```bash
# macOS: use dscacheutil to flush
dscacheutil -flushcache
# Then:
nslookup myvpn.databyte.co.za 8.8.8.8
```

---

## Cert Generation — Wait for DNS First

**Do NOT run `gen-certs.sh` with `SERVER_ID=myvpn.databyte.co.za` until the A record has propagated.**

Why: the server cert's Subject Alternative Name (SAN) includes `myvpn.databyte.co.za`. If DNS doesn't resolve yet, Let's Encrypt validation (future) or client certificate verification will fail.

**Check before cert generation:**
```bash
# From the VPS:
ping -c 1 myvpn.databyte.co.za
# Should resolve to the VPS public IP

# Or from your Mac:
nslookup myvpn.databyte.co.za 8.8.8.8
```

---

## Cloudflare API Token (for future automation)

If you want to automate DNS updates via the Cloudflare API (e.g., dynamic DNS for a changing home IP):

1. Go to **Cloudflare Dashboard → My Profile → API Tokens**
2. Create Token → **Edit zone DNS**
3. Scope to `databyte.co.za` only
4. Store the token securely — never commit to repo

Example API call:
```bash
curl -X POST "https://api.cloudflare.com/client/v4/zones/<ZONE_ID>/dns_records" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"type":"A","name":"myvpn","content":"'"${VPS_IP}"'","proxied":false}'
```

---

## Summary Checklist

| Task | When | Done |
|------|------|------|
| Create A record: `myvpn` → `<VPS_IP>`, grey cloud | Before DNS propagation test | ☐ |
| Verify DNS resolves to VPS IP | After A record created | ☐ |
| Generate server cert with `SERVER_ID=myvpn.databyte.co.za` | After DNS resolves | ☐ |
| Create A record for portal (e.g. `portal.myvpn`), orange cloud OK | When portal is deployed | ☐ |
| Verify IKEv2 connects from external network (LTE) | After VPN is live | ☐ |

---

## FAQ

**Q: Can I use Cloudflare proxy (orange cloud) for the VPN?**
No. Cloudflare proxy only handles HTTP/HTTPS (ports 80/443). IKEv2 uses UDP 500 and 4500 — these are passed through (not proxied) even with orange cloud. Use grey cloud for the VPN A record.

**Q: Does this mean the VPN traffic bypasses Cloudflare entirely?**
Yes. VPN traffic goes direct from the client to the VPS. Cloudflare only handles DNS for `myvpn.databyte.co.za`. The portal (when deployed) at port 443 goes through Cloudflare's proxy.

**Q: What about the portal being on the same VPS as the VPN?**
That's fine. Port 443 on the VPS will serve the portal. Cloudflare proxies HTTPS on port 443. The VPS firewall (iptables) routes port 443 to the portal service, and Cloudflare proxies HTTPS there. IKEv2 (UDP 500/4500) bypasses Cloudflare and goes direct.

**Q: Can I have two A records for the same name (multi-IP)?**
For HA (Phase 5H), you'd use a Floating IP rather than two A records. Two A records would round-robin clients unpredictably, breaking VPN sessions. Floating IP = one stable IP that moves between servers. Details in PLAN-5H-HA-LB.md.