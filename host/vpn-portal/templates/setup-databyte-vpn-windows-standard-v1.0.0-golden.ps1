<#
.SYNOPSIS
    Databyte VPN installer (Windows template) for Windows 10/11.
    AUTO-RECONSTRUCTED + PATCHED by Misha: real password + -Force + sanity check catches ***.
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'

# ============================================================================
# BAKED-IN CONFIG (PATCHED: real password baked in)
# ============================================================================
$ServerAddress  = "myvpn.databyte.co.za"
$RemoteId       = "myvpn.databyte.co.za"
$ConnectionName = "DatabyteVPN"
$PortalBase     = "https://vpn-portal.databyte.co.za"
$ServerCertSha256 = "01:B1:E8:06:82:F7:05:8D:33:B0:37:FB:97:DA:54:A2:F4:83:88:3B:C8:E7:05:99:FF:E5:BE:09:70:21:29:DB"
$LERootUrl = "$PortalBase/static/certs/isrg-root-x2.pem"
$Username = "zunaid-new-win11"
$Password = "6cwXbN_2Ctn8AoZQs8LqEA"
$ExpectedIssuerMatch = "Let.?s Encrypt|ISRG"
$Crypto = @{
    AuthenticationTransformConstants = "SHA256128"
    CipherTransformConstants         = "AES128"
    DHGroup                          = "Group14"
    EncryptionMethod                 = "AES128"
    IntegrityCheckMethod             = "SHA256"
    PfsGroup                         = "PFS2048"
}

# FIXED: sanity check also catches ***
if ($Username -like "REPLACE-ME*" -or $Password -like "REPLACE-ME*" -or $Password -eq "***") {
    Write-Host "ERROR: Replace *** and REPLACE-ME-* values first." -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}

# STEP 0 - Bootstrap ISRG Root X2
$lerRootLocal = Join-Path $env:TEMP "isrg-root-x2.pem"
$x2Thumb = "915B640DFE48298E5C75E60B1D8E3FA48D43A32B"
$x2AlreadyTrusted = $false
try {
    $existing = Get-ChildItem Cert:\CurrentUser\Root, Cert:\LocalMachine\Root -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $x2Thumb }
    if ($existing) { $x2AlreadyTrusted = $true }
} catch {}
if ($x2AlreadyTrusted) {
    Write-Host "  ISRG Root X2: already trusted" -ForegroundColor Green
} else {
    $curlOut = & curl.exe -ksSL -o $lerRootLocal $LERootUrl 2>&1
    if ($LASTEXITCODE -eq 0 -and (Test-Path $lerRootLocal)) {
        Write-Host "  Downloaded ISRG Root X2" -ForegroundColor Green
        & certutil.exe -addstore -f Root $lerRootLocal 2>&1 | Out-Null
    }
}

# STEP 1 - Verify server cert
$tcp = New-Object System.Net.Sockets.TcpClient
$tcp.SendTimeout    = 5000
$tcp.ReceiveTimeout = 5000
$iar = $tcp.BeginConnect($ServerAddress, 443, $null, $null)
if (-not $iar.AsyncWaitHandle.WaitOne(10000)) { $tcp.Close() }
$tcp.EndConnect($iar)
$ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, {[System.Net.Security.RemoteCertificateValidationCallback]{ $true }})
$ssl.AuthenticateAsClient($ServerAddress)
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
$ssl.Close(); $tcp.Close()
Write-Host "  Subject: $($cert.Subject)" -ForegroundColor Green
if ($cert.Issuer -notmatch $ExpectedIssuerMatch) {
    Write-Host "  [FAIL] Issuer is not Let's Encrypt" -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}
if ($ServerCertSha256 -ne "REPLACE-ME-sha256-fingerprint") {
    $actualFp = ($cert.GetCertHash("SHA256") | ForEach-Object { $_.ToString("X2") }) -join ":"
    if ($actualFp -ne $ServerCertSha256) {
        Write-Host "  [FAIL] Cert fingerprint mismatch" -ForegroundColor Red
        Read-Host "Press Enter to exit"; exit 1
    }
}

# STEP 2 - Clean slate
$stale = Get-VpnConnection -AllUserConnection:$false -ErrorAction SilentlyContinue | Where-Object { $_.ServerAddress -match [regex]::Escape($ServerAddress) }
foreach ($s in $stale) { Remove-VpnConnection -Name $s.Name -AllUserConnection:$false -Force }
$cmdkeyList = cmdkey /list 2>&1 | Out-String
foreach ($line in ($cmdkeyList -split "`r?`n")) {
    if ($line -match 'Target:\s*(?<t>.+?)\s*$') {
        $t = $Matches['t'].Trim()
        if ($t -match 'databyte|myvpn|zunaid') { cmdkey /delete:$t 2>&1 | Out-Null }
    }
}

# STEP 3 - Create VPN profile
try {
    $eap = New-EapConfiguration -ErrorAction Stop
    $xmlDoc = $eap.EapConfigXmlStream
    Add-VpnConnection -Name $ConnectionName -ServerAddress $ServerAddress -TunnelType "IKEv2" -EapConfigXmlStream $xmlDoc -RememberCredential -PassThru -ErrorAction Stop | Out-Null
} catch {
    Write-Host "  ERROR: Add-VpnConnection failed" -ForegroundColor Red
    exit 1
}

# STEP 4 - IPsec crypto (FIXED: -Force added)
Set-VpnConnectionIPsecConfiguration -ConnectionName $ConnectionName `
    -AuthenticationTransformConstants $Crypto.AuthenticationTransformConstants `
    -CipherTransformConstants         $Crypto.CipherTransformConstants `
    -DHGroup                          $Crypto.DHGroup `
    -EncryptionMethod                 $Crypto.EncryptionMethod `
    -IntegrityCheckMethod             $Crypto.IntegrityCheckMethod `
    -PfsGroup                         $Crypto.PfsGroup -Force

# STEP 5 - Registry
Set-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\RasMan\\Parameters" -Name "NegotiateDH2048_AES256" -Value 2 -Type DWord
Set-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\PolicyAgent" -Name "AssumeUDPEncapsulationContextOnSendRule" -Value 2 -Type DWord

# STEP 6 - RasSetCredentials (HOT-177: ByValTStr, HOT-183: Mask=0x7)
$sig = @'
using System.Runtime.InteropServices;
public class Cred {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct RASCREDENTIALS {
        public uint Size; public uint Mask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)] public string userName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 257)] public string password;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 16)] public string domain;
    }
    [DllImport("rasapi32.dll", CharSet=CharSet.Unicode)]
    public static extern uint RasSetCredentials(string phonebook, string entry, ref RASCREDENTIALS creds, bool fClear);
}
'@
Add-Type -TypeDefinition $sig -Force
$c = New-Object Cred+RASCREDENTIALS
$c.Size = [System.Runtime.InteropServices.Marshal]::SizeOf($c)
$c.Mask = 0x7
$c.UserName = $Username
$c.Password = $Password
$c.Domain = ""
[Cred]::RasSetCredentials("", $ConnectionName, [ref]$c, $false) | Out-Null

# STEP 7 - Connect (with inline creds)
rasdial $ConnectionName $Username $Password

# Verify
Start-Sleep -Seconds 5
$conn = Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue
if ($conn -and $conn.ConnectionStatus -eq "Connected") {
    Write-Host "  [OK] Connected" -ForegroundColor Green
} else {
    Write-Host "  Check Settings -> VPN" -ForegroundColor Yellow
    Start-Process ms-settings:network-vpn
}

Read-Host "Press Enter to exit"
