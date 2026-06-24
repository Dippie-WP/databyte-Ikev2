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
$ServerHostname   = "myvpn.databyte.co.za"
$ConnectionName   = "DatabyteVPN"
$Username         = "zun-operator"
$Password         = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# ============================================================================
# STEP 1 - Verify server cert is publicly trusted (Let's Encrypt)
# ============================================================================
Write-Host ""
Write-Host "=== [1/4] Verifying server TLS cert (Let's Encrypt) ===" -ForegroundColor Cyan

try {
    # Use .NET directly to avoid PS 5.1 parsing bugs with `echo | Invoke-WebRequest`
    $req = [System.Net.HttpWebRequest]::Create("https://$ServerHostname")
    $req.Timeout = 10000
    $req.GetResponse().Close()
    $cert = $req.ServicePoint.Certificate
    Write-Host "  Server cert subject: $($cert.Subject)" -ForegroundColor Green
    Write-Host "  Issuer:               $($cert.Issuer)" -ForegroundColor Green
    Write-Host "  Valid from:           $($cert.GetEffectiveDateString())" -ForegroundColor Green
    Write-Host "  Expires:              $($cert.GetExpirationDateString())" -ForegroundColor Green

    if ($cert.Issuer -match "Let's Encrypt" -or $cert.Issuer -match "ISRG") {
        Write-Host "  Chain: Let's Encrypt (ISRG Root) — publicly trusted by Windows." -ForegroundColor Green
    } else {
        Write-Host "  WARNING: Cert issuer is not Let's Encrypt. Verify manually." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Could not fetch server cert: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "  Continuing anyway..." -ForegroundColor Yellow
}

# ============================================================================
# STEP 2 - Remove old connection (if exists) and create fresh one
# ============================================================================
Write-Host ""
Write-Host "=== [2/4] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# IMPORTANT: do NOT pass -SplitTunneling (omitted = full tunnel = all traffic
# goes through VPN, including internet).
Add-VpnConnection `
    -Name $ConnectionName `
    -ServerAddress $ServerHostname `
    -TunnelType "IKEv2" `
    -AuthenticationMethod "EAP" `
    -RememberCredential `
    -PassThru | Out-Null

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
