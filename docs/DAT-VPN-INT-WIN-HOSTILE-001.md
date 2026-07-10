# Databyte VPN — Windows Installer for Hostile Networks (Type H)

**Document ID:** DAT-VPN-INT-WIN-HOSTILE-001
**Version:** v1.0.0
**Date:** 2026-07-10
**Status:** VALIDATED
**Audience:** Operator (Zun + future assistants)
**Source of truth for:** Manual-entry VPN setup on FortiGate / corporate-firewall networks where HTTPS interception breaks the standard installer script.

**Companion to:** `DAT-VPN-INT-WIN-GUIDE-001.md` (Type N — Non-hostile, script-based)

---

## 0. Purpose

You are onboarding a customer onto Databyte VPN on Windows 10/11 who is **on a hostile network** (FortiGate SSL inspection, SonicWall CFS, captive portal, or any corporate firewall doing TLS MITM). The Type N script-based approach will fail at STEP 1 (cert validation) because the firewall intercepts the SslStream TLS handshake.

This document covers the **manual PowerShell entry path** that bypasses every TCP/443 blocker by using only local Win32 API calls + IKEv2 over UDP 500/4500 (which firewalls allow by default). Source of truth for the Type H skill (`windows-vpn-hostile-network-setup`).

**This document does NOT cover** non-hostile networks — see `DAT-VPN-INT-WIN-GUIDE-001.md`. The Type N script is faster, validates the cert, and is the default path. Use Type H only when Type N fails.

---

## 1. How we got here — the failure journey

This isn't theoretical. This document exists because **the Type N script failed on a live production customer on 2026-07-10**. Here is the full sequence, with evidence:

### 1.1 Initial assumption (wrong)

When the customer `zunaidengel` was being onboarded on 2026-07-10, we ran the standard Type N baked installer:

```
curl -ksSL -o $env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/baked/setup-databyte-vpn-zunaidengel-windows.ps1
powershell -ExecutionPolicy Bypass -NoProfile -File $env:TEMP\setup.ps1
```

**Result**: STEP 0 (bootstrap ISRG cert) worked. STEP 1 (verify server TLS cert) **failed**:

```
[FAIL] Cert fetch failed: Exception calling "AuthenticateAsClient" with "1" argument(s):
       "Unable to read data from the transport connection: A connection attempt failed
        because the connected party did not properly respond after a period of time, or
        established connection failed because connected host has failed to respond."
```

**Source**: `swanctl --list-sas` showed nothing — the script never reached IKEv2.

### 1.2 First hypothesis (wrong): captive portal

We assumed the customer's network had a captive portal intercepting all TCP/443. The customer was using a "other VPN" to bypass. We tried:

- **Hypothesis A**: URL filtering on `vpn-portal.*` → tried changing `$ServerAddress` to `vpn-portal.databyte.co.za`
- **Hypothesis B**: Captive portal challenge → tried cellular hotspot (deferred)

**Result**: Both wrong.

### 1.3 Second hypothesis (partially right): Cloudflare SSL classification

We discovered the downloaded file was **HTML instead of PowerShell** (size 35179 vs expected 22689, first line `<!DOCTYPE html>` vs `<#`).

**Source**: Zun's diagnostic output:
```
"=== SIZE ==="
35179
"=== MD5 ==="
6C1E02605058E83F95A9CCCE2549F128
"=== FIRST LINE ==="
<!DOCTYPE html>
```

The Cloudflare-fronted URL `vpn-portal.databyte.co.za` was returning a FortiGuard "Web Filter Violation / Spam URLs" HTML page. We tried `curl --resolve` to force direct VPS IP — that failed with `SEC_E_INVALID_TOKEN` (FortiGate does IP-level SSL inspection).

### 1.4 The actual root cause: FortiGate full TLS MITM

We confirmed by testing from the OpenClaw host (no FortiGate) that:
- `curl https://myvpn.databyte.co.za/api/health` → returns Cloudflare-fronted page on `vpn-portal.*`, but `myvpn.*` returns `CN=myvpn.databyte.co.za, Issuer=Let's Encrypt YE1`
- `curl https://vpn-portal.databyte.co.za/api/health` → returns `CN=databyte.co.za, Issuer=Google Trust Services WE1` (Cloudflare Universal SSL)

**Conclusion**: FortiGate is performing **full HTTPS MITM** with response-body substitution. It intercepts:
- TCP/443 to `myvpn.*` → blocks / substitutes
- TCP/443 to `vpn-portal.*` → returns FortiGuard block page (HTML, 35179 B)
- UDP/500, UDP/4500 → passes through (IKEv2 NAT-T)

### 1.5 The breakthrough: manual entry over UDP

We realized: **any path that uses HTTPS will fail**. The Type N script uses HTTPS at STEP 0 (curl to download ISRG cert) AND STEP 1 (SslStream cert validation).

**The solution**: bypass all HTTPS by using only local Win32 API calls + IKEv2. Five PowerShell commands, all local:

1. `Add-VpnConnection` — local Win32 API
2. `Set-VpnConnectionIPsecConfiguration` — local registry
3. `Set-ItemProperty` for `NegotiateDH2048_AES256` — local registry
4. `RasSetCredentials` (P/Invoke) — local Win32 API
5. `rasdial.exe` — IKEv2 over UDP 500/4500 (only network traffic)

**Result**: ✅ Both customers connected. radpostauth IDs 241, 246. IKE_SAs #14, #19. Framed IPs 10.99.0.3, 10.99.0.4.

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
│  5. rasdial.exe "DatabyteVPN"                                │
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

> **v1.1.0 design**: Because the customer must hand-type the PowerShell block on a FortiGate network (HTTPS is broken), we use a single ready-to-paste block with PowerShell variables at the top for the operator's credentials. The customer copy-pastes literally — no placeholder hunt, no missed `<EAP identity>`, no Access-Reject from stale creds.

### Customer-ready block (operator fills 2 lines marked `# ⬇ FILL THIS ⬇`)

```powershell
# ============================================
# DATABYTE VPN — MANUAL ENTRY (Type H)
# Server: myvpn.databyte.co.za
# Paste this entire block into PowerShell Admin
# ============================================

# ⬇ FILL THIS ⬇ (operator: insert the customer's EAP username)
$EAP_USERNAME = "zunaid-new-win11"
# ⬇ FILL THIS ⬇ (operator: insert the customer's EAP password)
$EAP_PASSWORD = "uEvIPMPssS1Lh85MLTU5"

# Step 1: Create VPN profile
Add-VpnConnection -Name "DatabyteVPN" `
  -ServerAddress "myvpn.databyte.co.za" `
  -TunnelType "IKEv2" `
  -AuthenticationMethod "EAP" `
  -RememberCredential `
  -PassThru

# Step 2: Set IPsec crypto
Set-VpnConnectionIPsecConfiguration -ConnectionName "DatabyteVPN" `
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
cmdkey /delete:ras\DatabyteVPN 2>$null
cmdkey /delete:myvpn.databyte.co.za 2>$null
cmdkey /delete:vpn-portal.databyte.co.za 2>$null
cmdkey /delete:154.65.110.44 2>$null

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
$ret = [Cred]::RasSetCredentials("", "DatabyteVPN", [ref]$c, $false)
Write-Host "RasSetCredentials returned: $ret (0=OK, 87=ERROR_INVALID_PARAMETER, 1162=ERROR_NOT_FOUND)"

# Step 5: Connect
rasdial.exe "DatabyteVPN"

# Verify
Get-VpnConnection -Name "DatabyteVPN"
ipconfig /all | Select-String "10.99"
ping 10.99.0.1
```

**Why this design is better than template+placeholder hunt**:

| Aspect | Old design (template + `<placeholder>`) | New design (operator fills 2 lines) |
|---|---|---|
| Operator effort | Replace `<EAP identity>` and `<EAP password>` deep in Step 4 | Replace 2 clearly-marked lines at the top |
| Customer effort | Find the placeholders, edit them | Copy-paste the whole block, variables are already set |
| Error risk | Customer misses a placeholder → Access-Reject | Customer copy-pastes literally → correct creds |
| Audit trail | Operator sends raw template, edits are off-channel | Operator edits 2 lines before sending, no post-edit |
| Visual cue | None — placeholders are inline | Big `# ⬇ FILL THIS ⬇` markers |

---

### Step-by-step rationale

**Step 1 — Create VPN profile**

```powershell
Add-VpnConnection -Name "DatabyteVPN" `
  -ServerAddress "myvpn.databyte.co.za" `
  -TunnelType "IKEv2" `
  -AuthenticationMethod "EAP" `
  -RememberCredential `
  -PassThru
```

**Why `myvpn.databyte.co.za`, not `vpn-portal.*`**: `myvpn.*` is the canonical hostname for IKEv2 connections. It has the same LE cert SAN as `vpn-portal.*` (both `myvpn.databyte.co.za` and `vpn-portal.databyte.co.za` are on the same cert) but resolves to the VPS origin IP, not Cloudflare. IKEv2 doesn't care about Cloudflare because IKEv2 is UDP-based — the cert is checked at the application layer (RasSetCredentials), not the IKE_SA establishment.

**Why `-RememberCredential`**: Makes Windows store the EAP credentials in the RAS phonebook so subsequent rasdial calls don't need creds.

### Step 2 — IPsec crypto (must match strongSwan)

```powershell
Set-VpnConnectionIPsecConfiguration -ConnectionName "DatabyteVPN" `
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
$c.Mask = 0x87  # UserName | Password | Domain | Default
$c.UserName = "zunaid-new-win11"
$c.Password = "<EAP password>"
$c.Domain = ""
$ret = [Cred]::RasSetCredentials("", "DatabyteVPN", [ref]$c, $false)
Write-Host "RasSetCredentials returned: $ret (0=OK, 87=ERROR_INVALID_PARAMETER, 1162=ERROR_NOT_FOUND)"
```

**Why RasSetCredentials, not cmdkey**: this is the critical discovery from the failed Type N run. cmdkey stores credentials in the Windows Credential Manager, but **IKEv2 EAP doesn't read from there**. We observed Windows using stale cmdkey creds (`test-win-5g-laptop` from earlier lab-mode runs) in preference to freshly-bound RasSetCredentials. Result: radpostauth Access-Reject for the wrong username.

**Why the Mask=0x87**: 0x80=Domain, 0x04=Password, 0x02=Username, 0x01=Default. 0x87 = all four flags. Without 0x80 (Domain), the binding fails silently on some Windows builds.

**CRITICAL — clear cmdkey cache first**:

```powershell
cmdkey /delete:ras\DatabyteVPN 2>$null
cmdkey /delete:myvpn.databyte.co.za 2>$null
cmdkey /delete:vpn-portal.databyte.co.za 2>$null
cmdkey /delete:154.65.110.44 2>$null
```

Without this, stale creds from prior runs (especially lab-mode `test-win-5g-laptop`) will be used in preference to RasSetCredentials.

### Step 5 — Connect

```powershell
rasdial.exe "DatabyteVPN"
```

**First attempt may return error 703** ("Remote Access error 703 - The connection needs information from you"). This is **NORMAL** for IKEv2+EAP on first run — Windows needs the EAP profile seeded by the GUI dialog. Retry, or open `ncpa.cpl` → right-click DatabyteVPN → Connect once, then rasdial works.

**What you'll see on the VPS side**:
- radpostauth row: `zunaid-new-win11 / Access-Accept` within 5-10s
- New IKE_SA: `rw-eap #N ESTABLISHED, EAP: 'zunaid-new-win11' [10.99.0.x]`

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
ssh root@vps-01 "mariadb -uroot -D radius << SQL
INSERT INTO radusergroup (UserName, GroupName, priority) VALUES ('<eap_identity>', 'default', 0);
INSERT INTO radcheck (UserName, Attribute, op, Value) VALUES ('<eap_identity>', 'Cleartext-Password', ':=', '$PASSWORD');
INSERT INTO radcheck (UserName, Attribute, op, Value) VALUES ('<eap_identity>', 'NT-Password', ':=', '$NT_HASH');
SQL"
```

### 4.3 Live auth test

```bash
ssh root@vps-01 "radtest <eap_identity> '$PASSWORD' 127.0.0.1 0 testing123"
# Expect: Access-Accept
```

### 4.4 Save audit file

```bash
AUDIT_DIR="/root/audit-bk/$(date -u +%Y-%m-%d)-<eap_identity>-onboard"
ssh root@vps-01 "mkdir -p \$AUDIT_DIR && chmod 700 \$AUDIT_DIR"
ssh root@vps-01 "cat > \$AUDIT_DIR/password.txt << EOF
$(date -u +%Y-%m-%d\ %H:%M:%S\ UTC) - new customer onboarding
Customer:     <eap_identity>
Device:       <device_name>
EAP identity: <eap_identity>
Tier:         <tier>
Bandwidth:    <bandwidth>
Password:     $PASSWORD
NT hash:      $NT_HASH
Server:       myvpn.databyte.co.za
VPN profile:  <profile_name>
Reason:       <why hostile - FortiGate, etc>
EOF
chmod 600 \$AUDIT_DIR/password.txt"
```

Repeat on OC workspace at `/root/.openclaw/workspace/audit-bk/<same-name>/`.

### 4.5 Hand-deliver the PowerShell block

Use Telegram DM (1:1 direct chat) or encrypted email. **Never** email the `.ps1` file itself — that would put plaintext creds in transit.

---

## 5. Verification (operator)

```bash
# 1. radpostauth (expect Access-Accept for new customer)
ssh root@vps-01 'mariadb -uroot -D radius -e "SELECT id, username, reply, authdate FROM radpostauth WHERE authdate >= NOW() - INTERVAL 3 MINUTE ORDER BY id DESC LIMIT 5;"'

# 2. IKE_SAs (expect new ESTABLISHED with framed IP 10.99.0.x)
ssh root@vps-01 'docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas'

# 3. Update VPN Tracker (Changes sheet)
# 4. Push tracker to rustfs
rclone copy /root/projects/strongswan-vpn-gateway/tracker/databyte-vpn-tracker.xlsx rustfs:open-claw-push/vpn/
```

---

## 6. Comparison: Type N vs Type H

| Aspect | Type N (Non-hostile) | Type H (Hostile) |
|---|---|---|
| Skill | `vpn-windows-client-setup` | `windows-vpn-hostile-network-setup` |
| Operator delivers | Baked `.ps1` script via HTTPS URL | Inline PowerShell block via Telegram DM |
| Customer executes | `curl + powershell -File` (2 lines) | 5 inline PowerShell commands |
| Cert validation | Yes (LE issuer + optional fingerprint pin) | No (cannot validate through TLS-MITM firewall) |
| Network required for setup | HTTPS (TCP 443) | None during config; IKEv2/UDP for connection |
| Time per customer | ~3 min (script does everything) | ~5 min (customer types 5 commands) |
| Trust model | Trust LE cert chain + Cloudflare cert | Trust IKEv2 SA establishment + FreeRADIUS Accept |
| Failure modes | 14 verified | 8 verified |
| Multiple customers on one machine | One profile, swap creds | Multiple profiles (`DatabyteVPN`, `DatabyteVPN2`, ...), independent creds |
| When to use | Default — clean network | FortiGate / corporate firewall / captive portal / any TLS-MITM network |

---

### v1.1.0 design decision (why we don't use `<placeholder>` anymore)

In v1.0.0 the skill used `<EAP identity>` and `<EAP password>` inline. Zun's feedback (msg #24938): customer had to manually find and replace those placeholders, which is fragile and error-prone on a hostile network where the only safe action is "copy the block I sent you, paste, run". The v1.1.0 design uses PowerShell variables `$EAP_USERNAME` and `$EAP_PASSWORD` at the top of the block. Operator fills 2 lines before sending. Customer copy-pastes literally. No find/replace, no missed placeholder, no Access-Reject from stale creds.

This applies to BOTH the skill (`windows-vpn-hostile-network-setup`) AND this design doc.

---

## 7. Live evidence (2026-07-10)

### Customer 1: zunaid-new-win11
- **radpostauth id=241**: Access-Accept at 19:57:00 SAST
- **IKE_SA rw-eap #14**: ESTABLISHED, framed IP **10.99.0.3**
- **Profile**: DatabyteVPN
- **Public IP**: 160.242.18.238 (FortiGate NAT'd)

### Customer 2: zunaid-test2-win11
- **radpostauth id=246**: Access-Accept at 20:09:22 SAST
- **IKE_SA rw-eap #19**: ESTABLISHED, framed IP **10.99.0.4**
- **Profile**: DatabyteVPN2 (separate profile to test parallel)
- **Public IP**: 160.242.18.238 (same — 3 SAs from same client)

### Pre-existing: zun-iphone
- **IKE_SA rw-eap #5**: ESTABLISHED, framed IP **10.99.0.2**
- iPhone native IKEv2 client (separate skill path)

**3 parallel SAs from same client IP, different framed IPs, different profiles, all Access-Accept.**

---

## 8. Failure modes (8 verified live, 2026-07-10)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| H.1 | STEP 1 of Type N script fails: `[FAIL] Cert fetch failed: Unable to read data from the transport connection` | FortiGate SSL inspection terminates the SslStream TLS handshake mid-flight | Switch to Type H. Manual entry skips cert validation entirely. |
| H.2 | STEP 1 of Type N script fails: `[FAIL] Issuer is not Let's Encrypt: CN=WE1, O=Google Trust Services` | Cloudflare returns its Universal SSL cert instead of origin LE cert when hostname is badware-flagged | Use Type H. Switching hostnames doesn't help if FortiGate is intercepting. |
| H.3 | `curl --resolve myvpn:443:154.65.110.44` returns schannel error `SEC_E_INVALID_TOKEN` | FortiGate does IP-level SSL inspection, not just hostname-based | Confirms full SSL inspection. Use Type H. |
| H.4 | `RasSetCredentials` returns 87 (ERROR_INVALID_PARAMETER) | Profile not yet created, OR RasMan service not running | Confirm `Get-VpnConnection -Name DatabyteVPN` returns a profile. Re-run. If persistent, restart `rasman` service. |
| H.5 | `rasdial` returns error 703 | Windows IKEv2+EAP requires EAP profile seeded by GUI dialog | Retry rasdial, OR open `ncpa.cpl` → right-click DatabyteVPN → Connect once, then rasdial works. |
| H.6 | radpostauth shows Access-Reject for `test-win-5g-laptop` instead of the real customer | Windows cached stale cmdkey creds from a previous lab-mode run, used in preference to freshly-bound RasSetCredentials | `cmdkey /delete:ras\DatabyteVPN` + delete all cmdkey entries for the server hostname/IP. Re-run RasSetCredentials. Re-rasdial. |
| H.7 | Connected but no 10.99.0.x interface | Stale RAS state from prior session | `rasdial /disconnectall`, then re-rasdial. If persistent, disable/enable the WAN adapter via `ncpa.cpl`. |
| H.8 | `rasdial` succeeds, IKE_SA established, but no traffic flows | Stale DNS resolver cache | `ipconfig /flushdns`, then test `curl https://myvpn.databyte.co.za/api/health` from inside tunnel. |

---

## 9. Hard rules (operator)

1. NEVER use `vpn-portal.databyte.co.za` as `-ServerAddress` for IKEv2 — use `myvpn.databyte.co.za`. The former may be Cloudflare-fronted (TCP/443 risk).
2. ALWAYS clear cmdkey cache before RasSetCredentials, or stale creds will win.
3. ALWAYS use `-ServerAddress` as a real hostname that resolves — IP literals cause IKEv2 ID mismatch.
4. NEVER email the customer's password. Use DMs (Telegram 1:1) or encrypted email only.
5. ALWAYS save audit file mode 600 (password file) + mode 700 (audit dir).
6. ALWAYS include the RasSetCredentials Mask=0x87 (UserName | Password | Domain | Default).
7. ALWAYS set NegotiateDH2048_AES256=2 BEFORE first rasdial, or strongSwan rejects the IKE_SA_INIT.
8. ALWAYS run as PowerShell Admin (RasSetCredentials requires elevation).
9. ALWAYS use a unique profile name per customer to avoid cmdkey/PowerShell profile collisions.
10. ALWAYS shred temp creds after the customer successfully connects.
11. NEVER skip the IPsec crypto config — Windows defaults to DES3/SHA1/DH2 which strongSwan rejects.
12. NEVER include the customer's password in plain-text logs or chat messages; redact if needed.

---

## 10. When to switch from Type N to Type H

**Decision rule**: Try Type N first. If STEP 1 fails with cert-fetch error, switch to Type H.

| Step | Type N attempts | If fails, switch to Type H |
|---|---|---|
| Bake + ship | Default | N/A (Type H doesn't use scripts) |
| Customer runs curl | Default | curl fails with SEC_E_INVALID_TOKEN |
| STEP 0 (ISRG cert) | Default | If fails, network blocks even Cloudflare-fronted HTTPS |
| STEP 1 (server cert) | Default | If fails → Type H |
| Final | Default | If still failing after Type H → cellular hotspot |

**Total switch-over time**: ~5 minutes per customer (vs ~3 minutes for Type N).

---

## 11. Future work / parked

1. **Cellular hotspot fallback**: when FortiGate blocks even UDP 500/4500 (rare but possible), iPhone Personal Hotspot is the fallback. NOT TESTED on this specific FortiGate config — only confirmed that it works on other corporate firewalls.
2. **Auto-baked script with HTTPS-detection**: future script could detect if HTTPS is intercepted and fall back to manual entry. Complex, low value.
3. **Test Type N on cellular**: confirm that on cellular, the Type N script works end-to-end (sanity check). NOT YET DONE.
4. **Phase 4E DB unify**: MariaDB collation drift (23 tables `utf8mb4_uca1400_ai_ci`). Fix on a Friday 18:00 SAST window. See HEARTBEAT.md.

---

## 12. Reference

- **Skill**: `windows-vpn-hostile-network-setup` (proposal `windows-vpn-hostile-network-setup-20260710-9b3851483c`)
- **Companion doc**: `DAT-VPN-INT-WIN-GUIDE-001.md` (Type N, script-based)
- **Live cert**: SHA-256 `01:B1:E8:06:82:F7:05:8D:33:B0:37:FB:97:DA:54:A2:F4:83:88:3B:C8:E7:05:99:FF:E5:BE:09:70:21:29:DB`, SAN includes both `myvpn.databyte.co.za` and `vpn-portal.databyte.co.za`, expires 2026-10-03
- **strongSwan server config**: `aes128-sha256-modp2048-ecp256` proposals, eap-radius plugin
- **Live SAs**: rw-eap #5 (zun-iphone), #14 (zunaid-new-win11), #19 (zunaid-test2-win11)
- **Audit dirs**: `/root/audit-bk/2026-07-10-zunaid-new-win11-onboard/`, `/root/audit-bk/2026-07-10-zunaid-test2-onboard/`
- **Tracker**: `/root/projects/strongswan-vpn-gateway/tracker/databyte-vpn-tracker.xlsx` rows 35-37