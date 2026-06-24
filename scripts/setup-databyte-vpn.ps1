<#
.SYNOPSIS
    Databyte VPN installer for Windows 10/11 — IKEv2 + EAP-MSCHAPv2.

.DESCRIPTION
    Single-file installer. Idempotent: re-running safely updates the existing
    connection. Works on PowerShell 5.1 (default on Windows 10/11) and
    PowerShell 7+.

    What it does:
      1. Verifies the server presents a publicly-trusted Let's Encrypt cert
         (no CA install needed — Windows trusts LE natively via ISRG Root X1/X2)
      2. Removes all stale Databyte-related VPN connections (PPTP/IKEv2/etc.)
      3. Creates a fresh IKEv2 connection with EAP-MSCHAPv2 auth
         (proven hand-written XML — pins cert to ServerNames, suppresses creds
         and cert-trust GUI prompts)
      4. Sets IPsec crypto to match the strongSwan server
         (AES128/SHA256/Group14/PFS2048 — Microsoft Learn canonical, 2025-01-27)
      5. Configures Windows registry for strong DH (ENFORCE) and NAT-T
      6. Stores credentials via cmdkey (auto-fills on GUI connect)
      7. Attempts rasdial; falls back to Settings GUI on failure

    Usage:
      PS> powershell -ExecutionPolicy Bypass -File setup-databyte-vpn.ps1

    Or one-shot from the portal:
      PS> iex (irm https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1)

.NOTES
    File:           setup-databyte-vpn.ps1
    Version:        2.0.6
    Replaces:       setup-windows-vpn.ps1, connect-databyte-vpn.ps1
    Server:         myvpn.databyte.co.za (grey-cloud DNS → 154.65.110.44)
    Auth:           EAP-MSCHAPv2 (operator credentials, baked in)
    StrongSwan:     aes256-sha256-modp2048, aes128-sha256-modp2048
    Compatible:     Windows 10 1809+, Windows 11, Server 2019+
    PowerShell:     5.1 (default) and 7+
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'

# ============================================================================
# CONFIG (edit these per deployment)
# ============================================================================
$ServerAddress  = "myvpn.databyte.co.za"
$RemoteId       = "myvpn.databyte.co.za"   # must match cert CN/SAN; pins server identity
$ConnectionName = "DatabyteVPN"
# Legacy connection names from earlier manual setups — kept as a fallback
# hint for the human reader; actual cleanup is by ServerAddress (see STEP 2)
# because name lists miss leftovers from prior tests.
$LegacyNames    = @(
    "Databyte vpn","Databyte VPN","DatabyteVPN",
    "myvpn","MyVPN","vpn.homelab.local","HomelabVPN","homelab vpn"
)
$Username       = "zun-operator"
$Password       = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# ============================================================================
# Transcript (logs to %TEMP% for post-mortem)
# ============================================================================
$transcriptPath = Join-Path $env:TEMP "databyte-vpn-setup-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
try { Start-Transcript -Path $transcriptPath -Append -ErrorAction SilentlyContinue | Out-Null } catch {}

# ============================================================================
# STEP 1 — Verify server cert is publicly trusted
# ============================================================================
Write-Host ""
Write-Host "=== [1/7] Verifying server TLS cert ===" -ForegroundColor Cyan

# Raw SslStream does a real TLS handshake. (HttpWebRequest.ServicePoint.Certificate
# returns null on a fresh request — this is the workaround.)
# Bounded by 10s timeout so a firewall/NAT issue fails fast (lesson from
# 2026-06-24 VPS reboot: UFW lost TCP 80/443 rules, script hung on
# New-TcpClient(443) forever waiting for SYN-ACK).
$cert = $null
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.SendTimeout    = 5000
    $tcp.ReceiveTimeout = 5000
    $iar = $tcp.BeginConnect($ServerAddress, 443, $null, $null)
    $ok  = $iar.AsyncWaitHandle.WaitOne(10000)
    if (-not $ok) {
        $tcp.Close()
        throw "TCP connect to ${ServerAddress}:443 timed out after 10s (firewall or network issue?)"
    }
    $tcp.EndConnect($iar)

    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, {[System.Net.Security.RemoteCertificateValidationCallback]{ $true }})
    $ssl.AuthenticateAsClient($ServerAddress)
    if ($ssl.RemoteCertificate) {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
    }
    $ssl.Close(); $tcp.Close()
} catch {
    Write-Warning "Cert fetch failed: $($_.Exception.Message)"
}

if ($cert) {
    Write-Host "  Subject:   $($cert.Subject)"   -ForegroundColor Green
    Write-Host "  Issuer:    $($cert.Issuer)"    -ForegroundColor Green
    Write-Host "  Expires:   $($cert.NotAfter)"  -ForegroundColor Green
    if ($cert.Issuer -match "Let.?s Encrypt|ISRG") {
        Write-Host "  Trust:     LE (ISRG Root X1/X2) — trusted by Windows natively." -ForegroundColor Green
    } else {
        Write-Warning "Issuer is not Let's Encrypt. Cert trust may fail."
    }
} else {
    Write-Warning "Continuing — Windows will validate cert at connect time."
}

# ============================================================================
# STEP 2 — Clean slate: remove all Databyte-related connections
# ============================================================================
# Lesson (2026-06-24): a NAME list (e.g., "databyte","DatabyteVPN","myvpn")
# misses leftover profiles from prior tests. Windows will then use the stale
# one (with its old cmdkey creds) instead of the new profile we create here.
# Fix: enumerate ALL VPN connections and remove any whose ServerAddress points
# at our server, regardless of name. This catches "databyte" (lowercase,
# from Android test), "test-iphone-5g-iphone", "vpn.homelab.local", etc.
Write-Host ""
Write-Host "=== [2/7] Removing legacy VPN connections ===" -ForegroundColor Cyan

$removed = 0
foreach ($scope in @($false, $true)) {  # user-scope, then all-user-scope
    $scopeLabel = if ($scope) { 'all-user' } else { 'user' }
    $stale = Get-VpnConnection -AllUserConnection:$scope -ErrorAction SilentlyContinue |
        Where-Object { $_.ServerAddress -match [regex]::Escape($ServerAddress) }
    foreach ($s in $stale) {
        Write-Host "  Removing stale profile: '$($s.Name)' (ServerAddress=$($s.ServerAddress), scope=$scopeLabel)" -ForegroundColor Yellow
        try { rasdial $s.Name /disconnect 2>&1 | Out-Null } catch {}
        try {
            Remove-VpnConnection -Name $s.Name -AllUserConnection:$scope -Force -ErrorAction Stop
            $removed++
        } catch {
            Write-Warning "  Remove failed for '$($s.Name)': $_"
        }
    }
}
if ($removed -eq 0) {
    Write-Host "  (no leftover profiles found)" -ForegroundColor DarkGray
}

# Wipe stale Windows Credential Manager entries whose target matches our server.
# cmdkey /generic:<target> only replaces exact target matches — variants like
# LEGACYAPPS\myvpn.databyte.co.za or test-iphone-5g-iphone won't be overwritten
# and Windows will keep using them for EAP.
$cmdkeyRemoved = 0
$cmdkeyList = cmdkey /list 2>&1 | Out-String
$targetRegex = [regex]'Target:\s*(?<t>.+?)\s*$'
foreach ($line in ($cmdkeyList -split "`r?`n")) {
    if ($line -match $targetRegex) {
        $t = $Matches['t'].Trim()
        if ($t -match 'databyte|myvpn|test-android|test-iphone|test-win') {
            Write-Host "  Deleting cmdkey: $t" -ForegroundColor Yellow
            try {
                cmdkey /delete:$t 2>&1 | Out-Null
                $cmdkeyRemoved++
            } catch {
                Write-Warning "  cmdkey delete failed for '$t': $_"
            }
        }
    }
}
if ($cmdkeyRemoved -eq 0) {
    Write-Host "  (no stale cmdkey entries found)" -ForegroundColor DarkGray
}

Start-Sleep -Seconds 1

# ============================================================================
# STEP 3 — Create profile (IKEv2 + EAP-MSCHAPv2)
# ============================================================================
# Hand-written VPNProfile XML. Proven pattern (was in connect-databyte-vpn.ps1
# which actually worked). New-EapConfiguration's default output does NOT set
# <UseWinLogonCredentials>false</UseWinLogonCredentials> or <ServerNames>, so
# Windows shows credential/cert prompts at connect time. This XML pre-sets both.
Write-Host ""
Write-Host "=== [3/7] Creating VPN profile (IKEv2 + EAP-MSCHAPv2) ===" -ForegroundColor Cyan

$profileXml = @"
<VPNProfile>
  <NativeProfile>
    <Servers>$ServerAddress</Servers>
    <NativeProtocolType>IKEv2</NativeProtocolType>
    <Authentication>
      <UserMethod>Eap</UserMethod>
      <Eap>
        <Configuration>
          <EapHostConfig xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
            <EapMethod>
              <Type xmlns="http://www.microsoft.com/provisioning/EapCommon">26</Type>
              <VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
              <VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
              <AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">311</AuthorId>
            </EapMethod>
            <Config>
              <EapMsChapV2 xmlns="http://www.microsoft.com/provisioning/EapMsChapV2ConnectionPropertiesV1">
                <UseWinLogonCredentials>false</UseWinLogonCredentials>
              </EapMsChapV2>
            </Config>
          </EapHostConfig>
        </Configuration>
      </Eap>
    </Authentication>
    <RoutingPolicyType>ForceTunnel</RoutingPolicyType>
  </NativeProfile>
</VPNProfile>
"@

# MS Learn: -EapConfigXmlStream type is System.Xml.XmlDocument (NOT XmlReader).
# Verified empirically on Windows 11 24H2 — XmlReader causes a coercion error:
#   "Cannot convert value 'System.Xml.XmlTextReaderImpl' to type
#    'System.Xml.XmlDocument'."
$xmlDoc = New-Object System.Xml.XmlDocument
$xmlDoc.LoadXml($profileXml)

try {
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerAddress `
        -TunnelType "IKEv2" `
        -EapConfigXmlStream $xmlDoc `
        -RememberCredential `
        -PassThru -ErrorAction Stop | Out-Null
    Write-Host "  Profile created: $ConnectionName" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: Add-VpnConnection failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $profileXml -ForegroundColor DarkGray
    exit 1
}

# ============================================================================
# STEP 4 — IPsec crypto (Microsoft Learn canonical, updated 2025-01-27)
# ============================================================================
# Source: learn.microsoft.com/.../how-to-configure-diffie-hellman-protocol-over-ikev2-vpn-connections
# strongSwan server accepts: aes256-sha256-modp2048, aes128-sha256-modp2048
Write-Host ""
Write-Host "=== [4/7] Configuring IPsec crypto ===" -ForegroundColor Cyan

try {
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants SHA256128 `
        -CipherTransformConstants         AES128 `
        -DHGroup                          Group14 `
        -EncryptionMethod                 AES128 `
        -IntegrityCheckMethod             SHA256 `
        -PfsGroup                         PFS2048 `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "  AES128 / SHA256128 / Group14 / SHA256 / PFS2048" -ForegroundColor Green
} catch {
    Write-Warning "IPsec config failed: $_"
    Write-Warning "Windows defaults (DES3/SHA1/DH2) will be used — server will reject as insecure."
}

# ============================================================================
# STEP 5 — Registry tweaks
# ============================================================================
# NegotiateDH2048_AES256: 0=disable, 1=enable, 2=ENFORCE
# Without this, Win 10/11 proposes weak DH2 (1024-bit) by default.
Write-Host ""
Write-Host "=== [5/7] Registry tweaks ===" -ForegroundColor Cyan

New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 2 -Force | Out-Null
Write-Host "  RasMan\Parameters\NegotiateDH2048_AES256 = 2 (ENFORCE)" -ForegroundColor Green

# AssumeUDPEncapsulationContextOnSendRule: 2 = enabled (required if client behind NAT).
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\PolicyAgent" `
    -Name "AssumeUDPEncapsulationContextOnSendRule" -PropertyType DWord -Value 2 -Force | Out-Null
Write-Host "  PolicyAgent\AssumeUDPEncapsulationContextOnSendRule = 2" -ForegroundColor Green

# ============================================================================
# STEP 6 — Store credentials (cmdkey)
# ============================================================================
# Store under BOTH the server address AND the connection name.
# Different Windows builds look up VPN creds by different keys:
#   - Win 10 1809 / Server 2019: keyed by ServerAddress
#   - Win 11 21H2+: keyed by ConnectionName
# Storing under both = works everywhere.
Write-Host ""
Write-Host "=== [6/7] Storing credentials ===" -ForegroundColor Cyan

cmdkey /generic:$ServerAddress      /user:$Username /pass:$Password | Out-Null
cmdkey /generic:$ConnectionName     /user:$Username /pass:$Password | Out-Null
Write-Host "  Credentials stored for $ServerAddress AND $ConnectionName" -ForegroundColor Green

# ============================================================================
# STEP 7 — Connect (rasdial, with GUI fallback + poll loop)
# ============================================================================
# rasdial is a legacy RAS dialer (PPPT/L2TP era). It does NOT reliably
# speak EAP-MSCHAPv2 inside IKEv2 — success depends on Windows build.
# If it fails, open Settings so the user can click Connect there, then
# POLL for status change instead of exiting silently.
Write-Host ""
Write-Host "=== [7/7] Connecting to $ServerAddress ===" -ForegroundColor Cyan

Start-Sleep -Seconds 1

$connectOutput = rasdial $ConnectionName $Username $Password 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  rasdial: CONNECTED" -ForegroundColor Green
} else {
    Write-Host "  rasdial exit code: $LASTEXITCODE" -ForegroundColor Yellow
    Write-Host ($connectOutput -join "`n") -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  rasdial can't reliably handle EAP-MSCHAPv2 in IKEv2." -ForegroundColor Yellow
    Write-Host "  Opening Settings -> VPN. Click 'Connect' next to '$ConnectionName'." -ForegroundColor Yellow
    Write-Host "  Polling for connection state (up to 90s)..." -ForegroundColor Yellow
    Start-Process ms-settings:network-vpn
}

# ============================================================================
# VERIFY — poll for Connected state
# ============================================================================
Write-Host ""
Write-Host "=== Verifying ===" -ForegroundColor Cyan

$maxWait = 90
$pollSec = 3
$elapsed = 0
$connected = $false
$conn      = $null

while ($elapsed -lt $maxWait) {
    $conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
    if ($conn -and $conn.ConnectionStatus -eq "Connected") {
        $connected = $true
        break
    }
    Start-Sleep -Seconds $pollSec
    $elapsed += $pollSec
    Write-Host ("  ...waiting ({0}s/{1}s) — status: {2}" -f `
        $elapsed, $maxWait, $(if($conn){$conn.ConnectionStatus}else{'unknown'})).PadRight(60) -NoNewline
    Write-Host "`r" -NoNewline
}

Write-Host ""
Write-Host ""

if ($conn) {
    Write-Host "  Profile:     $($conn.Name)" -ForegroundColor Green
    Write-Host "  Server:      $($conn.ServerAddress)" -ForegroundColor Green
    Write-Host "  TunnelType:  $($conn.TunnelType)" -ForegroundColor Green
    Write-Host "  AuthMethod:  $($conn.AuthenticationMethod -join ',')" -ForegroundColor Green
    $statusColor = if ($connected) { "Green" } else { "Red" }
    Write-Host "  Status:      $($conn.ConnectionStatus)" -ForegroundColor $statusColor
    if ($connected) {
        Write-Host ""
        Write-Host "  [OK] CONNECTED to $ServerAddress" -ForegroundColor Green
        Write-Host "       First test:  tracert 8.8.8.8   (first hop should be VPS)" -ForegroundColor Cyan
        Write-Host "       Public IP:   Invoke-WebRequest https://ifconfig.me" -ForegroundColor Cyan
    } else {
        Write-Host ""
        Write-Host "  [FAIL] Still disconnected after ${maxWait}s." -ForegroundColor Red
        Write-Host "         Possible causes:" -ForegroundColor Yellow
        Write-Host "           1. Settings VPN page still showing a prompt (click Connect there)" -ForegroundColor Yellow
        Write-Host "           2. Server unreachable from your network" -ForegroundColor Yellow
        Write-Host "           3. Credentials wrong (rerun to overwrite cmdkey)" -ForegroundColor Yellow
        Write-Host "         Re-run this script to retry, or:" -ForegroundColor Yellow
        Write-Host "           rasdial $ConnectionName /disconnect   (reset)" -ForegroundColor Cyan
        Write-Host "           Start-Process ms-settings:network-vpn" -ForegroundColor Cyan
    }
} else {
    Write-Warning "Profile not visible. Refresh VPN settings."
}

Write-Host ""
Write-Host "Manual connect:  Settings -> Network & Internet -> VPN -> $ConnectionName -> Connect" -ForegroundColor Cyan
Write-Host "Disconnect:      rasdial $ConnectionName /disconnect" -ForegroundColor Cyan
Write-Host ""
Write-Host "Test after connecting:" -ForegroundColor Cyan
Write-Host "  tracert 8.8.8.8                (first hop should be VPS, not your router)"
Write-Host "  Invoke-WebRequest https://ifconfig.me  (should return VPS IP)"
Write-Host ""
Write-Host "Setup log: $transcriptPath" -ForegroundColor DarkGray
Write-Host ""

try { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null } catch {}

# Keep window open. Read-Host works in ALL hosts (Console, ISE, irm|iex).
# ReadKey silently fails in some PowerShell hosts.
Write-Host ""
Read-Host "Press Enter to exit"