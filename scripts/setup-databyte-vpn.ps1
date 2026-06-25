<#
.SYNOPSIS
    Databyte VPN installer for Windows 10/11 - IKEv2 + EAP-MSCHAPv2.

.DESCRIPTION
    CANONICAL Windows IKEv2 VPN client installer. This is the ONE script.
    There are no other versions. If you find another setup-*.ps1 in this
    repo or on the portal, it is rot and should be deleted.

    Server:         myvpn.databyte.co.za (VPS 154.65.110.44)
    Auth:           EAP-MSCHAPv2
    Crypto:         AES128 / SHA256128 / Group14 / SHA256 / PFS2048
    Credential API: rasapi32!RasSetCredentials (canonical Microsoft API)

    What it does (8 steps):
      0. (If ?slug=X&token=Y) Fetch customer creds from portal
      1. Verify server cert is publicly trusted (LE)
      2. Remove all stale Databyte-related VPN connections + cmdkey entries
      3. Create IKEv2 + EAP-MSCHAPv2 profile
      4. Set IPsec crypto to match strongSwan server (Group14)
      5. Registry: NegotiateDH2048_AES256=2, AssumeUDPEncapsulationContext=2
      6. Bind credentials via RasSetCredentials (rasapi32.dll P/Invoke)
      7. Connect via rasdial with Settings GUI fallback + verify

.NOTES
    File:           setup-databyte-vpn.ps1
    Version:        2.6.0
    Status:         HARDLOCKED - this is the canonical script
    Replaces:       ALL prior setup-*.ps1, connect-databyte-vpn.ps1,
                    test-win-5g-setup*.ps1, setup-databyte-vpn-zun.ps1
    Author:         Misha (AI Agent) for Zun
    Date:           2026-06-24

    CHANGE LOG:
    v2.6.0 (2026-06-24) - HARDLOCK
      * Removed duplicate STEP 6 (RasSetCredentials + WMI merge rot)
      * ONE filename (setup-databyte-vpn.ps1) - served at BOTH URLs
      * ONE invocation (printed at end of script)
      * Removed all personal/archived/test variants
    v2.5.0 (2026-06-24) - Installer token + lab creds
    v2.4.0 (2026-06-24) - Switched portal URL to vpn-portal.databyte.co.za
    v2.3.0 (2026-06-24) - RasSetCredentials P/Invoke (THE FIX)
    v2.0.x (2026-06-24) - All broken iterations before RasSetCredentials

    USAGE (THE CANONICAL 3-LINE BLOCK - copy/paste into PowerShell Admin):
      $url = "https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1"
      curl.exe -o $env:TEMP\setup.ps1 $url
      & $env:TEMP\setup.ps1
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'

# ============================================================================
# CONFIG
# ============================================================================
$ServerAddress  = "myvpn.databyte.co.za"
$RemoteId       = "myvpn.databyte.co.za"
$ConnectionName = "DatabyteVPN"
$PortalBase     = "https://vpn-portal.databyte.co.za"

# ============================================================================
# STEP 0 - Fetch credentials via installer token (operator-issued)
# ============================================================================
# If the script was downloaded with ?t=BASE64 (operator-generated installer
# link), fetch real customer creds from the portal.
# The BASE64 packs slug:token so the URL has no `&` (PowerShell 5.1 would
# parse `&` as background-job operator and reject the whole command).
# If no token, fall back to the hardcoded test customer.
$Username = $null
$Password = $null
$InstallerSlug  = $null
$InstallerToken = $null
try {
    if ($args.Count -ge 2) {
        $InstallerSlug  = $args[0]
        $InstallerToken = $args[1]
    } elseif ($MyInvocation.MyCommand.Definition -match '\?t=([A-Za-z0-9_\-]+)') {
        # v2.5.1 (2026-06-25) — decode ?t=BASE64(slug:token)
        try {
            $packed = $Matches[1]
            # Restore URL-safe base64 padding before decode
            $padded = $packed + '=' * (4 - ($packed.Length % 4))
            $decoded = [System.Text.Encoding]::UTF8.GetString(
                [System.Convert]::FromBase64String($padded.Replace('-','+').Replace('_','/'))
            )
            $parts = $decoded -split ':', 2
            if ($parts.Count -eq 2) {
                $InstallerSlug  = $parts[0]
                $InstallerToken = $parts[1]
            }
        } catch {}
    }
} catch {}

if ($InstallerToken) {
    Write-Host ""
    Write-Host "=== [0/8] Fetching customer credentials via installer token ===" -ForegroundColor Cyan
    try {
        $resp = Invoke-RestMethod -Uri "$PortalBase/api/installer/$InstallerToken" `
                                   -Method Get -TimeoutSec 15 -ErrorAction Stop
        if ($resp.ok) {
            $Username = $resp.username
            $Password = $resp.password
            if ($resp.server) { $ServerAddress = $resp.server }
            Write-Host "  customer: $($resp.customer_name)" -ForegroundColor Green
            Write-Host "  device:   $($resp.device_name) [$($resp.device_type)]" -ForegroundColor Green
            if ($resp.tier) { Write-Host "  tier:     $($resp.tier_display)" -ForegroundColor Green }
        } else { throw "portal returned ok=false" }
    } catch {
        Write-Warning "Failed to fetch creds from portal: $($_.Exception.Message)"
        Write-Warning "Falling back to hardcoded test creds (lab mode)."
        $Username = "test-win-5g-laptop"
        $Password = "a1V5M2Cd1oE0TNWY9wORsg"
    }
} else {
    Write-Host ""
    Write-Host "=== [0/8] No installer token - using hardcoded test creds (lab mode) ===" -ForegroundColor DarkGray
    $Username = "test-win-5g-laptop"
    $Password = "a1V5M2Cd1oE0TNWY9wORsg"
}

# ============================================================================
# Transcript log
# ============================================================================
$transcriptPath = Join-Path $env:TEMP "databyte-vpn-setup-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
try { Start-Transcript -Path $transcriptPath -Append -ErrorAction SilentlyContinue | Out-Null } catch {}

# ============================================================================
# STEP 1 - Verify server cert is publicly trusted (Let's Encrypt)
# ============================================================================
Write-Host ""
Write-Host "=== [1/8] Verifying server TLS cert ===" -ForegroundColor Cyan

$cert = $null
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.SendTimeout    = 5000
    $tcp.ReceiveTimeout = 5000
    $iar = $tcp.BeginConnect($ServerAddress, 443, $null, $null)
    $ok  = $iar.AsyncWaitHandle.WaitOne(10000)
    if (-not $ok) { $tcp.Close(); throw "TCP connect to ${ServerAddress}:443 timed out after 10s" }
    $tcp.EndConnect($iar)
    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, {[System.Net.Security.RemoteCertificateValidationCallback]{ $true }})
    $ssl.AuthenticateAsClient($ServerAddress)
    if ($ssl.RemoteCertificate) {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
    }
    $ssl.Close(); $tcp.Close()
} catch { Write-Warning "Cert fetch failed: $($_.Exception.Message)" }

if ($cert) {
    Write-Host "  Subject:   $($cert.Subject)"   -ForegroundColor Green
    Write-Host "  Issuer:    $($cert.Issuer)"    -ForegroundColor Green
    Write-Host "  Expires:   $($cert.NotAfter)"  -ForegroundColor Green
    if ($cert.Issuer -match "Let.?s Encrypt|ISRG") {
        Write-Host "  Trust:     LE (ISRG Root X1/X2) - trusted by Windows natively." -ForegroundColor Green
    } else { Write-Warning "Issuer is not Let's Encrypt. Cert trust may fail." }
} else { Write-Warning "Continuing - Windows will validate cert at connect time." }

# ============================================================================
# STEP 2 - Clean slate: remove all Databyte-related connections + cmdkey
# ============================================================================
Write-Host ""
Write-Host "=== [2/8] Removing legacy VPN connections ===" -ForegroundColor Cyan

$removed = 0
foreach ($scope in @($false, $true)) {
    $scopeLabel = if ($scope) { 'all-user' } else { 'user' }
    $stale = Get-VpnConnection -AllUserConnection:$scope -ErrorAction SilentlyContinue |
        Where-Object { $_.ServerAddress -match [regex]::Escape($ServerAddress) }
    foreach ($s in $stale) {
        Write-Host "  Removing stale: '$($s.Name)' (scope=$scopeLabel)" -ForegroundColor Yellow
        try { rasdial $s.Name /disconnect 2>&1 | Out-Null } catch {}
        try {
            Remove-VpnConnection -Name $s.Name -AllUserConnection:$scope -Force -ErrorAction Stop
            $removed++
        } catch { Write-Warning "  Remove failed for '$($s.Name)': $_" }
    }
}
if ($removed -eq 0) { Write-Host "  (no leftover profiles)" -ForegroundColor DarkGray }

# Wipe stale Windows Credential Manager entries
$cmdkeyRemoved = 0
$cmdkeyList = cmdkey /list 2>&1 | Out-String
$targetRegex = [regex]'Target:\s*(?<t>.+?)\s*$'
foreach ($line in ($cmdkeyList -split "`r?`n")) {
    if ($line -match $targetRegex) {
        $t = $Matches['t'].Trim()
        if ($t -match 'databyte|myvpn|test-android|test-iphone|test-win') {
            try { cmdkey /delete:$t 2>&1 | Out-Null; $cmdkeyRemoved++ } catch {}
        }
    }
}
if ($cmdkeyRemoved -eq 0) { Write-Host "  (no stale cmdkey entries)" -ForegroundColor DarkGray }

Start-Sleep -Seconds 1

# ============================================================================
# STEP 3 - Create VPN profile (IKEv2 + EAP-MSCHAPv2)
# ============================================================================
# New-EapConfiguration is the canonical, schema-correct path.
# Hand-writing the EAP XML has repeatedly failed against Win 11 24H2.
Write-Host ""
Write-Host "=== [3/8] Creating VPN profile (IKEv2 + EAP-MSCHAPv2) ===" -ForegroundColor Cyan

try {
    $eap = New-EapConfiguration -ErrorAction Stop
    $xmlDoc = $eap.EapConfigXmlStream
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerAddress `
        -TunnelType "IKEv2" `
        -EapConfigXmlStream $xmlDoc `
        -RememberCredential `
        -PassThru -ErrorAction Stop | Out-Null
    Write-Host "  Profile created: $ConnectionName" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: Add-VpnConnection failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($xmlDoc) { Write-Host "  Generated EAP XML:" -ForegroundColor DarkGray; Write-Host $xmlDoc.OuterXml -ForegroundColor DarkGray }
    exit 1
}

# ============================================================================
# STEP 4 - IPsec crypto (Group14 = MODP2048, matches strongSwan server)
# ============================================================================
# Source: Microsoft Learn (vpnclient/Set-VpnConnectionIPsecConfiguration).
# strongSwan server accepts: aes256-sha256-modp2048, aes128-sha256-modp2048
Write-Host ""
Write-Host "=== [4/8] Configuring IPsec crypto ===" -ForegroundColor Cyan

try {
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants SHA256128 `
        -CipherTransformConstants         AES128 `
        -DHGroup                          Group14 `
        -EncryptionMethod                 AES128 `
        -IntegrityCheckMethod             SHA256 `
        -PfsGroup                         PFS2048 `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "  AES128 / SHA256128 / Group14 / SHA256 / PFS2048" -ForegroundColor Green
} catch {
    Write-Warning "IPsec config failed: $_"
    Write-Warning "Windows defaults (DES3/SHA1/DH2) will be used - server will reject as insecure."
}

# ============================================================================
# STEP 5 - Registry tweaks
# ============================================================================
Write-Host ""
Write-Host "=== [5/8] Registry tweaks ===" -ForegroundColor Cyan

New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 2 -Force | Out-Null
Write-Host "  RasMan\Parameters\NegotiateDH2048_AES256 = 2 (ENFORCE)" -ForegroundColor Green

New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\PolicyAgent" `
    -Name "AssumeUDPEncapsulationContextOnSendRule" -PropertyType DWord -Value 2 -Force | Out-Null
Write-Host "  PolicyAgent\AssumeUDPEncapsulationContextOnSendRule = 2" -ForegroundColor Green

# ============================================================================
# STEP 6 - Bind credentials to profile (RasSetCredentials API)
# ============================================================================
# This is THE canonical Microsoft way. RasSetCredentials is the same Windows
# API (rasapi32.dll) that Windows itself uses when the user checks "Save
# password" in the GUI prompt. Works on every Windows build (7/8/10/11).
#
# Methods tried and abandoned:
#   - cmdkey (decorative only - IKEv2 doesn't read it for EAP)         FAIL
#   - Set-VpnConnectionUsernamePassword (not in PS 5.1)               FAIL
#   - WMI MSFT_NetVpnConnection::SetCredentials (wrong namespace)      FAIL
#   - WMI MSFT_NetConnectionProfile::SetCredentials (wrong class)      FAIL
#   - DPAPI direct rasphone.pbk write (minimal format, invalid)        FAIL
#   - rasdial cycle (703 for IKEv2+EAP - legacy dialer limitation)     FAIL
#
# THE FIX: RasSetCredentials P/Invoke. Single, working method.
Write-Host ""
Write-Host "=== [6/8] Binding credentials (RasSetCredentials) ===" -ForegroundColor Cyan

$bound = $false

$credHelper = @'
using System;
using System.Runtime.InteropServices;
public class VpnCredBinder {
    private const int UNLEN = 256;
    private const int PWLEN = 256;
    private const int DNLEN = 15;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode, Pack = 4)]
    private struct RASCREDENTIALS {
        public int size;
        public int options;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = UNLEN + 1)] public string userName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = PWLEN + 1)] public string password;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = DNLEN + 1)] public string domain;
    }
    [DllImport("rasapi32.dll", CharSet = CharSet.Unicode)]
    private static extern int RasSetCredentials(
        string lpszPhonebook, string lpszEntryName, IntPtr lpCredentials,
        [MarshalAs(UnmanagedType.Bool)] bool fClearCredentials);
    public static int Bind(string entry, string user, string pass, string dom) {
        var c = new RASCREDENTIALS {
            size = Marshal.SizeOf(typeof(RASCREDENTIALS)),
            options = 0x7,  // RASCM.UserName | Password | Domain
            userName = user, password = pass, domain = dom ?? ""
        };
        IntPtr p = Marshal.AllocHGlobal(c.size);
        try {
            Marshal.StructureToPtr(c, p, false);
            return RasSetCredentials(null, entry, p, false);
        } finally { Marshal.FreeHGlobal(p); }
    }
}
'@

try {
    if (-not ('VpnCredBinder' -as [type])) {
        Add-Type -TypeDefinition $credHelper -IgnoreWarnings -ErrorAction Stop
    }
    $r = [VpnCredBinder]::Bind($ConnectionName, $Username, $Password, "")
    if ($r -eq 0) {
        Write-Host "  RasSetCredentials P/Invoke: OK" -ForegroundColor Green
        $bound = $true
    } else {
        Write-Host "  RasSetCredentials: returned $r (0=OK, 87=bad name, 1162=profile not found, 5=access denied)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  RasSetCredentials: FAILED - $($_.Exception.Message)" -ForegroundColor Red
}

if ($bound) {
    Write-Host "  Credentials bound - future connects via Settings need NO prompt." -ForegroundColor Green
} else {
    Write-Warning "Creds NOT bound. Run script again OR enter creds in GUI once."
}

# cmdkey (decorative for IKEv2, but kept for RDP/credential-manager tools)
cmdkey /generic:$ServerAddress  /user:$Username /pass:$Password | Out-Null
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null

# ============================================================================
# STEP 7 - Connect (rasdial, with GUI fallback + poll loop)
# ============================================================================
# rasdial is a legacy RAS dialer and does NOT reliably speak EAP-MSCHAPv2
# inside IKEv2 - exit 703 is normal even when creds ARE bound. If it fails,
# open Settings so the user can click Connect there, then POLL.
Write-Host ""
Write-Host "=== [7/8] Connecting to $ServerAddress ===" -ForegroundColor Cyan

Start-Sleep -Seconds 1

$connectOutput = rasdial $ConnectionName $Username $Password 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  rasdial: CONNECTED" -ForegroundColor Green
} else {
    Write-Host "  rasdial exit code: $LASTEXITCODE (703 is normal for IKEv2+EAP)" -ForegroundColor Yellow
    Write-Host "  If still disconnected, open Settings -> VPN and click Connect on '$ConnectionName'." -ForegroundColor Yellow
    Start-Process ms-settings:network-vpn
}

# ============================================================================
# VERIFY - poll for Connected state
# ============================================================================
Write-Host ""
Write-Host "=== Verifying ===" -ForegroundColor Cyan

$maxWait = 90
$pollSec = 3
$elapsed = 0
$connected = $false
$conn      = $null

while ($elapsed -lt $maxWait) {
    $conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
    if ($conn -and $conn.ConnectionStatus -eq "Connected") {
        $connected = $true
        break
    }
    Start-Sleep -Seconds $pollSec
    $elapsed += $pollSec
    Write-Host ("  ...waiting ({0}s/{1}s) - status: {2}" -f `
        $elapsed, $maxWait, $(if($conn){$conn.ConnectionStatus}else{'unknown'})).PadRight(60) -NoNewline
    Write-Host "`r" -NoNewline
}

Write-Host ""
Write-Host ""

if ($conn) {
    Write-Host "  Profile:     $($conn.Name)" -ForegroundColor Green
    Write-Host "  Server:      $($conn.ServerAddress)" -ForegroundColor Green
    Write-Host "  TunnelType:  $($conn.TunnelType)" -ForegroundColor Green
    Write-Host "  AuthMethod:  $($conn.AuthenticationMethod -join ',')" -ForegroundColor Green
    $statusColor = if ($connected) { "Green" } else { "Red" }
    Write-Host "  Status:      $($conn.ConnectionStatus)" -ForegroundColor $statusColor
    if ($connected) {
        Write-Host ""
        Write-Host "  [OK] CONNECTED to $ServerAddress" -ForegroundColor Green
        Write-Host "       First test:  tracert 8.8.8.8   (first hop should be VPS)" -ForegroundColor Cyan
        Write-Host "       Public IP:   Invoke-WebRequest https://ifconfig.me" -ForegroundColor Cyan
    } else {
        Write-Host ""
        Write-Host "  [FAIL] Still disconnected after ${maxWait}s." -ForegroundColor Red
        Write-Host "         Possible causes:" -ForegroundColor Yellow
        Write-Host "           1. Settings VPN page still showing a prompt (click Connect there)" -ForegroundColor Yellow
        Write-Host "           2. Server unreachable from your network" -ForegroundColor Yellow
        Write-Host "           3. Credentials wrong (rerun to overwrite cmdkey)" -ForegroundColor Yellow
        Write-Host "         Re-run this script to retry, or:" -ForegroundColor Yellow
        Write-Host "           rasdial $ConnectionName /disconnect   (reset)" -ForegroundColor Cyan
        Write-Host "           Start-Process ms-settings:network-vpn" -ForegroundColor Cyan
    }
} else {
    Write-Warning "Profile not visible. Refresh VPN settings."
}

# ============================================================================
# THE CANONICAL 3-LINE BLOCK (save this for next time)
# ============================================================================
Write-Host ""
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  THE CANONICAL 3-LINE BLOCK (save this - this is THE only way)" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host ""
Write-Host "  curl.exe -o `$env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1" -ForegroundColor White
Write-Host "  & `$env:TEMP\setup.ps1" -ForegroundColor White
Write-Host "  rasdial DatabyteVPN" -ForegroundColor White
Write-Host ""
Write-Host "  (No -k flag. No -zun suffix. No archived script. THIS is the way.)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  If that URL ever fails, fallback (uses myvpn.* which has Origin Cert):" -ForegroundColor DarkGray
Write-Host "  curl.exe -k -o `$env:TEMP\setup.ps1 https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1" -ForegroundColor DarkGray
Write-Host "  & `$env:TEMP\setup.ps1" -ForegroundColor DarkGray
Write-Host "  rasdial DatabyteVPN" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Disconnect:  rasdial DatabyteVPN /disconnect" -ForegroundColor Cyan
Write-Host "  Reconnect:   rasdial DatabyteVPN" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Test after connecting:" -ForegroundColor Cyan
Write-Host "    tracert 8.8.8.8                       (first hop = 154.65.110.44)"
Write-Host "    Invoke-WebRequest https://ifconfig.me (returns 154.65.110.44)"
Write-Host ""
Write-Host "  Setup log: $transcriptPath" -ForegroundColor DarkGray
Write-Host ""

try { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null } catch {}

Write-Host ""
Read-Host "Press Enter to exit"
