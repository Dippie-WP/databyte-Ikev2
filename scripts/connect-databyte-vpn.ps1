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
         (no CA cert install needed — Windows trusts LE natively via ISRG Root X1/X2)
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
# Server = raw IP, NOT hostname. Cloudflare proxy does not relay IKEv2 (UDP 500/4500),
# so DNS for the hostname points at a Cloudflare edge IP and the tunnel fails.
# Remote ID = cert CN/SAN. Must match the cert, not the ServerAddress.
$ServerAddress    = "154.65.110.44"
$RemoteId         = "myvpn.databyte.co.za"
$ConnectionName   = "DatabyteVPN"
$Username         = "zun-operator"
$Password         = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# ============================================================================
# STEP 1 - Verify server cert is publicly trusted (Let's Encrypt)
# ============================================================================
Write-Host ""
Write-Host "=== [1/5] Verifying server TLS cert (Let's Encrypt) ===" -ForegroundColor Cyan

try {
    $cert = $(echo | Invoke-WebRequest -Uri "https://$RemoteId" -UseBasicParsing -TimeoutSec 10).Certificate
    Write-Host "  Server cert subject: $($cert.Subject)" -ForegroundColor Green
    Write-Host "  Issuer:               $($cert.Issuer)" -ForegroundColor Green
    Write-Host "  Valid from:           $($cert.GetEffectiveDateString())" -ForegroundColor Green
    Write-Host "  Expires:              $($cert.GetExpirationDateString())" -ForegroundColor Green

    # Verify it's a Let's Encrypt chain (ISRG Root X1/X2 are in Windows trust store)
    $leTrusted = $cert.Thumbprint -ne $null
    if ($cert.Issuer -match "Let's Encrypt" -or $cert.Issuer -match "ISRG") {
        Write-Host "  Chain: Let's Encrypt (ISRG Root) — publicly trusted by Windows." -ForegroundColor Green
    } else {
        Write-Host "  WARNING: Cert issuer is not Let's Encrypt. Verify manually." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Could not fetch server cert: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host "  Continuing anyway (certificate trust is handled by Windows at connect time)..." -ForegroundColor Yellow
}

# ============================================================================
# STEP 2 - Remove any existing connection (clean slate)
# ============================================================================
Write-Host ""
Write-Host "=== [2/5] Removing old connection (if any) ===" -ForegroundColor Cyan

$existing = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existing) {
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Write-Host "  Old connection removed." -ForegroundColor Yellow
} else {
    Write-Host "  No existing connection. Good." -ForegroundColor Green
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

# PS 5.1's Add-VpnConnection takes -EapConfigXmlStream (a byte array), NOT -ConfigurationFile.
# -ConfigurationFile was a wrong parameter invented for some other cmdlet/version.
$xmlBytes = [System.Text.Encoding]::UTF8.GetBytes($profileXml)

try {
    Add-VpnConnection `
        -Name $ConnectionName `
        -ServerAddress $ServerAddress `
        -TunnelType "IKEv2" `
        -EapConfigXmlStream $xmlBytes `
        -RememberCredential `
        -PassThru -ErrorAction Stop | Out-Null
    Write-Host "  Connection created. Server=$ServerAddress, RemoteID=$RemoteId" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "  ERROR: Add-VpnConnection failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Common causes:" -ForegroundColor Yellow
    Write-Host "    - Not running as Administrator" -ForegroundColor Yellow
    Write-Host "    - PowerShell 5.1 issue (this script supports 5.1 + 7)" -ForegroundColor Yellow
    Write-Host "    - Corrupted phone book (try: Remove-VpnConnection -Name $ConnectionName -Force)" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ============================================================================
# STEP 4 - Set IKEv2 IPsec crypto to match the strongSwan server
# ============================================================================
Write-Host ""
Write-Host "=== [4/5] Configuring IPsec crypto ===" -ForegroundColor Cyan

try {
    Set-VpnConnectionIPsecConfiguration `
        -ConnectionName $ConnectionName `
        -AuthenticationTransformConstants "SHA256" `
        -CipherTransformConstants "AES128" `
        -DHGroup "ECP384" `
        -EncryptionMethod "AES128" `
        -IntegrityCheckMethod "SHA256" `
        -PfsGroup "ECP384" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "  IPsec: SHA256 / AES128 / ECP384 (matches strongSwan server)" -ForegroundColor Green
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
# Without this, Windows sends NAT-T encapsulated packets to the wrong place.
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
Write-Host "Server:        $ServerAddress (raw IP, Cloudflare proxy can't relay IKEv2)" -ForegroundColor White
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

# Stop transcript + keep window open so the user can read the output.
try { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null } catch { }

Write-Host "---" -ForegroundColor DarkGray
Write-Host "Full log: $transcriptPath" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Press Enter to close..." -ForegroundColor Yellow
Read-Host | Out-Null
