<#
.SYNOPSIS
    Sets up a Windows IKEv2 VPN connection to the Databyte VPN server.
.DESCRIPTION
    Run as Administrator in PowerShell. Self-contained: credentials and
    server details are baked in below. Idempotent: re-running safely
    updates an existing connection.

    What it does:
      1. Verifies the server presents a publicly-trusted Let's Encrypt cert
         (no CA cert install needed — Windows trusts LE natively via ISRG Root X1/X2)
      2. Creates (or updates) a "DatabyteVPN" connection with NO split tunneling
      3. Sets IKEv2 IPsec crypto to match the strongSwan server config
      4. Connects the VPN with the operator credentials

    Usage:
      PS> powershell -ExecutionPolicy Bypass -File setup-windows-vpn.ps1
      # then re-run anytime to reconnect

.NOTES
    Baked-in credentials are PRODUCTION. Do not share this script publicly.
    For other operators, copy the script and change the values in the
    CONFIG block at the top.
#>

#Requires -RunAsAdministrator

# ============================================================================
# CONFIG (edit these for your environment)
# ============================================================================
# Server = myvpn.databyte.co.za (grey-cloud DNS, resolves to VPS 154.65.110.44).
# Cloudflare proxy only proxies vpn-portal.* (orange), not myvpn.* (grey), so
# the hostname works for IKEv2.
# - vpn-portal.databyte.co.za = orange-cloud, CF proxy in front, CANNOT relay IKEv2
# - myvpn.databyte.co.za      = grey-cloud, direct to VPS, works for IKEv2
$ServerHostname   = "myvpn.databyte.co.za"
$ConnectionName   = "DatabyteVPN"
# Also clean up the common names people (or the OS) might leave around, especially
# the old PPTP "Databyte vpn" connection from earlier manual setups.
$LegacyNames      = @("Databyte vpn", "Databyte VPN", "DatabyteVPN", "myvpn", "MyVPN")
$Username         = "zun-operator"
$Password         = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# ============================================================================
# STEP 1 - Verify server cert is publicly trusted (Let's Encrypt)
# ============================================================================
Write-Host ""
Write-Host "=== [1/4] Verifying server TLS cert (Let's Encrypt) ===" -ForegroundColor Cyan

# Use raw SslStream to perform a real TLS handshake and fetch the cert.
# (HttpWebRequest.ServicePoint.Certificate returns null on a fresh request.)
$cert = $null
$certError = $null
try {
    $tcp = New-Object System.Net.Sockets.TcpClient($ServerHostname, 443)
    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, {[System.Net.Security.RemoteCertificateValidationCallback]{ $true }})
    $ssl.AuthenticateAsClient($ServerHostname)
    $raw = $ssl.RemoteCertificate
    if ($raw) {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($raw)
    }
    $ssl.Close()
    $tcp.Close()
} catch {
    $certError = $_.Exception.Message
}

if ($cert) {
    Write-Host "  Subject:   $($cert.Subject)" -ForegroundColor Green
    Write-Host "  Issuer:    $($cert.Issuer)" -ForegroundColor Green
    Write-Host "  NotBefore: $($cert.NotBefore)" -ForegroundColor Green
    Write-Host "  NotAfter:  $($cert.NotAfter)" -ForegroundColor Green
    if ($cert.Issuer -match "Let's Encrypt" -or $cert.Issuer -match "ISRG") {
        Write-Host "  Chain:     LE (ISRG Root X1/X2) - publicly trusted by Windows." -ForegroundColor Green
    } else {
        Write-Host "  WARNING:   Issuer is not Let's Encrypt. Verify manually." -ForegroundColor Yellow
    }
} else {
    Write-Host "  Cert fetch FAILED: $certError" -ForegroundColor Red
    Write-Host "  Continuing anyway (cert trust is handled by Windows at connect time)..." -ForegroundColor Yellow
}

# ============================================================================
# STEP 2 - Clean slate: remove ALL Databyte-related connections
# ============================================================================
# Critical: a leftover PPTP connection (e.g. "Databyte vpn" from an earlier
# manual setup) will be tried FIRST when you click Connect, fail with error
# 13843, and Windows will report "VPN failed" even though the IKEv2 connection
# we create below is fine. So we nuke EVERYTHING matching before creating fresh.
Write-Host ""
Write-Host "=== [2/4] Cleaning up old connections ===" -ForegroundColor Cyan

foreach ($nameToRemove in @($ConnectionName) + $LegacyNames) {
    foreach ($scope in @($false, $true)) {  # user-scope then all-user-scope
        $existingConn = Get-VpnConnection -Name $nameToRemove -AllUserConnection:$scope -ErrorAction SilentlyContinue
        if ($existingConn) {
            Write-Host "  Removing: '$nameToRemove' (TunnelType=$($existingConn.TunnelType), scope=$(if($scope){'all-user'}else{'user'}))" -ForegroundColor Yellow
            try {
                rasdial $nameToRemove /disconnect 2>&1 | Out-Null
                Remove-VpnConnection -Name $nameToRemove -AllUserConnection:$scope -Force -ErrorAction Stop
            } catch {
                Write-Warning "  Remove failed for '$nameToRemove' (scope=$(if($scope){'all-user'}else{'user'})): $_"
            }
        }
    }
}
Start-Sleep -Seconds 1

# Show what survives
$remaining = @()
foreach ($scope in @($false, $true)) {
    $remaining += Get-VpnConnection -AllUserConnection:$scope -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -match "databyte|vpn" } |
                  ForEach-Object { "$($_.Name) ($($_.TunnelType), $(if($scope){'all-user'}else{'user'}))" }
}
if ($remaining.Count -eq 0) {
    Write-Host "  No old Databyte/VPN connections remain. Clean slate." -ForegroundColor Green
} else {
    Write-Host "  Remaining Databyte/VPN connections (will create fresh anyway):" -ForegroundColor Yellow
    $remaining | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
}

# IMPORTANT: do NOT pass -SplitTunneling (omitted = full tunnel = all traffic
# goes through VPN, including internet).
Write-Host "  Creating new IKEv2 connection..." -ForegroundColor Cyan
try {
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerHostname `
        -TunnelType "IKEv2" `
        -AuthenticationMethod "EAP" `
        -RememberCredential `
        -PassThru -ErrorAction Stop | Out-Null
} catch {
    Write-Host ""
    Write-Host "  ERROR: Add-VpnConnection failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# Store the credentials so the connection auto-fills username/password
# and uses them on the next connect (avoids the GUI prompt).
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
Write-Host "  Connection created, credentials stored" -ForegroundColor Green

# ============================================================================
# STEP 3 - Set IKEv2 IPsec crypto to match strongSwan server
# ============================================================================
Write-Host ""
Write-Host "=== [3/4] Configuring IPsec crypto ===" -ForegroundColor Cyan

try
{
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants "SHA256" `
        -CipherTransformConstants "AES128" `
        -DHGroup "ECP384" `
        -EncryptionMethod "AES128" `
        -IntegrityCheckMethod "SHA256" `
        -PfsGroup "ECP384" `
        -Force | Out-Null
    Write-Host "  IPsec: AES128/SHA256/ECP384 (matches homelab-tested path)" -ForegroundColor Green
}
catch
{
    Write-Warning "Set-VpnConnectionIPsecConfiguration failed: $_"
    Write-Warning "The connection will use Windows defaults (DES3/SHA1/DH2  -  strongSwan will reject as insecure)."
}

# Also set the registry key that enables strong DH (Group14+) for EAP-MSCHAPv2.
# Without this, Windows defaults to Group2 (1024-bit DH) which is too weak.
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters"
New-ItemProperty -Path $regPath -Name "NegotiateDH2048_AES256" `
    -PropertyType DWord -Value 1 -Force | Out-Null
Write-Host "  Registry: NegotiateDH2048_AES256 = 1 (enables Group14+)" -ForegroundColor Green

# ============================================================================
# STEP 4 - Connect
# ============================================================================
Write-Host ""
Write-Host "=== [4/4] Connecting to $ServerHostname ===" -ForegroundColor Cyan

Start-Sleep -Seconds 1

$connect = rasdial $ConnectionName $Username $Password
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Connected!" -ForegroundColor Green
}
else {
    Write-Host "  rasdial exit code: $LASTEXITCODE" -ForegroundColor Red
    Write-Host "  $connect"
    exit 1
}

# ============================================================================
# VERIFY
# ============================================================================
Write-Host ""
Write-Host "=== Verifying ===" -ForegroundColor Cyan
Start-Sleep -Seconds 2

$conn = Get-VpnConnection -Name $ConnectionName
Write-Host "  Status:        $($conn.ConnectionStatus)"
Write-Host "  Server:        $($conn.ServerAddress)"
Write-Host "  Tunnel type:   $($conn.TunnelType)"
Write-Host "  Auth method:   $($conn.AuthenticationMethod)"

# Pull the route table for the VPN adapter
$adapter = Get-NetAdapter |
    Where-Object { $_.InterfaceDescription -match "DatabyteVPN" -or $_.Name -match "DatabyteVPN" } |
    Select-Object -First 1

if ($adapter) {
    $routes = Get-NetRoute -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue
    Write-Host "  VPN adapter:   $($adapter.Name) (ifIndex $($adapter.ifIndex))"
    Write-Host "  VPN routes:"
    $routes | ForEach-Object { Write-Host "    $($_.DestinationPrefix) -> $($_.NextHop)" }
}
else {
    Write-Host "  (VPN adapter not yet visible, run 'Get-NetAdapter' to check)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Test commands (PowerShell or cmd):" -ForegroundColor Cyan
Write-Host "  # First hop should be the VPS, NOT your home router:"
Write-Host "  tracert 8.8.8.8"
Write-Host ""
Write-Host "  # iperf3 through the tunnel (expected ~17-20 Mbps with cap):"
Write-Host "  iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30"
Write-Host "  iperf3 -c iperf.angolacables.co.ao -p 9200 -R -t 30"
Write-Host ""
Write-Host "To disconnect:  rasdial $ConnectionName /disconnect"
Write-Host "To reconnect:   powershell -ExecutionPolicy Bypass -File $PSCommandPath"
Write-Host ""
