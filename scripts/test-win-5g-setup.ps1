<#
.SYNOPSIS
    TEST-ONLY Windows IKEv2 setup for the test-win-5g customer.
    Modelled on the homelab-tested connect-databyte-vpn.ps1 (the path
    that worked on Zun's laptop 2026-06-22 — Windows SA #23 ESTABLISHED).

.DESCRIPTION
    TEST CUSTOMER. Will be deleted after multi-device test.

    This script uses the PROVEN path:
      1. Calls setup-windows-vpn.ps1 for CA cert + base connection
         (idempotent, SHA256-pinned, falls back to bundled cert)
      2. Tears down any old DatabyteVPNTest profile
      3. Builds <VPNProfile> XML with EAP-MSCHAPv2 (AuthorId=311 — the
         Microsoft MSCHAPv2 implementation ID, NOT AuthorId=0 which
         v3-v7 of the old script used and which the IKEv2 stack rejected)
      4. Add-VpnConnection -ConfigurationFile $xmlPath  (this is the
         parameter that actually writes the EAP method to the phonebook;
         -EapConfigXmlStream on its own is silently rejected on Win10
         pre-1809 builds, as the v3-v7 arc proved)
      5. Set IPsec crypto to match strongSwan server
      6. cmdkey stores creds so GUI seed doesn't need typing
      7. GUI seed via Settings -> VPN -> Connect (one-time, populates
         HKEY_CURRENT_USERS\SOFTWARE\Microsoft\RAS EAP\UserEapInfo)
      8. From then on:  rasdial DatabyteVPNTest   (no creds needed)

    Usage (run as Administrator):
      PS> powershell -ExecutionPolicy Bypass -File test-win-5g-setup.ps1

.NOTES
    This is the SAME approach Zun used on the homelab laptop. The v3-v7
    arc of the old test script (archived) tried to use -EapConfigXmlStream
    and Set-VpnConnectionUsernamePassword — both are silently rejected on
    Zun's Windows build (pre-1809). This script uses -ConfigurationFile
    with the full <VPNProfile> wrapper, which the homelab test confirmed
    works.
#>

#Requires -RunAsAdministrator

# ============================================================================
# CONFIG (test-win-5g customer)
# ============================================================================
$ServerAddress   = "154.65.110.44"   # raw IP — Cloudflare proxy doesn't handle UDP 500/4500
$ConnectionName  = "DatabyteVPNTest"
$Username        = "test-win-5g-laptop"
$Password        = "a1V5M2Cd1oE0TNWY9wORsg"

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
# STEP 0 - Self-elevate to admin (handles irm|iex from non-elevated shell)
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
        # Stream mode: download to temp first, then run elevated
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
# STEP 1 - Run setup-windows-vpn.ps1 (CA cert + base connection)
# ============================================================================
Write-Host ""
Write-Host "=== [1/6] Running setup-windows-vpn.ps1 (CA cert + base profile) ===" -ForegroundColor Cyan

# Look for setup-windows-vpn.ps1 alongside this script, OR download it
$setupScript = $null
if ($ScriptDir) {
    $candidate = Join-Path $ScriptDir "setup-windows-vpn.ps1"
    if (Test-Path $candidate) { $setupScript = $candidate }
}
if (-not $setupScript) {
    # Download from live portal
    $setupScript = Join-Path $env:TEMP "setup-windows-vpn.ps1"
    Write-Host "  Downloading setup-windows-vpn.ps1 from live portal..." -ForegroundColor Yellow
    irm "https://myvpn.databyte.co.za/static/setup-windows-vpn.ps1" -OutFile $setupScript -UseBasicParsing
}

# Run setup, but capture output (we want to keep our own script flow)
Write-Host "  Running: $setupScript" -ForegroundColor Cyan
$setupOutput = & $setupScript 2>&1
$setupOutput | ForEach-Object { Write-Host "  [setup] $_" }

if ($LASTEXITCODE -ne 0) {
    Write-Host "  setup-windows-vpn.ps1 exited with code $LASTEXITCODE" -ForegroundColor Yellow
    Write-Host "  Continuing — cert may already be installed" -ForegroundColor Yellow
}

# ============================================================================
# STEP 2 - Tear down any old DatabyteVPNTest profile
# ============================================================================
Write-Host ""
Write-Host "=== [2/6] Removing any existing '$ConnectionName' profile ===" -ForegroundColor Cyan

rasdial $ConnectionName /disconnect 2>&1 | Out-Null
Start-Sleep -Seconds 1
Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Write-Host "  Old profile cleared (if any)." -ForegroundColor Green

# ============================================================================
# STEP 3 - Build <VPNProfile> XML with EAP-MSCHAPv2 (AuthorId=311)
# ============================================================================
Write-Host ""
Write-Host "=== [3/6] Building VPN profile XML (EAP-MSCHAPv2 via -ConfigurationFile) ===" -ForegroundColor Cyan

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
          <EapHostConfig xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
            <EapMethod>
              <Type xmlns="http://www.microsoft.com/provisioning/EapCommon">26</Type>
              <VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
              <VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
              <AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">311</AuthorId>
            </EapMethod>
            <Config xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
              <EapMsChapV2Config>
                <ServerValidation>
                  <DisableUserPromptForServerValidation>true</DisableUserPromptForServerValidation>
                  <ServerNames>myvpn.databyte.co.za</ServerNames>
                </ServerValidation>
              </EapMsChapV2Config>
            </Config>
          </EapHostConfig>
        </Configuration>
      </Eap>
    </Authentication>
    <RoutingPolicyType>ForceTunnel</RoutingPolicyType>
  </NativeProfile>
</VPNProfile>
"@ | Out-File $xmlPath -Encoding UTF8

Write-Host "  Profile XML written to: $xmlPath" -ForegroundColor Green
Write-Host "  EAP method: Type=26 (MSCHAPv2), AuthorId=311 (Microsoft's MSCHAPv2)" -ForegroundColor Green
Write-Host "  Server: $ServerAddress (raw IP, not hostname — Cloudflare proxy doesn't carry UDP)" -ForegroundColor Green

# ============================================================================
# STEP 4 - Create the connection from the profile XML
# ============================================================================
Write-Host ""
Write-Host "=== [4/6] Creating VPN connection '$ConnectionName' from profile XML ===" -ForegroundColor Cyan

Add-VpnConnection -Name $ConnectionName `
  -ServerAddress $ServerAddress `
  -TunnelType "IKEv2" `
  -RememberCredential `
  -ConfigurationFile $xmlPath

Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue

# Set the Remote ID (the cert CN/SAN). -ConfigurationFile doesn't always set
# this reliably on older builds, so we patch it via the registry. This is
# the same trick setup-windows-vpn.ps1 uses.
$connectionObj = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($connectionObj) {
    $guid = $connectionObj.Guid -replace '[\{\}]', ''
    $regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Book\RemoteAccess\Profiles\$guid"
    if (Test-Path $regPath) {
        Set-ItemProperty -Path $regPath -Name "RemoteId" -Value "myvpn.databyte.co.za" -ErrorAction SilentlyContinue
        Write-Host "  RemoteId set to: myvpn.databyte.co.za (matches cert CN/SAN)" -ForegroundColor Green
    } else {
        Write-Host "  WARN: Profile registry key not found at $regPath" -ForegroundColor Yellow
        Write-Host "        RemoteId may need to be set in ncpa.cpl > Properties > Security" -ForegroundColor Yellow
    }
}

# Set IPsec crypto to match what the Windows IKEv2 client actually accepts.
#
# SOURCE: Microsoft Learn "VPN authentication options" + Reddit-supported
# Windows 11 IKEv2 setup walkthrough. The homelab test (2026-06-22) used
# ECP384 for both DH and PFS, and it worked. The MS Learn canonical example
# uses PFS2048 (more portable across builds) but ECP384 is what the working
# script in this repo has always used. Keep ECP384 to match what the
# homelab test verified.
Set-VpnConnectionIPsecConfiguration -ConnectionName $ConnectionName `
    -AuthenticationTransformConstants "SHA256" `
    -CipherTransformConstants "AES128" `
    -DHGroup "ECP384" `
    -EncryptionMethod "AES128" `
    -IntegrityCheckMethod "SHA256" `
    -PfsGroup "ECP384" `
    -Force | Out-Null
Write-Host "  IPsec crypto: AES128/SHA256/ECP384 (matches homelab-tested path)" -ForegroundColor Green

# Strong DH (Group14+) registry tweak — required for MSCHAPv2 to negotiate
# Group14 instead of the default DH2 (1024-bit).
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null
Write-Host "  Registry: NegotiateDH2048_AES256 = 1 (Group14+ enabled)" -ForegroundColor Green

# Store creds so the GUI auto-fills (avoids typing)
cmdkey /delete:$ConnectionName 2>&1 | Out-Null
cmdkey /delete:"RAS:$ConnectionName" 2>&1 | Out-Null
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
cmdkey /generic:"RAS:$ConnectionName" /user:$Username /pass:$Password | Out-Null
Write-Host "  Credentials stored in Credential Manager (no GUI typing needed)." -ForegroundColor Green

# ============================================================================
# STEP 5 - GUI seed (one-time, populates HKCU\...\UserEapInfo)
# ============================================================================
Write-Host ""
Write-Host "=== [5/6] GUI seed (one-time) ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  The next step populates HKEY_CURRENT_USERS\SOFTWARE\Microsoft\RAS EAP\UserEapInfo," -ForegroundColor Yellow
Write-Host "  which is the only way to make rasdial work non-interactively on Win10 <1903." -ForegroundColor Yellow
Write-Host ""
Write-Host "  TO DO NOW:" -ForegroundColor White
Write-Host "    1. Open:  ncpa.cpl  (Win+R, type ncpa.cpl, Enter)" -ForegroundColor White
Write-Host "    2. Right-click the '$ConnectionName' adapter -> Connect" -ForegroundColor White
Write-Host "    3. If a dialog appears, click Connect (creds pre-filled)" -ForegroundColor White
Write-Host "    4. Wait for 'Connected'" -ForegroundColor White
Write-Host "    5. Close ncpa.cpl" -ForegroundColor White
Write-Host ""
Write-Host "  After that, future rasdial calls work non-interactively." -ForegroundColor Cyan
Write-Host ""

# Verify the profile is correct BEFORE the user does the GUI seed
Write-Host "  Profile check (before GUI seed):" -ForegroundColor Cyan
$conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "    Name:         $($conn.Name)"
    Write-Host "    Server:       $($conn.ServerAddress)"
    Write-Host "    Tunnel:       $($conn.TunnelType)"
    Write-Host "    Auth method:  $($conn.AuthenticationMethod)"
    Write-Host "    Encrypt lvl:  $($conn.EncryptionLevel)"
} else {
    Write-Host "  ERROR: Connection not found after Add-VpnConnection" -ForegroundColor Red
    exit 1
}

# ============================================================================
# STEP 6 - Verify and report
# ============================================================================
Write-Host ""
Write-Host "=== [6/6] Post-seed verification ===" -ForegroundColor Cyan

# Check if UserEapInfo got populated (proves the GUI seed worked)
$userEapInfoKey = "HKCU:\SOFTWARE\Microsoft\RAS EAP\UserEapInfo"
$userEapInfoStatus = if (Test-Path $userEapInfoKey) { "POPULATED" } else { "MISSING" }
Write-Host "  UserEapInfo registry key: $userEapInfoStatus"
if ($userEapInfoStatus -eq "POPULATED") {
    Write-Host "    -> GUI seed was successful. rasdial should work non-interactively." -ForegroundColor Green
} else {
    Write-Host "    -> GUI seed has not happened yet. Do steps 1-5 above, then re-run this script." -ForegroundColor Yellow
}

# Show the EAP method as read from the profile
Write-Host ""
Write-Host "  EAP config in profile (read from registry-backed phonebook):"
$pbkPath = "$env:AppData\Microsoft\Network\Connections\Pbk\rasphone.pbk"
if (Test-Path $pbkPath) {
    $pbkContent = Get-Content $pbkPath -Raw
    $guidBare = $conn.Guid
    $pattern = "(?s)\[$([regex]::Escape($guidBare))\].*?(?=\n\[)"
    $match = [regex]::Match($pbkContent, $pattern)
    if ($match.Success) {
        $section = $match.Value
        $eapType = if ($section -match "EapType=(\d+)") { $matches[1] } else { "not set" }
        Write-Host "    EapType: $eapType  (26 = MSCHAPv2, what we want)"
    } else {
        Write-Host "    (Could not find section in rasphone.pbk)" -ForegroundColor Yellow
    }
} else {
    Write-Host "    (rasphone.pbk not found at: $pbkPath)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host "  Connection:  $ConnectionName"
Write-Host "  Server:      $ServerAddress (raw IP, bypasses Cloudflare proxy)"
Write-Host "  Remote ID:   myvpn.databyte.co.za (cert CN/SAN matches)"
Write-Host "  Username:    $Username"
Write-Host ""
Write-Host "AFTER GUI SEED, future connections from any PowerShell/cmd:"
Write-Host "  rasdial $ConnectionName"
Write-Host "  rasdial $ConnectionName /disconnect"
Write-Host ""
Write-Host "Verify after connect:"
Write-Host "  ipconfig  (look for PPP adapter with 10.99.0.3 VIP — your test VIP)"
Write-Host "  tracert 8.8.8.8  (first hop should be 154.65.110.44)"
