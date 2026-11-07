# Cloudflare DNS Setup

Generic guide for configuring DNS records on Cloudflare (or any DNS provider with a certbot plugin) for a self-hosted IKEv2 VPN.

> **Critical:** The VPN server uses UDP ports 500 and 4500 for IKEv2.
> These are **NOT** proxied by Cloudflare (Cloudflare only proxies HTTP/HTTPS on ports 80/443).
> **Use DNS-only (grey cloud) for the VPN A record** — orange cloud will block IKEv2.

Replace `<your-domain>` with your actual domain (e.g. `example.com`) and `<vpn-subdomain>` with your VPN subdomain choice (e.g. `vpn`, `myvpn`, `gw` — your call).

---

## Records to Create / Update

### 1. VPN Server A Record (REQUIRED — before cert generation)

| Name | Type | Content | Proxy | TTL |
|------|------|---------|-------|-----|
| `<vpn-subdomain>` | A | `<your-public-ip>` | **DNS only (grey cloud)** | Auto |

**Why grey cloud:** Cloudflare's proxy only works for HTTP/HTTPS. UDP 500/4500 (IKEv2) must go directly to the VPS IP. Grey cloud passes traffic through without proxying.

**Verification after setting:**
```bash
# From outside the VPS
dig +short <vpn-subdomain>.<your-domain>
# Should return your VPS public IP

# From the VPS itself
curl -s https://ifconfig.me
# Should match the A record content
```

### 2. Optional: Portal A Record (for customer portal)

After the VPN is live, when the customer portal is deployed:

| Name | Type | Content | Proxy | TTL |
|------|------|---------|-------|-----|
| `portal` or `vpn-portal` | A | `<your-public-ip>` | Proxied (orange cloud) OK | Auto |

**For portal:** orange cloud is FINE because the portal is HTTPS on port 443. Cloudflare will proxy it, add HTTPS, and cache static assets.

### 3. CAA Record (optional but recommended)

Prevents accidental misissuance of certificates for your VPN hostname:

| Name | Type | Content | Flag | Tag |
|------|------|---------|------|-----|
| `<vpn-subdomain>` | CAA | `0 issue "letsencrypt.org"` | 0 | issue |

### 4. SPF / DMARC (if you add email from this domain)

N/A for VPN-only deployment.

---

## DNS Propagation

DNS changes propagate within 30 seconds to 5 minutes typically. Cloudflare's "Cloudflare is my DNS provider" means **all** lookups for your domain go through Cloudflare's nameservers, so propagation is instant from their side.

**To check propagation** (use your assigned Cloudflare nameservers, not the examples below):
```bash
# Check what Cloudflare's nameservers resolve (use YOUR nameservers, not carl/lola)
dig +short <vpn-subdomain>.<your-domain> @<your-ns1>.ns.cloudflare.com
dig +short <vpn-subdomain>.<your-domain> @<your-ns2>.ns.cloudflare.com

# Check from any public DNS
nslookup <vpn-subdomain>.<your-domain> 8.8.8.8
```

Your Cloudflare-assigned nameservers are shown in the Cloudflare dashboard under DNS → Records (top-right).

---

## Cert Generation — Wait for DNS First

**Do NOT run certbot with your VPN hostname until the A record has propagated.**

Why: the server cert's Subject Alternative Name (SAN) includes your VPN hostname. If DNS doesn't resolve yet, Let's Encrypt validation (DNS-01 challenge via the dns-cloudflare plugin) or client certificate verification will fail.

**Check before cert generation:**
```bash
# From the VPS:
ping -c 1 <vpn-subdomain>.<your-domain>
# Should resolve to the VPS public IP
```

---

## Cloudflare API Token (for future automation)

If you want to automate DNS updates via the Cloudflare API (e.g., dynamic DNS for a changing home IP):

1. Go to **Cloudflare Dashboard → My Profile → API Tokens**
2. Create Token → **Edit zone DNS**
3. Scope to `<your-domain>` only
4. Store the token securely — never commit to repo

Example API call:
```bash
curl -X POST "https://api.cloudflare.com/client/v4/zones/<ZONE_ID>/dns_records" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"type":"A","name":"<vpn-subdomain>","content":"'"${PUBLIC_IP}"'","proxied":false}'
```

---

## Summary Checklist

| Task | When | Done |
|------|------|------|
| Create A record: `<vpn-subdomain>` → `<your-public-ip>`, grey cloud | Before DNS propagation test | ☐ |
| Verify DNS resolves to VPS IP | After A record created | ☐ |
| Generate server cert with `<vpn-subdomain>.<your-domain>` as SAN | After DNS resolves | ☐ |
| Create A record for portal (e.g. `portal.<your-domain>`), orange cloud OK | When portal is deployed | ☐ |
| Verify IKEv2 connects from external network (LTE) | After VPN is live | ☐ |

---

## FAQ

**Q: Can I use Cloudflare proxy (orange cloud) for the VPN?**
No. Cloudflare proxy only handles HTTP/HTTPS (ports 80/443). IKEv2 uses UDP 500 and 4500 — these are passed through (not proxied) even with orange cloud. Use grey cloud for the VPN A record.

**Q: Does this mean the VPN traffic bypasses Cloudflare entirely?**
Yes. VPN traffic goes direct from the client to the VPS. Cloudflare only handles DNS for your VPN hostname. The portal (when deployed) at port 443 goes through Cloudflare's proxy.

**Q: What about the portal being on the same VPS as the VPN?**
That's fine. Port 443 on the VPS will serve the portal. Cloudflare proxies HTTPS on port 443. The VPS firewall routes port 443 to the portal service, and Cloudflare proxies HTTPS there. IKEv2 (UDP 500/4500) bypasses Cloudflare and goes direct.

**Q: Can I have two A records for the same name (multi-IP)?**
For HA, use a Floating IP rather than two A records. Two A records would round-robin clients unpredictably, breaking VPN sessions. A Floating IP is one stable IP that moves between servers. Most cloud providers offer this as a standard feature.

---

## Alternatives to Cloudflare

This doc uses Cloudflare because it's the most common choice (free tier, good API, reliable certbot plugin). The same approach works with any DNS provider that has a certbot plugin:
- **Route 53** (AWS) → `certbot-dns-route53`
- **Cloudflare** → `certbot-dns-cloudflare`
- **Google Cloud DNS** → `certbot-dns-google`
- **Hetzner DNS** → `certbot-dns-hetzner`
- **Porkbun** → `certbot-dns-porkbun`

For providers without a certbot plugin, use the HTTP-01 challenge instead of DNS-01 (requires port 80 open during cert renewal).

---

**Hand-curated 2026-10-17** for generic use. This doc is part of the generic template — all technical claims verified, all databyte-specific references removed.
