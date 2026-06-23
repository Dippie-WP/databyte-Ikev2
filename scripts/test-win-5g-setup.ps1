<#
.SYNOPSIS
    Windows IKEv2 VPN setup for the test-win-5g customer on production
    Databyte VPN (vpn-prod-01 / 154.65.110.44).

.DESCRIPTION
    Same pattern as setup-windows-vpn.ps1, but:

    1. Uses the **raw IP** 154.65.110.44 as ServerAddress (bypasses Cloudflare
       proxy which doesn't forward IKEv2 UDP 500/4500).
    2. Uses **myvpn.databyte.co.za** as Remote ID (cert CN/SAN matches).
    3. Uses **test-win-5g-laptop** EAP credentials (not the operator account).

    This is the same Server/RemoteID separation that worked for iPhone and
    Android strongSwan app. Same configuration for Windows IKEv2 native
    client.

    Run as Administrator:
      PS> powershell -ExecutionPolicy Bypass -File .\test-win-5g-setup.ps1

.NOTES
    This is a TEST CUSTOMER setup. Will be deleted after the multi-device
    test completes. NOT for production customer use.
#>

#Requires -RunAsAdministrator

# ============================================================================
# CONFIG (test-win-5g specific)
# ============================================================================
$ServerIp         = "154.65.110.44"                  # raw IP, NOT hostname
$RemoteId         = "myvpn.databyte.co.za"          # matches cert SAN
$ConnectionName   = "DatabyteVPNTest"
$Username         = "test-win-5g-laptop"
$Password         = "a1V5M2Cd1oE0TNWY9wORsg"

# CA cert pinned SHA256 fingerprint (the cert we EXPECT to receive).
$ExpectedCaSha256 = "5C:10:B9:6A:97:06:10:29:7C:8D:8F:B3:6B:E3:5A:98:58:CF:F4:10:C8:1E:72:78:7E:25:08:43:B2:71:CE:06"

# URL to fetch the live CA cert from. Cloudflare edge caches for 24h.
# Uses the hostname (HTTPS) because this is for cert download, not VPN tunnel.
$CaCertUrl        = "https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem"

# Path to bundled fallback CA cert.
$CaCertFallback   = Join-Path $PSScriptRoot "strongswan-ca.crt.pem"
if (-not (Test-Path $CaCertFallback)) {
    $CaCertFallback = ".\strongswan-ca.crt.pem"
}

# Expected CA cert subject for "already installed" check.
$CaSubject = "CN=strongSwan CA"

# ============================================================================
# Helper: compute SHA256 fingerprint of a file
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
# STEP 1 - Install CA cert (fetch from live URL with fingerprint pinning)
# ============================================================================
Write-Host ""
Write-Host "=== [1/5] Installing CA cert to LocalMachine\Root ===" -ForegroundColor Cyan

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
                $certSource = "live"
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

        if (-not $certSource) {
            if (-not (Test-Path $CaCertFallback)) {
                Write-Host "  ERROR: Bundled fallback cert not found at: $CaCertFallback" -ForegroundColor Red
                exit 1
            }
            $bundledSha = Get-Sha256Fingerprint -Path $CaCertFallback
            if ($bundledSha -eq $ExpectedCaSha256) {
                Write-Host "  Bundled cert SHA256 matches pinned value." -ForegroundColor Green
                $CaCertPath = $CaCertFallback
                $certSource = "bundled-fallback"
            } else {
                Write-Host "  ERROR: Bundled cert SHA256 mismatch." -ForegroundColor Red
                Write-Host "    Expected: $ExpectedCaSha256" -ForegroundColor Red
                Write-Host "    Got:      $bundledSha" -ForegroundColor Red
                exit 1
            }
        }

        Import-Certificate -FilePath $CaCertPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        if ($certSource -eq "live") {
            Write-Host "  CA cert installed from LIVE URL." -ForegroundColor Green
        } else {
            Write-Host "  CA cert installed from BUNDLED FALLBACK." -ForegroundColor Yellow
        }
    } finally {
        if ($tempLive -and (Test-Path $tempLive)) {
            Remove-Item $tempLive -ErrorAction SilentlyContinue
        }
    }
}

# ============================================================================
# STEP 2 - Remove old connection, create fresh with raw IP
# ============================================================================
Write-Host ""
Write-Host "=== [2/5] Creating VPN connection '$ConnectionName' ===" -ForegroundColor Cyan

$existingConn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Host "  Removing existing connection..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>&1 | Out-Null
    Remove-VpnConnection -Name $ConnectionName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# Build a profile XML that pre-configures EAP-MSCHAPv2 with the Remote ID.
# Native Add-VpnConnection can't set RemoteID directly — it must be in the
# profile XML's <ServerNames> under <ServerValidation>.
$xmlPath = Join-Path $env:TEMP "databyte-test-vpn.xml"
@"
<VPNProfile>
  <NativeProfile>
    <Servers>$ServerIp</Servers>
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
                  <DisableUserPromptForServerValidation>false</DisableUserPromptForServerValidation>
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
"@ | Out-File $xmlPath -Encoding UTF8

Add-VpnConnection `
    -Name $ConnectionName `
    -ServerAddress $ServerIp `
    -TunnelType "IKEv2" `
    -RememberCredential `
    -ConfigurationFile $xmlPath `
    -PassThru | Out-Null

Remove-Item $xmlPath -Force
Write-Host "  Connection created with Server=$ServerIp, Remote ID=$RemoteId" -ForegroundColor Green

# ============================================================================
# STEP 3 - Set IKEv2 IPsec crypto to match strongSwan server
# ============================================================================
Write-Host ""
Write-Host "=== [3/5] Configuring IPsec crypto ===" -ForegroundColor Cyan

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
}

# Enable strong DH (Group14+) registry tweak
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
    -Name "NegotiateDH2048_AES256" -PropertyType DWord -Value 1 -Force | Out-Null

# ============================================================================
# STEP 4 - Store creds so GUI auto-fills
# ============================================================================
Write-Host ""
Write-Host "=== [4/5] Storing credentials ===" -ForegroundColor Cyan
cmdkey /generic:$ConnectionName /user:$Username /pass:$Password | Out-Null
Write-Host "  Credentials stored for '$Username'" -ForegroundColor Green

# ============================================================================
# STEP 5 - Connect
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
    Write-Host "  Troubleshooting:" -ForegroundColor Yellow
    Write-Host "  1. Check Windows Event Viewer: Applications and Services Logs -> Microsoft -> Windows -> NetworkProfile -> Operational" -ForegroundColor Yellow
    Write-Host "  2. Run: Get-VpnConnection -Name '$ConnectionName' | Format-List" -ForegroundColor Yellow
    Write-Host "  3. Try: rasdial '$ConnectionName' /phonebook:$(Split-Path $PSCommandPath)\vpn-phonebook.pbk" -ForegroundColor Yellow
}

# ============================================================================
# Summary
# ============================================================================
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host "  Connection:  $ConnectionName"
Write-Host "  Server:      $ServerIp (raw IP, bypasses Cloudflare proxy)"
Write-Host "  Remote ID:   $RemoteId (matches cert CN/SAN)"
Write-Host "  Username:    $Username"
Write-Host "  Crypto:      AES256/SHA256/Group14/ECP384"
Write-Host "  Tunnel:      ForceTunnel (full tunnel, no split)"
Write-Host ""
Write-Host "To disconnect:  rasdial '$ConnectionName' /disconnect"
Write-Host "To reconnect:   re-run this script OR click Connect in Settings"
Write-Host ""
Write-Host "Verify after connect:"
Write-Host "  ipconfig  (look for PPP adapter with 10.99.0.x VIP)"
Write-Host "  tracert 8.8.8.8  (first hop should be 154.65.110.44)"
Write-Host "  Test bandwidth: iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30"
Write-Host "    (expect 17-20 Mbps per tier_10gb cap)"
