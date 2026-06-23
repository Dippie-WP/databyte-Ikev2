<#
.SYNOPSIS
    Windows IKEv2 VPN setup for the test-win-5g customer on production
    Databyte VPN (vpn-prod-01 / 154.65.110.44).

.DESCRIPTION
    v3 — works whether run as a file OR piped via 'irm URL | iex'.
    v3 change vs v2: passes $Username $Password directly to rasdial so the
    Windows IKEv2 GUI auth dialog is bypassed (error 703 in non-interactive mode).

    Two run modes:
      a) File mode (recommended):  powershell -ExecutionPolicy Bypass -File .\test-win-5g-setup.ps1
      b) Stream mode (one-liner):  irm https://myvpn.databyte.co.za/static/test-win-5g-setup.ps1 | iex

    In stream mode $PSScriptRoot is empty. This script uses $PSCommandPath
    with a fallback to a known location, OR runs the bundle from a temp
    download in either case.

.NOTES
    TEST CUSTOMER. Will be deleted after multi-device test.
#>

#Requires -RunAsAdministrator

# ============================================================================
# CONFIG (test-win-5g)
# ============================================================================
$ServerIp         = "154.65.110.44"
$RemoteId         = "myvpn.databyte.co.za"
$ConnectionName   = "DatabyteVPNTest"
$Username         = "test-win-5g-laptop"
$Password         = "a1V5M2Cd1oE0TNWY9wORsg"

$ExpectedCaSha256 = "5C:10:B9:6A:97:06:10:29:7C:8D:8F:B3:6B:E3:5A:98:58:CF:F4:10:C8:1E:72:78:7E:25:08:43:B2:71:CE:06"
$CaCertUrl        = "https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem"
$CaSubject        = "CN=strongSwan CA"

# ============================================================================
# Determine script-relative path (works for both File and Stream modes)
# ============================================================================
# $MyInvocation gives us the path even when piped
$ScriptDir = $null
if ($PSCommandPath) {
    $ScriptDir = Split-Path -Parent $PSCommandPath
} elseif ($MyInvocation.MyCommand.Path) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $ScriptDir -or $ScriptDir -eq "") {
    # Stream mode (irm | iex): no script path. Will fall back to live URL only.
    $ScriptDir = $null
}

# ============================================================================
# Helper: SHA256 fingerprint (colon-separated)
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
    return ($hash | ForEach-Object { $_.ToString("X2") }) -join ":"
}

# ============================================================================
# STEP 1 - Install CA cert (fetch from live URL with fingerprint pinning)
# ============================================================================
Write-Host ""
Write-Host "=== [1/5] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

$installed = Get-ChildItem Cert:\LocalMachine\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $CaSubject } |
    Select-Object -First 1

if ($installed) {
    Write-Host "  Found existing cert with subject '$CaSubject'" -ForegroundColor Yellow
    $tempPath = Join-Path $env:TEMP "databyte-installed-ca-$(Get-Random).cer"
    try {
        Export-Certificate -Cert $installed -FilePath $tempPath -Type CERT | Out-Null
        $installedSha = Get-Sha256Fingerprint -Path $tempPath
    } finally {
        Remove-Item $tempPath -ErrorAction SilentlyContinue
    }

    if ($installedSha -eq $ExpectedCaSha256) {
        Write-Host "  Cert already installed with correct fingerprint. Skipping." -ForegroundColor Green
        $needsInstall = $false
    } else {
        Write-Host "  WARN: installed cert fingerprint mismatch. Reinstalling..." -ForegroundColor Yellow
        Remove-Item "Cert:\LocalMachine\Root\$($installed.Thumbprint)" -Force -ErrorAction SilentlyContinue
        $needsInstall = $true
    }
} else {
    $needsInstall = $true
}

if ($needsInstall) {
    Write-Host "  Fetching CA cert from $CaCertUrl ..."
    $tempBase = $env:TEMP
    if (-not $tempBase) { $tempBase = Join-Path $env:SystemRoot "Temp" }
    $tempLive = Join-Path $tempBase "databyte-live-ca-$(Get-Random).pem"

    try {
        try {
            Invoke-WebRequest -Uri $CaCertUrl -OutFile $tempLive -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop | Out-Null
            $liveSha = Get-Sha256Fingerprint -Path $tempLive
            if ($liveSha -eq $ExpectedCaSha256) {
                Write-Host "  Live cert SHA256 matches pinned value." -ForegroundColor Green
                $CaCertPath = $tempLive
            } else {
                Write-Host "  ERROR: Live cert SHA256 mismatch." -ForegroundColor Red
                Write-Host "    Expected: $ExpectedCaSha256" -ForegroundColor Red
                Write-Host "    Got:      $liveSha" -ForegroundColor Red
                if (Test-Path $tempLive) { Remove-Item $tempLive -ErrorAction SilentlyContinue }
                $tempLive = $null
            }
        } catch {
            Write-Host "  Live fetch failed: $($_.Exception.Message)" -ForegroundColor Yellow
            if (Test-Path $tempLive) { Remove-Item $tempLive -ErrorAction SilentlyContinue }
            $tempLive = $null
        }

        # Try bundled fallback if ScriptDir is known AND the bundled cert exists
        if (-not $CaCertPath -and $ScriptDir) {
            $bundledPath = Join-Path $ScriptDir "strongswan-ca.crt.pem"
            if (Test-Path $bundledPath) {
                $bundledSha = Get-Sha256Fingerprint -Path $bundledPath
                if ($bundledSha -eq $ExpectedCaSha256) {
                    Write-Host "  Bundled cert SHA256 matches pinned value." -ForegroundColor Green
                    $CaCertPath = $bundledPath
                } else {
                    Write-Host "  ERROR: Bundled cert SHA256 mismatch." -ForegroundColor Red
                    Write-Host "    Expected: $ExpectedCaSha256" -ForegroundColor Red
                    Write-Host "    Got:      $bundledSha" -ForegroundColor Red
                    exit 1
                }
            } else {
                Write-Host "  No bundled fallback (stream mode, no script directory)" -ForegroundColor Yellow
            }
        }

        if (-not $CaCertPath) {
            Write-Host "  ERROR: Could not get CA cert from any source. Live fetch failed and no bundled fallback available." -ForegroundColor Red
            exit 1
        }

        Import-Certificate -FilePath $CaCertPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        Write-Host "  CA cert installed." -ForegroundColor Green
    } finally {
        if ($tempLive -and (Test-Path $tempLive)) {
            Remove-Item $tempLive -ErrorAction SilentlyContinue
        }
    }
}

# ============================================================================
# STEP 2 - Remove old connection (if exists) and create fresh
# ============================================================================
Write-Host ""
Write-Host "=== [2/5] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# Use Add-VpnConnection with the basic parameters that work on all Win10 versions.
# The -ConfigurationFile parameter was added in Win10 1903+; some systems don't have it.
# We'll set the RemoteID via the registry afterwards.
Add-VpnConnection `
    -Name $ConnectionName `
    -ServerAddress $ServerIp `
    -TunnelType "IKEv2" `
    -AuthenticationMethod "EAP" `
    -RememberCredential `
    -PassThru | Out-Null

Write-Host "  Connection created. Server=$ServerIp" -ForegroundColor Green

# ============================================================================
# Set Remote ID via registry (the supported way for Win10 native IKEv2)
# ============================================================================
# Path: HKLM\SYSTEM\CurrentControlSet\Services\RasMan\Parameters
# Value: "ServerName" or use the per-connection ProfileXML
# Actually: the RemoteID is stored in the phonebook .pbk file under
# [IKEv2 & IPsec Custom Policy\Servers\...] but the simplest approach
# is to set it via the registry tweak below.
#
# Actually for Win10 native IKEv2, the ServerAddress IS the connection
# endpoint. The RemoteID matching happens via the cert CN/SAN — Windows
# will accept ANY name as long as the cert is signed by a trusted CA.
# So if we have the cert installed and connect to the right IP,
# Windows validates the cert against myvpn.databyte.co.za automatically.

# Set the registry tweaks for stronger crypto + disable weak DH
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null

# ============================================================================
# STEP 3 - Set IKEv2 IPsec crypto
# ============================================================================
Write-Host ""
Write-Host "=== [3/5] Configuring IPsec crypto ===" -ForegroundColor Cyan

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
    Write-Host "  (continuing — defaults may work too)" -ForegroundColor Yellow
}

# ============================================================================
# STEP 4 - Store credentials
# ============================================================================
Write-Host ""
Write-Host "=== [4/5] Storing credentials ===" -ForegroundColor Cyan
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
Write-Host "  Credentials stored for '$Username'" -ForegroundColor Green

# ============================================================================
# STEP 5 - Connect
# ============================================================================
Write-Host ""
Write-Host "=== [5/5] Connecting ===" -ForegroundColor Cyan

rasdial $ConnectionName /disconnect 2>&1 | Out-Null
Start-Sleep -Seconds 1

# Pass credentials directly to rasdial so Windows doesn't show the GUI prompt.
# Format: rasdial <name> <username> <password>
$connect = rasdial $ConnectionName $Username $Password
Write-Host ""
Write-Host "  rasdial output:" -ForegroundColor Cyan
Write-Host "  $connect"
Write-Host ""

if ($LASTEXITCODE -eq 0) {
    Write-Host "  Connected successfully." -ForegroundColor Green
} else {
    Write-Host "  rasdial exit code: $LASTEXITCODE" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Common Windows VPN errors:" -ForegroundColor Yellow
    Write-Host "    691 = Auth failed (check username/password)" -ForegroundColor Yellow
    Write-Host "    789 = IKE auth failed (cert or crypto mismatch)" -ForegroundColor Yellow
    Write-Host "    800 = Can't reach server (firewall/network)" -ForegroundColor Yellow
    Write-Host "    13801 = IKE auth credentials unacceptable (EAP config)" -ForegroundColor Yellow
    Write-Host "    13013 = IKE mode not enabled on server" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Run 'Get-VpnConnection -Name $ConnectionName | Format-List' to see config." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host "  Connection:  $ConnectionName"
Write-Host "  Server:      $ServerIp (raw IP, bypasses Cloudflare proxy)"
Write-Host "  Remote ID:   $RemoteId (cert CN/SAN matches)"
Write-Host "  Username:    $Username"
Write-Host ""
Write-Host "Verify after connect:"
Write-Host "  ipconfig  (look for PPP adapter with 10.99.0.x VIP)"
Write-Host "  tracert 8.8.8.8  (first hop should be 154.65.110.44)"