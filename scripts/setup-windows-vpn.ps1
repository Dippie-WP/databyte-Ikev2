<#
.SYNOPSIS
    Sets up a Windows IKEv2 VPN connection to the Databyte VPN server.
.DESCRIPTION
    Run as Administrator in PowerShell. Self-contained: credentials and
    server details are baked in below. Idempotent: re-running safely
    updates an existing connection.

    What it does:
      1. Fetches the strongSwan CA cert from the live portal URL (preferred)
         or falls back to a bundled copy. Verifies SHA256 fingerprint before
         installing (defence against MITM / cert substitution attacks).
      2. Creates (or updates) a "DatabyteVPN" connection with NO split tunneling
      3. Sets IKEv2 IPsec crypto to match the strongSwan server config
      4. Connects the VPN with the operator credentials

    Usage:
      PS> powershell -ExecutionPolicy Bypass -File setup-windows-vpn.ps1
      # then re-run anytime to reconnect

    CA cert pinning (defence-in-depth):
      Even though Windows validates the cert chain against the installed CA,
      we also verify the SHA256 fingerprint of the cert we fetch against the
      pinned value below. This means a compromised CA on your system OR a
      network attacker can't substitute a different cert.

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

# CA cert pinned SHA256 fingerprint (the cert we EXPECT to receive).
# This is the strongSwan CA from /opt/strongswan-vpn-gateway/docker/swanctl/x509ca/
# on the production VPN server. Defence against MITM — if the fetched cert
# doesn't match this fingerprint, we refuse to install it.
$ExpectedCaSha256 = "5C:10:B9:6A:97:06:10:29:7C:8D:8F:B3:6B:E3:5A:98:58:CF:F4:10:C8:1E:72:78:7E:25:08:43:B2:71:CE:06"

# URL to fetch the live CA cert from. Cloudflare edge caches for 24h.
$CaCertUrl = "https://$ServerHostname/certs/strongswan-ca.crt.pem"

# Path to the bundled (fallback) CA cert. Used only if the live fetch fails.
# Default: expects it in the same directory as this script.
$CaCertFallback = Join-Path $PSScriptRoot "strongswan-ca.crt.pem"
if (-not (Test-Path $CaCertFallback)) {
    $CaCertFallback = ".\strongswan-ca.crt.pem"
}

# Expected CA cert subject for "already installed" check.
$CaSubject = "CN=strongSwan CA"

# ============================================================================
# Helper: compute SHA256 fingerprint of a file (returns colon-separated hex)
# ============================================================================
function Get-Sha256Fingerprint {
    param([Parameter(Mandatory=$true)][string]$Path)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $hash = $sha256.ComputeHash($stream)
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha256.Dispose()
    }
    # Format as colon-separated uppercase hex
    return ($hash | ForEach-Object { $_.ToString("X2") }) -join ":"
}

# ============================================================================
# STEP 1 - Install CA cert (fetch from live URL with fingerprint pinning)
# ============================================================================
Write-Host ""
Write-Host "=== [1/4] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

# First, check if the right cert is already installed. Compare by SHA256
# so a re-run on an already-good system is a no-op.
$installed = Get-ChildItem Cert:\LocalMachine\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $CaSubject } |
    Select-Object -First 1

if ($installed) {
    Write-Host "  Found existing cert with subject '$CaSubject'" -ForegroundColor Yellow
    $tempPath = Join-Path $env:TEMP "databyte-installed-ca-$(Get-Random).cer"
    try {
        # Export to a temp file to compute SHA256 (Windows cert objects
        # don't expose raw cert bytes directly without Export-Certificate).
        Export-Certificate -Cert $installed -FilePath $tempPath -Type CERT | Out-Null
        $installedSha = Get-Sha256Fingerprint -Path $tempPath
    } finally {
        Remove-Item $tempPath -ErrorAction SilentlyContinue
    }

    if ($installedSha -eq $ExpectedCaSha256) {
        Write-Host "  Cert already installed with correct fingerprint ($installedSha). Skipping." -ForegroundColor Green
        $needsInstall = $false
    } else {
        Write-Host "  WARN: installed cert fingerprint ($installedSha)" -ForegroundColor Red
        Write-Host "        does NOT match expected ($ExpectedCaSha256)" -ForegroundColor Red
        Write-Host "        Removing and re-installing correct cert..." -ForegroundColor Yellow
        Remove-Item "Cert:\LocalMachine\Root\$($installed.Thumbprint)" -Force -ErrorAction SilentlyContinue
        $needsInstall = $true
    }
} else {
    $needsInstall = $true
}

if ($needsInstall) {
    # Try live fetch first (preferred — freshest, comes from production server).
    Write-Host "  Fetching CA cert from $CaCertUrl ..."

    # Some Windows systems may not have $env:TEMP set in non-interactive contexts.
    # Fall back to a known-good path.
    $tempBase = $env:TEMP
    if (-not $tempBase) {
        $tempBase = Join-Path $env:SystemRoot "Temp"
        Write-Host "  Note: \$env:TEMP not set, using $tempBase" -ForegroundColor Yellow
    }
    $tempLive = Join-Path $tempBase "databyte-live-ca-$(Get-Random).pem"

    try {
        try {
            # -UseBasicParsing: don't need IE parser. -TimeoutSec 15.
            Invoke-WebRequest -Uri $CaCertUrl -OutFile $tempLive -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop | Out-Null
            $liveSha = Get-Sha256Fingerprint -Path $tempLive
            if ($liveSha -eq $ExpectedCaSha256) {
                Write-Host "  Live cert SHA256 matches pinned value." -ForegroundColor Green
                $CaCertPath = $tempLive
                $certSource = "live"
            } else {
                Write-Host "  ERROR: Live cert SHA256 mismatch." -ForegroundColor Red
                Write-Host "    Expected: $ExpectedCaSha256" -ForegroundColor Red
                Write-Host "    Got:      $liveSha" -ForegroundColor Red
                Write-Host "    Possible MITM attack OR server cert was rotated without updating this script." -ForegroundColor Red
                Write-Host "    Falling back to bundled cert..." -ForegroundColor Yellow
                if (Test-Path $tempLive) { Remove-Item $tempLive -ErrorAction SilentlyContinue }
                $tempLive = $null
            }
        } catch {
            Write-Host "  Live fetch failed: $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "  Falling back to bundled cert..." -ForegroundColor Yellow
            if ($tempLive -and (Test-Path $tempLive)) { Remove-Item $tempLive -ErrorAction SilentlyContinue }
            $tempLive = $null
        }

        # If live didn't work, try bundled fallback
        if (-not $certSource) {
            if (-not (Test-Path $CaCertFallback)) {
                Write-Host ""
                Write-Host "  ERROR: Bundled fallback cert not found at: $CaCertFallback" -ForegroundColor Red
                Write-Host "  Place strongswan-ca.crt.pem next to this script and try again." -ForegroundColor Red
                Write-Host ""
                exit 1
            }
            $bundledSha = Get-Sha256Fingerprint -Path $CaCertFallback
            if ($bundledSha -eq $ExpectedCaSha256) {
                Write-Host "  Bundled cert SHA256 matches pinned value." -ForegroundColor Green
                $CaCertPath = $CaCertFallback
                $certSource = "bundled-fallback"
            } else {
                Write-Host ""
                Write-Host "  ERROR: Bundled cert SHA256 mismatch." -ForegroundColor Red
                Write-Host "    Expected: $ExpectedCaSha256" -ForegroundColor Red
                Write-Host "    Got:      $bundledSha" -ForegroundColor Red
                Write-Host "    Either the bundled cert is stale OR the pinned value in this script is wrong." -ForegroundColor Red
                Write-Host "    Refusing to install. Please update either the bundled cert or the \$ExpectedCaSha256." -ForegroundColor Red
                Write-Host ""
                exit 1
            }
        }

        # Install the cert
        Import-Certificate -FilePath $CaCertPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        if ($certSource -eq "live") {
            Write-Host "  CA cert installed from LIVE URL." -ForegroundColor Green
        } else {
            Write-Host "  CA cert installed from BUNDLED FALLBACK." -ForegroundColor Yellow
            Write-Host "  WARN: live fetch failed. Re-run when network is healthy to refresh from URL." -ForegroundColor Yellow
        }
    } finally {
        # Clean up temp live cert if it was downloaded
        if ($tempLive -and (Test-Path $tempLive)) {
            Remove-Item $tempLive -ErrorAction SilentlyContinue
        }
    }
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

# PowerShell 5.1 quirk: 'catch' must be on its own line, not after '}'.
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
    Write-Host "  IPsec crypto: AES128/SHA256/ECP384 (matches homelab-tested path)" -ForegroundColor Green
}
catch
{
    Write-Warning "Set-VpnConnectionIPsecConfiguration failed: $_"
    Write-Warning "The connection will use Windows defaults (DES3/SHA1/DH2 — strongSwan will reject as insecure)."
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
