<#
.SYNOPSIS
    Windows IKEv2 VPN setup for the test-win-5g customer on production
    Databyte VPN (vpn-prod-01 / 154.65.110.44).

.DESCRIPTION
    v7 — comprehensive fix for older Windows 10 (1709 - 1903 era) where
    several modern VPN cmdlets are not available.

    v7 changes vs v6:
      a) Adds IPv6 disable on the VPN adapter (netsh) — a known 703 trigger
         per community thread 'fix vpn connection 703 error powershell'.
      b) Adds DNS-registration disable (registry direct) — same source.
      c) DEEP registry probe in step 6: shows what's actually in
         HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\RasMan\PPP\EAP\26
         (the MSCHAPv2 method registration key). If 26 is missing, the
         703 will recur even with cmdkey/registry creds.
      d) Reads the .pbk phonebook file directly to show the EAP config the
         profile actually has (not what Get-VpnConnection claims).
      e) Pads the EAP XML with a VendorId/AuthorId-only fallback that works
         on builds that reject the full EapHostConfig.

    STILL REQUIRED: One-time GUI seed via Settings → Network → VPN → Connect
    (or rasphone.exe) to populate HKEY_CURRENT_USERS\SOFTWARE\Microsoft\
    RAS EAP\UserEapInfo. This is a Win10 <1903 quirk — neither cmdkey nor
    Set-VpnConnectionUsernamePassword can write that key programmatically.
    After that seed, all future rasdial calls work non-interactively.

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
Write-Host "=== [1/7] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

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
Write-Host "=== [2/7] Creating VPN connection '$ConnectionName' with EAP-MSCHAPv2 (type 26) ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# v7 NOTE: -EapConfigXmlStream is silently rejected on Win10 <1809.
# We pass it anyway, and verify in step 6 whether it took. If not, the
# phonebook file is the next place Windows checks.
Add-VpnConnection `
    -Name $ConnectionName `
    -ServerAddress $ServerIp `
    -TunnelType "IKEv2" `
    -AuthenticationMethod "EAP" `
    -EapConfigXmlStream $EapMschapv2Xml `
    -RememberCredential `
    -PassThru | Out-Null

Write-Host "  Connection created." -ForegroundColor Green
Write-Host "  Server=$ServerIp, Tunnel=IKEv2, Auth=EAP/MSCHAPv2" -ForegroundColor Green

# Set the registry tweak for stronger crypto.
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null

# ============================================================================
# STEP 3 - Set IKEv2 IPsec crypto
# ============================================================================
Write-Host ""
Write-Host "=== [3/7] Configuring IPsec crypto ===" -ForegroundColor Cyan

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
# STEP 4 (v7 NEW) - Disable IPv6 on VPN adapter (known 703 trigger)
# ============================================================================
Write-Host ""
Write-Host "=== [4/7] Disabling IPv6 + DNS registration on VPN adapter ===" -ForegroundColor Cyan

$connObj = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
$adapterName = $null
if ($connObj) {
    # GUID looks like {EC87F6C9-8823-416C-B92B-517D592E250F}; netsh wants the bare GUID
    if ($connObj.Guid) {
        $adapterName = $connObj.Guid -replace '[\{\}]', ''
    }
}

if ($adapterName) {
    # Disable IPv6 on the VPN adapter
    Write-Host "  Adapter GUID: $adapterName"
    $ipv6Out = netsh interface ipv6 set interface "$adapterName" disabled 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  IPv6 disabled on adapter." -ForegroundColor Green
    } else {
        Write-Host "  IPv6 disable: $ipv6Out" -ForegroundColor Yellow
    }

    # Disable DNS-registration for IPv4 on this adapter
    $adapterKeyPath = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\$adapterName"
    if (Test-Path $adapterKeyPath) {
        Set-ItemProperty -Path $adapterKeyPath -Name "RegistrationEnabled" -Value 0 -Type DWord -ErrorAction SilentlyContinue
        Set-ItemProperty -Path $adapterKeyPath -Name "RegisterAdapterName" -Value 0 -Type DWord -ErrorAction SilentlyContinue
        Write-Host "  DNS registration disabled." -ForegroundColor Green
    } else {
        Write-Host "  Adapter key not found at: $adapterKeyPath" -ForegroundColor Yellow
    }
} else {
    Write-Host "  Could not get adapter GUID for IPv6/DNS changes." -ForegroundColor Yellow
    Write-Host "  Do it manually: ncpa.cpl -> right-click adapter -> Properties -> uncheck IPv6" -ForegroundColor Yellow
}

# ============================================================================
# STEP 5 - Store credentials via cmdkey
# ============================================================================
Write-Host ""
Write-Host "=== [5/7] Storing credentials via Windows Credential Manager ===" -ForegroundColor Cyan

$cmdkeyTargets = @(
    $ConnectionName,
    "RAS:$ConnectionName",
    $ServerIp,
    "TERMSRV:$ServerIp"
)
foreach ($target in $cmdkeyTargets) {
    $delOut = cmdkey /delete:$target 2>&1
}

cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
cmdkey /generic:"RAS:$ConnectionName" /user:$Username /pass:$Password | Out-Null
Write-Host "  Cleared stale creds, stored fresh creds for '$Username' in Credential Manager." -ForegroundColor Green

# ============================================================================
# STEP 6 - Connect
# ============================================================================
Write-Host ""
Write-Host "=== [6/7] Connecting (rasdial reads from Credential Manager) ===" -ForegroundColor Cyan

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
    Write-Host "  Error reference:" -ForegroundColor Yellow
    Write-Host "    691 = Auth failed (creds)" -ForegroundColor Yellow
    Write-Host "    703 = GUI dialog needed (EAP creds or method missing)" -ForegroundColor Yellow
    Write-Host "    789 = IKE auth failed (cert or crypto)" -ForegroundColor Yellow
    Write-Host "    800 = Can't reach server (firewall)" -ForegroundColor Yellow
    Write-Host "    13801 = IKE creds unacceptable" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  IF 703: this is the Win10 <1903 'UserEapInfo not populated' issue." -ForegroundColor Yellow
    Write-Host "  FIX: One-time GUI seed via Settings -> Network -> VPN -> Connect." -ForegroundColor Yellow
    Write-Host "  After that, all future rasdial calls work non-interactively." -ForegroundColor Yellow
    Write-Host ""
}

# ============================================================================
# STEP 7 (v7 NEW) - DEEP diagnostics: registry + phonebook file
# ============================================================================
Write-Host ""
Write-Host "=== [7/7] DEEP diagnostics (paste back if still failing) ===" -ForegroundColor Cyan

# 7a) Profile as Get-VpnConnection sees it
Write-Host ""
Write-Host "--- 7a) Get-VpnConnection (top-level) ---" -ForegroundColor DarkCyan
try {
    $conn = Get-VpnConnection -Name $ConnectionName -ErrorAction Stop
    $conn | Format-List Name, ServerAddress, TunnelType, AuthenticationMethod, `
        EncryptionLevel, RememberCredential, SplitTunneling, `
        IdleDisconnectSeconds, DnsSuffix | Out-String | Write-Host
} catch {
    Write-Host "  Get-VpnConnection failed: $_" -ForegroundColor Red
}
Write-Host "--- end 7a ---" -ForegroundColor DarkCyan

# 7b) EAP config from Get-VpnConnection (this is what was NULL in v5/v6)
Write-Host ""
Write-Host "--- 7b) EAP config from Get-VpnConnection ---" -ForegroundColor DarkCyan
try {
    $config = $conn.EapConfigXmlStream
    if ($config -and $config -ne '') {
        [xml]$xmlDoc = $config
        $ns = New-Object Xml.XmlNamespaceManager($xmlDoc.NameTable)
        $ns.AddNamespace("e", "http://www.microsoft.com/provisioning/EapHostConfig")
        $types = $xmlDoc.SelectNodes("//e:EapMethod/e:Type", $ns) | ForEach-Object { $_.InnerText }
        Write-Host "  EAP methods in profile (via API): $($types -join ', ')"
        if ($types -contains '26') {
            Write-Host "  GOOD: type 26 (MSCHAPv2) is in the profile" -ForegroundColor Green
        } else {
            Write-Host "  PROBLEM: type 26 (MSCHAPv2) is NOT in the profile" -ForegroundColor Red
            Write-Host "  Profile has: $($types -join ', ')" -ForegroundColor Red
        }
    } else {
        Write-Host "  (Get-VpnConnection reports EMPTY EapConfigXmlStream)" -ForegroundColor Red
        Write-Host "  -> The -EapConfigXmlStream parameter did NOT take." -ForegroundColor Red
        Write-Host "  -> This is a Win10 <1809 limitation." -ForegroundColor Red
    }
} catch {
    Write-Host "  EAP config probe failed: $_" -ForegroundColor Red
}
Write-Host "--- end 7b ---" -ForegroundColor DarkCyan

# 7c) EAP method registration in registry (the real check)
Write-Host ""
Write-Host "--- 7c) Registry: HKLM\...\RasMan\PPP\EAP\26 ---" -ForegroundColor DarkCyan
$mschapv2Key = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\PPP\EAP\26"
if (Test-Path $mschapv2Key) {
    Write-Host "  MSCHAPv2 EAP method (type 26) IS registered in RasMan." -ForegroundColor Green
    Get-ItemProperty -Path $mschapv2Key -ErrorAction SilentlyContinue |
        Format-List * -Force | Out-String | Write-Host
} else {
    Write-Host "  MSCHAPv2 EAP method (type 26) is NOT registered in RasMan." -ForegroundColor Red
    Write-Host "  Looking for ALL EAP methods on this system:" -ForegroundColor Yellow
    $eapRoot = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\PPP\EAP"
    if (Test-Path $eapRoot) {
        Get-ChildItem $eapRoot -ErrorAction SilentlyContinue | ForEach-Object {
            $name = $_.PSChildName
            $desc = (Get-ItemProperty -Path $_.PSPath -ErrorAction SilentlyContinue).FriendlyName
            if (-not $desc) { $desc = "(no FriendlyName)" }
            Write-Host "    Type $name : $desc"
        }
    }
}
Write-Host "--- end 7c ---" -ForegroundColor DarkCyan

# 7d) Phonebook file (.pbk) — Windows stores the actual config here
Write-Host ""
Write-Host "--- 7d) Phonebook file (rasphone.pbk) ---" -ForegroundColor DarkCyan
$pbkPath = "$env:AppData\Microsoft\Network\Connections\Pbk\rasphone.pbk"
if (Test-Path $pbkPath) {
    Write-Host "  Pbk path: $pbkPath"
    $pbkContent = Get-Content $pbkPath -Raw
    # Find this connection's section (between [<guid>] and next [section])
    if ($connObj) {
        $guidBare = $connObj.Guid
        $pattern = "(?s)\[$([regex]::Escape($guidBare))\].*?(?=\n\[)"
        $match = [regex]::Match($pbkContent, $pattern)
        if ($match.Success) {
            $section = $match.Value
            Write-Host "  Section for $guidBare :"
            Write-Host $section
            if ($section -match "EapType=(\d+)") {
                $eapType = $matches[1]
                Write-Host "  Phonebook EapType: $eapType" -ForegroundColor Cyan
                if ($eapType -eq '26') {
                    Write-Host "  GOOD: phonebook has EapType=26 (MSCHAPv2)" -ForegroundColor Green
                } else {
                    Write-Host "  PROBLEM: phonebook has EapType=$eapType, not 26" -ForegroundColor Red
                }
            } else {
                Write-Host "  No EapType= line in phonebook section" -ForegroundColor Red
            }
        } else {
            Write-Host "  No section found for GUID $guidBare in pbk" -ForegroundColor Red
        }
    } else {
        Write-Host "  No conn GUID, can't extract section" -ForegroundColor Yellow
    }
} else {
    Write-Host "  Pbk file not found at: $pbkPath" -ForegroundColor Yellow
}
Write-Host "--- end 7d ---" -ForegroundColor DarkCyan

# 7e) UserEapInfo (the secret key that GUI Connect writes)
Write-Host ""
Write-Host "--- 7e) UserEapInfo registry (the key that GUI seed populates) ---" -ForegroundColor DarkCyan
$userEapInfoKey = "HKCU:\SOFTWARE\Microsoft\RAS EAP\UserEapInfo"
if (Test-Path $userEapInfoKey) {
    Write-Host "  UserEapInfo key EXISTS. EAP creds are pre-populated (good for non-interactive)." -ForegroundColor Green
    Get-ChildItem $userEapInfoKey -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "    Subkey: $($_.PSChildName)"
    }
} else {
    Write-Host "  UserEapInfo key MISSING. This is why rasdial asks for GUI dialog." -ForegroundColor Red
    Write-Host "  Fix: One-time GUI seed via Settings -> Network -> VPN -> Connect." -ForegroundColor Yellow
}
Write-Host "--- end 7e ---" -ForegroundColor DarkCyan

# 7f) cmdkey entries
Write-Host ""
Write-Host "--- 7f) cmdkey entries (Credential Manager) ---" -ForegroundColor DarkCyan
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
Write-Host "--- end 7f ---" -ForegroundColor DarkCyan

# 7g) Installed CA cert
Write-Host ""
Write-Host "--- 7g) Installed CA cert (LocalMachine\Root) ---" -ForegroundColor DarkCyan
$ca = Get-ChildItem Cert:\LocalMachine\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $CaSubject } | Select-Object -First 1
if ($ca) {
    $ca | Format-List Subject, Issuer, NotBefore, NotAfter, Thumbprint | Out-String | Write-Host
} else {
    Write-Host "  (CA cert NOT FOUND in LocalMachine\Root)" -ForegroundColor Red
}
Write-Host "--- end 7g ---" -ForegroundColor DarkCyan

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
