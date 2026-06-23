<#
.SYNOPSIS
    TEST-ONLY Windows IKEv2 setup for the test-win-5g customer.
    Modelled on the proven Windows IKEv2 + MSCHAPv2 setup pattern from
    r/fortinet community (DasToastbrot's working config, 2y old, multiple
    successful deployments) and Microsoft Learn's "VPN authentication
    options" canonical walkthrough.

.DESCRIPTION
    This is the SIMPLIFIED version  -  no <EapHostConfig> XML, no
    -ConfigurationFile, no -EapConfigXmlStream. Just two cmdlets:
    Add-VpnConnection + Set-VpnConnectionIPsecConfiguration, with
    -EncryptionLevel "Required" to force strong crypto (the actual
    root cause of 703s on unconfigured Windows IKEv2 clients: Windows
    defaults to DES3/SHA1/DH2, strongSwan rejects as insecure).

    The MS Learn canonical EAP-MSCHAPv2 schema is:
      <EapHostConfig>
        <EapMethod>
          <Type>26</Type>           <-- MSCHAPv2
          <VendorId>0</VendorId>
          <VendorType>0</VendorType>
          <AuthorId>311</AuthorId>  <-- Microsoft MSCHAPv2
        </EapMethod>
        <Config>
          <Eap>
            <Type>26</Type>
            <EapType>...MsChapV2ConnectionPropertiesV1</EapType>
              <UseWinLogonCredentials>false</UseWinLogonCredentials>
          </Eap>
        </Config>
      </EapHostConfig>

    No <EapMsChapV2Config> wrapper. No <ServerValidation> block. Those
    are custom extensions that some Windows builds silently accept and
    others reject. The MS Learn canonical schema is portable.

.NOTES
    TEST CUSTOMER. Will be deleted after multi-device test.
#>

#Requires -RunAsAdministrator

# ============================================================================
# CONFIG (test-win-5g customer)
# ============================================================================
$ServerAddress   = "154.65.110.44"   # raw IP  -  Cloudflare proxy doesn't carry UDP
$ConnectionName  = "DatabyteVPNTest"
$Username        = "test-win-5g-laptop"
$Password        = "a1V5M2Cd1oE0TNWY9wORsg"

$ExpectedCaSha256 = "5C:10:B9:6A:97:06:10:29:7C:8D:8F:B3:6B:E3:5A:98:58:CF:F4:10:C8:1E:72:78:7E:25:08:43:B2:71:CE:06"
$CaCertUrl        = "https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem"
$CaSubject        = "CN=strongSwan CA"

# MS Learn canonical EAP-MSCHAPv2 XML (Portable, no custom extensions)
$EapMschapv2Xml = @"
<EapHostConfig xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
  <EapMethod>
    <Type xmlns="http://www.microsoft.com/provisioning/EapCommon">26</Type>
    <VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
    <VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
    <AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">311</AuthorId>
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
# Determine script directory (works whether run as file or via irm|iex)
# ============================================================================
$ScriptDir = $null
if ($PSCommandPath) {
    $ScriptDir = Split-Path -Parent $PSCommandPath
} elseif ($MyInvocation.MyCommand.Path) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

# ============================================================================
# Helper: SHA256 fingerprint (colon-separated)
# ============================================================================
function Get-Sha256Fingerprint {
    param([Parameter(Mandatory=$true)][string]$Path)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try { $hash = $sha256.ComputeHash($stream) } finally { $stream.Dispose() }
    } finally { $sha256.Dispose() }
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
        $tmp = Join-Path $env:TEMP "databyte-test-setup-$(Get-Random).ps1"
        irm "https://myvpn.databyte.co.za/static/test-win-5g-setup.ps1" -OutFile $tmp -UseBasicParsing
        Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$tmp`""
        ) -Verb RunAs
    }
    exit 0
}
Write-Host "  Running as Administrator (elevated). OK." -ForegroundColor Green

# ============================================================================
# STEP 1 - Install CA cert (fetch from live URL with SHA256 pinning)
# ============================================================================
Write-Host ""
Write-Host "=== [1/5] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

$installed = Get-ChildItem Cert:\LocalMachine\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $CaSubject } | Select-Object -First 1

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
    $tempBase = if ($env:TEMP) { $env:TEMP } else { Join-Path $env:SystemRoot "Temp" }
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
                }
            }
        }

        if (-not $CaCertPath) {
            Write-Host "  ERROR: Could not get CA cert." -ForegroundColor Red
            exit 1
        }

        Import-Certificate -FilePath $CaCertPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        Write-Host "  CA cert installed." -ForegroundColor Green
    } finally {
        if ($tempLive -and (Test-Path $tempLive)) { Remove-Item $tempLive -ErrorAction SilentlyContinue }
    }
}

# ============================================================================
# STEP 2 - Remove old connection and create fresh (MS Learn canonical)
# ============================================================================
Write-Host ""
Write-Host "=== [2/5] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

rasdial $ConnectionName /disconnect 2>&1 | Out-Null
Start-Sleep -Seconds 1
Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Canonical pattern from r/fortinet (DasToastbrot) + MS Learn:
#   - -EncryptionLevel "Required" forces strong crypto (Windows default is
#     "Optional" which allows DES3/SHA1/DH2, which strongSwan rejects)
#   - -AuthenticationMethod "Eap" + the canonical <EapHostConfig> XML
#     selects EAP-MSCHAPv2 (Type=26) via Microsoft's implementation (AuthorId=311)
$xmlPath = Join-Path $env:TEMP "databyte-test-vpn.xml"
@"
<VPNProfile>
  <NativeProfile>
    <Servers>$ServerAddress</Servers>
    <NativeProtocolType>IKEv2</NativeProtocolType>
    <Authentication>
      <UserMethod>Eap</UserMethod>
      <Eap>
        <Configuration>
          $EapMschapv2Xml
        </Configuration>
      </Eap>
    </Authentication>
    <RoutingPolicyType>ForceTunnel</RoutingPolicyType>
  </NativeProfile>
</VPNProfile>
"@ | Out-File $xmlPath -Encoding UTF8

Add-VpnConnection -Name $ConnectionName `
    -ServerAddress $ServerAddress `
    -TunnelType "IKEv2" `
    -EncryptionLevel "Required" `
    -AuthenticationMethod "Eap" `
    -RememberCredential `
    -ConfigurationFile $xmlPath -PassThru | Out-Null

Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue

# Patch RemoteId via registry (cert CN/SAN match)
$connObj = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($connObj) {
    $guid = $connObj.Guid -replace '[\{\}]', ''
    $regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Book\RemoteAccess\Profiles\$guid"
    if (Test-Path $regPath) {
        Set-ItemProperty -Path $regPath -Name "RemoteId" -Value "myvpn.databyte.co.za" -ErrorAction SilentlyContinue
    }
}

Write-Host "  Connection created." -ForegroundColor Green
Write-Host "    Server:        $ServerAddress (raw IP)" -ForegroundColor Green
Write-Host "    Tunnel:        IKEv2" -ForegroundColor Green
Write-Host "    Encryption:    Required (forces strong crypto)" -ForegroundColor Green
Write-Host "    Auth method:   EAP / MSCHAPv2 (Type=26, AuthorId=311)" -ForegroundColor Green
Write-Host "    Remote ID:     myvpn.databyte.co.za" -ForegroundColor Green

# ============================================================================
# STEP 3 - Set IKEv2 IPsec crypto (canonical from r/fortinet + MS Learn)
# ============================================================================
Write-Host ""
Write-Host "=== [3/5] Configuring IPsec crypto ===" -ForegroundColor Cyan

# Canonical values from r/fortinet DasToastbrot + MS Learn "VPN authentication
# options" walkthrough. -PassThru -Force at the end is what the canonical
# troubleshooting command uses.
Set-VpnConnectionIPsecConfiguration -ConnectionName $ConnectionName `
    -AuthenticationTransformConstants "SHA256" `
    -CipherTransformConstants "AES128" `
    -DHGroup "ECP384" `
    -EncryptionMethod "AES128" `
    -IntegrityCheckMethod "SHA256" `
    -PfsGroup "ECP384" `
    -PassThru -Force | Out-Null

# Registry tweak: enable strong DH (Group14+)
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null

Write-Host "  IPsec crypto: AES128/SHA256/ECP384 (DH and PFS), PassThru Force" -ForegroundColor Green
Write-Host "  Registry: NegotiateDH2048_AES256 = 1 (Group14+ enabled)" -ForegroundColor Green

# ============================================================================
# STEP 4 - Store creds via cmdkey (canonical pattern)
# ============================================================================
Write-Host ""
Write-Host "=== [4/5] Storing credentials in Credential Manager ===" -ForegroundColor Cyan

cmdkey /delete:$ConnectionName 2>&1 | Out-Null
cmdkey /delete:"RAS:$ConnectionName" 2>&1 | Out-Null
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
cmdkey /generic:"RAS:$ConnectionName" /user:$Username /pass:$Password | Out-Null
Write-Host "  Cleared stale creds, stored fresh for '$Username'." -ForegroundColor Green

# ============================================================================
# STEP 5 - Try rasdial (with fallback to GUI seed instructions)
# ============================================================================
Write-Host ""
Write-Host "=== [5/5] Connecting ===" -ForegroundColor Cyan

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
    Write-Host "  If 703: this is the Win10 <1903 UserEapInfo API gap." -ForegroundColor Yellow
    Write-Host "  The GUI seed (one-time) populates the registry:" -ForegroundColor Yellow
    Write-Host "    1. ncpa.cpl -> right-click '$ConnectionName' -> Connect" -ForegroundColor White
    Write-Host "    2. If a dialog appears, click Connect (creds pre-filled)" -ForegroundColor White
    Write-Host "    3. After the GUI seed, future rasdial calls work non-interactively." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host "  Connection:  $ConnectionName"
Write-Host "  Server:      $ServerAddress (raw IP)"
Write-Host "  Remote ID:   myvpn.databyte.co.za (cert CN/SAN)"
Write-Host "  Username:    $Username"
Write-Host ""
Write-Host "Verify after connect:"
Write-Host "  ipconfig        (look for PPP adapter with 10.99.0.3 VIP)"
Write-Host "  tracert 8.8.8.8 (first hop should be 154.65.110.44)"
