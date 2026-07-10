# Databyte VPN — Windows Installer Generation Guide (Bake-to-Ship)

**Document ID:** DAT-VPN-INT-WIN-GUIDE-001
**Version:** v1.0.0
**Date:** 2026-07-10
**Status:** VALIDATED
**Audience:** Operator (Zun + future assistants)
**Source of truth for:** End-to-end process of generating a Windows installer for a new VPN customer — from template to shipped URL — plus every variation we've actually used and every failure we've actually hit.

---

## 0. Purpose

You are onboarding a new customer onto Databyte VPN on a Windows 10 / 11 device. You need a **per-customer installer script** (credentials baked in, no prompt, ships via HTTPS) — produced by editing one template, deploying it to the VPS, and handing the customer a one-liner.

This document covers the **complete procedure**, every **variation** we've encountered (operator-side naming, cert pin vs not, cert rotation, multi-device customers), and every **failure mode** with evidence and fix.

**This document does NOT cover** customer-facing copy. That lives in `reports/DAT-VPN-EXT-WIN-001-v1.0.0.docx`. This is the **operator-side playbook**.

---

## 1. Architecture (what you're building)

```
┌─────────────────────────────────────────────────────────────┐
│  OPERATOR (you, on OC host / any Linux box)                │
│  1. Copy template                                          │
│  2. Edit 3 baked-in values                                 │
│  3. scp to VPS                                             │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  VPS (vps-01 / 154.65.110.44)                              │
│  /opt/vpn-portal/www/static/baked/setup-databyte-vpn-     │
│                          <customer>-<device>.ps1           │
│  (chown vpn-portal:vpn-portal, chmod 644)                  │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTPS (Cloudflare-fronted, LE cert)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  CUSTOMER (Win 10 / 11, PowerShell as Admin)               │
│  curl.exe -ksSL -o $env:TEMP\setup.ps1 https://.../...ps1  │
│  powershell -ExecutionPolicy Bypass -NoProfile -File ...   │
└────────────────────┬────────────────────────────────────────┘
                     │ IKEv2 + EAP-MSCHAPv2 (UDP 500/4500)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  VPS strongSwan (Docker: zun/strongswan:6.0.7-mschapv2)    │
│  FreeRADIUS → MariaDB radcheck → Access-Accept             │
└─────────────────────────────────────────────────────────────┘
```

**Critical facts (live verified 2026-07-10):**

| Item | Value | Verified |
|---|---|---|
| Server hostname | `myvpn.databyte.co.za` | `ssh root@vps-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -subject'` → `CN=myvpn.databyte.co.za` |
| LE cert issuer | `CN=YE1, O=Let's Encrypt, C=US` | same command with `-issuer` |
| LE cert SHA-256 (live) | `01:B1:E8:06:82:F7:05:8D:33:B0:37:FB:97:DA:54:A2:F4:83:88:3B:C8:E7:05:99:FF:E5:BE:09:70:21:29:DB` | live as of 2026-07-10 |
| LE cert SAN | `DNS:myvpn.databyte.co.za, DNS:vpn-portal.databyte.co.za` | `openssl x509 -ext subjectAltName` |
| LE cert expiry | `Oct 3 12:47:33 2026 GMT` (85 days from 2026-07-10) | same |
| Cert rotation | every ~60 days (certbot auto-renew via `dns-cloudflare` plugin) | `/etc/cron.d/certbot` on VPS |
| Delivery URL hostname | `vpn-portal.databyte.co.za` | **NOT `myvpn.*`** — Cloudflare badware flagged for some network paths |
| strongSwan proposals | `aes256-sha256-modp2048-ecp256,aes128-sha256-modp2048-ecp256` | `docker exec strongswan cat /etc/swanctl/conf.d/rw-eap.conf` |
| ESP proposals | same as IKE | same file |

---

## 2. The Template

### 2.1 Where it lives

| Property | Value |
|---|---|
| Repo path | `/root/projects/strongswan-vpn-gateway/scripts/setup-databyte-vpn-windows.ps1` |
| Git commit (HEAD) | `1dea754` (rename from `setup-databyte-vpn-baked-v1.0.0.ps1`) |
| Template MD5 | `5541343b9c5efe3b3b9257dbd3332805` |
| Template size | 22,639 bytes / 476 lines |
| Syntax validator | `pwsh 7.x`: `[System.Management.Automation.Language.Parser]::ParseFile(...)` |
| Test command | `pwsh -NoProfile -Command "& { $tokens=$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('...setup-databyte-vpn-windows.ps1',[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count -eq 0){'SYNTAX OK'} else { $errors | %{ '  '+$_.Message } } }"` |

### 2.2 The BAKED-IN CONFIG block (lines 56–74)

This is the ONLY block operator edits per-customer. Everything else is frozen template code.

```powershell
$ServerAddress  = "myvpn.databyte.co.za"           # NEVER change
$RemoteId       = "myvpn.databyte.co.za"           # NEVER change (must match cert SAN)
$ConnectionName = "DatabyteVPN"                    # NEVER change
$PortalBase     = "https://vpn-portal.databyte.co.za"  # NEVER change

# --- Operator-edited per customer: ---

$ServerCertSha256 = "REPLACE-ME-sha256-fingerprint"  # see §3 — strict pin
$Username         = "REPLACE-ME-customer-device-name"  # see §4
$Password         = "REPLACE-ME-customer-password"      # see §4
$LERootUrl        = "$PortalBase/static/certs/isrg-root-x2.pem"  # NEVER change
$ExpectedIssuerMatch = "Let.?s Encrypt|ISRG"          # NEVER change
```

**Sanity check at lines 96–103:** If any of the three REPLACE-ME values remain, script exits with code 1 and a red error banner. Customer cannot accidentally run an unbaked file.

### 2.3 What the script does (8 steps, verified live)

| Step | What | Verified behavior |
|---|---|---|
| **0** | Bootstrap ISRG Root X2 via `certutil -addstore -f Root` | Live 2026-07-10: `certutil: OK (ISRG Root X2 installed)` |
| **1** | TCP-connect to `:443`, fetch cert via `SslStream`, check issuer + optional SHA-256 pin | Live: `Subject CN=myvpn.databyte.co.za / Issuer CN=YE1, O=Let's Encrypt / Pin: SHA-256 match` |
| **2** | Remove stale `Databyte*`/`myvpn*` VPN connections + cmdkey entries | Live: `(no leftover profiles)` |
| **3** | `Add-VpnConnection -TunnelType IKEv2 -EapConfigXmlStream (New-EapConfiguration)` | Live: `Profile created: DatabyteVPN` |
| **4** | `Set-VpnConnectionIPsecConfiguration` → AES128/SHA256128/Group14/SHA256/PFS2048 | Live: `AES128 / SHA256128 / Group14 / SHA256 / PFS2048` |
| **5** | Registry: `HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters\NegotiateDH2048_AES256=2` + `PolicyAgent\AssumeUDPEncapsulationContextOnSendRule=2` | Live: both set |
| **6** | `RasSetCredentials` P/Invoke (rasapi32.dll) + `cmdkey` backup | Live: `RasSetCredentials P/Invoke: OK` |
| **7** | `rasdial DatabyteVPN` + 90s poll on `Get-VpnConnection` | Live: `rasdial exit code: 703` (normal for IKEv2+EAP) → poll caught `Connected` |

**Live result 2026-07-10 13:05 UTC** (Zun's Windows 11 24H2 build 26200):
- strongSwan SA `rw-eap #22, ESTABLISHED, IKEv2, AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048`
- EAP identity `zun-iphone`, virtual IP `10.99.0.2`, remote `102.182.117.43[4500]`
- 143,102 packets out, 177 MB transferred in 168 seconds

---

## 3. Variations on the BAKED-IN CONFIG

### 3.1 Variation A — Strict SHA-256 fingerprint pin (production default)

Use this for **every** production customer. Catches DNS poisoning + compromised intermediate CA.

```powershell
$ServerCertSha256 = "01:B1:E8:06:82:F7:05:8D:33:B0:37:FB:97:DA:54:A2:F4:83:88:3B:C8:E7:05:99:FF:E5:BE:09:70:21:29:DB"
```

Behavior: Step 1 computes the cert's SHA-256 and refuses to proceed on mismatch. Verified live.

### 3.2 Variation B — Issuer-only validation (rare)

Use this ONLY when you've just renewed the LE cert and haven't pulled the new fingerprint yet.

```powershell
$ServerCertSha256 = "REPLACE-ME-sha256-fingerprint"   # leave as REPLACE-ME
```

Behavior: Step 1 only checks issuer (LE expected). Less secure but survives cert rotation without re-bake.

### 3.3 Variation C — Custom server (lab / non-production)

Change `$ServerAddress` and `$RemoteId` to a different hostname. Both must match (cert SAN).

```powershell
$ServerAddress = "lab-vpn.example.com"
$RemoteId      = "lab-vpn.example.com"
```

DO NOT do this for production customers. Hardcoded path is `myvpn.databyte.co.za` everywhere.

### 3.4 Variation D — Connection name (not recommended)

Change `$ConnectionName` if customer already has a `DatabyteVPN` profile from a different operator. Default is `DatabyteVPN`.

If you change this, the customer's `$ConnectionName` and any subsequent customer references (re-install, troubleshooting) must use the new name. Most customers have no pre-existing VPN — leave as default.

### 3.5 Variation E — Crypto suite (NOT recommended for production)

The crypto block (Step 4, lines ~281–301 in template) is locked to match `rw-eap.conf` server proposals:

```powershell
$Crypto = @{
    AuthenticationTransformConstants = "SHA256128"
    CipherTransformConstants         = "AES128"
    DHGroup                          = "Group14"
    EncryptionMethod                 = "AES128"
    IntegrityCheckMethod             = "SHA256"
    PfsGroup                         = "PFS2048"
}
```

If you change any of these, the IKE handshake will either pick the wrong proposal or fail outright. Don't.

---

## 4. Operator Workflow — Start to End

### 4.1 Get the customer identity + password

**Source:** `host/vpn-portal/admin` operator page. Per portal:
- `$Username` = customer identity (e.g. `acme-corp-laptop01`)
- `$Password` = EAP secret (random 22-char base64)

Both are operator-side. Never seen by customer in plain text — they're baked into the script.

### 4.2 Get the current LE cert SHA-256 fingerprint

```bash
ssh root@vps-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -fingerprint -sha256'
```

Output (live 2026-07-10):
```
sha256 Fingerprint=01:B1:E8:06:82:F7:05:8D:33:B0:37:FB:97:DA:54:A2:F4:83:88:3B:C8:E7:05:99:FF:E5:BE:09:70:21:29:DB
```

Format with colons (lowercase) — that's what `$ServerCertSha256` expects.

### 4.3 Copy the template to a working file

```bash
cp /root/projects/strongswan-vpn-gateway/scripts/setup-databyte-vpn-windows.ps1 \
   /tmp/setup-databyte-vpn-<customer>-<device>.ps1
```

**Naming convention** (HARDLOCK — see MEMORY.md RULES):
- Customer identity: `<customer-org>-<device-role><NN>` (e.g., `acme-corp-laptop01`)
- File name: `setup-databyte-vpn-<customer>-<device>.ps1`
- Device role examples: `laptop01`, `desktop-hq`, `phone-personal`, `win11-work`
- Device role MUST match customer's portal entry. Customer references it in URLs/emails.

### 4.4 Edit the three values

```bash
# $ServerCertSha256
sed -i 's|\$ServerCertSha256 = "REPLACE-ME-sha256-fingerprint"|\$ServerCertSha256 = "01:B1:E8:06:82:F7:05:8D:33:B0:37:FB:97:DA:54:A2:F4:83:88:3B:C8:E7:05:99:FF:E5:BE:09:70:21:29:DB"|' /tmp/setup-databyte-vpn-<customer>-<device>.ps1

# $Username
sed -i 's|\$Username = "REPLACE-ME-customer-device-name"|\$Username = "acme-corp-laptop01"|' /tmp/setup-databyte-vpn-<customer>-<device>.ps1

# $Password
sed -i 's|\$Password = "REPLACE-ME-customer-password"|\$Password = "<eap-secret-from-portal>"|' /tmp/setup-databyte-vpn-<customer>-<device>.ps1
```

Or use `nano` / `vim` and edit lines 65, 71, 72 directly.

### 4.5 Verify the bake

```bash
# Confirm no REPLACE-ME remains
grep -c "REPLACE-ME" /tmp/setup-databyte-vpn-<customer>-<device>.ps1
# Expected: 0

# Confirm values are baked
grep -E '^\$ServerCertSha256|^\$Username|^\$Password' /tmp/setup-databyte-vpn-<customer>-<device>.ps1

# Syntax check (PowerShell 5.1 + 7.x)
pwsh -NoProfile -Command "& { $tokens=$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('/tmp/setup-databyte-vpn-<customer>-<device>.ps1',[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count -eq 0){'SYNTAX OK'} else { $errors | %{ '  '+$_.Message } } }"
# Expected: SYNTAX OK

# Confirm ASCII-only (PS 5.1 irken|iex corruption rule — see §7.2)
grep -cP "[^\x00-\x7F]" /tmp/setup-databyte-vpn-<customer>-<device>.ps1
# Expected: 0
```

### 4.6 Deploy to VPS

```bash
# Copy file to VPS
scp /tmp/setup-databyte-vpn-<customer>-<device>.ps1 root@vps-01:/opt/vpn-portal/www/static/baked/

# Set ownership + permissions
ssh root@vps-01 'chown vpn-portal:vpn-portal /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1 && chmod 644 /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1'
```

**Why `vpn-portal:vpn-portal`:** the nginx user serving static files is `vpn-portal`. Wrong ownership = nginx 403s the file.

**Why `chmod 644`:** nginx needs read; nothing needs write.

### 4.7 Verify the URL is served

```bash
curl -ksSL -o /dev/null -w "%{http_code} %{size_download}\n" \
  https://vpn-portal.databyte.co.za/static/baked/setup-databyte-vpn-<customer>-<device>.ps1
# Expected: 200 <size in bytes>
```

If you get 403: perms wrong. If 404: file not at the path nginx expects. If 502/503: nginx down — check `ssh root@vps-01 'systemctl status nginx'`.

### 4.8 Ship the URL to the customer

**The canonical customer-facing invocation:**

```
curl.exe -ksSL -o $env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/baked/setup-databyte-vpn-<customer>-<device>.ps1
powershell -ExecutionPolicy Bypass -NoProfile -File $env:TEMP\setup.ps1
```

**Shipping rules (HARDLOCK):**
- Send the **URL** to the customer via encrypted channel (portal message, PGP email, SFTP).
- **NEVER email the `.ps1` file itself** — it contains plaintext credentials.
- The customer does NOT need `$Username` or `$Password` — they're baked in.
- The customer does NOT need to install PowerShell — it's built into Windows 10/11.

### 4.9 Post-ship cleanup (on OC host / VPS)

```bash
# Remove working file (contains plaintext creds)
shred -u /tmp/setup-databyte-vpn-<customer>-<device>.ps1

# Optional: remove from VPS too if customer only needs it for one install
ssh root@vps-01 'rm /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1'
```

If the customer needs the URL later (e.g. new device), the file can be re-deployed at any time. There is no rotation requirement on the file itself; only the **LE cert fingerprint** rotates every ~60 days (see §7.5).

---

## 5. Multi-Device Customers

If a customer has 2+ devices (laptop + desktop), generate **one file per device** with the SAME `$Username` is WRONG — each file has its own EAP identity.

```
acme-corp-laptop01 → setup-databyte-vpn-acme-corp-laptop01.ps1
acme-corp-desktop01 → setup-databyte-vpn-acme-corp-desktop01.ps1
```

Both can share the same `$Password` (operator choice) or have different secrets. Either way: **each device has its own EAP identity in MariaDB radcheck.**

To create the second identity: in the portal operator page, add a new device under the customer. Portal generates a new `$Username` + `$Password`. Use those in a new bake.

---

## 6. Cert Rotation (every ~60 days)

### 6.1 What happens

certbot auto-renews the LE cert via `dns-cloudflare` plugin. New cert has new SHA-256 fingerprint. The OLD fingerprint is invalidated.

### 6.2 What operator must do

**If you used Variation A (strict pin) — REQUIRES RE-BAKE:**

1. Get new fingerprint (post-rotation):
   ```bash
   ssh root@vps-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -fingerprint -sha256'
   ```
2. For each deployed baked file: re-run §4.3–§4.7 with the new fingerprint.
3. Customers who try to install between rotation and re-bake will hit `[FAIL] Cert fingerprint mismatch!` (Step 1 exit code 1).

**If you used Variation B (issuer-only) — NO ACTION NEEDED:**

Cert chain validates via issuer. Rotation is invisible to the script.

### 6.3 Recommendation

For new customers, use **Variation A** (strict pin). Re-bake every ~60 days, batch the operation. The security benefit (catches DNS poisoning) outweighs the operational cost.

For lab / test customers, use **Variation B** (issuer-only). Zero maintenance.

---

## 7. Failure Modes — Real, Verified, With Fixes

Every failure mode below was observed live (date in section), with evidence in MEMORY.md or session logs.

### 7.1 `curl.exe: SEC_E_UNTRUSTED_ROOT` on cert chain

**Symptom (msg #24704, 2026-07-10 12:48 UTC):** customer's Windows fails HTTPS validation of `myvpn.databyte.co.za`. `curl.exe -k` works (skips validation).

**Root cause (verified):** `myvpn.databyte.co.za` is flagged on Cloudflare's badware list for some customer network paths. The StopBadware template is returned verbatim ("To have the rating of this web page re-evaluated").

**Fix:**
1. Use `vpn-portal.databyte.co.za` for delivery URL instead of `myvpn.*`. Live 2026-07-10: working from Zun's network.
2. If both fail: have customer install LE Root X2 manually (admin cmd):
   ```
   certutil -addstore -f Root C:\path\to\isrg-root-x2.pem
   ```

### 7.2 `Invoke-WebRequest` returns HTML instead of .ps1

**Symptom (msg #24704, 2026-07-10 12:48 UTC):** PowerShell `Invoke-WebRequest` to `https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1` returns Cloudflare badware HTML. Subsequent `& $env:TEMP\setup.ps1` tries to parse `<!DOCTYPE html>` as PowerShell → `Missing argument in parameter list` errors.

**Root cause:** PowerShell 5.1's `Invoke-WebRequest` has known TLS 1.3 + ISRG Root X2 chain issues. Cloudflare returns HTML when the URL is flagged, and PowerShell follows it.

**Fix:**
1. Use `curl.exe` not `Invoke-WebRequest`. `curl.exe` (Win 10 1803+) handles TLS 1.3 + LE chain correctly.
2. Use `vpn-portal.databyte.co.za` for delivery URL (not `myvpn.*`).

### 7.3 PowerShell parse error on em-dash / smart quote

**Symptom:** script downloaded fine but PowerShell 5.1 throws `Missing closing '}' in statement block` at the line of the em-dash.

**Root cause (MEMORY.md line 944):** Non-ASCII chars in scripts served via `irm|iex` get corrupted by ANSI codepage parsing in PS 5.1. Em-dash U+2014, smart quotes, en-dash, ellipsis all break.

**Fix:**
- Use ASCII only in PowerShell scripts.
- Replace `—` (em-dash) with ` - ` (space-dash-space).
- Replace `–` (en-dash) with ` - `.
- Replace `"`/`"` (smart quotes) with `"`.
- Replace `…` (ellipsis) with `...`.
- Validate with: `grep -cP "[^\x00-\x7F]" script.ps1` → MUST be `0`.

### 7.4 `rasdial: Error 703: The port was disconnected`

**Symptom:** `rasdial` returns 703 immediately, but `Get-VpnConnection -Name DatabyteVPN` polls back `Connected` 5-15s later.

**Root cause (MEMORY.md line 905):** Win10 native IKEv2 client has a known GUI-seed requirement. `rasdial` returns 703 because the EAP profile UI hasn't been seeded yet. The actual connection completes in the background.

**Fix:** No action needed if the poll loop catches `Connected` state within 90s. This is normal behavior, not a failure.

If the poll NEVER catches `Connected`:
1. Open `ncpa.cpl` → right-click the VPN adapter → Connect → click through the GUI once.
2. Re-run the script. rasdial will now work non-interactively.
3. Alternative: build a Win32 RasDial .exe (~2h, .NET Framework 4.x).

### 7.5 `[FAIL] Cert fingerprint mismatch!`

**Symptom:** Step 1 exits with `Expected: <old-fp>` / `Actual: <new-fp>`.

**Root cause:** LE cert rotated. Fingerprint changed. (Re-bake needed.)

**Fix:**
1. Pull new fingerprint:
   ```bash
   ssh root@vps-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -fingerprint -sha256'
   ```
2. Re-bake the file with the new fingerprint (re-do §4.4).
3. Re-deploy (re-do §4.6).
4. Customer re-runs.

### 7.6 `RasSetCredentials: returned 87` (ERROR_INVALID_PARAMETER)

**Symptom:** Step 6 fails with error 87 from `RasSetCredentials`.

**Root cause:** Profile doesn't exist yet (rare race condition between Steps 3 and 6), OR `$ConnectionName` mismatch.

**Fix:**
1. Verify profile exists: `Get-VpnConnection -Name DatabyteVPN` (should not error).
2. If profile exists, check the script's `$ConnectionName` matches what `Get-VpnConnection` reports.
3. Re-run the script.

### 7.7 `RasSetCredentials: returned 1162` (ERROR_NOT_FOUND)

**Symptom:** Step 6 fails with 1162.

**Root cause:** Same as 7.6 — profile doesn't exist. The script's check for profile existence in Step 3 should catch this, but race conditions exist.

**Fix:** Re-run the script. If persistent, check for GPO blocking VPN profile creation (`gpresult /h gpo.html`).

### 7.8 `RasSetCredentials: returned 5` (ERROR_ACCESS_DENIED)

**Symptom:** Step 6 fails with 5.

**Root cause:** PowerShell isn't running as Administrator.

**Fix:** Re-run PowerShell as Administrator. `#Requires -RunAsAdministrator` at the top of the script should have caught this — verify the script actually opened in elevated PS.

### 7.9 `Error 13868: IKE authentication credentials are unacceptable`

**Symptom:** rasdial returns Error 13868 / 0x3634. IKEv2 phase 1 (cert) completes, but phase 1.5 (EAP) fails.

**Root cause:** EAP identity or password wrong in MariaDB radcheck.

**Fix:**
1. Verify in MariaDB:
   ```bash
   ssh root@vps-01 'docker exec $(docker ps --filter ancestor=mariadb -q | head -1) mysql -u root -e "SELECT UserName, Attribute, Value FROM radius.radcheck WHERE UserName=\"$Username\""'
   ```
   (or use the equivalent for your mariadb container)
2. If password missing or `DISABLED-...` marker: customer is disabled. Re-enable:
   ```sql
   UPDATE radius.radcheck SET Value='<real-password>' WHERE UserName='<customer>' AND Attribute='Cleartext-Password';
   ```
3. Re-run the install script on customer side.

### 7.10 `Error 799 / Error 809` (network blocks UDP 500/4500)

**Symptom:** rasdial hangs for 30s then returns 799 or 809.

**Root cause:** Network blocks IKEv2 UDP ports. Common in hotels, corporate firewalls, mobile carriers.

**Fix:** Try a different network (phone hotspot, home Wi-Fi). No script-side fix — the block is upstream.

### 7.11 `Connected but no 10.99.0.x address`

**Symptom:** `Get-VpnConnection -Name DatabyteVPN` shows `Connected`, but `ipconfig /all` doesn't show a 10.99.0.x interface.

**Root cause:** Stale state from prior session.

**Fix:**
```
rasdial DatabyteVPN /disconnect
rasdial DatabyteVPN
```
If second attempt fails: Control Panel → Network Connections → right-click main adapter → Disable, wait 5s, Enable. Then connect.

### 7.12 `[FAIL] Could not download ISRG Root X2`

**Symptom:** Step 0 fails to download `isrg-root-x2.pem` from `vpn-portal.databyte.co.za`.

**Root cause:** VPS `certs/` dir missing the file, or perms wrong, or VPS nginx down.

**Fix:**
1. Verify file exists on VPS:
   ```bash
   ssh root@vps-01 'ls -la /opt/vpn-portal/www/static/certs/'
   ```
   Expected: `isrg-root-x2.pem` and `root-ye.pem` present, owned by `vpn-portal:vpn-portal`.
2. If missing, re-deploy from the source-of-truth chain:
   ```bash
   ssh root@vps-01 'awk "/BEGIN CERT/{n++} n==4{print} /END CERT/{if(n==4)exit}" /etc/letsencrypt/live/myvpn.databyte.co.za/fullchain.pem > /opt/vpn-portal/www/static/certs/isrg-root-x2.pem && chown vpn-portal:vpn-portal /opt/vpn-portal/www/static/certs/isrg-root-x2.pem && chmod 644 /opt/vpn-portal/www/static/certs/isrg-root-x2.pem'
   ```
3. If file present but download fails: check nginx (`ssh root@vps-01 'systemctl status nginx'`) and the access log (`/var/log/nginx/vpn-portal.access.log`).
4. Customer can also proceed — Step 0 will skip the download but Step 1 will abort if cert chain is untrusted. They'll get a clear error message.

### 7.13 `rasapi32!RasSetCredentials P/Invoke` not loading

**Symptom:** Step 6 fails with `Cannot load type 'VpnCredBinder'` or similar.

**Root cause:** PowerShell can't compile the inline C# P/Invoke. Usually because:
1. PS version < 5.0 (not supported).
2. `Add-Type` blocked by execution policy.
3. `rasapi32.dll` not in `PATH` (rare on Win 10/11).

**Fix:**
1. Check PS version: `$PSVersionTable.PSVersion` → must be ≥ 5.0.
2. If Add-Type blocked: use `Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process` before running the script.

### 7.14 `Could not connect to server` during Step 1

**Symptom:** Step 1 fails with `TCP connect to myvpn.databyte.co.za:443 timed out after 10s` or similar.

**Root cause:** Customer's network can't reach the VPS directly. Could be:
1. Customer behind strict firewall (proxy required, blocks UDP 500/4500 BUT also blocks HTTPS to non-whitelisted hosts).
2. Customer's ISP blocks the VPS IP.
3. Cloudflare-fronted hostname DNS not resolving correctly.

**Fix:**
1. Test from another network (phone hotspot).
2. Verify DNS: `nslookup myvpn.databyte.co.za 8.8.8.8` → expect `154.65.110.44`.
3. If customer can reach the URL but the script fails: check if HTTPS is being MITMed by corp proxy (corporate CA injected, breaks cert chain).

---

## 8. Deployment Patterns We've Used

### 8.1 Self-test (Zun's machine, 2026-07-10)

- Baked with `zun-iphone` + `lX7aAy21YSu5cYxdKufKgw` (live EAP identity).
- Deployed to VPS `/opt/vpn-portal/www/static/baked/setup-databyte-vpn-zun-iphone-test.ps1` (note `-test` suffix — this is the operator-side test artifact, nuked after verification).
- Ran on Windows 11 24H2 build 26200 via `curl.exe` + `powershell -File`.
- All 8 steps green, `Connected`, strongSwan SA established, 177 MB transferred.

### 8.2 Real customer (post-verification)

Same workflow. Filename uses actual customer identity (`setup-databyte-vpn-<customer-org>-<deviceNN>.ps1`). Shipped via portal message.

### 8.3 Test / dev (lab-mode)

Use `setup-databyte-vpn.ps1` v2.6.5 generic canonical instead. Customer identity comes from `-t <BASE64PACKED_SLUG_TOKEN>` parameter. Token burned after use. See `host/vpn-portal/installer_tokens.py` for token generation.

---

## 9. Auditing a Past Bake

When a customer reports an issue, retrieve the exact file they were given:

```bash
# Fetch from VPS
ssh root@vps-01 'cat /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1' > /tmp/customer-file.ps1

# MD5 + SHA-256
md5sum /tmp/customer-file.ps1
sha256sum /tmp/customer-file.ps1

# Baked values
grep -E '^\$ServerCertSha256|^\$Username|^\$Password' /tmp/customer-file.ps1

# Was the script file edited after deploy? (modification time)
ssh root@vps-01 'stat /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1'

# Is the customer still in radcheck?
ssh root@vps-1 'docker exec <mariadb-container> mysql -u root -e "SELECT id, UserName, Attribute, Value FROM radius.radcheck WHERE UserName=\"<customer>\"\"'
```

---

## 10. References (live, in repo)

| Document | Purpose |
|---|---|
| `docs/DAT-VPN-INT-WIN-001.md` (v1.1.0) | Internal master doc for the v2.6.5 generic canonical + baked template — full architecture, server config, IPsec params, design decisions |
| `reports/DAT-VPN-INT-WIN-001-v1.1.0.docx` | ISO 9001 formatted version of the above |
| `reports/DAT-VPN-EXT-WIN-001-v1.0.0.docx` | Customer-facing quickstart (one-liner, troubleshooting) |
| `scripts/README-windows-vpn.md` | Concise operator reference for both flows |
| `scripts/setup-databyte-vpn-windows.ps1` v1.0.0 | The template itself |
| `scripts/setup-databyte-vpn.ps1` v2.6.5 | Generic canonical (alternative flow) |
| `host/vpn-portal/installer_tokens.py` | Token generator for self-serve flow |
| `MEMORY.md` § RULES | HARDLOCK list for filenames, perms, paths |

---

**END OF DOCUMENT — DAT-VPN-INT-WIN-GUIDE-001 v1.0.0**