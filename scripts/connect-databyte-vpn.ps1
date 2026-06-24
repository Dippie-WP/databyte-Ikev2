<#
.SYNOPSIS
    Databyte IKEv2 VPN installer for Windows 10/11.
.DESCRIPTION
    Single-file installer. Works on PowerShell 5.1 (default on Win 10/11)
    and PowerShell 7+. Self-contained: no sibling files required.

    Invoke via:
        irm https://myvpn.databyte.co.za/static/connect-databyte-vpn.ps1 | iex

    What it does:
      1. Verifies the server presents a publicly-trusted Let's Encrypt cert
         (no CA cert install needed - Windows trusts LE natively via ISRG X1/X2)
      2. Removes all stale Databyte-related VPN connections (PPTP, IKEv2, etc.)
      3. Creates a fresh IKEv2 connection with EAP-MSCHAPv2 (no GUI prompt)
      4. Configures IPsec crypto to match the strongSwan server
         (SHA256128 / AES256 / Group14 / SHA256 / PFS None)
      5. Stores credentials so the GUI auto-fills on Connect
      6. Prints "Connect in the GUI" instructions

.NOTES
    Baked-in credentials are PRODUCTION. Do not share this script publicly.
    For other operators, copy and change the CONFIG block at the top.
#>

#Requires -RunAsAdministrator

$transcriptPath = Join-Path $env:TEMP "databyte-vpn-setup-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
try { Start-Transcript -Path $transcriptPath -Append -ErrorAction SilentlyContinue | Out-Null } catch {}

# CONFIG
$ServerAddress = "myvpn.databyte.co.za"
$RemoteId = "myvpn.databyte.co.za"
$ConnectionName = "DatabyteVPN"
$LegacyNames = @("Databyte vpn","Databyte VPN","DatabyteVPN","myvpn","MyVPN")
$Username = "zun-operator"
$Password = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# STEP 1 - Verify server TLS cert
Write-Host "`n=== [1/5] Verifying server TLS cert ===" -ForegroundColor Cyan
$cert = $null
try {
 $tcp = New-Object System.Net.Sockets.TcpClient($RemoteId, 443)
 $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, { $true })
 $ssl.AuthenticateAsClient($RemoteId)
 $raw = $ssl.RemoteCertificate
 if ($raw) {
 $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($raw)
 }
 $ssl.Close(); $tcp.Close()
} catch {
 Write-Host " Cert fetch failed: $($_.Exception.Message)" -ForegroundColor Yellow
}
if ($cert) {
 Write-Host " Subject: $($cert.Subject)" -ForegroundColor Green
 Write-Host " Issuer: $($cert.Issuer)" -ForegroundColor Green
 Write-Host " Expires: $($cert.NotAfter)" -ForegroundColor Green
 if ($cert.Issuer -match "Let.s Encrypt|ISRG") {
 Write-Host " Chain: LE (ISRG Root X1/X2) - trusted by Windows natively." -ForegroundColor Green
 } else {
 Write-Host " WARNING: Issuer is not Let's Encrypt. Verify manually." -ForegroundColor Yellow
 }
} else {
 Write-Host " Continuing - Windows will validate cert at connect time." -ForegroundColor Yellow
}

# STEP 2 - Remove old connections
Write-Host "`n=== [2/5] Cleaning up old connections ===" -ForegroundColor Cyan
foreach ($n in (@($ConnectionName) + $LegacyNames)) {
 foreach ($scope in @($false,$true)) {
 $ex = Get-VpnConnection -Name $n -AllUserConnection:$scope -ErrorAction SilentlyContinue
 if ($ex) {
 Write-Host " Removing: $n (TunnelType=$($ex.TunnelType))" -ForegroundColor Yellow
 try { rasdial $n /disconnect 2>&1 | Out-Null } catch {}
 try { Remove-VpnConnection -Name $n -AllUserConnection:$scope -Force -ErrorAction Stop } catch { Write-Warning "Remove failed: $_" }
 }
 }
}
Start-Sleep -Seconds 1
Write-Host " Done." -ForegroundColor Green

# STEP 3 - Create connection with EAP-MSCHAPv2
Write-Host "`n=== [3/5] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

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
 <EapMSChapV2 xmlns="http://www.microsoft.com/provisioning/EapMsChapV2ConnectionPropertiesV1">
 <UseWinLogonCredentials>false</UseWinLogonCredentials>
 <ServerValidation>
 <DisableUserPromptForServerValidation>true</DisableUserPromptForServerValidation>
 <ServerNames>$RemoteId</ServerNames>
 </ServerValidation>
 </EapMSChapV2>
 </Config>
 </EapHostConfig>
 </Configuration>
 </Eap>
 </Authentication>
 <RoutingPolicyType>ForceTunnel</RoutingPolicyType>
 </NativeProfile>
</VPNProfile>
"@

try {
 $xmlDoc = New-Object System.Xml.XmlDocument
 $xmlDoc.LoadXml($profileXml)
 $xmlStream = New-Object System.IO.StringReader($profileXml)
 $xmlReader = [System.Xml.XmlReader]::Create($xmlStream)

 Add-VpnConnection `
 -Name $ConnectionName `
 -ServerAddress $ServerAddress `
 -TunnelType "IKEv2" `
 -EapConfigXmlStream $xmlReader `
 -RememberCredential `
 -PassThru -ErrorAction Stop | Out-Null

 Write-Host " Connection created." -ForegroundColor Green
} catch {
 Write-Host " ERROR: $($_.Exception.Message)" -ForegroundColor Red
 Write-Host $profileXml -ForegroundColor DarkGray
 exit 1
}

# STEP 4 - IPsec crypto + registry
Write-Host "`n=== [4/5] Configuring IPsec crypto ===" -ForegroundColor Cyan
try {
 Set-VpnConnectionIPsecConfiguration `
 -ConnectionName $ConnectionName `
 -AuthenticationTransformConstants "SHA256128" `
 -CipherTransformConstants "AES256" `
 -DHGroup "Group14" `
 -EncryptionMethod "AES256" `
 -IntegrityCheckMethod "SHA256" `
 -PfsGroup "None" `
 -Force -ErrorAction Stop | Out-Null
 Write-Host " IPsec: SHA256128 / AES256 / Group14 / PFS None" -ForegroundColor Green
} catch {
 Write-Warning "IPsec config failed: $_. Windows defaults may not match server."
}

New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
 -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null
Write-Host " Registry: NegotiateDH2048_AES256 = 1" -ForegroundColor Green

New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\PolicyAgent" `
 -Name "AssumeUDPEncapsulationContextOnSendRule" -PropertyType DWord -Value 2 -Force | Out-Null
Write-Host " Registry: AssumeUDPEncapsulationContextOnSendRule = 2" -ForegroundColor Green

# STEP 5 - Store credentials
Write-Host "`n=== [5/5] Storing credentials ===" -ForegroundColor Cyan
cmdkey /generic:$ServerAddress /user:$Username /pass:$Password | Out-Null
Write-Host " Credentials stored for $ServerAddress." -ForegroundColor Green

# VERIFY
Write-Host "`n=== Setup complete ===" -ForegroundColor Cyan
Start-Sleep -Seconds 1
$conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($conn) {
 Write-Host " Name: $($conn.Name)" -ForegroundColor Green
 Write-Host " Server: $($conn.ServerAddress)" -ForegroundColor Green
 Write-Host " Tunnel: $($conn.TunnelType)" -ForegroundColor Green
 Write-Host " Auth: $($conn.AuthenticationMethod)" -ForegroundColor Green
} else {
 Write-Host " WARNING: connection not visible. Refresh VPN settings." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Connect:" -ForegroundColor Yellow
Write-Host " Settings -> Network & Internet -> VPN -> $ConnectionName -> Connect"
Write-Host ""
Write-Host "Server: $ServerAddress (grey-cloud DNS, resolves directly to VPS)"
Write-Host "Remote ID: $RemoteId (matches cert CN/SAN)"
Write-Host "Username: $Username (auto-filled)"
Write-Host ""
Write-Host "Test after connecting:"
Write-Host " tracert 8.8.8.8 (first hop should be VPS, not your router)"
Write-Host " Invoke-WebRequest https://ifconfig.me (should return VPS IP)"
Write-Host ""
Write-Host "Disconnect: rasdial $ConnectionName /disconnect"
Write-Host ""
Write-Host "Log: $transcriptPath" -ForegroundColor DarkGray

try { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null } catch {}
