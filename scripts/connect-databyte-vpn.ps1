<#
.SYNOPSIS
    Self-contained Windows IKEv2 VPN installer for Databyte VPN.
.DESCRIPTION
    Single-file installer for Windows 10/11. Works on PowerShell 5.1
    (the default on Win 10/11) and PowerShell 7+.

    Does NOT depend on any sibling file. Can be invoked via:
        irm https://myvpn.databyte.co.za/static/connect-databyte-vpn.ps1 | iex

    What it does:
      1. Verifies the server presents a publicly-trusted Let's Encrypt cert
         (no CA cert install needed - Windows trusts LE natively via ISRG Root X1/X2)
      2. Removes any existing DatabyteVPN connection
      3. Creates a new connection with EAP-MSCHAPv2 via -EapConfigXmlStream
         (no GUI dialog at connect time; RemoteID baked into the profile)
      4. Sets IKEv2 IPsec crypto to match the strongSwan server config
      5. Enables strong DH (Group14+) + NAT-T behind double-NAT via registry
      6. Stores creds so the GUI auto-fills username/password (no typing)
      7. Prints "Click Connect in the GUI"

.NOTES
    Baked-in credentials are PRODUCTION. Do not share this script publicly.
    For other operators, copy and change the values in the CONFIG block.
#>

#Requires -RunAsAdministrator

# Start a transcript so the output survives even if the PowerShell window
# closes (common when piped via 'irm | iex' from a fresh PowerShell).
# Transcript lives at $env:TEMP\databyte-vpn-setup-<timestamp>.log
$transcriptPath = Join-Path $env:TEMP "databyte-vpn-setup-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
try {
    Start-Transcript -Path $transcriptPath -Append -ErrorAction SilentlyContinue | Out-Null
    Write-Host "(Transcript: $transcriptPath)" -ForegroundColor DarkGray
} catch { }

# ============================================================================
# CONFIG (edit these for your environment)
# ============================================================================
# Server = myvpn.databyte.co.za (grey-cloud DNS, resolves directly to VPS 154.65.110.44).
# Cloudflare proxy only proxies vpn-portal.* (orange), not myvpn.* (grey), so the
# hostname works for IKEv2 here. Use the hostname (not raw IP) so the server
# identity matches the cert CN/SAN.
#
# - vpn-portal.databyte.co.za = orange-cloud, CF proxy in front, CANNOT relay IKEv2
# - myvpn.databyte.co.za      = grey-cloud, direct to VPS, works for IKEv2
#
# Remote ID = cert CN/SAN. Must match the cert subject, not the ServerAddress.
$ServerAddress    = "myvpn.databyte.co.za"
$RemoteId         = "myvpn.databyte.co.za"
$ConnectionName   = "DatabyteVPN"
# Also clean up the common names people (or the OS) might leave around, especially
# the old PPTP "Databyte vpn" connection from earlier manual setups.
$LegacyNames      = @("Databyte vpn", "Databyte VPN", "DatabyteVPN", "myvpn", "MyVPN")
$Username         = "zun-operator"
$Password         = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# ============================================================================
# STEP 1 - Verify server cert is publicly trusted (Let's Encrypt)
# ============================================================================
Write-Host ""
Write-Host "=== [1/5] Verifying server TLS cert (Let's Encrypt) ===" -ForegroundColor Cyan

# Use raw SslStream to perform a real TLS handshake and fetch the cert.
# (HttpWebRequest.ServicePoint.Certificate returns null on a fresh request
# because the handshake doesn't complete before the cert lookup.)
$cert = $null
$certError = $null
try {
    $tcp = New-Object System.Net.Sockets.TcpClient($RemoteId, 443)
    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, {[System.Net.Security.RemoteCertificateValidationCallback]{ $true }})
    $ssl.AuthenticateAsClient($RemoteId)
    $raw = $ssl.RemoteCertificate
    if ($raw) {
        $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($raw)
    }
    $ssl.Close()
    $tcp.Close()
} catch {
    $certError = $_.Exception.Message
}

if ($cert) {
    Write-Host "  Subject:   $($cert.Subject)" -ForegroundColor Green
    Write-Host "  Issuer:    $($cert.Issuer)" -ForegroundColor Green
    Write-Host "  NotBefore: $($cert.NotBefore)" -ForegroundColor Green
    Write-Host "  NotAfter:  $($cert.NotAfter)" -ForegroundColor Green

    if ($cert.Issuer -match "Let's Encrypt" -or $cert.Issuer -match "ISRG") {
        Write-Host "  Chain:     LE (ISRG Root X1/X2) - publicly trusted by Windows." -ForegroundColor Green
    } else {
        Write-Host "  WARNING:   Issuer is not Let's Encrypt. Verify manually." -ForegroundColor Yellow
    }
} else {
    Write-Host "  Cert fetch FAILED: $certError" -ForegroundColor Red
    Write-Host "  Continuing anyway (cert trust is handled by Windows at connect time)..." -ForegroundColor Yellow
}

# ============================================================================
# STEP 2 - Clean slate: remove ALL Databyte-related connections
# ============================================================================
# Critical: a leftover PPTP connection (e.g. "Databyte vpn" from an earlier
# manual setup) will be tried FIRST when you click Connect, fail with error
# 13843, and Windows will report "VPN failed" even though the IKEv2 connection
# we create below is fine. So we nuke EVERYTHING matching before creating fresh.
Write-Host ""
Write-Host "=== [2/5] Cleaning up old connections ===" -ForegroundColor Cyan

foreach ($nameToRemove in @($ConnectionName) + $LegacyNames) {
    foreach ($scope in @($false, $true)) {  # user-scope then all-user-scope
        $existing = Get-VpnConnection -Name $nameToRemove -AllUserConnection:$scope -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host "  Removing: '$nameToRemove' (TunnelType=$($existing.TunnelType), scope=$(if($scope){'all-user'}else{'user'}))" -ForegroundColor Yellow
            try {
                rasdial $nameToRemove /disconnect 2>&1 | Out-Null
                Remove-VpnConnection -Name $nameToRemove -AllUserConnection:$scope -Force -ErrorAction Stop
            } catch {
                Write-Warning "  Remove failed for '$nameToRemove' (scope=$(if($scope){'all-user'}else{'user'})): $_"
            }
        }
    }
}
Start-Sleep -Seconds 1

# Show what survives
$remaining = @()
foreach ($scope in @($false, $true)) {
    $remaining += Get-VpnConnection -AllUserConnection:$scope -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -match "databyte|vpn" } |
                  ForEach-Object { "$($_.Name) ($($_.TunnelType), $(if($scope){'all-user'}else{'user'}))" }
}
if ($remaining.Count -eq 0) {
    Write-Host "  No old Databyte/VPN connections remain. Clean slate." -ForegroundColor Green
} else {
    Write-Host "  Remaining Databyte/VPN connections (will create fresh anyway):" -ForegroundColor Yellow
    $remaining | ForEach-Object { Write-Host "    - $_" -ForegroundColor Yellow }
}

# ============================================================================
# STEP 3 - Create connection with EAP-MSCHAPv2 (no GUI dialog at connect)
# ============================================================================
Write-Host ""
Write-Host "=== [3/5] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

# Inline the profile XML so the script has zero file dependencies.
# - EapMethod Type=26 = EAP-MSCHAPv2 (AuthorId=311)
# - ServerNames = cert SAN/CN for server validation
# - DisableUserPromptForServerValidation = no cert-prompt dialog
# - ForceTunnel = full tunnel (no split tunnel; ALL traffic through VPN)
$profileXml = @"
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
                  <ServerNames>$RemoteId</ServerNames>
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
"@

# Per Microsoft Learn docs, -EapConfigXmlStream expects an XmlDocument, NOT a byte array.
# Passing byte[] is type-coerced on PS 7 but can silently fail or throw on PS 5.1.
$xmlDoc = New-Object System.Xml.XmlDocument
$xmlDoc.LoadXml($profileXml)

try {
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerAddress `
        -TunnelType "IKEv2" `
        -EapConfigXmlStream $xmlDoc `
        -RememberCredential `
        -PassThru -ErrorAction Stop | Out-Null
    Write-Host "  Connection created. Server=$ServerAddress, RemoteID=$RemoteId" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "  ERROR: Add-VpnConnection failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Common causes:" -ForegroundColor Yellow
    Write-Host "    - Not running as Administrator" -ForegroundColor Yellow
    Write-Host "    - PS 5.1 + XML escaping issue (check the script source for stray characters)" -ForegroundColor Yellow
    Write-Host "    - Corrupted phone book (the cleanup in Step 2 should prevent this)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Full XML being passed:" -ForegroundColor DarkGray
    Write-Host $profileXml -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

# ============================================================================
# STEP 4 - Set IKEv2 IPsec crypto to match the strongSwan server
# ============================================================================
Write-Host ""
Write-Host "=== [4/5] Configuring IPsec crypto ===" -ForegroundColor Cyan

try {
    # Per Microsoft Learn: -AuthenticationTransformConstants valid values are
    # MD596, SHA196, SHA256128, GCMAES128, GCMAES192, GCMAES256, None.
    # "SHA256" is NOT a valid value — using it causes the cmdlet to throw and
    # leaves the connection on Windows' insecure defaults (DES3/SHA1/DH2).
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants "SHA256128" `
        -CipherTransformConstants "AES128" `
        -DHGroup "Group14" `
        -EncryptionMethod "AES128" `
        -IntegrityCheckMethod "SHA256" `
        -PfsGroup "PFS2048" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "  IPsec: SHA256128 / AES128 / Group14 / SHA256 / PFS2048 (Microsoft Learn secure IKEv2 template)" -ForegroundColor Green
} catch {
    Write-Warning "Set-VpnConnectionIPsecConfiguration failed: $_"
    Write-Warning "Connection will use Windows defaults (insecure). strongSwan will likely reject."
}

# Enable strong DH (Group14+) for EAP-MSCHAPv2. Without this, Win defaults to Group2 (1024-bit DH).
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters"
New-ItemProperty -Path $regPath -Name "NegotiateDH2048_AES256" `
    -PropertyType DWord -Value 1 -Force | Out-Null
Write-Host "  Registry: NegotiateDH2048_AES256 = 1 (Group14+ enabled)" -ForegroundColor Green

# NAT-T fix for error 809 behind double-NAT (e.g., 4G hotspot behind a router).
$policyPath = "HKLM:\SYSTEM\CurrentControlSet\Services\PolicyAgent"
New-ItemProperty -Path $policyPath -Name "AssumeUDPEncapsulationContextOnSendRule" `
    -PropertyType DWord -Value 2 -Force | Out-Null
Write-Host "  Registry: AssumeUDPEncapsulationContextOnSendRule = 2 (NAT-T fix)" -ForegroundColor Green

# ============================================================================
# STEP 5 - Store credentials (GUI auto-fills username/password)
# ============================================================================
Write-Host ""
Write-Host "=== [5/5] Storing credentials ===" -ForegroundColor Cyan

cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
Write-Host "  Creds stored. GUI will auto-fill on Connect." -ForegroundColor Green

# ============================================================================
# VERIFY + instructions
# ============================================================================
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Start-Sleep -Seconds 1

$conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "  Connection found." -ForegroundColor Green
    Write-Host "    Server:      $($conn.ServerAddress)"
    Write-Host "    Tunnel type: $($conn.TunnelType)"
    Write-Host "    Auth method: $($conn.AuthenticationMethod)"
} else {
    Write-Host "  WARN: connection not visible. Try a hard refresh of the VPN settings page." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Click Connect in the GUI:" -ForegroundColor Yellow
Write-Host "  Settings -> Network & Internet -> VPN -> $ConnectionName -> Connect" -ForegroundColor Yellow
Write-Host ""
Write-Host "Server:        $ServerAddress (hostname, grey-cloud DNS, resolves to VPS)" -ForegroundColor White
Write-Host "Remote ID:     $RemoteId (matches cert CN/SAN)" -ForegroundColor White
Write-Host "Username:      $Username (auto-filled)" -ForegroundColor White
Write-Host ""
Write-Host "Test after connecting:" -ForegroundColor Cyan
Write-Host "  tracert 8.8.8.8                       (first hop should be $ServerAddress, NOT your router)"
Write-Host "  iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30   (expect ~17-20 Mbps with cap)"
Write-Host ""
Write-Host "To reconnect:  powershell -ExecutionPolicy Bypass -File connect-databyte-vpn.ps1"
Write-Host "To disconnect: rasdial $ConnectionName /disconnect"
Write-Host ""

# Stop transcript (so the file is flushed/closed)
try { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null } catch { }

Write-Host "---" -ForegroundColor DarkGray
Write-Host "Full log: $transcriptPath" -ForegroundColor DarkGray
Write-Host ""
# No explicit pause — the PowerShell window stays open by default after
# a script completes in an interactive session. The transcript file above
# captures everything in case the window does close.
