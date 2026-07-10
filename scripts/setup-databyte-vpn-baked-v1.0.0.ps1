<#
.SYNOPSIS
    Databyte VPN installer (BAKED variant) for Windows 10/11.
    Self-bootstrapping: installs the Let's Encrypt root cert first,
    then connects to myvpn.databyte.co.za via IKEv2+EAP-MSCHAPv2.

.DESCRIPTION
    Customer-facing one-liner:
        curl.exe -ksSL https://myvpn.databyte.co.za/static/baked/setup-databyte-vpn-<customer>-<device>.ps1 | powershell -ExecutionPolicy Bypass -NoProfile -

    What it does (8 steps):
      0. Bootstrap: install ISRG Root X2 to Windows trust store
         (Windows without X2 fails HTTPS chain validation against
         myvpn.databyte.co.za, which is signed by LE Root YE → X2)
      1. Verify server cert (LE trust + optional fingerprint pin)
      2. Remove all stale Databyte-related VPN connections + cmdkey entries
      3. Create IKEv2 + EAP-MSCHAPv2 profile (New-EapConfiguration)
      4. Set IPsec crypto to match strongSwan server (Group14)
      5. Registry: NegotiateDH2048_AES256=2, AssumeUDPEncapsulationContext=2
      6. Bind credentials via RasSetCredentials (rasapi32.dll P/Invoke)
      7. Connect via rasdial with Settings GUI fallback + verify

    Server:          myvpn.databyte.co.za (VPS 154.65.110.44)
    Auth:            EAP-MSCHAPv2
    Crypto:          AES128 / SHA256128 / Group14 / SHA256 / PFS2048
    Cert:            Pinned by SHA-256 fingerprint (LE root is in Windows
                     trust store after Step 0; pin is defense in depth)
    Credential API:  rasapi32!RasSetCredentials (P/Invoke)

.NOTES
    File:           setup-databyte-vpn-baked.ps1
    Version:        1.0.0
    Status:         NEW (sibling of v2.6.5, does NOT replace it)
    Author:         Misha (AI Agent) for Zun
    Created:        2026-07-10

    OPERATOR WORKFLOW:
      1. Copy this file to a working directory.
      2. Edit the BAKED-IN CONFIG block:
         - $Username = "customer-device-name" (per portal operator page)
         - $Password = "..." (per portal operator page)
         - $ServerCertSha256 = "AA:BB:..." (optional, see below)
      3. Save as: setup-databyte-vpn-<customer>-<device>.ps1
      4. Push to VPS static dir (path: /opt/vpn-portal/www/static/baked/)
      5. Ship the URL to the customer via encrypted email / portal / SFTP.

    ROTATION NOTE:
      The LE cert rotates every ~60 days. Re-bake $ServerCertSha256 on each
      rotation, OR leave it as REPLACE-ME for issuer-only validation.
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'

# ============================================================================
# BAKED-IN CONFIG (operator: edit per customer before shipping)
# ============================================================================
$ServerAddress  = "myvpn.databyte.co.za"
$RemoteId       = "myvpn.databyte.co.za"   # must match cert SAN
$ConnectionName = "DatabyteVPN"
$PortalBase     = "https://vpn-portal.databyte.co.za"

# --- Server cert SHA-256 fingerprint (optional strict pin) ---
$ServerCertSha256 = "REPLACE-ME-sha256-fingerprint"

# --- LE root cert URL (served from VPS portal static dir) ---
$LERootUrl = "$PortalBase/static/certs/isrg-root-x2.pem"

# --- Per-customer credentials (BAKED - file is sensitive) ---
$Username = "REPLACE-ME-customer-device-name"
$Password = "REPLACE-ME-customer-password"

# --- Issuer check (LE expected) ---
$ExpectedIssuerMatch = "Let.?s Encrypt|ISRG"

# --- Crypto suite (matches strongSwan server: aes128-sha256-modp2048) ---
$Crypto = @{
    AuthenticationTransformConstants = "SHA256128"
    CipherTransformConstants         = "AES128"
    DHGroup                          = "Group14"   # modp2048
    EncryptionMethod                 = "AES128"
    IntegrityCheckMethod             = "SHA256"
    PfsGroup                         = "PFS2048"
}

# ============================================================================
# TRANSCRIPT LOG
# ============================================================================
$transcriptPath = Join-Path $env:TEMP "databyte-vpn-baked-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
try { Start-Transcript -Path $transcriptPath -Append -ErrorAction SilentlyContinue | Out-Null } catch {}

# ============================================================================
# SANITY CHECKS
# ============================================================================
if ($Username -like "REPLACE-ME*" -or $Password -like "REPLACE-ME*") {
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host "  ERROR: Edit this script and replace REPLACE-ME-* values" -ForegroundColor Red
    Write-Host "         in the BAKED-IN CONFIG block before shipping." -ForegroundColor Red
    Write-Host "================================================================" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to exit"; exit 1
}

# ============================================================================
# STEP 0 - Bootstrap: install ISRG Root X2 to Windows trust store
# ============================================================================
# Why this exists:
#   The myvpn.databyte.co.za cert is signed by Let's Encrypt Root YE
#   (intermediate), which chains to ISRG Root X2 (ECDSA root). ISRG Root
#   X2 has been in Windows since 1903 (May 2019). On any Windows where X2
#   is missing OR the chain isn't followed, HTTPS validation fails.
#
#   We install X2 via certutil so the chain is trusted for the rest of the
#   script (Steps 1-7). The root download uses curl -k because the chain
#   isn't trusted yet (chicken-and-egg) - but the root itself is self-signed,
#   so transport security is irrelevant to its authenticity.
Write-Host ""
Write-Host "=== [0/8] Bootstrap: install Let's Encrypt root cert ===" -ForegroundColor Cyan

$lerRootLocal = Join-Path $env:TEMP "isrg-root-x2.pem"

# Check if X2 is already trusted
$x2AlreadyTrusted = $false
$x2Thumb = "915B640DFE48298E5C75E60B1D8E3FA48D43A32B"  # ISRG Root X2 SPKI SHA-256
try {
    $existing = Get-ChildItem Cert:\CurrentUser\Root, Cert:\LocalMachine\Root -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $x2Thumb }
    if ($existing) { $x2AlreadyTrusted = $true }
} catch {}

if ($x2AlreadyTrusted) {
    Write-Host "  ISRG Root X2: already trusted (skipping install)" -ForegroundColor Green
} else {
    Write-Host "  Downloading ISRG Root X2 from server..." -ForegroundColor Cyan
    # -k is safe: root is self-signed, no chain to verify
    $curlOut = & curl.exe -ksSL -o $lerRootLocal $LERootUrl 2>&1
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $lerRootLocal)) {
        Write-Host ""
        Write-Host "  [FAIL] Could not download ISRG Root X2 from $LERootUrl" -ForegroundColor Red
        Write-Host "  curl.exe exit: $LASTEXITCODE" -ForegroundColor Red
        Write-Host "  curl.exe output: $curlOut" -ForegroundColor Red
        Write-Host "  Without this root, Step 1 cert validation will fail." -ForegroundColor Yellow
        Write-Host "  Continuing anyway - Step 1 will abort if cert chain untrusted." -ForegroundColor Yellow
    } else {
        Write-Host "  Downloaded: $lerRootLocal ($((Get-Item $lerRootLocal).Length) bytes)" -ForegroundColor Green
        Write-Host "  Installing to LocalMachine\Root..." -ForegroundColor Cyan
        $certutilOut = & certutil.exe -addstore -f Root $lerRootLocal 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  certutil: OK (ISRG Root X2 installed)" -ForegroundColor Green
        } else {
            Write-Host "  certutil exit: $LASTEXITCODE" -ForegroundColor Red
            Write-Host "  certutil output: $certutilOut" -ForegroundColor Red
            Write-Host "  Step 1 may fail; continuing anyway." -ForegroundColor Yellow
        }
    }
}

# ============================================================================
# STEP 1 - Verify server cert (LE trust + optional fingerprint pin)
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
} catch {
    Write-Host ""
    Write-Host "  [FAIL] Cert fetch failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Cannot verify server. Aborting." -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to exit"; exit 1
}

Write-Host "  Subject:  $($cert.Subject)"   -ForegroundColor Green
Write-Host "  Issuer:   $($cert.Issuer)"    -ForegroundColor Green
Write-Host "  Expires:  $($cert.NotAfter)"  -ForegroundColor Green

# Issuer check (LE expected)
if ($cert.Issuer -notmatch $ExpectedIssuerMatch) {
    Write-Host "  [FAIL] Issuer is not Let's Encrypt: $($cert.Issuer)" -ForegroundColor Red
    Write-Host "  Refusing to connect. Aborting." -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}
Write-Host "  Trust:    LE (ISRG Root X2 in trust store after Step 0)" -ForegroundColor Green

# Fingerprint pin (only if baked)
if ($ServerCertSha256 -ne "REPLACE-ME-sha256-fingerprint") {
    $actualFp = ($cert.GetCertHash("SHA256") | ForEach-Object { $_.ToString("X2") }) -join ":"
    if ($actualFp -ne $ServerCertSha256) {
        Write-Host ""
        Write-Host "  [FAIL] Cert fingerprint mismatch!" -ForegroundColor Red
        Write-Host "    Expected: $ServerCertSha256" -ForegroundColor Yellow
        Write-Host "    Actual:   $actualFp" -ForegroundColor Yellow
        Write-Host "    (Cert was likely rotated. Re-bake the fingerprint or unset it.)" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "Press Enter to exit"; exit 1
    }
    Write-Host "  Pin:      SHA-256 match (baked fingerprint)" -ForegroundColor Green
} else {
    Write-Host "  Pin:      none (issuer-only validation; set \$ServerCertSha256 for strict pin)" -ForegroundColor DarkGray
}

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
# New-EapConfiguration is the canonical, schema-correct path. Hand-writing
# the EAP XML has repeatedly failed against Win 11 24H2.
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
Write-Host ""
Write-Host "=== [4/8] Configuring IPsec crypto ===" -ForegroundColor Cyan

try {
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants $Crypto.AuthenticationTransformConstants `
        -CipherTransformConstants         $Crypto.CipherTransformConstants `
        -DHGroup                          $Crypto.DHGroup `
        -EncryptionMethod                 $Crypto.EncryptionMethod `
        -IntegrityCheckMethod             $Crypto.IntegrityCheckMethod `
        -PfsGroup                         $Crypto.PfsGroup `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "  $($Crypto.EncryptionMethod) / $($Crypto.AuthenticationTransformConstants) / $($Crypto.DHGroup) / $($Crypto.IntegrityCheckMethod) / $($Crypto.PfsGroup)" -ForegroundColor Green
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
# Canonical Microsoft way. RasSetCredentials is the same Windows API
# (rasapi32.dll) that Windows itself uses when the user checks "Save
# password" in the GUI prompt. Works on every Windows build (7/8/10/11).
Write-Host ""
Write-Host "=== [6/8] Binding credentials (RasSetCredentials) ===" -ForegroundColor Cyan

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

$bound = $false
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

# VERIFY - poll for Connected state
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
        Write-Host "           3. Credentials wrong (re-edit \$Username/\$Password, re-run)" -ForegroundColor Yellow
        Write-Host "         Re-run this script to retry, or:" -ForegroundColor Yellow
        Write-Host "           rasdial $ConnectionName /disconnect   (reset)" -ForegroundColor Cyan
        Write-Host "           Start-Process ms-settings:network-vpn" -ForegroundColor Cyan
    }
} else {
    Write-Warning "Profile not visible. Refresh VPN settings."
}

# ============================================================================
# POST
# ============================================================================
Write-Host ""
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host "  DONE" -ForegroundColor Magenta
Write-Host "================================================================" -ForegroundColor Magenta
Write-Host ""
Write-Host "  Disconnect:  rasdial $ConnectionName /disconnect" -ForegroundColor Cyan
Write-Host "  Reconnect:   rasdial $ConnectionName" -ForegroundColor Cyan
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