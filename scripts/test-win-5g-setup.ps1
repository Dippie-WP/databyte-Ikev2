<#
.SYNOPSIS
    Windows IKEv2 VPN setup for the test-win-5g customer on production
    Databyte VPN (vpn-prod-01 / 154.65.110.44).

.DESCRIPTION
    v6 — works whether run as a file OR piped via 'irm URL | iex'.
    Compatible with older Windows 10 builds (1709 - 1903 era) where
    Set-VpnConnectionUsernamePassword and New-EapConfiguration -Type
    are NOT available.

    v6 changes vs v5:
      a) Embeds EAP-MSCHAPv2 XML as a here-string (no New-EapConfiguration
         cmdlet needed). Type=26 forces MSCHAPv2, NOT EAP-TLS.
      b) Uses cmdkey to store creds in Windows Credential Manager
         (not Set-VpnConnectionUsernamePassword). This is the documented
         fallback per the Microsoft Support community thread that flagged
         "rasdial does not change the previous username and password" —
         the issue was a stale Credential Manager cache. v6 calls
         `cmdkey /delete` first to clear it, then `cmdkey /generic:...`
         to set fresh creds.
      c) Self-elevates to admin via Start-Process -Verb RunAs.
      d) Dumps profile + EAP config + cmdkey entries at the end for
         diagnostics — paste back if still failing.

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
# EAP-MSCHAPv2 EapHostConfig XML (canonical, from MS Learn)
# Type 26 = EapMsChapV2 (NOT 13 = EapTls which would prompt for cert)
# ============================================================================
$EapMschapv2Xml = @"
<EapHostConfig xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
  <EapMethod>
    <Type xmlns="http://www.microsoft.com/provisioning/EapCommon">26</Type>
    <VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
    <VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
    <AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</AuthorId>
  </EapMethod>
  <Config xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
    <Eap xmlns="http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1">
      <Type>26</Type>
      <EapType xmlns="http://www.microsoft.com/provisioning/MsChapV2ConnectionPropertiesV1">
        <UseWinLogonCredentials>false</UseWinLogonCredentials>
      </EapType>
    </Eap>
  </Config>
</EapHostConfig>
"@

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
# STEP 0 - Self-elevate to admin
# ============================================================================
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
Write-Host "=== [2/6] Creating VPN connection '$ConnectionName' with EAP-MSCHAPv2 (type 26) ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# v6 CHANGE: Use embedded EapHostConfig XML directly. No dependency on
# New-EapConfiguration cmdlet (not available on older Win10 builds).
# The XML has Type=26 (MSCHAPv2), NOT Type=13 (TLS).
Add-VpnConnection `
    -Name $ConnectionName `
    -ServerAddress $ServerIp `
    -TunnelType "IKEv2" `
    -AuthenticationMethod "EAP" `
    -EapConfigXmlStream $EapMschapv2Xml `
    -RememberCredential `
    -PassThru | Out-Null

Write-Host "  Connection created with explicit EAP-MSCHAPv2 (type 26)." -ForegroundColor Green
Write-Host "  Server=$ServerIp, Tunnel=IKEv2, Auth=EAP/MSCHAPv2" -ForegroundColor Green

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
# STEP 4 - Store credentials via cmdkey (compatible with older Windows)
# ============================================================================
Write-Host ""
Write-Host "=== [4/6] Storing credentials via Windows Credential Manager ===" -ForegroundColor Cyan

# v6 CHANGE: clear any stale cached creds first. The Microsoft Support
# thread "rasdial does not change the previous username and password"
# identified Credential Manager cache as the cause of stale creds being
# used. We delete-then-set to ensure freshness.
$cmdkeyTargets = @(
    $ConnectionName,           # generic by connection name
    "RAS:$ConnectionName",     # generic by RAS-namespace name
    $ServerIp,                 # by server IP
    "TERMSRV:$ServerIp"        # by Remote Desktop namespace (in case of conflict)
)
foreach ($target in $cmdkeyTargets) {
    $delOut = cmdkey /delete:$target 2>&1
    # silently ignore "not found" errors
}

# Now store fresh creds in Credential Manager
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
cmdkey /generic:"RAS:$ConnectionName" /user:$Username /pass:$Password | Out-Null
Write-Host "  Cleared stale creds, stored fresh creds for '$Username' in Credential Manager." -ForegroundColor Green
Write-Host "  (rasdial reads from here, NOT from the VPN profile directly)" -ForegroundColor DarkCyan

# ============================================================================
# STEP 5 - Connect (no inline creds, let Windows read from Credential Manager)
# ============================================================================
Write-Host ""
Write-Host "=== [5/6] Connecting (rasdial reads from Credential Manager) ===" -ForegroundColor Cyan

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
    Write-Host "  If 703: profile likely still has wrong EAP method. Check step 6 diagnostics." -ForegroundColor Yellow
    Write-Host "  Other errors:" -ForegroundColor Yellow
    Write-Host "    691 = Auth failed (creds)" -ForegroundColor Yellow
    Write-Host "    789 = IKE auth failed (cert or crypto)" -ForegroundColor Yellow
    Write-Host "    800 = Can't reach server (firewall)" -ForegroundColor Yellow
    Write-Host "    13801 = IKE creds unacceptable" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  FALLBACK: Open PowerShell AS ADMIN manually, then run:" -ForegroundColor Yellow
    Write-Host "    rasdial $ConnectionName" -ForegroundColor White
    Write-Host "  If a GUI dialog appears, fill in user/pass and click OK." -ForegroundColor White
    Write-Host "  One successful interactive connect seeds the creds permanently." -ForegroundColor Yellow
    Write-Host ""
}

# ============================================================================
# STEP 6 - Diagnostics
# ============================================================================
Write-Host ""
Write-Host "=== [6/6] Profile diagnostics (paste back if still failing) ===" -ForegroundColor Cyan

try {
    $conn = Get-VpnConnection -Name $ConnectionName -ErrorAction Stop
    Write-Host ""
    Write-Host "--- Get-VpnConnection ---" -ForegroundColor DarkCyan
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
    Write-Host "  (CA cert NOT FOUND in LocalMachine\Root)" -ForegroundColor Red
}
Write-Host "--- end ---" -ForegroundColor DarkCyan

# Show EAP config — verify type 26 (MSCHAPv2) not 13 (TLS)
Write-Host ""
Write-Host "--- EAP config in profile ---" -ForegroundColor DarkCyan
try {
    $config = $conn.EapConfigXmlStream
    if ($config) {
        [xml]$xmlDoc = $config
        $ns = New-Object Xml.XmlNamespaceManager($xmlDoc.NameTable)
        $ns.AddNamespace("e", "http://www.microsoft.com/provisioning/EapHostConfig")
        $types = $xmlDoc.SelectNodes("//e:EapMethod/e:Type", $ns) | ForEach-Object { $_.InnerText }
        Write-Host "  EAP methods in profile: $($types -join ', ')"
        Write-Host "  (EapMsChapV2 = type 26, what we want)"
        Write-Host "  (EapTls = type 13, would prompt for cert -> 703)"
    } else {
        Write-Host "  (No EapConfigXmlStream on this connection)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Get EAP config failed: $_" -ForegroundColor Red
}
Write-Host "--- end ---" -ForegroundColor DarkCyan

# Show Credential Manager entries for this connection
Write-Host ""
Write-Host "--- cmdkey entries (Credential Manager) ---" -ForegroundColor DarkCyan
try {
    $listOut = cmdkey /list 2>&1
    $relevant = $listOut | Where-Object { $_ -match [regex]::Escape($ConnectionName) -or $_ -match [regex]::Escape($ServerIp) }
    if ($relevant) {
        $relevant | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "  (No cmdkey entries for '$ConnectionName' or '$ServerIp')" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  cmdkey /list failed: $_" -ForegroundColor Red
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
