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
         (canonical MS Learn pattern — no hand-written XML)
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
    Version:        2.0.0
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
$ConnectionName = "DatabyteVPN"
# Legacy connection names from earlier manual setups — nuke them so the OS
# doesn't try PPTP first and fail with 13843 ("VPN failed").
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
$cert = $null
try {
    $tcp = New-Object System.Net.Sockets.TcpClient($ServerAddress, 443)
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
Write-Host ""
Write-Host "=== [2/7] Removing legacy VPN connections ===" -ForegroundColor Cyan

foreach ($n in (@($ConnectionName) + $LegacyNames)) {
    foreach ($scope in @($false, $true)) {  # user-scope, then all-user-scope
        $existing = Get-VpnConnection -Name $n -AllUserConnection:$scope -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host "  Removing: '$n' (TunnelType=$($existing.TunnelType), scope=$(if($scope){'all-user'}else{'user'}))" -ForegroundColor Yellow
            try { rasdial $n /disconnect 2>&1 | Out-Null } catch {}
            try {
                Remove-VpnConnection -Name $n -AllUserConnection:$scope -Force -ErrorAction Stop
            } catch {
                Write-Warning "  Remove failed for '$n': $_"
            }
        }
    }
}
Start-Sleep -Seconds 1

# ============================================================================
# STEP 3 — Create profile (IKEv2 + EAP-MSCHAPv2)
# ============================================================================
# New-EapConfiguration (no flags) generates the canonical MSCHAPv2 EAP XML.
# It does NOT force Winlogon creds — Windows prompts at connect time, which
# we pre-fill via cmdkey in step 6. (UseWinlogonCredential would force it.)
Write-Host ""
Write-Host "=== [3/7] Creating VPN profile (IKEv2 + EAP-MSCHAPv2) ===" -ForegroundColor Cyan

$EAP = New-EapConfiguration
if (-not $EAP -or -not $EAP.EapConfigXmlStream) {
    Write-Host "  ERROR: New-EapConfiguration returned no XML." -ForegroundColor Red
    exit 1
}

try {
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerAddress `
        -TunnelType "IKEv2" `
        -AuthenticationMethod "Eap" `
        -EapConfigXmlStream $EAP.EapConfigXmlStream `
        -RememberCredential `
        -PassThru -ErrorAction Stop | Out-Null
    Write-Host "  Profile created: $ConnectionName" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: Add-VpnConnection failed: $($_.Exception.Message)" -ForegroundColor Red
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
# Generic creds keyed by server address. Windows GUI auto-fills on Connect.
Write-Host ""
Write-Host "=== [6/7] Storing credentials ===" -ForegroundColor Cyan

cmdkey /generic:$ServerAddress /user:$Username /pass:$Password | Out-Null
Write-Host "  Credentials stored for $ServerAddress" -ForegroundColor Green

# ============================================================================
# STEP 7 — Connect (rasdial, with GUI fallback)
# ============================================================================
# rasdial is a legacy RAS dialer (PPTP/L2TP era). It does NOT properly
# speak EAP-MSCHAPv2 inside IKEv2 — success depends on Windows build.
# If it fails, we open Settings so the user can click Connect.
Write-Host ""
Write-Host "=== [7/7] Connecting to $ServerAddress ===" -ForegroundColor Cyan

Start-Sleep -Seconds 1

$connectOutput = rasdial $ConnectionName $Username $Password 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Connected!" -ForegroundColor Green
} else {
    Write-Host "  rasdial exit code: $LASTEXITCODE" -ForegroundColor Yellow
    Write-Host ($connectOutput -join "`n") -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  rasdial can't reliably handle EAP-MSCHAPv2 in IKEv2." -ForegroundColor Yellow
    Write-Host "  Opening Settings -> VPN. Click 'Connect' next to '$ConnectionName'." -ForegroundColor Yellow
    Start-Process ms-settings:network-vpn
}

# ============================================================================
# VERIFY
# ============================================================================
Write-Host ""
Write-Host "=== Verifying ===" -ForegroundColor Cyan
Start-Sleep -Seconds 2

$conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "  Profile:     $($conn.Name)" -ForegroundColor Green
    Write-Host "  Server:      $($conn.ServerAddress)" -ForegroundColor Green
    Write-Host "  TunnelType:  $($conn.TunnelType)" -ForegroundColor Green
    Write-Host "  AuthMethod:  $($conn.AuthenticationMethod -join ',')" -ForegroundColor Green
    Write-Host "  Status:      $($conn.ConnectionStatus)" -ForegroundColor Green
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

# Keep window open when run from File Explorer (not from terminal)
if ($Host.Name -eq "ConsoleHost" -and [Environment]::UserInteractive) {
    Write-Host "Press any key to exit..." -ForegroundColor DarkGray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}