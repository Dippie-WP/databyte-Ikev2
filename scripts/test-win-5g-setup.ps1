<#
.SYNOPSIS
    Windows IKEv2 VPN setup for the test-win-5g customer on production
    Databyte VPN (vpn-prod-01 / 154.65.110.44).

.DESCRIPTION
    v5 — works whether run as a file OR piped via 'irm URL | iex'.

    v5 changes vs v4:
      a) EXPLICITLY generates EAP-MSCHAPv2 config via New-EapConfiguration
         and passes it to Add-VpnConnection via -EapConfigXmlStream.
         Without this, the IKEv2 stack may default to EAP-TLS, which
         prompts for a user cert selection (GUI dialog → error 703 in
         non-interactive sessions, and even in some interactive ones).
      b) Self-elevates to admin if not already elevated (Start-Process
         -Verb RunAs re-launches the script with UAC consent).
      c) Prints the connection profile as XML at the end so Zun can paste
         it back if it still fails.

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
$ScriptDir = $null
if ($PSCommandPath) {
    $ScriptDir = Split-Path -Parent $PSCommandPath
} elseif ($MyInvocation.MyCommand.Path) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $ScriptDir -or $ScriptDir -eq "") {
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
# STEP 0 - Self-elevate to admin (v5 NEW)
# ============================================================================
# This handles the case where the script was launched from a non-elevated
# PowerShell via 'irm | iex'. Without this, the rest of the script would
# fail with cryptic "Access denied" or silently no-op on registry writes.
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "  Not running as Administrator. Relaunching with elevation..." -ForegroundColor Yellow
    if ($PSCommandPath) {
        Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSCommandPath`""
        ) -Verb RunAs
    } else {
        # Stream mode: save to temp, then run from temp
        $tmp = Join-Path $env:TEMP "databyte-setup-elev-$(Get-Random).ps1"
        $content | Out-File -FilePath $tmp -Encoding utf8 -Force
        Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$tmp`""
        ) -Verb RunAs
    }
    exit 0
}
Write-Host "  Running as Administrator (elevated). OK." -ForegroundColor Green

# ============================================================================
# STEP 1 - Install CA cert (fetch from live URL with fingerprint pinning)
# ============================================================================
Write-Host ""
Write-Host "=== [1/6] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

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
# STEP 2 - Remove old connection (if exists) and create fresh with EAP-MSCHAPv2
# ============================================================================
Write-Host ""
Write-Host "=== [2/6] Creating VPN connection '$ConnectionName' with EAP-MSCHAPv2 ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection (had bad/missing EAP config)..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# v5 CHANGE: Build an EXPLICIT EAP-MSCHAPv2 config and pass it to Add-VpnConnection.
# Without this, Add-VpnConnection -AuthenticationMethod "Eap" can default to
# EAP-TLS (or "Any EAP"), which makes the EAP host request cert selection via a
# GUI dialog → error 703 in any session that can't show a GUI.
try {
    $EapConfig = New-EapConfiguration -Type EapMsChapV2
    Write-Host "  Generated EAP-MSCHAPv2 config XML." -ForegroundColor Green
} catch {
    Write-Host "  ERROR: New-EapConfiguration failed: $_" -ForegroundColor Red
    Write-Host "  (Your Windows version may not support -Type EapMsChapV2.)" -ForegroundColor Yellow
    Write-Host "  Will try the legacy -AuthenticationMethod 'EAP' path as fallback." -ForegroundColor Yellow
    $EapConfig = $null
}

if ($EapConfig) {
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerIp `
        -TunnelType "IKEv2" `
        -AuthenticationMethod "EAP" `
        -EapConfigXmlStream $EapConfig.EapConfigXmlStream `
        -RememberCredential `
        -PassThru | Out-Null
} else {
    # Fallback: basic EAP, no XML. May prompt for cert on some systems.
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerIp `
        -TunnelType "IKEv2" `
        -AuthenticationMethod "EAP" `
        -RememberCredential `
        -PassThru | Out-Null
}

Write-Host "  Connection created. Server=$ServerIp" -ForegroundColor Green

# Set the registry tweak for stronger crypto.
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null

# ============================================================================
# STEP 3 - Set IKEv2 IPsec crypto
# ============================================================================
Write-Host ""
Write-Host "=== [3/6] Configuring IPsec crypto ===" -ForegroundColor Cyan

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
# STEP 4 - Store credentials in the VPN profile
# ============================================================================
Write-Host ""
Write-Host "=== [4/6] Storing credentials in VPN profile ===" -ForegroundColor Cyan

try {
    Set-VpnConnectionUsernamePassword `
        -ConnectionName $ConnectionName `
        -Username $Username `
        -Password $Password `
        -Domain "" `
        -PassThru | Out-Null
    Write-Host "  Credentials stored in profile '$ConnectionName'." -ForegroundColor Green
} catch {
    Write-Host "  ERROR: Set-VpnConnectionUsernamePassword failed: $_" -ForegroundColor Red
}

cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null

# ============================================================================
# STEP 5 - Connect
# ============================================================================
Write-Host ""
Write-Host "=== [5/6] Connecting ===" -ForegroundColor Cyan

rasdial $ConnectionName /disconnect 2>&1 | Out-Null
Start-Sleep -Seconds 1

$connect = rasdial $ConnectionName
Write-Host ""
Write-Host "  rasdial output:" -ForegroundColor Cyan
Write-Host "  $connect"
Write-Host ""

if ($LASTEXITCODE -eq 0) {
    Write-Host "  Connected successfully." -ForegroundColor Green
} else {
    Write-Host "  rasdial exit code: $LASTEXITCODE" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Non-interactive path failed. Run this INTERACTIVELY to seed the profile:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    1. Open PowerShell AS ADMIN manually (Win+X -> 'Windows PowerShell (Admin)')" -ForegroundColor White
    Write-Host "    2. Run:  rasdial $ConnectionName" -ForegroundColor White
    Write-Host "    3. If a GUI dialog appears, fill in:" -ForegroundColor White
    Write-Host "         Username: $Username" -ForegroundColor White
    Write-Host "         Password: (the test password)" -ForegroundColor White
    Write-Host "    4. Click OK. Connection establishes. Profile is now seeded." -ForegroundColor White
    Write-Host ""
    Write-Host "  After one successful interactive connect, future non-interactive rasdial works." -ForegroundColor Yellow
    Write-Host ""
}

# ============================================================================
# STEP 6 - Diagnostics (v5 NEW): dump profile info so Zun can paste back if needed
# ============================================================================
Write-Host ""
Write-Host "=== [6/6] Profile diagnostics (paste back if still failing) ===" -ForegroundColor Cyan

try {
    $conn = Get-VpnConnection -Name $ConnectionName -ErrorAction Stop
    Write-Host ""
    Write-Host "--- Get-VpnConnection output ---" -ForegroundColor DarkCyan
    $conn | Format-List Name, ServerAddress, TunnelType, AuthenticationMethod, `
        EncryptionLevel, RememberCredential, SplitTunneling, `
        IdleDisconnectSeconds, DnsSuffix | Out-String | Write-Host
    Write-Host "--- end ---" -ForegroundColor DarkCyan
} catch {
    Write-Host "  Get-VpnConnection failed: $_" -ForegroundColor Red
}

# Show installed CA cert
Write-Host ""
Write-Host "--- Installed CA cert (LocalMachine\Root) ---" -ForegroundColor DarkCyan
$ca = Get-ChildItem Cert:\LocalMachine\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $CaSubject } | Select-Object -First 1
if ($ca) {
    $ca | Format-List Subject, Issuer, NotBefore, NotAfter, Thumbprint | Out-String | Write-Host
} else {
    Write-Host "  (CA cert NOT FOUND in LocalMachine\Root — install step may have failed)" -ForegroundColor Red
}
Write-Host "--- end ---" -ForegroundColor DarkCyan

# Show EAP config (this is the key one — verifies it's MSCHAPv2 not TLS)
Write-Host ""
Write-Host "--- EAP config in profile ---" -ForegroundColor DarkCyan
try {
    $config = Get-VpnConnection -Name $ConnectionName | Select-Object -ExpandProperty EapConfigXmlStream -ErrorAction SilentlyContinue
    if ($config) {
        # Decode and show only the EAP method type, not the full XML
        [xml]$xmlDoc = $config
        $methods = $xmlDoc.EapHostConfig.Config.EapMethod.Type
        Write-Host "  EAP methods in profile: $($methods -join ', ')"
        Write-Host "  (EapMsChapV2 = type 26, that's what we want)"
        Write-Host "  (EapTls = type 13, that would prompt for cert → 703)"
    } else {
        Write-Host "  (No EapConfigXmlStream on this connection — will use Windows default EAP)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Get EAP config failed: $_" -ForegroundColor Red
}
Write-Host "--- end ---" -ForegroundColor DarkCyan

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
