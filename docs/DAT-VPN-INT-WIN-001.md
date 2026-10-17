# Windows IKEv2 + EAP-MSCHAPv2 VPN Client: Complete Build Manual

Generic build manual for a Windows IKEv2 + EAP-MSCHAPv2 VPN client. Covers server-side strongSwan + FreeRADIUS + MariaDB configuration, the Windows PowerShell installer, iptables, Docker compose, failure-mode troubleshooting, and end-to-end verification.

> **Generic note:** This doc was originally databyte-internal (v1.1.0, 2026-07-07). It has been hand-curated 2026-10-17 for generic use — all databyte branding, dates, internal ticket references, and deployment-specific details removed. Replace all `<placeholder>` values with your own.

---

## THE CANONICAL 3-LINE BLOCK (one way to install)

Open PowerShell as Administrator. Copy/paste this. Press Enter three times.

```powershell
# Primary (LE cert, no -k):
curl.exe -o $env:TEMP\setup.ps1 https://<your-portal-domain>/static/setup-vpn.ps1
& $env:TEMP\setup.ps1
rasdial <vpn-connection-name>
```

**Fallback (VPN host direct, with `-k` for self-signed Cloudflare Origin Cert):**
```powershell
curl.exe -k -o $env:TEMP\setup.ps1 https://<your-vpn-hostname>/static/setup-vpn.ps1
& $env:TEMP\setup.ps1
rasdial <vpn-connection-name>
```

**General rules:**
- ONE canonical installer script — name it consistently (e.g. `setup-vpn.ps1` or your own)
- TWO delivery URLs: portal-fronted (primary) and VPN-host-direct (fallback, needs `-k`)
- ONE credential binding method — `RasSetCredentials` from `rasapi32.dll` is the canonical Win32 API
- For self-serve portal flow: customer supplies creds at install via GUI prompt
- For operator-assisted onboarding: bake creds per-customer into a separate `<customer>-<device>.ps1`
- Verify file size, MD5, and version on your own deploy — values change over time

| Property | Generic canonical | Per-customer baked |
|---|---|---|
| Canonical filename | `setup-vpn.ps1` (rename for your deploy) | `setup-vpn-<customer>-<device>.ps1` |
| Version | Verify on your deploy | Verify on your deploy |
| MD5 | Verify on your deploy | Verify per customer (changes when creds are baked) |
| Size | Verify on your deploy | Verify on your deploy |
| Primary URL | `https://<your-portal-domain>/static/setup-vpn.ps1` | `https://<your-portal-domain>/static/baked/setup-vpn-<customer>-<device>.ps1` |
| Fallback URL | `https://<your-vpn-hostname>/static/setup-vpn.ps1` (needs `-k`) | n/a (portal only) |
| Connection name | `<vpn-connection-name>` (e.g. `MyVPN`) | Same as generic |
| Server | `<your-vpn-hostname>` → `<your-public-ip>` | Same |
| Credentials source | Customer supplies via prompt | Baked into file at operator edit time |

---

## 0. Purpose

This document is the **complete, authoritative, from-scratch build manual** for a self-hosted IKEv2 + EAP-MSCHAPv2 Windows VPN client. It is designed to enable reconstruction of the entire solution — server and client — from bare metal, even if all prior context is lost.

**What this document IS:**
- A complete architectural reference (server + client)
- A step-by-step rebuild procedure
- A failure-mode encyclopedia with fixes
- A lesson-bank distilled from extensive Windows IKEv2 debugging

**What this document IS NOT:**
- A marketing document
- A user-facing guide
- A theoretical design — every line has been tested on live hardware

**Empirical validation (representative test):**
```
rasdial <vpn-connection-name>          ← NO CREDENTIALS PASSED
→ Successfully connected to <vpn-connection-name>. Command completed successfully.

tracert 8.8.8.8
  1  <your-public-ip>                    ← VPS as first hop
```

---

## 1. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Windows Client (any Win 10 1809+ / Win 11)                 │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ PowerShell script (setup-vpn.ps1 generic, OR         │   │
│  │  setup-vpn-<customer>-<device>.ps1 baked)           │   │
│  │   STEP 1: Remove stale profiles                       │   │
│  │   STEP 2: Download Let's Encrypt / ISRG CA             │   │
│  │   STEP 3: New-VpnConnection (IKEv2, Custom crypto)    │   │
│  │   STEP 4: New-EapConfiguration (EAP-MSCHAPv2)        │   │
│  │   STEP 5: Registry (AssumeUDPEncapsulationContext=2) │   │
│  │   STEP 6: RasSetCredentials (bind creds via Win32)   │   │
│  │   STEP 7: Connect + poll                             │   │
│  └────────────────────┬──────────────────────────────────┘   │
│                       │ UDP 500/4500                         │
└───────────────────────┼─────────────────────────────────────┘
                        │ <your-router> → Internet
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  Internet                                                   │
└─────────────────────────────────────────────────────────────┘
                        │
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  VPS (<your-vps-provider>, <your-public-ip>, Debian 13)   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ strongSwan container (charon, Docker)                 │   │
│  │   • EAP-MSCHAPv2 auth                                │   │
│  │   • LE certificate (CN=<your-vpn-hostname>)          │   │
│  │   • VPN pool: 10.99.0.0/24 (or your pool)            │   │
│  │   • ESP proposals: CBC + GCM (Windows rekeys GCM)     │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ iptables (netfilter-persistent)                      │   │
│  │   • NAT MASQUERADE: <your-vpn-pool> → <eth0>         │   │
│  │   • FORWARD ACCEPT: <your-vpn-pool>                 │   │
│  │   • INPUT: UDP 500/4500, TCP 80/443, SSH 22         │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ nginx (TLS termination)                              │   │
│  │   • Serves: setup-vpn.ps1, CA certs, portal assets  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                        │
                        ↓ Internet (MASQUERADED traffic)
┌─────────────────────────────────────────────────────────────┐
│  Internet destinations (Google, etc.)                      │
│  Source IP appears as <your-public-ip> (VPS public IP)     │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

| Decision | Rationale | Source |
|---|---|---|
| EAP-MSCHAPv2 (not certificate-based) | Windows native support, no PKI required on client | strongSwan docs |
| Let's Encrypt certificate on VPS | Trusted by all platforms, no CA cert install needed on client | strongSwan ios.html |
| AES-CBC + GCM ESP proposals | Windows negotiates CBC on init, GCM on rekey. Both required | strongSwan docs |
| `RasSetCredentials` for credential binding | Canonical Windows API (rasapi32.dll), not WMI, not cmdkey | Microsoft SDK |
| GCM ESP required for rekey | Windows IKEv2 uses GCM for rekey even when initial is CBC | strongSwan docs |
| Per-platform test customers | Avoids Windows IKEv2 EAP identity cache poisoning | Microsoft IKEEXT docs |
| UDP port 4500 (not 500) for NAT traversal | Modern Windows IKEv2 uses 4500 primarily | Standard practice |

---

## 2. Server-Side Build (VPS)

### 2.1 Prerequisites

| Item | Value |
|---|---|
| VPS | Any Debian 13 host, public IP `<your-public-ip>` |
| Docker | Installed on host |
| Domain | `<your-vpn-hostname>` (e.g. `vpn.example.com`) pointing to VPS IP |
| DNS provider | Your choice (Cloudflare shown in examples; see CLOUDFLARE-DNS.md) |
| Firewall | UDP 500, UDP 4500, TCP 80, TCP 443 open on VPS |
| Origin Certificate | `*.<your-domain>` SAN, stored on VPS (if using Cloudflare) |

### 2.2 strongSwan Configuration

**File:** `<your-install-root>/docker/swanctl/conf.d/rw-eap.conf`

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
      id = "<your-vpn-hostname>"
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
        # Without GCM: "NO_PROPOSAL_CHOSEN" on rekey
        esp_proposals = aes256-sha256-modp2048-ecp256,aes128-sha256-modp2048-ecp256,aes256-sha1-modp2048,aes128-sha1-modp2048,aes256gcm16-ecp256,aes128gcm16-ecp256,aes256gcm16,aes128gcm16
      }
    }
  }
}

# secrets — one per VPN customer
secrets {
  eap-<customer-1> {
    id = <customer-1>
    secret = "<password-1>"
  }
  eap-<customer-2> {
    id = <customer-2>
    secret = "<password-2>"
  }
  # ... add one block per customer
}
```

**File:** `<your-install-root>/docker/swanctl/swanctl.conf` (top-level)

```ini
include conf.d/*.conf

pools {
  rw-pool {
    addrs = <your-vpn-pool>  # e.g. 10.99.0.0/24
    dns   = 1.1.1.1, 8.8.8.8
  }
}
```

### 2.3 Server Certificate (Let's Encrypt)

**CA files on VPS:**

| File | Content | Source |
|---|---|---|
| `<your-install-root>/docker/swanctl/x509/server.pem` | Leaf certificate (CN=`<your-vpn-hostname>`) | LE certbot |
| `<your-install-root>/docker/swanctl/x509ca/le-intermediate.pem` | LE intermediate | LE certbot |
| `<your-install-root>/docker/swanctl/x509ca/isrg-root.pem` | ISRG Root X1 (cross-signed) | LE certbot |

> **CRITICAL (strongSwan issue #3072):** LE certbot's `fullchain.pem` has 3 certs. If put into a single x509ca file, only the first loads. **Each cert must be in its own file in x509ca/.** Deploy hook must split them.

### 2.4 iptables (netfilter-persistent)

**File:** `/etc/iptables/rules.v4` (persisted via `netfilter-persistent save`)

```iptables
# NAT — CRITICAL for VPN client internet access
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -s <your-vpn-pool> ! -d <your-vpn-pool> -j MASQUERADE
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
-A FORWARD -s <your-vpn-pool> -j ACCEPT
-A FORWARD -d <your-vpn-pool> -j ACCEPT

# Docker
-A FORWARD -o docker0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A FORWARD -i docker0 ! -o docker0 -j ACCEPT
-A FORWARD -i docker0 -o docker0 -j ACCEPT

COMMIT
```

> **NOTE:** Docker restart regenerates iptables chains and CAN WIPE custom FORWARD rules. Insert rules BEFORE Docker chains or use UFW `before.rules`.

### 2.5 VPS Certificate for HTTPS (if using Cloudflare)

| Item | Value |
|---|---|
| Type | Cloudflare Origin Certificate |
| SAN | `*.<your-domain>` |
| Key algo | RSA 2048 |
| Stored | `/etc/ssl/cloudflare/` (VPS) |
| Used by | nginx (TLS termination) |

### 2.6 Docker Compose (strongSwan)

**File:** `<your-install-root>/docker/docker-compose.yml`

```yaml
services:
  strongswan:
    image: <your-namespace>/strongswan:6.0.7-mschapv2-attrsql
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
| Network | Internet access to `<your-portal-domain>`:443 | Same |
| Antivirus | May block `Add-Type` of inline C# | Whitelist PowerShell |

### 3.2 The Script — Complete Annotated Source

**File:** `setup-vpn.ps1` (canonical generic installer)
**Location (primary):** `https://<your-portal-domain>/static/setup-vpn.ps1` (CDN-proxied, LE cert)
**Location (fallback):** `https://<your-vpn-hostname>/static/setup-vpn.ps1` (grey-cloud / direct, needs `-k`)

```powershell
<#
    setup-vpn.ps1 — Self-hosted IKEv2 + EAP-MSCHAPv2 VPN installer
    Platform: Windows 10/11 (PowerShell 5.1+)
    Server: <your-vpn-hostname>

    USAGE:
    iex (irm 'https://<your-portal-domain>/static/setup-vpn.ps1?v=latest')

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
    [string]$VpnName      = "<vpn-connection-name>",
    [string]$ServerAddress = "<your-vpn-hostname>",
    [string]$Username      = "<your-username>",
    [string]$Password      = "<your-password>"
)

# ============================================================================
# STEP 1 — Remove any existing profile with the same name
# ============================================================================
# Windows stores VPN profiles per-user in rasphone.pbk. If an old profile
# exists with wrong EAP identity cached, it poisons new connections.
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
# The VPS serves the installer over HTTPS using a CA-signed cert.
# Win 10/11 trusts LE's cross-signed ISRG Root X1 natively, but we include
# the explicit CA cert to be safe. Downloaded to Temp, imported to
# Cert:\CurrentUser\Root\ so Invoke-WebRequest trusts the portal.
Write-Host ""
Write-Host "=== [2/7] Downloading LE CA cert ===" -ForegroundColor Cyan
$caUrls = @(
    "https://letsencrypt.org/certs/isrgrootx1.der",
    "https://letsencrypt.org/certs/isrg-root-x1-cross-signed.pem"
)
$caPath = "$env:TEMP\isrg-root-x1.pem"
$ok = $false
foreach ($url in $caUrls) {
    try {
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

# Set crypto settings: bypass Windows IKEv2 default cipher restrictions
# This makes Windows accept the server's cipher suite
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
# Instead of hand-writing XML (which fails due to schema case requirements),
# use the built-in New-EapConfiguration cmdlet which generates valid EAP XML.
#
# EAP config (RAS schema):
#   EapType: 26 (EAP-MSCHAPv2)
#   UseWinLogonCredential: 0 (don't use domain creds — VPN has its own)
#   IdentityTimeout: 0 (always ask for identity)
Write-Host ""
Write-Host "=== [4/7] Configuring EAP-MSCHAPv2 ===" -ForegroundColor Cyan
try {
    $eapXml = New-EapConfiguration -Method MSChapV2 -RememberCredential:$true
    $eapBytes = $eapXml.EapConfigXmlStream
    Set-VpnConnection -Name $VpnName -EapConfigXmlStream $eapBytes -ErrorAction Stop
    Write-Host "  EAP configured: MSCHAPv2 + RememberCredential" -ForegroundColor Green
} catch {
    Write-Host "  EAP config failed: $($_.Exception.Message)" -ForegroundColor Red

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
# THIS IS THE CRITICAL STEP.
#
# PROBLEM: Multiple approaches fail:
#   - cmdkey (decorative only — IKEv2 doesn't read it for EAP auth)
#   - Set-VpnConnectionUsernamePassword (not in PS 5.1)
#   - WMI MSFT_NetVpnConnection::SetCredentials (wrong namespace)
#   - WMI MSFT_NetConnectionProfile::SetCredentials (wrong class)
#   - DPAPI direct rasphone.pbk write (minimal format, invalid)
#   - rasdial cycle (703 for IKEv2+EAP — legacy dialer limitation)
#
# THE FIX: RasSetCredentials from rasapi32.dll — the CANONICAL Windows API.
# This is EXACTLY what Windows itself calls when the user checks "Save password"
# in the GUI prompt. Available on every Windows build (7/8/10/11).
#
# Implementation: Inline C# P/Invoke. No third-party module needed.
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
#   entryName = the VPN connection name
#   pCredentials = pointer to RASCREDENTIALS struct (marshalled from C#)
#   FALSE = don't clear credentials (set/add)
#
# Return codes:
#   0    = SUCCESS
#   87   = ERROR_INVALID_PARAMETER (entry name wrong, or profile type wrong)
#   1162 = ERROR_NOT_FOUND (profile doesn't exist yet — run Add-VpnConnection first)
#   5    = ERROR_ACCESS_DENIED (run as the user who owns the profile)
#   1312 = ERROR_NO_SUCH_DOMAIN (domain string wrong — use "" for workgroup)
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
# But for IKEv2+EAP, the definitive test is: Settings → Connect
#
# rasdial exit codes:
#   0    = connected
#   703  = needs interactive prompt (EAP layer can't satisfy without GUI)
#   13801 = authentication failed (wrong user/pass)
#   13806 = TLS/EAP config error
#   809   = network unreachable / firewall blocking UDP 500/4500
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
| 1 | Commit to your repo |
| 2 | SCP to VPS: `scp setup-vpn.ps1 <your-hostname>:/tmp/` |
| 3 | On VPS: `sudo cp /tmp/setup-vpn.ps1 <your-portal-web-root>/static/` |
| 4 | Set correct ownership: `sudo chown <web-user>:<web-group> <your-portal-web-root>/static/setup-vpn.ps1` |
| 5 | Clear CDN cache if applicable |
| 6 | Verify: `curl -sI 'https://<your-portal-domain>/static/setup-vpn.ps1?v=N'` |

### 4.2 Cache-Busting

The script URL must include a cache-buster query string to prevent nginx/CDN from serving stale versions:
```
https://<your-portal-domain>/static/setup-vpn.ps1?v=<unique-value>
```
Where `v=N` is any unique value (timestamp, version number, etc.).

### 4.3 nginx Configuration (VPS)

Relevant excerpt from `vpn-portal.conf`:

```nginx
location /static/ {
    alias <your-portal-web-root>/static/;
    expires -1;
    add_header Cache-Control "no-cache, no-store, must-revalidate";
    add_header Pragma "no-cache";
    add_header Expires "0";
}
```

> **CRITICAL:** Without `no-cache` headers, nginx's default cache behavior will serve stale script even after deployment. Windows PowerShell's `Invoke-RestMethod` respects HTTP cache and will execute the OLD script.

---

## 5. Verification

### 5.1 Connection Test

```powershell
# Test 1: rasdial WITH credentials (should always connect if profile exists)
rasdial <vpn-connection-name> <your-username> <your-password>

# Test 2: rasdial WITHOUT credentials (SUCCESS = creds are bound)
rasdial <vpn-connection-name> /disconnect
rasdial <vpn-connection-name>
# Expected: "Successfully connected to <vpn-connection-name>."
# If 703: RasSetCredentials did not work — retry Method B

# Test 3: Settings UI (definitive test — no prompt = success)
# Settings → Network & Internet → VPN → <vpn-connection-name> → Connect
# If no prompt appears: SUCCESS
# If prompt appears: credential binding failed
```

### 5.2 Routing Test (tracert)

```powershell
tracert 8.8.8.8
```

**Expected:**
```
  1  <your-public-ip>
```

First hop MUST be `<your-public-ip>` (VPS public IP). If it shows a private IP (e.g., `192.168.x.x`), traffic is NOT going through the VPN.

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

**Expected:** `<your-public-ip>` (VPS public IP)

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
| `rasdial` exit 13892 | Certificate SAN mismatch | Server cert CN must match `<your-vpn-hostname>` |
| Connects but no internet | Missing MASQUERADE | `iptables -t nat -A POSTROUTING -s <your-vpn-pool> -j MASQUERADE` on VPS |
| Connects but can't ping VPS | Missing FORWARD | `iptables -I FORWARD 1 -s <your-vpn-pool> -j ACCEPT` on VPS |
| Works 1h then drops | ESP rekey failure (GCM missing) | Add GCM to `esp_proposals` in `rw-eap.conf` |
| `rasdial` exit 703 (with creds bound) | rasdial limitation for IKEv2+EAP | Use Settings → Connect instead — THIS IS NORMAL |

### 6.3 Windows IKEv2 EAP Identity Cache

**Problem:** Windows caches the EAP identity per-server in the registry. Even after removing/recreating the profile, the old cached identity persists, causing authentication failures or wrong username being sent.

**Registry location:** `HKLM\SYSTEM\CurrentControlSet\Services\RemoteAccess\Parameters\DeviceIdentityCache`

**Fix (definitive):**
```powershell
# Remove the profile completely
Remove-VpnConnection -Name <vpn-connection-name> -Force

# Clear the EAP identity cache
$cacheKey = "HKLM:\SYSTEM\CurrentControlSet\Services\RemoteAccess\Parameters\DeviceIdentityCache"
Remove-ItemProperty -Path $cacheKey -Name "*" -ErrorAction SilentlyContinue

# Reboot Windows (registry cache doesn't clear without reboot)
Restart-Computer -Force
```

**Prevention:** Use per-platform test customers to avoid cross-contamination between platforms.

---

## 7. Top 20 Lessons (from extensive Windows IKEv2 debugging)

| # | Lesson | Source |
|---|---|---|
| 1 | `RasSetCredentials` from `rasapi32.dll` is the canonical Windows API for binding VPN credentials. Not WMI, not cmdkey, not DPAPI direct pbk writes. | Microsoft SDK, confirmed empirically |
| 2 | WMI namespace for VPN profiles is `ROOT\Microsoft\Windows\RemoteAccess\Client` — not `root\StandardCimv2`. | Microsoft docs |
| 3 | `rasdial` returns 703 for IKEv2+EAP even when creds ARE bound. The definitive test is Settings → Connect. | Empirically observed |
| 4 | Windows IKEv2 rekeys ESP with GCM even when initial is CBC. Server `esp_proposals` MUST include GCM ciphers or rekey fails. | strongSwan docs |
| 5 | Windows IKEv2 EAP identity cache is in registry `HKLM\...\DeviceIdentityCache`, NOT in the IKEEXT service. Reboot clears it. | Microsoft IKEEXT docs |
| 6 | cmdkey is for Credential Manager (RDP, mapped drives). It is NOT read by Windows IKEv2 for EAP identity lookup. | Empirically observed |
| 7 | `Set-VpnConnectionUsernamePassword` is NOT a built-in PowerShell cmdlet. It exists only in the `VPNCredentialsHelper` third-party module. | Microsoft docs |
| 8 | Generic AI fabricates cmdlet names — always verify with `Get-Command` before applying. | Empirically observed |
| 9 | `New-EapConfiguration` (built-in) is better than hand-written EAP XML. The XML schema has strict case requirements (`EapMsChapV2`, not `EapMSChapV2`). | Microsoft docs |
| 10 | After a `pkill -HUP charon`, swanctl reloads config without dropping active SAs. Safe to apply without disconnecting clients. | strongSwan docs |
| 11 | Docker restart regenerates iptables chains and CAN WIPE custom FORWARD rules. Insert rules BEFORE Docker chains or use UFW `before.rules`. | Empirically observed |
| 12 | Windows PowerShell `curl` is an alias for `Invoke-WebRequest`. On PS 5.1, `Invoke-WebRequest` has NO `-k` flag (that's curl's flag). Use `-SkipCertificateCheck` only on PS 6+. | Empirically observed |
| 13 | The `rasphone.pbk` password field format is DPAPI-encrypted (CurrentUser scope), base64-encoded. A minimal pbk entry (4 fields) is invalid — Windows requires 20+ fields. | Empirically observed |
| 14 | `tracert 8.8.8.8` first hop tells you if traffic is going through the VPN. First hop = VPS IP = tunnel working. First hop = private IP = MASQUERADE missing. | Standard diagnostic |
| 15 | Windows `NegotiateDH2048_AES256` registry key (HKLM...\Parameters): values 0=disable, 1=enable, 2=ENFORCE. Value 2 actually forces AES-256/MODP-2048. | Microsoft docs |
| 16 | `OsBuildNumber` (e.g., `26200`) is the true Windows version indicator. `WindowsProductName` (e.g., "Windows 10 Pro") can lie on Win 11 24H2. | Empirically observed |
| 17 | PowerShell `curl` on Windows is `Invoke-WebRequest`, NOT the real curl. Real curl: `curl.exe`. | Empirically observed |
| 18 | For "Save password" to work in the VPN Settings GUI, credentials must be stored in the Windows Credential Manager (Vault) — which is exactly what `RasSetCredentials` does. | Empirically observed |
| 19 | Per-platform test customers prevent EAP identity cache poisoning between Windows, iOS, and Android clients. | Empirically observed |
| 20 | `rasdial $Name $User $Pass` writes to `rasphone.pbk` on SUCCESS — but for IKEv2+EAP, rasdial itself fails with 703, so this path never executes. `RasSetCredentials` is the workaround. | Empirically observed |

---

## 8. File Inventory

### 8.1 On VPS (server-side)

| File | Purpose | Backed up? |
|---|---|---|
| `<your-install-root>/docker/swanctl/conf.d/rw-eap.conf` | IKEv2 connection config | Git + your backup |
| `<your-install-root>/docker/swanctl/swanctl.conf` | Top-level swanctl config | Git |
| `<your-install-root>/docker/swanctl/x509/server.pem` | LE leaf certificate | On VPS only |
| `<your-install-root>/docker/swanctl/x509ca/*.pem` | LE chain certificates | On VPS only |
| `<your-install-root>/docker/docker-compose.yml` | Container definition | Git |
| `<your-portal-web-root>/static/setup-vpn.ps1` | Windows client installer | Git + portal |
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
| `setup-vpn.ps1` | Windows client installer |
| `host/vpn-portal/app.py` | Portal backend |
| `host/vpn-portal/www/static/` | Portal web assets |
| `host/systemd/` | Systemd units |
| `host/firewall/rules.v4` | Firewall rules |
| `docker/swanctl/conf.d/` | strongSwan connection configs |

---

## 9. Restoration Procedure (From Scratch)

### If everything is lost and you only have this document:

**Step 1 — Get access:**
1. Get VPS SSH credentials
2. `ssh root@<your-public-ip>`
3. Install Docker if not present: `curl -fsSL https://get.docker.com | sh`

**Step 2 — Clone the project:**
```bash
cd /opt
git clone https://github.com/<your-org>/<your-vpn-repo>.git
cd <your-vpn-repo>
```

**Step 3 — Deploy strongSwan:**
```bash
cd <your-install-root>
docker compose up -d strongswan
# Verify: docker exec strongswan swanctl --list-conns
```

**Step 4 — Deploy portal:**
```bash
docker compose up -d vpn-portal
# Portal runs at https://<your-portal-domain>
```

**Step 5 — Configure firewall:**
```bash
# Apply iptables from Section 2.4 of this document
# Then persist: netfilter-persistent save
```

**Step 6 — Configure DNS:**
- `<your-vpn-hostname>` → `<your-public-ip>` (A record)
- Disable any CDN proxy for `<your-vpn-hostname>` (or UDP 500/4500 will be blocked)

**Step 7 — Test server:**
```bash
docker exec strongswan swanctl --list-sas
# Should show: rw-eap, version=2.0, local=<your-vpn-hostname>, remote=%any
```

**Step 8 — Get TLS certificate:**
```bash
# On VPS (or use your DNS provider's ACME integration):
certbot certonly --standalone -d <your-vpn-hostname> --agree-tos --email <your-email> -n
# Copy certs to swanctl/x509/ and swanctl/x509ca/ (SPLIT into separate files!)
```

**Step 9 — Deploy Windows client:**
```powershell
# On Windows machine:
iex (irm 'https://<your-portal-domain>/static/setup-vpn.ps1?v=latest')
```

**Step 10 — Verify (Section 5 of this document)**

---

## 10. Version History

This doc is generic — version history is in your git log (`git log docs/DAT-VPN-INT-WIN-001.md`). Do not rely on version numbers from any single deployment; verify against your own fork.

---

## 11. References

| Source | URL | What it provides |
|---|---|---|
| Microsoft Learn | `learn.microsoft.com/en-us/powershell/module/vpnclient/` | `Add-VpnConnection`, `Set-VpnConnection` cmdlets |
| strongSwan docs | `docs.strongswan.org/docs/latest/interop/windowsEapConf.html` | Windows IKEv2 GUI setup canonical guide |
| strongSwan GitHub | `github.com/strongswan/strongswan/issues/3072` | YR2 transition cert chain issue |
| PowerShell Gallery | `powershellgallery.com/packages/VPNCredentialsHelper/1.1` | Canonical `RasSetCredentials` implementation |
| Let's Encrypt | `community.letsencrypt.org/t/ikev2-vpn-connection-fails-after-certificate-update/239739` | LE cert + IKEv2 failure reports |
| Windows SDK | `learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--0-499-` | RAS error codes (0, 87, 1162, 5, 1312) |

---

## 12. Per-Customer Baked Variant (Optional)

**When to use:** If you want to ship a one-liner to customers with credentials pre-baked (no GUI prompt at install time).

The structure is identical to the generic installer above. The difference is only the defaults at the top of the file:

```powershell
# Generic (customer supplies creds at install):
param(
    [string]$VpnName      = "<vpn-connection-name>",
    [string]$ServerAddress = "<your-vpn-hostname>",
    [string]$Username      = "<your-username>",
    [string]$Password      = "<your-password>"
)

# Baked per-customer (operator edits and ships):
# $Username = "<customer-username>"
# $Password = "<customer-password>"
```

Save the baked file as `setup-vpn-<customer>-<device>.ps1` and serve from `<your-portal-web-root>/static/baked/`.

**Operator workflow (per customer onboarding):**
1. Copy the generic template to `setup-vpn-<customer>-<device>.ps1`
2. Edit the four default values (or bake them in directly)
3. Deploy to VPS: `scp setup-vpn-<customer>-<device>.ps1 root@<your-hostname>:<your-portal-web-root>/static/baked/`
4. Set correct ownership: `sudo chown <web-user>:<web-group> <your-portal-web-root>/static/baked/setup-vpn-<customer>-<device>.ps1`
5. Verify: `curl -ksSL -o /dev/null -w "%{http_code} %{size_download}\n" https://<your-portal-domain>/static/baked/setup-vpn-<customer>-<device>.ps1`
6. Ship the URL to the customer via encrypted channel. **Never** email the .ps1 file itself.

**Customer invocation (one-liner):**
```
curl.exe -ksSL -o $env:TEMP\setup.ps1 https://<your-portal-domain>/static/baked/setup-vpn-<customer>-<device>.ps1
powershell -ExecutionPolicy Bypass -NoProfile -File $env:TEMP\setup.ps1
```

---

**Hand-curated 2026-10-17 for generic use.** All databyte-specific branding, dates, internal ticket references, and deployment-specific details removed. For the full production stack with FreeRADIUS + MariaDB + portal + bot, see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).
