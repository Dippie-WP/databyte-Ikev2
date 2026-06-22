<#
.SYNOPSIS
    Sets up a Windows IKEv2 VPN connection to the Databyte VPN server.
.DESCRIPTION
    Run as Administrator in PowerShell. Self-contained — credentials and
    server details are baked in below. Idempotent: re-running safely
    updates an existing connection.

    What it does:
      1. Imports the strongSwan CA cert into LocalMachine\Root
      2. Creates (or updates) a "DatabyteVPN" connection with NO split tunneling
      3. Sets IKEv2 IPsec crypto to match the strongSwan server config
      4. Connects the VPN with the operator credentials

    Usage:
      PS> powershell -ExecutionPolicy Bypass -File setup-windows-vpn.ps1
      # then re-run anytime to reconnect
.NOTES
    Baked-in credentials are PRODUCTION. Don't share this script publicly.
    For other operators, copy the script and change the values in the
    CONFIG block at the top.
#>

#Requires -RunAsAdministrator

# ============================================================================
# CONFIG — edit these for your environment
# ============================================================================
$ServerHostname   = "myvpn.databyte.co.za"
$ConnectionName   = "DatabyteVPN"
$Username         = "zun-operator"
$Password         = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# Path to the strongSwan CA cert (PEM format).
# Default: expects it in the same directory as this script.
$CaCertPath = Join-Path $PSScriptRoot "strongswan-ca.crt.pem"
if (-not (Test-Path $CaCertPath)) {
    # Fall back to CWD
    $CaCertPath = ".\strongswan-ca.crt.pem"
}

# ============================================================================
# STEP 1 — Install CA cert
# ============================================================================
Write-Host "`n=== [1/4] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

if (-not (Test-Path $CaCertPath)) {
    Write-Error "CA cert not found at: $CaCertPath"
    Write-Error "Place strongswan-ca.crt.pem next to this script and try again."
    exit 1
}

# Check if cert is already installed
$certThumbprint = (Get-PfxCertificate -FilePath $CaCertPath).Thumbprint
$existing = Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Thumbprint -eq $certThumbprint }

if ($existing) {
    Write-Host "  CA cert already installed (thumbprint $certThumbprint)" -ForegroundColor Yellow
} else {
    Import-Certificate -FilePath $CaCertPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    Write-Host "  CA cert installed (thumbprint $certThumbprint)" -ForegroundColor Green
}

# ============================================================================
# STEP 2 — Remove old connection (if exists) and create fresh one
# ============================================================================
Write-Host "`n=== [2/4] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection..." -ForegroundColor Yellow
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
}

# IMPORTANT: do NOT pass -SplitTunneling (omitted = full tunnel = all traffic
# goes through VPN, including internet)
Add-VpnConnection `
    -Name $ConnectionName `
    -ServerAddress $ServerHostname `
    -TunnelType "IKEv2" `
    -AuthenticationMethod "EAP" `
    -RememberCredential `
    -PassThru | Out-Null

# Pre-populate the credentials so the connection auto-fills username/password
# and uses them on the next connect (avoids the GUI prompt)
$cmd = "rasdial `"$ConnectionName`" `"$Username`" `"$Password`" /DOMAIN:`"`""
# Don't actually run rasdial here — just save creds for later
# The credentials are stored per-user via the cmdkey mechanism
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
Write-Host "  Connection created, credentials stored" -ForegroundColor Green

# ============================================================================
# STEP 3 — Set IKEv2 IPsec crypto to match strongSwan server
# ============================================================================
Write-Host "`n=== [3/4] Configuring IPsec crypto ===" -ForegroundColor Cyan

try {
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants "SHA256128" `
        -CipherTransformConstants "AES256" `
        -DHGroup "Group14" `
        -EncryptionMethod "AES256" `
        -IntegrityCheckMethod "SHA256" `
        -PfsGroup "ECP384" `
        -Force | Out-Null
    Write-Host "  IPsec crypto: AES256/SHA256/Group14/ECP384" -ForegroundColor Green
} catch {
    Write-Warning "Set-VpnConnectionIPsecConfiguration failed: $_"
    Write-Warning "The connection will use Windows defaults — this may not work."
}

# Also set the registry key that enables strong DH (Group14+) for EAP-MSCHAPv2
# Without this, Windows defaults to Group2 (1024-bit DH) which is too weak
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters"
New-ItemProperty -Path $regPath -Name "NegotiateDH2048_AES256" `
    -PropertyType DWord -Value 1 -Force | Out-Null
Write-Host "  Registry: NegotiateDH2048_AES256 = 1 (enables Group14+)" -ForegroundColor Green

# ============================================================================
# STEP 4 — Connect
# ============================================================================
Write-Host "`n=== [4/4] Connecting to $ServerHostname ===" -ForegroundColor Cyan

# Kill any existing connection first
rasdial $ConnectionName /disconnect 2>&1 | Out-Null
Start-Sleep -Seconds 1

# Connect with credentials
$connect = rasdial $ConnectionName $Username $Password
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Connected!" -ForegroundColor Green
} else {
    Write-Host "  rasdial exit code: $LASTEXITCODE" -ForegroundColor Red
    Write-Host "  $connect"
    exit 1
}

# ============================================================================
# VERIFY
# ============================================================================
Write-Host "`n=== Verifying ===" -ForegroundColor Cyan
Start-Sleep -Seconds 2

$conn = Get-VpnConnection -Name $ConnectionName
Write-Host "  Status:        $($conn.ConnectionStatus)"
Write-Host "  Server:        $($conn.ServerAddress)"
Write-Host "  Tunnel type:   $($conn.TunnelType)"
Write-Host "  Auth method:   $($conn.AuthenticationMethod)"

# Pull the route table for the VPN adapter
$adapter = Get-NetAdapter | Where-Object { $_.InterfaceDescription -match "DatabyteVPN" -or $_.Name -match "DatabyteVPN" } | Select-Object -First 1
if ($adapter) {
    $routes = Get-NetRoute -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue
    Write-Host "  VPN routes:"
    $routes | ForEach-Object { Write-Host "    $($_.DestinationPrefix) -> $($_.NextHop)" }
}

Write-Host ""
Write-Host "Test commands (PowerShell):" -ForegroundColor Cyan
Write-Host "  # First hop should be the VPS, NOT your home router:"
Write-Host "  tracert 8.8.8.8"
Write-Host ""
Write-Host "  # iperf3 through the tunnel (expected ~17-20 Mbps with cap):"
Write-Host "  iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30"
Write-Host "  iperf3 -c iperf.angolacables.co.ao -p 9200 -R -t 30"
Write-Host ""
Write-Host "To disconnect:  rasdial $ConnectionName /disconnect"
Write-Host "To reconnect:   $PSCommandPath   (re-runs the whole setup safely)"
