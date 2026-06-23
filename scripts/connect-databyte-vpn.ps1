$ServerAddress = "myvpn.databyte.co.za"
$ConnectionName = "DatabyteVPN"
$Username = "zun-operator"
$Password = "vrRvjQua-cmK9fWYe-jGWqdJWg-Cjc9oaXi"

# Step 0 - Make sure the strongSwan CA cert is installed (fetches from live URL
# with SHA256 fingerprint pinning, falls back to bundled cert).
$setupScript = Join-Path $PSScriptRoot "setup-windows-vpn.ps1"
if (Test-Path $setupScript) {
    Write-Host "Running setup-windows-vpn.ps1 (steps 1-3) before connecting..." -ForegroundColor Cyan
    & $setupScript -ErrorAction Continue
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Setup failed with exit code $LASTEXITCODE. Aborting connect." -ForegroundColor Red
        exit $LASTEXITCODE
    }
} else {
    Write-Host "WARN: setup-windows-vpn.ps1 not found at $setupScript" -ForegroundColor Yellow
    Write-Host "      CA cert may not be installed. Run it manually first." -ForegroundColor Yellow
}

# Tear down old connection if it exists
rasdial $ConnectionName /disconnect 2>&1 | Out-Null
Start-Sleep -Seconds 1
Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Build a VPN profile XML that pre-configures EAP-MSCHAPv2 (no dialog at connect time)
$xmlPath = Join-Path $env:TEMP "databyte-vpn.xml"
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

# Create the connection from the profile XML
Add-VpnConnection -Name $ConnectionName `
  -ServerAddress $ServerAddress `
  -TunnelType "IKEv2" `
  -RememberCredential `
  -ConfigurationFile $xmlPath

Remove-Item $xmlPath -Force

# Set IKEv2 crypto to match the server
Set-VpnConnectionIPsecConfiguration -ConnectionName $ConnectionName `
  -AuthenticationTransformConstants "SHA256" `
  -CipherTransformConstants "AES128" `
  -DHGroup "ECP384" `
  -EncryptionMethod "AES128" `
  -IntegrityCheckMethod "SHA256" `
  -PfsGroup "ECP384" -Force

# Enable strong DH (Group14+) registry tweak
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
  -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null

# Store creds so the GUI auto-fills (no typing)
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null

Write-Host ""
Write-Host "Setup complete. Click Connect in the GUI:"
Write-Host "  Settings -> Network & Internet -> VPN -> DatabyteVPN -> Connect"
Write-Host ""
Write-Host "Username is pre-filled. Password is pre-filled. No typing."
Write-Host ""
Write-Host "Test after connecting:"
Write-Host "  tracert 8.8.8.8  (first hop should be 154.65.110.44)"
Write-Host "  iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30  (expect ~17-20 Mbps)"
