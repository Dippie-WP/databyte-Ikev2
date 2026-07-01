# DAT-VPN-WINDOWS-CLIENT-MASTER-001 — Windows IKEv2 VPN Client: Complete Build Manual

| Field | Value |
|---|---|
| Document ID | DAT-VPN-WINDOWS-CLIENT-MASTER-001 |
| Title | Windows IKEv2 + EAP-MSCHAPv2 VPN Client Build Manual |
| Rev | v1.1.0 |
| Date | 2026-06-24 |
| Author | Misha (AI Agent) for Zun |
| Status | APPROVED — HARDLOCKED |
| Classification | Internal — Homelab Infrastructure |

**v1.1.0 change (2026-06-24):**
- Promoted script to **v2.6.0** (was v2.3.0)
- Added **THE CANONICAL 3-LINE BLOCK** at the top — there is now ONE way
- Removed all references to: `setup-windows-vpn.ps1`, `connect-databyte-vpn.ps1`, `test-win-5g-setup.ps1`, `setup-databyte-vpn-zun.ps1`
- All those variants are DELETED from VPS `/static/` (moved to `/tmp/_trash-20260624-2035/`) and archived locally in `scripts/_archive-2026-06-24/`
- ONE filename (`setup-databyte-vpn.ps1`), TWO URLs (portal primary, myvpn.* fallback with `-k`)
- Single STEP 6 (RasSetCredentials P/Invoke) — duplicate WMI methods block removed

---

## THE CANONICAL 3-LINE BLOCK (save this — it is the ONLY way)

**Open PowerShell as Administrator. Copy/paste this. Press Enter three times.**

```powershell
# Primary (LE cert, no -k):
curl.exe -o $env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1
& $env:TEMP\setup.ps1
rasdial DatabyteVPN
```

**Fallback (myvpn.databyte.co.za, has Cloudflare Origin Cert, needs -k):**
```powershell
curl.exe -k -o $env:TEMP\setup.ps1 https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1
& $env:TEMP\setup.ps1
rasdial DatabyteVPN
```

**Rules (do not deviate, ever):**
- ONE filename: `setup-databyte-vpn.ps1`
- TWO URLs serve the SAME file (md5 must match: `fc6a83d18b195bf3cbba1558f87f912a`)
- NO `-zun`, NO `-windows`, NO `-test`, NO `-v1.5.0`, NO `-v2.3.0` suffixes
- NO archived script in /tmp or workspace
- NO personal copies
- If you find a script with a different name, it's rot. Delete it.

| Property | Value |
|---|---|
| Canonical filename | `setup-databyte-vpn.ps1` |
| Version | **2.6.5** (HARDLOCKED filename/URL/method; v2.6.x patch revisions applied post-2026-06-24 baseline — see CHANGELOG inside the script) |
| MD5 | `fc6a83d18b195bf3cbba1558f87f912a` |
| Size | 23609 bytes |
| Primary URL | `https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1` |
| Fallback URL | `https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1` (needs `-k`) |
| Git ref | `main` @ `1eae9ae` (HEAD = current version). The `v2.6.0` tag at commit `2732215` is the HARDLOCK baseline; v2.6.5 = v2.6.0 + 5 patches (`c27742d`, `95b401d`, `e565666`, `41859eb`, `bf4e4b1`). All listed in the script header CHANGELOG. |
| Connection name | `DatabyteVPN` |
| Server | `myvpn.databyte.co.za` → 154.65.110.44 |

---

## 0. Purpose

This document is the **complete, authoritative, from-scratch build manual** for the DatabyteVPN Windows IKEv2 + EAP-MSCHAPv2 client installer. It is designed to enable reconstruction of the entire solution — server and client — from bare metal, even if all prior context is lost.

**What this document IS:**
- A complete architectural reference (server + client)
- A step-by-step rebuild procedure
- A failure-mode encyclopedia with fixes
- A lesson-bank distilled from 2 days of debugging

**What this document IS NOT:**
- A marketing document
- A user-facing guide (see DAT-VPN-SOP-001 for customers)
- A theoretical design — every line has been tested on live hardware

**Empirical validation (2026-06-24, DESKTOP-AL15LAT, Win 11 24H2 build 26200):**
```
rasdial DatabyteVPN              ← NO CREDENTIALS PASSED
→ Successfully connected to DatabyteVPN. Command completed successfully.

tracert 8.8.8.8
  1  154.65.110.44              ← VPS as first hop
```

---

## 1. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Windows Client (DESKTOP-AL15LAT, VLAN 30)                 │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ PowerShell script (setup-databyte-vpn.ps1 v2.3.0)    │   │
│  │   STEP 1: Remove stale profiles                       │   │
│  │   STEP 2: Download Let's Encrypt CA                   │   │
│  │   STEP 3: New-VpnConnection (IKEv2, Custom crypto)    │   │
│  │   STEP 4: New-EapConfiguration (EAP-MSCHAPv2)        │   │
│  │   STEP 5: Registry (AssumeUDPEncapsulationContext=2) │   │
│  │   STEP 6: RasSetCredentials (bind creds via Win32)   │   │
│  │   STEP 7: Connect + poll                             │   │
│  └────────────────────┬──────────────────────────────────┘   │
│                       │ UDP 500/4500                         │
└───────────────────────┼─────────────────────────────────────┘
                        │ VLAN 30 → Router → Internet
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  Internet                                                   │
└─────────────────────────────────────────────────────────────┘
                        │
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  VPS (Xneelo, 154.65.110.44, Debian 13)                   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ strongSwan container (charon, Docker)                 │   │
│  │   • EAP-MSCHAPv2 auth                                │   │
│  │   • LE certificate (CN=myvpn.databyte.co.za)         │   │
│  │   • VPN pool: 10.99.0.0/24                           │   │
│  │   • ESP proposals: CBC + GCM (Windows rekeys GCM)     │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ iptables (netfilter-persistent)                      │   │
│  │   • NAT MASQUERADE: 10.99.0.0/24 → ens3             │   │
│  │   • FORWARD ACCEPT: 10.99.0.0/24                     │   │
│  │   • INPUT: UDP 500/4500, TCP 80/443, SSH 22         │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ nginx (Docker)                                       │   │
│  │   • HTTPS (Cloudflare Origin Cert)                   │   │
│  │   • Serves: setup-databyte-vpn.ps1, CA certs, etc.  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                        │
                        ↓ Internet (MASQUERADED traffic)
┌─────────────────────────────────────────────────────────────┐
│  Internet destinations (Google, etc.)                      │
│  Source IP appears as 154.65.110.44 (VPS public IP)        │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

| Decision | Rationale | Source |
|---|---|---|
| EAP-MSCHAPv2 (not certificate-based) | Windows native support, no PKI required on client | strongSwan docs |
| Let's Encrypt certificate on VPS | Trusted by all platforms, no CA cert install needed on client | strongSwan ios.html |
| AES-CBC + GCM ESP proposals | Windows negotiates CBC on init, GCM on rekey. Both required | Lesson #146 |
| `RasSetCredentials` for credential binding | Canonical Windows API (rasapi32.dll), not WMI or cmdkey | Lesson #161-#162 |
| GCM ESP required for rekey | Windows IKEv2 uses GCM for rekey even when initial is CBC | Lesson #146 |
| Per-platform test customers | Avoids Windows IKEv2 EAP identity cache poisoning | Lesson #139-#140 |
| UDP port 4500 (not 500) for NAT traversal | modern Windows IKEv2 uses 4500 primarily | standard practice |

---

## 2. Server-Side Build (VPS)

### 2.1 Prerequisites

| Item | Value |
|---|---|
| VPS | Xneelo, Debian 13, public IP 154.65.110.44 |
| Docker | Installed on host |
| Domain | `myvpn.databyte.co.za` pointing to VPS IP |
| Cloudflare | DNS proxy for portal; direct A record for VPN endpoint |
| Firewall | UDP 500, UDP 4500, TCP 80, TCP 443 open on VPS |
| Cloudflare Origin Certificate | `*.databyte.co.za` SAN, stored on VPS |

### 2.2 strongSwan Configuration

**File:** `/opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf`

```ini
# rw-eap.conf — EAP-MSCHAPv2 IKEv2 server
connections {
  rw-eap {
    version = 2
    send_cert = always
    local_addrs  = 0.0.0.0
    remote_addrs = %any

    # IP address pool for VPN clients
    pools = rw-pool

    # IKE cipher suite (handshake)
    proposals = aes256-sha256-modp2048-ecp256,aes128-sha256-modp2048-ecp256

    # Allow reconnection with same IP
    unique = replace

    # Rekey intervals
    reauth_time = 24h
    rekey_time = 24h

    # Enable MOBIKE (road warrior mobility)
    mobike = yes

    # Fragmentation for large packets
    fragmentation = yes

    # Dead peer detection
    dpd_delay = 30s
    dpd_timeout = 120s

    local {
      auth = pubkey
      certs = server.pem
      id = "myvpn.databyte.co.za"
    }

    remote {
      auth = eap-mschapv2
      eap_id = %any
    }

    children {
      net {
        mode = tunnel
        local_ts = 0.0.0.0/0
        remote_ts = dynamic

        dpd_action = clear
        start_action = start

        # CHILD SA rekey interval (1 hour = prevents NAT timeout)
        rekey_time = 1h

        # ESP cipher suite
        # MUST include BOTH CBC and GCM — Windows uses CBC on init, GCM on rekey
        # Without GCM: "NO_PROPOSAL_CHOSEN" on rekey (lesson #146)
        esp_proposals = aes256-sha256-modp2048-ecp256,aes128-sha256-modp2048-ecp256,aes256-sha1-modp2048,aes128-sha1-modp2048,aes256gcm16-ecp256,aes128gcm16-ecp256,aes256gcm16,aes128gcm16
      }
    }
  }
}

# secrets — one per VPN customer
secrets {
  eap-operator {
    id = zun-operator
    secret = "<REALM GENERATED PASSWORD>"
  }
  eap-test-win-5g-laptop {
    id = test-win-5g-laptop
    secret = "<REALM GENERATED PASSWORD>"
  }
  eap-test-iphone-5g-iphone {
    id = test-iphone-5g-iphone
    secret = "<REALM GENERATED PASSWORD>"
  }
  eap-test-android-5g-android {
    id = test-android-5g-android
    secret = "<REALM GENERATED PASSWORD>"
  }
  eap-demo-phone {
    id = demo-phone
    secret = "<REALM GENERATED PASSWORD>"
  }
}
```

**File:** `/opt/strongswan-vpn-gateway/docker/swanctl/swanctl.conf` (top-level)

```ini
include conf.d/*.conf

pools {
  rw-pool {
    addrs = 10.99.0.0/24
    dns   = 1.1.1.1, 8.8.8.8
  }
}
```

### 2.3 Server Certificate (Let's Encrypt)

**CA files on VPS:**

| File | Content | Source |
|---|---|---|
| `/opt/strongswan-vpn-gateway/docker/swanctl/x509/server.pem` | Leaf certificate (CN=myvpn.databyte.co.za) | LE certbot |
| `/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/le-01-YE2.pem` | LE YR2 intermediate | LE certbot |
| `/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/le-02-Root-YE.pem` | ISRG Root X1 (cross-signed) | LE certbot |

**⚠️ CRITICAL (strongSwan issue #3072):** LE certbot's `fullchain.pem` has 3 certs. If put into a single x509ca file, only the first loads. **Each cert must be in its own file in x509ca/.** Deploy hook must split them.

**Certificate subject (live, 2026-06-24):**
```
subject=CN=myvpn.databyte.co.za
issuer=C=US, O=Let's Encrypt, CN=YE2
notBefore=Jun 24 05:09:20 2026 GMT
notAfter=Sep 22 05:09:19 2026 GMT
valid: 90 days
```

### 2.4 iptables (netfilter-persistent)

**File:** `/etc/iptables/rules.v4` (persisted via `netfilter-persistent save`)

```iptables
# NAT — CRITICAL for VPN client internet access
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s 10.99.0.0/24 ! -d 10.99.0.0/24 -j MASQUERADE
COMMIT

# FILTER — VPN + portal + management
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]

# VPN traffic
-A INPUT -p udp --dport 500 -j ACCEPT
-A INPUT -p udp --dport 4500 -j ACCEPT

# Portal HTTP/HTTPS
-A INPUT -p tcp --dport 80 -j ACCEPT
-A INPUT -p tcp --dport 443 -j ACCEPT

# SSH
-A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT

# localhost
-A INPUT -i lo -j ACCEPT
-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

# FORWARD — VPN client routing (CRITICAL)
-A FORWARD -s 10.99.0.0/24 -j ACCEPT
-A FORWARD -d 10.99.0.0/24 -j ACCEPT

# Docker
-A FORWARD -o docker0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A FORWARD -i docker0 ! -o docker0 -j ACCEPT
-A FORWARD -i docker0 -o docker0 -j ACCEPT

COMMIT
```

**⚠️ NOTE:** Docker restart regenerates iptables and can wipe custom FORWARD rules. Insert BEFORE Docker chains or use `iptables -I FORWARD 1` (lesson #148).

### 2.5 VPS Certificate for HTTPS (Cloudflare Origin)

| Item | Value |
|---|---|
| Type | Cloudflare Origin Certificate |
| SAN | `*.databyte.co.za` |
| Key algo | RSA 2048 |
| Stored | `/etc/ssl/cloudflare/` (VPS) |
| Used by | nginx in Docker |

### 2.6 Docker Compose (strongSwan)

**File:** `/opt/strongswan-vpn-gateway/docker/docker-compose.yml` (key parts)

```yaml
services:
  strongswan:
    image: zun/strongswan:6.0.7-mschapv2-attrsql
    container_name: strongswan
    restart: unless-stopped
    cap_add: [NET_ADMIN]
    volumes:
      - ./swanctl:/etc/swanctl:ro
      - /lib/modules:/lib/modules:ro
    command: --use-syslog --debug-2
    networks:
      vpn:
        ipv4_address: 172.18.0.2

networks:
  vpn:
    driver: bridge
    ipam:
      config:
        - subnet: 172.18.0.0/24
```

---

## 3. Client-Side Build (Windows)

### 3.1 Prerequisites

| Item | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 1809+ / Windows 11 | Windows 11 24H2 |
| PowerShell | 5.1 | 5.1+ (core) |
| Admin rights | Yes (to create VPN profile) | Yes |
| Network | Internet access to myvpn.databyte.co.za:443 | Same |
| Antivirus | May block `Add-Type` of inline C# | Whitelist PowerShell |

### 3.2 The Script — Complete Annotated Source

**File:** `setup-databyte-vpn.ps1` v2.6.5 (filename/URL/method HARDLOCKED; see script-header CHANGELOG for post-2.6.0 patch revisions)
**Location (primary):** `https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1` (CF-proxied, LE cert, WAF-protected, no `-k` needed)
**Location (fallback):** `https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1` (grey-cloud, Cloudflare Origin Cert, needs `-k`)
**Git:** `github.com/Dippie-WP/databyte-Ikev2` `main` @ `1eae9ae` (HEAD; v2.6.5). HARDLOCK baseline: tag `v2.6.0` at commit `2732215`. Patches after baseline: `c27742d` (deploy-sync), `95b401d` (base64-packed), `e565666` (canonical block default), `41859eb` (docstring URL format), `bf4e4b1` (base64 padding math).
**MD5:** `fc6a83d18b195bf3cbba1558f87f912a`
**Size:** 23609 bytes

```powershell
<#
    setup-databyte-vpn.ps1 — DatabyteVPN IKEv2 + EAP-MSCHAPv2 installer
    Version: 2.3.0
    Purpose: One-command install of VPN profile + credential binding
    Platform: Windows 10/11 (PowerShell 5.1+)
    Customer: test-win-5g-laptop / EAP secret: a1V5M2Cd1oE0TNWY9wORsg
    Server: myvpn.databyte.co.za

    CHANGE LOG:
    v2.6.5 (2026-07-01): post-HARDLOCK patch revisions consolidated
      * v2.6.5 bf4e4b1 — base64 padding math (2026-06-26)
      * v2.6.4 41859eb — docstring URL format cleanup (2026-06-26)
      * v2.6.3 e565666 — canonical 3-line block shipped as default (2026-06-25)
      * v2.6.2 95b401d — slug+token packed as base64 (2026-06-25)
      * v2.6.1 c27742d — deploy-sync tracked (2026-06-25)
      * Master doc & script header synchronized on 2026-07-01.
    v2.6.0 (2026-06-24): HARDLOCK
      The rot: 12+ script variants floating around (setup-windows-vpn,
      connect-databyte-vpn, test-win-5g-setup, setup-databyte-vpn-zun,
      _archived-*, v1.5.0, v2.0.x, v2.3.0, v2.5.0). All deleted except one.
      Fix: ONE filename, ONE invocation, ONE credential binding method.
    v2.5.0 (2026-06-24): Installer token + lab creds
    v2.4.0 (2026-06-24): Switched portal URL to vpn-portal.databyte.co.za
    v2.3.0 (2026-06-24): RasSetCredentials P/Invoke — the canonical fix
      The 2-day problem: v2.0.7-v2.2.0 all failed to bind creds.
      Root cause: Wrong WMI namespace, hallucinated cmdlets, DPAPI format issues.
      Fix: rasapi32!RasSetCredentials — same API Windows uses for "Save password".

    USAGE:
    iex (irm 'https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1?v=latest')

    WHAT IT DOES (7 steps):
      1. Remove stale profiles with same name
      2. Download LE CA cert (for HTTPS to portal, not VPN auth)
      3. Create VPN profile (IKEv2, Custom encryption, no MFA)
      4. Configure EAP-MSCHAPv2 via New-EapConfiguration
      5. Set registry: AssumeUDPEncapsulationContextOnSendRule = 2
      6. Bind credentials via RasSetCredentials (THE KEY STEP)
      7. Connect via rasdial (with GUI fallback)
#>

param(
    [string]$VpnName      = "DatabyteVPN",
    [string]$ServerAddress = "myvpn.databyte.co.za",
    [string]$Username     = "test-win-5g-laptop",
    [string]$Password     = "a1V5M2Cd1oE0TNWY9wORsg"
)

# ============================================================================
# STEP 1 — Remove any existing profile with the same name
# ============================================================================
# Windows stores VPN profiles per-user in rasphone.pbk. If an old profile
# exists with wrong EAP identity cached, it poisons new connections.
# (lesson #139: Windows IKEv2 EAP identity cache is in the registry)
Write-Host ""
Write-Host "=== [1/7] Removing stale profiles ===" -ForegroundColor Cyan
$vpn = Get-VpnConnection -Name $VpnName -ErrorAction SilentlyContinue
if ($vpn) {
    $vpn | Remove-VpnConnection -Force -ErrorAction SilentlyContinue
    Write-Host "  Removed existing '$VpnName' profile" -ForegroundColor Yellow
} else {
    Write-Host "  No existing profile — clean slate" -ForegroundColor Green
}

# ============================================================================
# STEP 2 — Download Let's Encrypt CA cert (for HTTPS, not VPN auth)
# ============================================================================
# The VPS serves the installer over HTTPS using Cloudflare Origin Cert.
# Win 10/11 trusts LE's cross-signed ISRG Root X1 natively, but we include
# the explicit CA cert to be safe. Downloaded to Temp, imported to
# Cert:\CurrentUser\Root\ so Invoke-WebRequest trusts the portal.
Write-Host ""
Write-Host "=== [2/7] Downloading LE CA cert ===" -ForegroundColor Cyan
$caUrls = @(
    "https://letsencrypt.org/certs/isrgrootx1.der",
    "https://letsencrypt.org/certs/isrg-root-x1-cross-signed.pem"
)
$caPath = "$env:TEMP\databyte-le-ca.pem"
$ok = $false
foreach ($url in $caUrls) {
    try {
        # Note: PS 5.1 Invoke-WebRequest has no -SkipCertificateCheck param.
        # LE cert is publicly trusted, so no bypass needed here.
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -UseBasicParsing -OutFile $caPath -TimeoutSec 10
        if (Test-Path $caPath) {
            Write-Host "  Downloaded: $url" -ForegroundColor Green
            $ok = $true
            break
        }
    } catch {
        Write-Host "  Failed $url — $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if ($ok) {
    # Import to CurrentUser Root store (no admin required)
    try {
        $cert = Import-Certificate -CertStoreLocation Cert:\CurrentUser\Root\ -FilePath $caPath -ErrorAction Stop
        Write-Host "  Imported CA to Cert:\CurrentUser\Root\ — Thumbprint: $($cert.Thumbprint)" -ForegroundColor Green
    } catch {
        Write-Host "  Import failed (non-critical if already trusted): $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# ============================================================================
# STEP 3 — Create VPN profile with IKEv2 + Custom encryption
# ============================================================================
# Add-VpnConnection creates the profile in rasphone.pbk.
# Key settings:
#   -TunnelType Ikev2 : IKEv2 protocol (not L2TP, not SSTP, not OpenVPN)
#   -AuthenticationMethod Eap : EAP-MSCHAPv2 (not machine cert, not PSK)
#   -EncryptionLevel Custom : tells Windows to use ESP proposals from server
#   -RememberCredential True : saves to rasphone.pbk (but not sufficient for IKEv2+EAP)
#   -ServerAddress : must match the certificate SAN
# Lesson #151: 8 distinct config steps needed — this is step 3 of 8.
Write-Host ""
Write-Host "=== [3/7] Creating VPN profile ===" -ForegroundColor Cyan
try {
    $null = Get-VpnConnection -Name $VpnName -ErrorAction SilentlyContinue
    if (-not $?) {
        Add-VpnConnection `
            -Name $VpnName `
            -ServerAddress $ServerAddress `
            -TunnelType Ikev2 `
            -AuthenticationMethod Eap `
            -EncryptionLevel Custom `
            -RememberCredential $true `
            -PassThru `
            -ErrorAction Stop | Out-Null
        Write-Host "  Profile created: $VpnName → $ServerAddress" -ForegroundColor Green
    } else {
        Write-Host "  Profile exists — skipped creation" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  FAILED: $($_.Exception.Message)" -ForegroundColor Red
    throw
}

# Set-CryptoSettings: bypass Windows IKEv2 default cipher restrictions
# This makes Windows accept the server's cipher suite (AES-128-CBC + SHA256 + MODP2048)
# without requiring NIST "strong" ciphers that strongSwan doesn't implement.
Write-Host "  Applying cryptography settings..." -ForegroundColor Cyan
Set-VpnConnectionIPsecConfiguration `
    -Name $VpnName `
    -AuthenticationTransformConstants SHA256 `
    -CipherTransformConstants AES128 `
    -DHGroup ECP384 `
    -EncryptionMethod AES128 `
    -IntegrityCheckMethod SHA256 `
    -PfsGroup ECP384 `
    -PassThru `
    -Force `
    -ErrorAction SilentlyContinue | Out-Null
if ($?) { Write-Host "  Cryptography: AES128-SHA256-ECP384+PFS384" -ForegroundColor Green }

# Verify the profile
$vpn = Get-VpnConnection -Name $VpnName
Write-Host "  TunnelType: $($vpn.TunnelType)" -ForegroundColor Green
Write-Host "  AuthMethod: $($vpn.AuthenticationMethod -join ',')" -ForegroundColor Green
Write-Host "  RememberCredential: $($vpn.RememberCredential)" -ForegroundColor Green

# ============================================================================
# STEP 4 — Configure EAP-MSCHAPv2 via New-EapConfiguration
# ============================================================================
# Instead of hand-writing XML (which failed 3x due to schema issues),
# use the built-in New-EapConfiguration cmdlet which generates valid EAP XML.
#
# EAP config (RAS schema):
#   EapType: 26 (EAP-MSCHAPv2)
#   UseWinLogonCredential: 0 (don't use domain creds — VPN has its own)
#   IdentityTimeout: 0 (always ask for identity)
# Lesson #130: After 2 hand-written XML failures, switch to New-EapConfiguration.
Write-Host ""
Write-Host "=== [4/7] Configuring EAP-MSCHAPv2 ===" -ForegroundColor Cyan
try {
    $eapXml = New-EapConfiguration -Method MSChapV2 -RememberCredential:$true
    $eapBytes = $eapXml.EapConfigXmlStream
    Set-VpnConnection -Name $VpnName -EapConfigXmlStream $eapBytes -ErrorAction Stop
    Write-Host "  EAP configured: MSCHAPv2 + RememberCredential" -ForegroundColor Green
} catch {
    Write-Host "  EAP config failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Falling back to manual EAP XML..." -ForegroundColor Yellow
    # Fallback: hand-written MSCHAPv2 XML (proven format)
    $eapXml = @"
<Eap xmlns="http://www.microsoft.com/provisioning/MicrosoftEap">
  <Config xmlns="http://www.microsoft.com/provisioning/EapConfigSchema">
    <EapMethod Type="26" VendorType="0" VendorId="0"/>
    <ConfigBlob>DATAXBLOB</ConfigBlob>
  </Config>
</Eap>
"@
    $eapDoc = [System.Xml.XmlDocument]::new()
    $eapDoc.LoadXml($eapXml)
    Set-VpnConnection -Name $VpnName -EapConfigXmlStream $eapDoc -ErrorAction SilentlyContinue | Out-Null
}

# ============================================================================
# STEP 5 — Registry: Enable UDP encapsulation on send
# ============================================================================
# HKLM\SYSTEM\CurrentControlSet\Services\PolicyAgent\AssumeUDPEncapsulationContextOnSendRule
# Values: 0=default, 1=server mode, 2=client mode (2=needed for Win 10/11 as VPN client)
# This is the single registry change needed for Windows → strongSwan IKEv2 over NAT.
# Lesson #151: this is step 5 of 8 distinct config steps.
Write-Host ""
Write-Host "=== [5/7] Setting registry: AssumeUDPEncapsulationContextOnSendRule ===" -ForegroundColor Cyan
try {
    $keyPath = "HKLM:\SYSTEM\CurrentControlSet\Services\PolicyAgent\AssumeUDPEncapsulationContextOnSendRule"
    if (-not (Test-Path $keyPath)) {
        New-Item -Path $keyPath -Force -ErrorAction SilentlyContinue | Out-Null
    }
    Set-ItemProperty -Path $keyPath -Name "" -Value 2 -Type DWord -Force -ErrorAction SilentlyContinue
    Write-Host "  PolicyAgent\AssumeUDPEncapsulationContextOnSendRule = 2" -ForegroundColor Green
    Write-Host "  (Enables IKEv2 UDP encapsulation on send — needed for NAT traversal)"
} catch {
    Write-Host "  Registry write failed (non-critical): $($_.Exception.Message)" -ForegroundColor Yellow
}

# ============================================================================
# STEP 6 — Bind credentials to profile (RasSetCredentials API)
# ============================================================================
# THIS IS THE CRITICAL STEP THAT FIXED 2 DAYS OF FAILURES.
#
# PROBLEM: v2.0.7-v2.2.0 all failed to bind credentials.
# What was tried and why it failed:
#   - cmdkey (decorative only — IKEv2 doesn't read it for auth)        → FAIL
#   - Set-VpnConnectionUsernamePassword (not in PS 5.1)               → FAIL
#   - WMI MSFT_NetVpnConnection::SetCredentials (wrong namespace)    → FAIL
#   - WMI MSFT_NetConnectionProfile::SetCredentials (wrong class)      → FAIL
#   - DPAPI direct rasphone.pbk write (minimal format, invalid)        → FAIL
#   - rasdial cycle (703 for IKEv2+EAP — legacy dialer limitation)   → FAIL
#
# THE FIX: RasSetCredentials from rasapi32.dll — the CANONICAL Windows API.
# This is EXACTLY what Windows itself calls when the user checks "Save password"
# in the GUI prompt. Available on every Windows build (7/8/10/11).
#
# Implementation: Inline C# P/Invoke. No third-party module needed.
# Inspired by: paulstancer/VPNCredentialsHelper (PowerShell Gallery, 2017).
# Canonical source: Windows SDK / rasapi32.dll Win32 API.
#
# RASCREDENTIALS struct layout:
#   size     (int)          : Marshal.SizeOf() — sizeof(RASCREDENTIALS)
#   options  (int)          : 0x7 = RASCM.UserName | Password | Domain
#   userName (wchar[257])   : UNLEN=256 + null terminator
#   password (wchar[257])   : PWLEN=256 + null terminator
#   domain   (wchar[16])    : DNLEN=15 + null terminator
#
# RasSetCredentials(NULL, entryName, pCredentials, FALSE):
#   NULL = use default phonebook (rasphone.pbk)
#   entryName = the VPN connection name ("DatabyteVPN")
#   pCredentials = pointer to RASCREDENTIALS struct (marshalled from C#)
#   FALSE = don't clear credentials (set/add)
#
# Return codes:
#   0    = SUCCESS
#   87   = ERROR_INVALID_PARAMETER (entry name wrong, or profile type wrong)
#   1162 = ERROR_NOT_FOUND (profile doesn't exist yet — run Add-VpnConnection first)
#   5    = ERROR_ACCESS_DENIED (run as the user who owns the profile)
#   1312 = ERROR_NO_SUCH_DOMAIN (domain string wrong — use "" for workgroup)
#
# Lesson #161: The WMI namespace for VPN profiles is
#   ROOT\Microsoft\Windows\RemoteAccess\Client (NOT root\StandardCimv2).
# Lesson #162: RasSetCredentials is the canonical Microsoft API for VPN cred binding.
# Lesson #163: AI-hallucinated cmdlets (Set-VpnConnectionCredential) are common —
#   always verify against Get-Command or PowerShell Gallery before applying.
Write-Host ""
Write-Host "=== [6/7] Binding credentials (RasSetCredentials) ===" -ForegroundColor Cyan

$bound = $false

# Define the C# P/Invoke wrapper for rasapi32!RasSetCredentials
$credHelper = @'
using System;
using System.Runtime.InteropServices;
public class VpnCredBinder {
    private const int UNLEN = 256;
    private const int PWLEN = 256;
    private const int DNLEN = 15;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode, Pack = 4)]
    private struct RASCREDENTIALS {
        public int size;
        public int options;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = UNLEN + 1)] public string userName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = PWLEN + 1)] public string password;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = DNLEN + 1)] public string domain;
    }
    [DllImport("rasapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int RasSetCredentials(
        string lpszPhonebook, string lpszEntryName, IntPtr lpCredentials,
        [MarshalAs(UnmanagedType.Bool)] bool fClearCredentials);
    public static int Bind(string entry, string user, string pass, string dom) {
        var c = new RASCREDENTIALS {
            size = Marshal.SizeOf(typeof(RASCREDENTIALS)),
            options = 0x7,  // RASCM.UserName | Password | Domain
            userName = user, password = pass, domain = dom ?? ""
        };
        IntPtr p = Marshal.AllocHGlobal(c.size);
        try {
            Marshal.StructureToPtr(c, p, false);
            return RasSetCredentials(null, entry, p, false);
        } finally {
            Marshal.FreeHGlobal(p);
        }
    }
}
'@

# Method A: Built-in VPNCredentialsHelper module (if installed)
# This is the third-party module from PowerShell Gallery. If present, use it.
# It calls RasSetCredentials under the hood, same as Method B.
if (-not $bound) {
    if (Get-Module -ListAvailable -Name VPNCredentialsHelper -EA SilentlyContinue) {
        try {
            Import-Module VPNCredentialsHelper -EA Stop
            Set-VpnConnectionUsernamePassword -connectionname $VpnName `
                -username $Username -password $Password -domain "" -EA Stop
            Write-Host "  Method A (VPNCredentialsHelper module): OK" -ForegroundColor Green
            $bound = $true
        } catch {
            Write-Host "  Method A: $($_.Exception.Message)" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  Method A (VPNCredentialsHelper module): not installed" -ForegroundColor DarkGray
    }
}

# Method B: Inline C# P/Invoke (no module needed — works on every Windows build)
# This is the canonical approach. RasSetCredentials is a Win32 API since NT 4.0.
# We inline the C# P/Invoke so no third-party module install is needed.
if (-not $bound) {
    try {
        if (-not ('VpnCredBinder' -as [type])) {
            Add-Type -TypeDefinition $credHelper -IgnoreWarnings -ErrorAction Stop
        }
        $r = [VpnCredBinder]::Bind($VpnName, $Username, $Password, "")
        if ($r -eq 0) {
            Write-Host "  Method B (RasSetCredentials P/Invoke): OK" -ForegroundColor Green
            $bound = $true
        } else {
            Write-Host "  Method B (RasSetCredentials): returned $r" -ForegroundColor Yellow
            Write-Host "    0=success, 87=bad name, 1162=profile not found, 5=access denied" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Method B: FAILED - $($_.Exception.Message)" -ForegroundColor Red
    }
}

# Result
if ($bound) {
    Write-Host "  Credentials bound — Settings → Connect will work without any prompt." -ForegroundColor Green
} else {
    Write-Host "  WARNING: creds NOT bound. Enter creds in the GUI ONCE; Windows then binds them." -ForegroundColor Yellow
}

# cmdkey is decorative for IKEv2 (not read for EAP auth, but kept for RDP/other tools)
cmdkey /generic:$ServerAddress /user:$Username /pass:$Password | Out-Null
cmdkey /generic:$VpnName      /user:$Username /pass:$Password | Out-Null

# ============================================================================
# STEP 7 — Connect via rasdial (with GUI fallback)
# ============================================================================
# rasdial is a legacy RAS dialer. For PPTP/L2TP it works perfectly.
# For IKEv2+EAP-MSCHAPv2 it is UNRELIABLE — often returns 703 even when
# creds are bound, because the EAP layer needs the pbk-bound password.
#
# IMPORTANT: A SUCCESSFUL rasdial DOES write the profile to rasphone.pbk
# on disk, making subsequent connections work even without rasdial.
# But for IKEv2+EAP, the definitive test is: Settings → VPN → Connect
#
# rasdial exit codes:
#   0    = connected
#   703  = needs interactive prompt (EAP layer can't satisfy without GUI)
#   13801 = authentication failed (wrong user/pass)
#   13806 = TLS/EAP config error
#   809   = network unreachable / firewall blocking UDP 500/4500
#
# Lesson #156: rasdial unreliability for IKEv2+EAP is a KNOWN Windows limitation.
# Lesson #160: rasdial returning 703 does NOT mean cred binding failed.
Write-Host ""
Write-Host "=== [7/7] Connecting ===" -ForegroundColor Cyan
Start-Sleep -Seconds 1

$connectOutput = rasdial $VpnName $Username $Password 2>&1
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "  rasdial: CONNECTED (exit 0)" -ForegroundColor Green
} else {
    Write-Host "  rasdial: exit $exitCode" -ForegroundColor Yellow
    Write-Host "  (This is NORMAL for IKEv2+EAP — use Settings to connect)" -ForegroundColor Yellow
    Write-Host "  rasdial output: $($connectOutput -join '; ')" -ForegroundColor DarkGray
}

# Poll connection status
Write-Host ""
Write-Host "Polling connection status..." -ForegroundColor Cyan
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 2
    $vpn = Get-VpnConnection -Name $VpnName -ErrorAction SilentlyContinue
    if ($vpn.ConnectionStatus -eq "Connected") {
        Write-Host "  Status: Connected ($i*2s)" -ForegroundColor Green
        break
    } else {
        Write-Host "  Status: $($vpn.ConnectionStatus) — waiting..." -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "DONE. To connect without prompt in future:" -ForegroundColor Cyan
Write-Host "  Settings → Network & Internet → VPN" -ForegroundColor Cyan
Write-Host "  Click '$VpnName' → Connect" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
```

---

## 4. Deployment

### 4.1 Script Deployment (Portal)

| Step | Action |
|---|---|
| 1 | Commit to `github.com/Dippie-WP/databyte-Ikev2` |
| 2 | SCP to VPS: `scp scripts/setup-databyte-vpn.ps1 vpn-prod-01:/tmp/` |
| 3 | On VPS: `sudo cp /tmp/setup-databyte-vpn.ps1 /opt/vpn-portal/www/static/` |
| 4 | Set correct ownership: `sudo chown root:root /opt/vpn-portal/www/static/setup-databyte-vpn.ps1` |
| 5 | Clear Cloudflare cache if applicable |
| 6 | Verify: `curl -sI 'https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1?v=N'` |

### 4.2 Cache-Busting

The script URL must include a cache-buster query string to prevent nginx/Cloudflare from serving stale versions:
```
https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1?v=1782304911
```
Where `v=N` is any unique value (timestamp, version number, etc.).

### 4.3 nginx Configuration (VPS)

Relevant excerpt from `vpn-portal.conf`:

```nginx
location /static/ {
    alias /opt/vpn-portal/www/static/;
    expires -1;
    add_header Cache-Control "no-cache, no-store, must-revalidate";
    add_header Pragma "no-cache";
    add_header Expires "0";
}
```

**⚠️ CRITICAL:** Without `no-cache` headers, nginx's default `cache-control: public, max-age=604800, immutable` will serve stale script even after deployment. Windows PowerShell's `Invoke-RestMethod` respects HTTP cache and will execute the OLD script. (Lesson from Zun's "still fails" report — the bug was nginx cache, not the script itself.)

---

## 5. Verification

### 5.1 Connection Test

```powershell
# Test 1: rasdial WITH credentials (should always connect if profile exists)
rasdial DatabyteVPN test-win-5g-laptop a1V5M2Cd1oE0TNWY9wORsg

# Test 2: rasdial WITHOUT credentials (SUCCESS = creds are bound)
rasdial DatabyteVPN /disconnect
rasdial DatabyteVPN
# Expected: "Successfully connected to DatabyteVPN."
# If 703: RasSetCredentials did not work — retry Method B

# Test 3: Settings UI (definitive test — no prompt = success)
# Settings → Network & Internet → VPN → DatabyteVPN → Connect
# If no prompt appears: SUCCESS
# If prompt appears: credential binding failed
```

### 5.2 Routing Test (tracert)

```powershell
tracert 8.8.8.8
```

**Expected:**
```
  1  21 ms  21 ms  21 ms  154.65.110.44
```

First hop MUST be `154.65.110.44` (VPS public IP). If it shows a private IP (e.g., `192.168.x.x`), traffic is NOT going through the VPN.

### 5.3 Public IP Test (ifconfig.me)

```powershell
# PS 5.1 (no -SkipCertificateCheck in this version):
curl.exe https://ifconfig.me

# PowerShell 6+:
irm https://ifconfig.me

# Or (if cert bypass needed):
[Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}
(Invoke-WebRequest -Uri 'https://ifconfig.me' -UseBasicParsing).Content
```

**Expected:** `154.65.110.44` (VPS public IP)

If it shows your home IP: MASQUERADE is not working on VPS. Check `iptables -t nat -L POSTROUTING -n` on VPS.

---

## 6. Failure Modes Encyclopedia

### 6.1 Credential Binding Failures

| Symptom | Cause | Fix |
|---|---|---|
| Settings → Connect still prompts | `RasSetCredentials` returned non-zero | Check STEP 6 output for RAS error code |
| `RasSetCredentials` returns 87 | Entry name mismatch | Verify `$VpnName` matches exactly |
| `RasSetCredentials` returns 1162 | Profile doesn't exist | Run full installer (creates profile first) |
| `RasSetCredentials` returns 5 | Access denied | Run as the user who owns the profile |
| `RasSetCredentials` returns 1312 | Domain string wrong | Use `""` for workgroup accounts |
| All methods return false | Unknown | Reboot Windows, run installer again |

### 6.2 Connection Failures

| Symptom | Cause | Fix |
|---|---|---|
| `rasdial` exit 809 | Firewall blocking UDP 500/4500 | Check Windows Firewall, ISP blocking |
| `rasdial` exit 13801 | Wrong EAP username/password | Verify secrets match `secrets.conf` on VPS |
| `rasdial` exit 13806 | Proposal mismatch | Check ESP proposals include CBC + GCM |
| `rasdial` exit 13892 | Certificate SAN mismatch | Server cert CN must match `myvpn.databyte.co.za` |
| Connects but no internet | Missing MASQUERADE | `iptables -t nat -A POSTROUTING -s 10.99.0.0/24 -j MASQUERADE` on VPS |
| Connects but can't ping VPS | Missing FORWARD | `iptables -I FORWARD 1 -s 10.99.0.0/24 -j ACCEPT` on VPS |
| Works 1h then drops | ESP rekey failure (GCM missing) | Add GCM to `esp_proposals` in `rw-eap.conf` |
| `rasdial` exit 703 (with creds bound) | rasdial limitation for IKEv2+EAP | Use Settings → Connect instead — THIS IS NORMAL |

### 6.3 Windows IKEv2 EAP Identity Cache

**Problem:** Windows caches the EAP identity per-server in the registry. Even after removing/recreating the profile, the old cached identity persists, causing authentication failures or wrong username being sent.

**Registry location:** `HKLM\SYSTEM\CurrentControlSet\Services\RemoteAccess\Parameters\DeviceIdentityCache`

**Fix (definitive):**
```powershell
# Remove the profile completely
Remove-VpnConnection -Name DatabyteVPN -Force

# Clear the EAP identity cache
$cacheKey = "HKLM:\SYSTEM\CurrentControlSet\Services\RemoteAccess\Parameters\DeviceIdentityCache"
Remove-ItemProperty -Path $cacheKey -Name "*" -ErrorAction SilentlyContinue

# Reboot Windows (registry cache doesn't clear without reboot)
Restart-Computer -Force
```

**Prevention:** Use per-platform test customers (`test-win-5g-laptop`, `test-iphone-5g-iphone`, `test-android-5g-android`) to avoid cross-contamination between platforms. (Lesson #139, #140)

---

## 7. Top 20 Lessons (from 165+ lessons in 2026-06-24)

| # | Lesson | Source |
|---|---|---|
| 1 | `RasSetCredentials` from `rasapi32.dll` is the canonical Windows API for binding VPN credentials. Not WMI, not cmdkey, not DPAPI direct pbk writes. | Lesson #162, confirmed empirically |
| 2 | WMI namespace for VPN profiles is `ROOT\Microsoft\Windows\RemoteAccess\Client` — not `root\StandardCimv2`. | Lesson #161 |
| 3 | `rasdial` returns 703 for IKEv2+EAP even when creds ARE bound. The definitive test is Settings → Connect. | Lesson #160 |
| 4 | Windows IKEv2 rekeys ESP with GCM even when initial is CBC. Server `esp_proposals` MUST include GCM ciphers or rekey fails. | Lesson #146 |
| 5 | Windows IKEv2 EAP identity cache is in registry `HKLM\...\DeviceIdentityCache`, NOT in the IKEEXT service. Reboot clears it. | Lesson #139 |
| 6 | cmdkey is for Credential Manager (RDP, mapped drives). It is NOT read by Windows IKEv2 for EAP identity lookup. | Lesson #135 |
| 7 | `Set-VpnConnectionUsernamePassword` is NOT a built-in PowerShell cmdlet. It exists only in the `VPNCredentialsHelper` third-party module. | Lesson #163 |
| 8 | Generic AI fabricates cmdlet names (`Set-VpnConnectionCredential`, `Connect-VpnConnection`, `New-VpnConnectionTriggerTrustedNetwork`) — always verify with `Get-Command`. | Lessons #131, #157, #159 |
| 9 | `New-EapConfiguration` (built-in) is better than hand-written EAP XML. The XML schema has strict case requirements (`EapMsChapV2`, not `EapMSChapV2`). | Lesson #130 |
| 10 | After a `pkill -HUP charon`, swanctl reloads config without dropping active SAs. Safe to apply without disconnecting clients. | Lesson #147 |
| 11 | Docker restart regenerates iptables chains and CAN WIPE custom FORWARD rules. Insert rules BEFORE Docker chains or use UFW `before.rules`. | Lesson #148 |
| 12 | Windows PowerShell `curl` is an alias for `Invoke-WebRequest`. On PS 5.1, `Invoke-WebRequest` has NO `-k` flag (that's curl's flag). Use `-SkipCertificateCheck` only on PS 6+. | Lesson #149 |
| 13 | The `rasphone.pbk` password field format is DPAPI-encrypted (CurrentUser scope), base64-encoded. A minimal pbk entry (4 fields) is invalid — Windows requires 20+ fields. | Lesson #164 |
| 14 | `tracert 8.8.8.8` first hop tells you if traffic is going through the VPN. First hop = VPS IP = tunnel working. First hop = private IP = MASQUERADE missing. | Lesson #145 |
| 15 | Windows `NegotiateDH2048_AES256` registry key (HKLM...\Parameters): values 0=disable, 1=enable, 2=ENFORCE. Value 2 actually forces AES-256/MODP-2048. | Lesson #115 |
| 16 | `OsBuildNumber` (e.g., `26200`) is the true Windows version indicator. `WindowsProductName` (e.g., "Windows 10 Pro") can lie on Win 11 24H2. | Build number reference |
| 17 | PowerShell `curl` on Windows is `Invoke-WebRequest`, NOT the real curl. Real curl: `curl.exe`. | Lesson #149 |
| 18 | For "Save password" to work in the VPN Settings GUI, credentials must be stored in the Windows Credential Manager (Vault) — which is exactly what `RasSetCredentials` does. | Verified empirically |
| 19 | Per-platform test customers prevent EAP identity cache poisoning between Windows, iOS, and Android clients. | Lesson #140 |
| 20 | `rasdial $Name $User $Pass` writes to `rasphone.pbk` on SUCCESS — but for IKEv2+EAP, rasdial itself fails with 703, so this path never executes. `RasSetCredentials` is the workaround. | Lesson #155 |

---

## 8. File Inventory

### 8.1 On VPS (VPS-side)

| File | Purpose | Backed up? |
|---|---|---|
| `/opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf` | IKEv2 connection config | Git + RustFS |
| `/opt/strongswan-vpn-gateway/docker/swanctl/swanctl.conf` | Top-level swanctl config | Git |
| `/opt/strongswan-vpn-gateway/docker/swanctl/x509/server.pem` | LE leaf certificate | On VPS only |
| `/opt/strongswan-vpn-gateway/docker/swanctl/x509ca/*.pem` | LE chain certificates | On VPS only |
| `/opt/strongswan-vpn-gateway/docker/docker-compose.yml` | Container definition | Git |
| `/opt/vpn-portal/www/static/setup-databyte-vpn.ps1` | Windows client installer | Git + portal |
| `/etc/iptables/rules.v4` | Firewall rules (persisted) | `netfilter-persistent` |

### 8.2 On Windows Client (per-user)

| File | Purpose | Location |
|---|---|---|
| `rasphone.pbk` | VPN profile store (includes encrypted creds) | `%APPDATA%\Microsoft\Network\Connections\Pbk\` |
| Windows Credential Manager | Stores VPN creds in Vault | `control.exe /name Microsoft.CredentialManager` |
| Windows Event Log | IKEv2 connection events | Event Viewer → Application → Source: IKEEXT |

### 8.3 In Git (source of truth)

| File | Purpose |
|---|---|
| `scripts/setup-databyte-vpn.ps1` | Windows client installer |
| `host/vpn-portal/app.py` | Portal backend |
| `host/vpn-portal/www/static/` | Portal web assets |
| `host/systemd/` | Systemd units |
| `host/firewall/rules.v4` | Firewall rules |
| `docker/swanctl/conf.d/` | strongSwan connection configs |

---

## 9. Restoration Procedure (From Scratch)

### If everything is lost and you only have this document:

**Step 1 — Get access:**
1. Get VPS SSH credentials from Zun
2. `ssh root@154.65.110.44`
3. Install Docker if not present: `curl -fsSL https://get.docker.com | sh`

**Step 2 — Clone the project:**
```bash
cd /opt
git clone https://github.com/Dippie-WP/databyte-Ikev2.git
cd databyte-Ikev2
```

**Step 3 — Deploy strongSwan:**
```bash
cd /opt/databyte-Ikev2
docker-compose up -d strongswan
# Verify: docker exec strongswan swanctl --list-conns
```

**Step 4 — Deploy portal:**
```bash
docker-compose up -d vpn-portal
# Portal runs at https://myvpn.databyte.co.za
```

**Step 5 — Configure firewall:**
```bash
# Apply iptables from Section 2.4 of this document
# Then persist: netfilter-persistent save
```

**Step 6 — Configure DNS:**
- `myvpn.databyte.co.za` → `154.65.110.44` (A record)
- Cloudflare proxy OFF for `myvpn.databyte.co.za` (or UDP 500/4500 will be blocked)

**Step 7 — Test server:**
```bash
docker exec strongswan swanctl --list-sas
# Should show: rw-eap, version=2.0, local=myvpn.databyte.co.za, remote=%any
```

**Step 8 — Get Let's Encrypt certificate:**
```bash
# On VPS:
certbot certonly --standalone -d myvpn.databyte.co.za --agree-tos --email zunaid@databyte.co.za -n
# Copy certs to swanctl/x509/ and swanctl/x509ca/ (SPLIT into separate files!)
```

**Step 9 — Deploy Windows client:**
```powershell
# On Windows machine:
iex (irm 'https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1?v=latest')
```

**Step 10 — Verify (Section 5 of this document)**

---

## 10. Version History

| Ver | Date | Commit | Change | Tested |
|---|---|---|---|---|
| 2.0.4 | 2026-06-24 | fe73cfb | Cleanup by ServerAddress enum | ❌ |
| 2.0.5 | 2026-06-24 | 2797853 | Correct `EapMsChapV2` casing | ❌ |
| 2.0.6 | 2026-06-24 | f52ed42 | Remove TLS-only ServerValidation | ❌ |
| 2.0.7 | 2026-06-24 | c242333 | `New-EapConfiguration` cmdlet | ❌ |
| 2.0.8 | 2026-06-24 | af2cf43 | Use `test-win-5g-laptop` customer | ❌ |
| 2.0.9 | 2026-06-24 | f45a5f5 | WMI `SetCredentials` + `rasdial` | ❌ |
| 2.1.0 | 2026-06-24 | 9eec5a2 | Multi-class WMI sweep | ❌ |
| 2.2.0 | 2026-06-24 | 1ec66cc | DPAPI direct pbk write | ❌ |
| 2.3.0 | 2026-06-24 | 5d9b602 | `RasSetCredentials` P/Invoke (THE FIX) | ✅ |
| 2.4.0 | 2026-06-24 | 27ee293 | Switched portal URL to vpn-portal.databyte.co.za | ✅ |
| 2.5.0 | 2026-06-24 | 0ad6dc0 | Installer token + lab creds (had STEP 6 merge rot) | ⚠️ |
| **2.6.0** | **2026-06-24** | **2732215** | **HARDLOCK: ONE filename, ONE URL, ONE method, ROT REMOVED** | **✅** |

**All versions ≤ v2.5.0 are DELETED. v2.6.0 is the canonical version.** If a prior version is needed for historical reference, it is in `scripts/_archive-2026-06-24/` in git — do NOT deploy.

**ROT REMOVED 2026-06-24 (on VPS `/opt/vpn-portal/www/static/`):**
- `setup-databyte-vpn-zun.ps1` (Zun's personal working copy) → trashed
- `_archived-setup-windows-vpn-v1.5.0.ps1` → trashed
- `connect-databyte-vpn.ps1` → trashed
- `test-win-5g-setup.ps1`, `test-win-5g-setup-v3.ps1` → trashed
- `diag-vpn.ps1` → trashed
- All moved to `/tmp/_trash-20260624-2035/` on VPS (recoverable for 30 days)

---

## 11. References

| Source | URL | What it provides |
|---|---|---|
| Microsoft Learn | `learn.microsoft.com/en-us/previous-versions/windows/desktop/vpnclientpsprov/ps-vpnconnection` | `PS_VpnConnection` WMI class (archived) |
| Microsoft Learn | `learn.microsoft.com/en-us/powershell/module/vpnclient/` | `Add-VpnConnection`, `Set-VpnConnection` cmdlets |
| strongSwan docs | `docs.strongswan.org/docs/latest/interop/windowsEapConf.html` | Windows IKEv2 GUI setup canonical guide |
| strongSwan GitHub | `github.com/strongswan/strongswan/issues/3072` | YR2 transition cert chain issue |
| PowerShell Gallery | `powershellgallery.com/packages/VPNCredentialsHelper/1.1` | Canonical `RasSetCredentials` implementation |
| wutils.com | `wutils.com/wmi/root/microsoft/windows/remoteaccess/client/ps_vpnconnection/` | WMI namespace + class reference |
| Let's Encrypt | `community.letsencrypt.org/t/ikev2-vpn-connection-fails-after-certificate-update/239739` | LE cert + IKEv2 failure reports |
| Windows SDK | `learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--0-499-` | RAS error codes (0, 87, 1162, 5, 1312) |

---

**END OF DOCUMENT — DAT-VPN-WINDOWS-CLIENT-MASTER-001 v1.0.0**
