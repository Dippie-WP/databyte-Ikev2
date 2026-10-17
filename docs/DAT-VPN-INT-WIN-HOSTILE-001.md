# Windows Installer for Hostile Networks (Type H)

Generic manual-entry path for installing the Windows IKEv2 + EAP-MSCHAPv2 VPN client on a FortiGate / SonicWall / corporate-firewall network where HTTPS interception breaks the standard installer script.

> **Generic note:** This doc was originally databyte-internal (v1.1.0, 2026-07-10). It has been hand-curated 2026-10-17 for generic use — all databyte branding, dates, internal ticket references, and customer-specific details removed. Replace all `<placeholder>` values with your own.

**Companion to:** [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) (Type N — Non-hostile, script-based). Use Type N when the customer's network is normal. Use Type H when Type N fails due to TLS MITM by a corporate firewall.

---

## 0. Purpose

You are onboarding a new customer onto your self-hosted IKEv2 VPN on Windows 10/11 who is **on a hostile network** (FortiGate SSL inspection, SonicWall CFS, captive portal, or any corporate firewall doing TLS MITM). The Type N script-based approach will fail at STEP 1 (cert validation) because the firewall intercepts the SslStream TLS handshake.

This document covers the **manual PowerShell entry path** that bypasses every TCP/443 blocker by using only local Win32 API calls + IKEv2 over UDP 500/4500 (which firewalls allow by default). Source of truth for the Type H install approach.

**This document does NOT cover** non-hostile networks — see [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) for the standard approach. The Type N script is faster, validates the cert, and is the default path. Use Type H only when Type N fails.

---

## 1. How we got here — the failure journey

This isn't theoretical. The Type H approach exists because the Type N script failed on a real production customer. Here is the full sequence:

### 1.1 Initial assumption (wrong)

The customer was being onboarded, the standard Type N baked installer was run:

```powershell
curl -ksSL -o $env:TEMP\setup.ps1 https://<your-portal-domain>/static/baked/<your-template-name>-<customer>-<device>.ps1
powershell -ExecutionPolicy Bypass -NoProfile -File $env:TEMP\setup.ps1
```

**Result**: STEP 0 (bootstrap ISRG cert) worked. STEP 1 (verify server TLS cert) **failed**:

```powershell
[FAIL] Cert fetch failed: Exception calling "AuthenticateAsClient" with "1" argument(s):
       "Unable to read data from the transport connection: A connection attempt failed
        because the connected party did not properly respond after a period of time, or
        established connection failed because connected host has failed to respond."
```

**Source**: `swanctl --list-sas` showed nothing — the script never reached IKEv2.

### 1.2 First hypothesis (wrong): captive portal

We assumed the customer's network had a captive portal intercepting all TCP/443. The customer was using a "other VPN" to bypass. We tried:

- **Hypothesis A**: URL filtering on `<your-portal-domain>` → tried changing `$ServerAddress` to `<your-vpn-hostname>`
- **Hypothesis B**: Captive portal challenge → tried cellular hotspot (deferred)

**Result**: Both wrong.

### 1.3 Second hypothesis (partially right): CDN SSL classification

We discovered the downloaded file was **HTML instead of PowerShell** (size 35,179 vs expected 23,609, first line `<!DOCTYPE html>` vs `<#`).

**Source**: diagnostic output:
```
"=== SIZE ==="
35179
"=== MD5 ==="
<wrong-hash>
"=== FIRST LINE ==="
<!DOCTYPE html>
```

The CDN-fronted URL `<your-portal-domain>` was returning a FortiGuard "Web Filter Violation / Spam URLs" HTML page. We tried `curl --resolve` to force direct VPS IP — that failed with `SEC_E_INVALID_TOKEN` (FortiGate does IP-level SSL inspection).

### 1.4 The actual root cause: FortiGate full TLS MITM

We confirmed by testing from a clean network that:
- `curl https://<your-vpn-hostname>/api/health` → returns CDN-fronted page on `<your-portal-domain>`, but `<your-vpn-hostname>` returns `CN=<your-vpn-hostname>, Issuer=Let's Encrypt YE1`
- `curl https://<your-portal-domain>/api/health` → returns `CN=<your-domain>, Issuer=Google Trust Services WE1` (CDN Universal SSL)

**Conclusion**: FortiGate is performing **full HTTPS MITM** with response-body substitution. It intercepts:
- TCP/443 to `<your-vpn-hostname>` → blocks / substitutes
- TCP/443 to `<your-portal-domain>` → returns FortiGuard block page (HTML, 35,179 B)
- UDP/500, UDP/4500 → passes through (IKEv2 NAT-T)

### 1.5 The breakthrough: manual entry over UDP

We realized: **any path that uses HTTPS will fail**. The Type N script uses HTTPS at STEP 0 (curl to download ISRG cert) AND STEP 1 (SslStream cert validation).

**The solution**: bypass all HTTPS by using only local Win32 API calls + IKEv2. Five PowerShell commands, all local:

1. `Add-VpnConnection` — local Win32 API
2. `Set-VpnConnectionIPsecConfiguration` — local registry
3. `Set-ItemProperty` for `NegotiateDH2048_AES256` — local registry
4. `RasSetCredentials` (P/Invoke) — local Win32 API
5. `rasdial.exe` — IKEv2 over UDP 500/4500 (only network traffic)

**Result**: ✅ Both customers connected. IKE_SAs established. Framed IPs assigned.

---

## 2. Architecture (what you build differently)

```
┌─────────────────────────────────────────────────────────────┐
│  OPERATOR (you, on any host)                                 │
│  1. Generate EAP credentials (python secrets)                │
│  2. Compute NT-Password hash (openssl md4 -provider legacy)  │
│  3. Insert into MariaDB FreeRADIUS (radusergroup, radcheck)  │
│  4. Save audit file (mode 600)                               │
│  5. Hand-deliver PowerShell 5-step block to customer         │
└────────────────────┬────────────────────────────────────────┘
                     │ Telegram DM, encrypted email, or SFTP
                     │ (NEVER email the .ps1 file with creds inlined)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  CUSTOMER (Win 10 / 11, PowerShell Admin, hostile network)    │
│  1. Add-VpnConnection                                        │
│  2. Set-VpnConnectionIPsecConfiguration                      │
│  3. Set-ItemProperty (registry tweak)                        │
│  4. RasSetCredentials (P/Invoke)                              │
│  5. rasdial.exe "<vpn-connection-name>"                                │
└────────────────────┬────────────────────────────────────────┘
                     │ IKEv2 + EAP-MSCHAPv2 (UDP 500/4500)
                     │ NO HTTPS involved
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  VPS strongSwan (Docker)                                     │
│  eap-radius → FreeRADIUS → MariaDB radcheck → Access-Accept  │
└─────────────────────────────────────────────────────────────┘
```

**Critical difference from Type N**: Zero HTTPS during configuration. The only network traffic is IKEv2/UDP which FortiGate allows.

---

## 3. The 5-step manual entry (Type H)

> **Design note:** Because the customer must hand-type the PowerShell block on a FortiGate network (HTTPS is broken), we use a single ready-to-paste block with PowerShell variables at the top for the operator's credentials. The customer copy-pastes literally — no placeholder hunt, no missed `<EAP identity>`, no Access-Reject from stale creds.

### Customer-ready block (operator fills 2 lines marked `# ⬇ FILL THIS ⬇`)

```powershell
# ============================================
# <vpn-connection-name> — MANUAL ENTRY (Type H)
# Server: <your-vpn-hostname>
# Paste this entire block into PowerShell Admin
# ============================================

# ⬇ FILL THIS ⬇ (operator: insert the customer's EAP username)
$EAP_USERNAME = "<customer-eap-username>"
# ⬇ FILL THIS ⬇ (operator: insert the customer's EAP password)
$EAP_PASSWORD = "<customer-eap-password>"

# Step 1: Create VPN profile
Add-VpnConnection -Name "<vpn-connection-name>" `
  -ServerAddress "<your-vpn-hostname>" `
  -TunnelType "IKEv2" `
  -AuthenticationMethod "EAP" `
  -RememberCredential `
  -PassThru

# Step 2: Set IPsec crypto
Set-VpnConnectionIPsecConfiguration -ConnectionName "<vpn-connection-name>" `
  -AuthenticationTransformConstants "SHA256128" `
  -CipherTransformConstants "AES128" `
  -DHGroup "Group14" `
  -EncryptionMethod "AES256" `
  -IntegrityCheckMethod "SHA256" `
  -PfsGroup "PFS2048"

# Step 3: Force modp2048 for IKE
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
  -Name "NegotiateDH2048_AES256" -Value 2 -Type DWord

# Step 4: Bind credentials via RasSetCredentials
cmdkey /delete:ras\<vpn-connection-name> 2>$null
cmdkey /delete:<your-vpn-hostname> 2>$null
cmdkey /delete:<your-public-ip> 2>$null

$sig = @"
using System.Runtime.InteropServices;
public class Cred {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct RASCREDENTIALS {
        public uint Size; public uint Mask;
        [MarshalAs(UnmanagedType.LPWStr)] public string UserName;
        [MarshalAs(UnmanagedType.LPWStr)] public string Password;
        [MarshalAs(UnmanagedType.LPWStr)] public string Domain;
    }
    [DllImport("rasapi32.dll", CharSet=CharSet.Unicode)]
    public static extern uint RasSetCredentials(string phonebook, string entry, ref RASCREDENTIALS creds, bool fClear);
}

"@
Add-Type -TypeDefinition $sig -Force
$c = New-Object Cred+RASCREDENTIALS
$c.Size = [System.Runtime.InteropServices.Marshal]::SizeOf($c)
$c.Mask = 0x87
$c.UserName = $EAP_USERNAME
$c.Password = $EAP_PASSWORD
$c.Domain = ""
$ret = [Cred]::RasSetCredentials("", "<vpn-connection-name>", [ref]$c, $false)
Write-Host "RasSetCredentials returned: $ret (0=OK, 87=ERROR_INVALID_PARAMETER, 1162=ERROR_NOT_FOUND)"

# Step 5: Connect
rasdial.exe "<vpn-connection-name>"

# Verify
Get-VpnConnection -Name "<vpn-connection-name>"
ipconfig /all | Select-String "<vpn-pool-subnet>"
ping 10.99.0.1
```

**Why this design is better than template+placeholder hunt:**

| Aspect | Old design (template + `<placeholder>`) | New design (operator fills 2 lines) |
|---|---|---|
| Operator effort | Replace `<EAP identity>` and `<EAP password>` deep in Step 4 | Replace 2 clearly-marked lines at the top |
| Customer effort | Find the placeholders, edit them | Copy-paste the whole block, variables are already set |
| Error risk | Customer misses a placeholder → Access-Reject | Customer copy-pastes literally → correct creds |
| Audit trail | Operator sends raw template, edits are off-channel | Operator edits 2 lines before sending, no post-edit |
| Visual cue | None — placeholders are inline | Big `# ⬇ FILL THIS ⬇` markers |

### Step-by-step rationale

**Step 1 — Create VPN profile**

```powershell
Add-VpnConnection -Name "<vpn-connection-name>" `
  -ServerAddress "<your-vpn-hostname>" `
  -TunnelType "IKEv2" `
  -AuthenticationMethod "EAP" `
  -RememberCredential `
  -PassThru
```

**Why `<your-vpn-hostname>`, not `<your-portal-domain>`**: The VPN connection itself uses the VPN hostname directly. HTTPS is irrelevant for IKEv2 (UDP/TCP-based). The cert is checked at the application layer (RasSetCredentials), not the IKE_SA establishment.

**Why `-RememberCredential`**: Makes Windows store the EAP credentials in the RAS phonebook so subsequent rasdial calls don't need creds.

### Step 2 — IPsec crypto (must match strongSwan)

```powershell
Set-VpnConnectionIPsecConfiguration -ConnectionName "<vpn-connection-name>" `
  -AuthenticationTransformConstants "SHA256128" `
  -CipherTransformConstants "AES128" `
  -DHGroup "Group14" `
  -EncryptionMethod "AES256" `
  -IntegrityCheckMethod "SHA256" `
  -PfsGroup "PFS2048"
```

**Why these specific values**: strongSwan server's proposals are `aes128-sha256-modp2048-ecp256` and `aes256-sha256-modp2048-ecp256`. Windows defaults to DES3/SHA1/DH2/modp1024 which strongSwan rejects. We must explicitly set:
- `CipherTransformConstants AES128` / `EncryptionMethod AES256` — AES128 for ESP, AES256 for IKE
- `AuthenticationTransformConstants SHA256128` / `IntegrityCheckMethod SHA256` — HMAC-SHA-256 with 128-bit truncation for ESP, full SHA-256 for IKE
- `DHGroup Group14` / `PfsGroup PFS2048` — DH group 14 = modp2048 (not modp1024 default)

### Step 3 — Force modp2048 for IKE (registry)

```powershell
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters" `
  -Name "NegotiateDH2048_AES256" -Value 2 -Type DWord
```

**Why registry and not Set-VpnConnection**: this is a Windows-wide IKE policy, not a per-connection setting. `NegotiateDH2048_AES256=2` means **enforce** DH2048 + AES256. Without it, Windows may try DH1024 on its first proposal (NIST-deprecated since 2015), which strongSwan rejects with `NO_PROPOSAL_CHOSEN`. Set once, persists across all profiles.

### Step 4 — Bind credentials via RasSetCredentials (P/Invoke)

```powershell
$sig = @"
using System.Runtime.InteropServices;
public class Cred {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct RASCREDENTIALS {
        public uint Size; public uint Mask;
        [MarshalAs(UnmanagedType.LPWStr)] public string UserName;
        [MarshalAs(UnmanagedType.LPWStr)] public string Password;
        [MarshalAs(UnmanagedType.LPWStr)] public string Domain;
    }
    [DllImport("rasapi32.dll", CharSet=CharSet.Unicode)]
    public static extern uint RasSetCredentials(string phonebook, string entry, ref RASCREDENTIALS creds, bool fClear);
}
"@
Add-Type -TypeDefinition $sig -Force
$c = New-Object Cred+RASCREDENTIALS
$c.Size = [System.Runtime.InteropServices.Marshal]::SizeOf($c)
$c.Mask = 0x87  # UserName | Password | Domain
$c.UserName = $EAP_USERNAME
$c.Password = $EAP_PASSWORD
$c.Domain = ""
$ret = [Cred]::RasSetCredentials("", "<vpn-connection-name>", [ref]$c, $false)
Write-Host "RasSetCredentials returned: $ret (0=OK, 87=ERROR_INVALID_PARAMETER, 1162=ERROR_NOT_FOUND)"
```

**Why RasSetCredentials, not cmdkey**: this is the critical discovery from the failed Type N run. cmdkey stores credentials in the Windows Credential Manager, but **IKEv2 EAP doesn't read from there**. We observed Windows using stale cmdkey creds (from earlier lab-mode runs) in preference to freshly-bound RasSetCredentials. Result: radpostauth Access-Reject for the wrong username.

**Why the Mask=0x87**: 0x80=Domain, 0x04=Password, 0x02=Username, 0x01=Default. 0x87 = all four flags. Without 0x80 (Domain), the binding fails silently on some Windows builds.

**CRITICAL — clear cmdkey cache first:**

```powershell
cmdkey /delete:ras\<vpn-connection-name> 2>$null
cmdkey /delete:<your-vpn-hostname> 2>$null
cmdkey /delete:<your-public-ip> 2>$null
```

Without this, stale creds from prior runs (especially lab-mode test customers) will be used in preference to RasSetCredentials.

### Step 5 — Connect

```powershell
rasdial.exe "<vpn-connection-name>"
```

**First attempt may return error 703** ("Remote Access error 703 - The connection needs information from you"). This is **NORMAL** for IKEv2+EAP on first run — Windows needs the EAP profile seeded by the GUI dialog. Retry, or open `ncpa.cpl` → right-click the VPN → Connect once, then rasdial works.

**What you'll see on the VPS side:**
- radpostauth row: `<customer-eap-username> / Access-Accept` within 5-10s
- New IKE_SA: `rw-eap #N ESTABLISHED, EAP: '<customer-eap-username>' [<vpn-pool-ip>]`

---

## 4. Operator-side: provisioning the customer

### 4.1 Generate credentials

```bash
# 20-char password from python secrets (cryptographically secure)
PASSWORD=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)))")

# NT-Password hash (Python 3.13 has MD4 disabled; use OpenSSL legacy provider)
NT_HASH=$(printf '%s' "$PASSWORD" | iconv -t UTF-16LE | openssl md4 -provider legacy -provider default | awk '{print toupper($NF)}')
```

### 4.2 Insert into MariaDB FreeRADIUS

```bash
ssh root@<your-hostname> "mariadb -uroot -D radius << SQL
INSERT INTO radusergroup (UserName, GroupName, priority) VALUES ('<eap-username>', 'default', 0);
INSERT INTO radcheck (UserName, Attribute, op, Value) VALUES ('<eap-username>', 'Cleartext-Password', ':=', '$PASSWORD');
INSERT INTO radcheck (UserName, Attribute, op, Value) VALUES ('<eap-username>', 'NT-Password', ':=', '$NT_HASH');
SQL"
```

### 4.3 Live auth test

```bash
ssh root@<your-hostname> "radtest <eap-username> '$PASSWORD' 127.0.0.1 0 testing123"
# Expect: Access-Accept
```

### 4.4 Save audit file

```bash
AUDIT_DIR="/root/audit-bk/$(date -u +%Y-%m-%d)-<eap-username>-onboard"
ssh root@<your-hostname> "mkdir -p \$AUDIT_DIR && chmod 700 \$AUDIT_DIR"
ssh root@<your-hostname> "cat > \$AUDIT_DIR/password.txt << EOF
$(date -u +%Y-%m-%d\ %H:%M:%S\ UTC) - new customer onboarding
Customer:     <eap-username>
Device:       <device-name>
EAP identity: <eap-username>
Tier:         <tier>
Bandwidth:    <bandwidth>
Password:     $PASSWORD
NT hash:      $NT_HASH
Server:       <your-vpn-hostname>
VPN profile:  <profile-name>
Reason:       <why hostile - FortiGate, etc>
EOF
chmod 600 \$AUDIT_DIR/password.txt"
```

Repeat on the operator's local host at `<your-local-root>/audit-bk/...` for redundancy.

### 4.5 Hand-deliver the PowerShell block

Customer copies the block from §3 above, fills the 2 `# ⬇ FILL THIS ⬇` lines (operator has already done this in the block), and runs in PowerShell Admin.

---

## 5. Cross-network testing matrix

| Customer network type | Recommended approach | Why |
|---|---|---|
| Normal home/office | Type N ([`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md)) | Faster, cert validation, full automation |
| Hotel Wi-Fi (some intercept) | Type H (this doc) | HTTPS interception may block the cert download |
| Corporate FortiGate with full TLS MITM | Type H (this doc) | Only path that works — bypasses HTTPS entirely |
| Captive portal (no auth yet) | Customer opens browser, authenticates, then runs installer | Captive portal blocks all traffic until authenticated |
| Public Wi-Fi with HTTPS interception | Type H (this doc) | Same as FortiGate scenario |

---

## 6. Failure modes (Type H specific)

### H.1 Step 0 fails (no need for ISRG Root X2)

**Type N requires** downloading ISRG Root X2 via HTTPS. **Type H does NOT** — it uses only local Win32 API calls, no HTTPS needed. So the ISRG Root X2 bootstrap step is absent from the Type H block. If the customer still gets cert errors, their system is doing something unusual.

### H.2 Step 4 `RasSetCredentials` returns 87 (ERROR_INVALID_PARAMETER)

**Root cause:** Profile doesn't exist yet, or the script's `$VpnName` doesn't match the actual profile name. Verify:

```powershell
Get-VpnConnection -Name "<vpn-connection-name>"
```

If empty: re-run Step 1. If exists but doesn't match the script's name: edit the script's `$VpnName` and re-run from Step 4.

### H.3 Step 4 `RasSetCredentials` returns 1162 (ERROR_NOT_FOUND)

**Root cause:** Same as H.2 — profile doesn't exist. The script's check for profile existence (Step 1) should catch this. If it didn't: re-run Step 1, then Step 4.

### H.4 Step 4 `RasSetCredentials` returns 5 (ERROR_ACCESS_DENIED)

**Root cause:** PowerShell isn't running as Administrator. Re-run PowerShell as Admin.

### H.5 `rasdial` returns error 703

**Root cause:** Windows IKEv2+EAP requires the EAP profile seeded by GUI dialog on first run. This is normal. Retry, or open `ncpa.cpl` → right-click the VPN → Connect once, then rasdial works.

### H.6 radpostauth shows Access-Reject for a stale test customer (e.g. a lab-mode username)

**Root cause:** Windows cached stale cmdkey creds from a previous lab-mode run, used in preference to freshly-bound RasSetCredentials. The cmdkey clear at Step 4 should prevent this, but if it persists:

```powershell
cmdkey /list
# Look for entries matching the VPN server hostname/IP
cmdkey /delete:ras\<vpn-connection-name>
cmdkey /delete:<your-vpn-hostname>
cmdkey /delete:<your-public-ip>
```

Then re-run Step 4.

### H.7 No IKE_SA in `swanctl --list-sas`

**Root cause:** UDP 500/4500 is being blocked, or the customer's host file has `<your-vpn-hostname>` pointing to a wrong IP. Test:

```powershell
nslookup <your-vpn-hostname> 8.8.8.8
# Should return <your-public-ip>
ping <your-public-ip>
# If ping fails, UDP is blocked
```

If UDP is blocked: customer is on a network that only allows TCP (rare, but some corporate networks do this). Try a different network.

### H.8 SA establishes but no internet

**Root cause:** MASQUERADE missing on VPS. Verify on VPS:

```bash
ssh root@<your-hostname> 'iptables -t nat -L POSTROUTING -n -v | grep <vpn-pool>'
```

If empty: MASQUERADE is not configured. Re-run the firewall setup from the main build manual.

### H.9 SA establishes, has IP, but `tracert 8.8.8.8` first hop is private

**Root cause:** MASQUERADE missing. Same fix as H.8. The customer's traffic is going through the VPN tunnel but not being NATted to the VPS public IP, so it exits via their local network.

---

## 7. Verification matrix (per network type)

Run these after every Type H install:

```powershell
# 1. SA established?
Get-VpnConnection -Name "<vpn-connection-name>" | Format-List Name, ConnectionStatus

# 2. Client has the right VIP?
ipconfig /all | Select-String "<vpn-pool-subnet>"

# 3. Traffic goes through VPN (not direct)?
tracert 8.8.8.8
# First hop should be <your-public-ip>

# 4. Source IP is the VPS (not the customer's local IP)?
irm https://ifconfig.me
# Or: curl.exe https://ifconfig.me
# Should return <your-public-ip>

# 5. UDP ports open?
Test-NetConnection -ComputerName <your-vpn-hostname> -Port 500 -Udp
Test-NetConnection -ComputerName <your-vpn-hostname> -Port 4500 -Udp
# Both should return TcpTestSucceeded: True
```

If all 5 pass: customer is connected and traffic is VPN-routed.

---

## 8. Pre-flight checklist (Type H)

Before sending the customer the PowerShell block, verify on the VPS:

- [ ] Customer identity created in FreeRADIUS (via portal or direct SQL insert)
- [ ] Customer's EAP password set in `radcheck` (verified by `radtest` from VPS)
- [ ] NT-Password hash also set in `radcheck` (for Windows EAP-MSCHAPv2 fallback)
- [ ] `Customer`'s `radcheck.Value` is NOT `DISABLED-...` (verify by looking for that prefix)
- [ ] strongSwan charon is running: `docker ps | grep strongswan`
- [ ] UDP 500/4500 open on VPS firewall: `iptables -L INPUT -n -v | grep -E '500|4500'`
- [ ] MASQUERADE in place: `iptables -t nat -L POSTROUTING -n -v | grep <vpn-pool>`
- [ ] LE cert valid: `certbot certificates | grep -A 2 <your-vpn-hostname>`
- [ ] `<your-portal-domain>` DNS resolves to VPS IP and is NOT Cloudflare-flagged on customer's network
- [ ] Customer's mobile hotspot (if FortiGate blocks home network) is available as backup

---

## 9. References

| Document | Purpose |
|---|---|
| [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) | Type N (Non-hostile) — primary install doc |
| [`docs/DAT-VPN-INT-WIN-GUIDE-001.md`](DAT-VPN-INT-WIN-GUIDE-001.md) | Operator's playbook for generating per-customer baked installers (works for both Type N and Type H) |
| [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) | Top-level architecture, server config, MariaDB/FreeRADIUS |
| Microsoft Learn — RasSetCredentials | `learn.microsoft.com/en-us/windows/win32/api/ras/nf-ras-rassetcredentialsa` | The canonical Win32 API for VPN credential binding |
| Microsoft Learn — Add-VpnConnection | `learn.microsoft.com/en-us/powershell/module/vpnclient/` | PowerShell cmdlet for VPN profile creation |
| strongSwan — Windows interop | `docs.strongswan.org/docs/latest/interop/windowsEapConf.html` | IKEv2 + EAP-MSCHAPv2 from Windows client |
| FortiGate — SSL inspection | `docs.fortinet.com/document/fortigate/7.4.0/administration-guide` | How FortiGate intercepts HTTPS (for understanding failure modes) |

---

**Hand-curated 2026-10-17 for generic use.** This doc is the Type H (hostile network) manual-entry path for the Windows VPN client. For the standard Type N (non-hostile) path, see [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md). For the operator's per-customer baked installer workflow, see [`docs/DAT-VPN-INT-WIN-GUIDE-001.md`](DAT-VPN-INT-WIN-GUIDE-001.md).
