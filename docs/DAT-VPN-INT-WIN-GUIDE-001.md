# Windows Installer Generation Guide (Bake-to-Ship)

Generic operator-side playbook for generating per-customer baked Windows VPN installers. Documents the complete process of taking a generic PowerShell template and producing individual customer installers with credentials pre-baked, plus every variation and failure mode we've encountered.

> **Generic note:** This doc was originally databyte-internal (v1.0.0, 2026-07-10). It has been hand-curated 2026-10-17 for generic use — all databyte branding, dates, internal ticket references, and deployment-specific details removed. Replace all `<placeholder>` values with your own.

**Companion doc:** [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) — the master build manual (server config, PowerShell script source, failure modes). This doc focuses specifically on the operator workflow for generating per-customer baked installers.

---

## 0. Purpose

You are onboarding a new customer onto your self-hosted IKEv2 VPN on a Windows 10/11 device. You need a **per-customer installer script** (credentials baked in, no prompt, ships via HTTPS) — produced by editing one template, deploying it to the VPS, and handing the customer a one-liner.

This document covers the **complete procedure**, every **variation** (operator-side naming, cert pin vs not, cert rotation, multi-device customers), and every **failure mode** with evidence and fix.

**This document does NOT cover** customer-facing copy or the generic self-serve installer (customer enters creds via prompt at install). The generic self-serve flow is in [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) Section 12 and the QUICKSTART.md doc.

---

## 1. Architecture (what you're building)

```
┌─────────────────────────────────────────────────────────────┐
│  OPERATOR (you, on any Linux box)                          │
│  1. Copy template                                          │
│  2. Edit 3 baked-in values                                 │
│  3. scp to VPS                                             │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  VPS (<your-hostname> / <your-public-ip>)                  │
│  <your-portal-web-root>/static/baked/<your-template-name>-│
│                          <customer>-<device>.ps1            │
│  (chown <nginx-user>:<nginx-group>, chmod 644)             │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTPS (CDN-fronted, LE cert)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  CUSTOMER (Win 10 / 11, PowerShell as Admin)               │
│  curl.exe -ksSL -o $env:TEMP\setup.ps1 https://.../...ps1  │
│  powershell -ExecutionPolicy Bypass -NoProfile -File ...   │
└────────────────────┬────────────────────────────────────────┘
                     │ IKEv2 + EAP-MSCHAPv2 (UDP 500/4500)
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  VPS strongSwan (Docker: <your-namespace>/strongswan:6.0.7) │
│  FreeRADIUS → MariaDB radcheck → Access-Accept             │
└─────────────────────────────────────────────────────────────┘
```

**Critical facts (verify on your deploy):**

| Item | Value | Verified |
|---|---|---|
| Server hostname | `<your-vpn-hostname>` | `ssh root@<your-hostname> 'openssl x509 -in /etc/letsencrypt/live/<your-vpn-hostname>/cert.pem -noout -subject'` → `CN=<your-vpn-hostname>` |
| LE cert issuer | `CN=YE1, O=Let's Encrypt, C=US` | same command with `-issuer` |
| LE cert SHA-256 | *Verify on your deploy* | `openssl x509 -noout -fingerprint -sha256` |
| LE cert SAN | `DNS:<your-vpn-hostname>, DNS:<your-portal-domain>` | `openssl x509 -ext subjectAltName` |
| LE cert expiry | *Verify on your deploy* (~90 days from issue) | same |
| Cert rotation | every ~60 days (certbot auto-renew via `dns-cloudflare` plugin) | `/etc/cron.d/certbot` on VPS |
| Delivery URL hostname | `<your-portal-domain>` | **NOT `<your-vpn-hostname>`** — Cloudflare badware may flag VPN hostnames on some network paths |
| strongSwan proposals | `aes256-sha256-modp2048-ecp256,aes128-sha256-modp2048-ecp256` | `docker exec strongswan cat /etc/swanctl/conf.d/10-eap-radius.conf` |
| ESP proposals | same as IKE | same file |

---

## 2. The Template

### 2.1 Where it lives

| Property | Value |
|---|---|
| Repo path | `<your-install-root>/host/vpn-portal/scripts/<your-template-name>` (e.g. `setup-vpn-windows.ps1`) |
| Git commit (HEAD) | *Verify on your deploy* |
| Template MD5 | *Verify on your deploy* — changes if template is edited |
| Template size | *Verify on your deploy* |
| Syntax validator | `pwsh 7.x`: `[System.Management.Automation.Language.Parser]::ParseFile(...)` |
| Test command | `pwsh -NoProfile -Command "& { $tokens=$errors=$null; [System.Management.Automation.language.Parser]::ParseFile('<your-template-name>',[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count -eq 0){'SYNTAX OK'} else { $errors | %{ '  '+$_.Message } } }"` |

### 2.2 The BAKED-IN CONFIG block (top of template)

This is the ONLY block operator edits per-customer. Everything else is frozen template code.

```powershell
$ServerAddress  = "<your-vpn-hostname>"           # NEVER change (must match cert CN/SAN)
$RemoteId       = "<your-vpn-hostname>"           # NEVER change (must match cert SAN)
$ConnectionName = "<vpn-connection-name>"        # e.g. "MyVPN" or "DatabyteVPN" — keep consistent
$PortalBase     = "https://<your-portal-domain>"  # NEVER change

# --- Operator-edited per customer: ---

$ServerCertSha256 = "REPLACE-ME-sha256-fingerprint"  # see §3 — strict pin
$Username         = "REPLACE-ME-customer-device-name"  # see §4
$Password         = "REPLACE-ME-customer-password"      # see §4
$LERootUrl        = "$PortalBase/static/certs/isrg-root-x2.pem"  # NEVER change
$ExpectedIssuerMatch = "Let.?s Encrypt|ISRG"          # NEVER change
```

**Sanity check:** If any of the three `REPLACE-ME` values remain, the script should exit with code 1 and a red error banner. Customer cannot accidentally run an unbaked file.

### 2.3 What the script does (8 steps, verified live)

| Step | What | Verified behavior |
|---|---|---|
| **0** | Bootstrap ISRG Root X2 via `certutil -addstore -f Root` | `certutil: OK (ISRG Root X2 installed)` |
| **1** | TCP-connect to `:443`, fetch cert via `SslStream`, check issuer + optional SHA-256 pin | `Subject CN=<your-vpn-hostname> / Issuer CN=YE1, O=Let's Encrypt / Pin: SHA-256 match` |
| **2** | Remove stale `<vpn-connection-name>*`/`<your-vpn-hostname>*` VPN connections + cmdkey entries | `(no leftover profiles)` |
| **3** | `Add-VpnConnection -TunnelType IKEv2 -EapConfigXmlStream (New-EapConfiguration)` | `Profile created: <vpn-connection-name>` |
| **4** | `Set-VpnConnectionIPsecConfiguration` → AES128/SHA256128/Group14/SHA256/PFS2048 | `AES128 / SHA256128 / Group14 / SHA256 / PFS2048` |
| **5** | Registry: `HKLM:\SYSTEM\CurrentControlSet\Services\RasMan\Parameters\NegotiateDH2048_AES256=2` + `PolicyAgent\AssumeUDPEncapsulationContextOnSendRule=2` | both set |
| **6** | `RasSetCredentials` P/Invoke (rasapi32.dll) + `cmdkey` backup | `RasSetCredentials P/Invoke: OK` |
| **7** | `rasdial <vpn-connection-name>` + 90s poll on `Get-VpnConnection` | `rasdial exit code: 703` (normal for IKEv2+EAP) → poll caught `Connected` |

---

## 3. Variations on the BAKED-IN CONFIG

### 3.1 Variation A — Strict SHA-256 fingerprint pin (production default)

Use this for **every** production customer. Catches DNS poisoning + compromised intermediate CA.

```powershell
$ServerCertSha256 = "<your-current-le-cert-fingerprint>"
```

Get the current fingerprint:
```bash
ssh root@<your-hostname> 'openssl x509 -in /etc/letsencrypt/live/<your-vpn-hostname>/cert.pem -noout -fingerprint -sha256'
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

DO NOT do this for production customers.

### 3.4 Variation D — Connection name (not recommended)

Change `$ConnectionName` if a customer already has a profile from a different operator. Default is your standard connection name.

If you change this, the customer's `$ConnectionName` and any subsequent customer references (re-install, troubleshooting) must use the new name.

### 3.5 Variation E — Crypto suite (NOT recommended for production)

The crypto block (Step 4) is locked to match the server's strongSwan proposals:

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

**Source:** your portal's operator page. Per portal:
- `$Username` = customer identity (e.g. `<customer-org>-laptop01`)
- `$Password` = EAP secret (random 22-char base64)

Both are operator-side. Never seen by customer in plain text — they're baked into the script.

### 4.2 Get the current LE cert SHA-256 fingerprint

```bash
ssh root@<your-hostname> 'openssl x509 -in /etc/letsencrypt/live/<your-vpn-hostname>/cert.pem -noout -fingerprint -sha256'
```

Output (verify format with colons, lowercase):
```
sha256 Fingerprint=<your-current-fingerprint>
```

Format with colons (lowercase) — that's what `$ServerCertSha256` expects.

### 4.3 Copy the template to a working file

```bash
cp <your-install-root>/host/vpn-portal/scripts/<your-template-name> \
   /tmp/<your-template-name>-<customer>-<device>.ps1
```

**Naming convention:**
- Customer identity: `<customer-org>-<device-role><NN>` (e.g., `acme-corp-laptop01`)
- File name: `<your-template-name>-<customer>-<device>.ps1`
- Device role examples: `laptop01`, `desktop-hq`, `phone-personal`, `win11-work`
- Device role MUST match customer's portal entry. Customer references it in URLs/emails.

### 4.4 Edit the three values

```bash
# $ServerCertSha256
sed -i 's|\$ServerCertSha256 = "REPLACE-ME-sha256-fingerprint"|\$ServerCertSha256 = "<your-current-fingerprint>"|' /tmp/<your-template-name>-<customer>-<device>.ps1

# $Username
sed -i 's|\$Username = "REPLACE-ME-customer-device-name"|\$Username = "<customer>"|' /tmp/<your-template-name>-<customer>-<device>.ps1

# $Password
sed -i 's|\$Password = "REPLACE-ME-customer-password"|\$Password = "<eap-secret-from-portal>"|' /tmp/<your-template-name>-<customer>-<device>.ps1
```

Or use `nano` / `vim` and edit the lines directly.

### 4.5 Verify the bake

```bash
# Confirm no REPLACE-ME remains
grep -c "REPLACE-ME" /tmp/<your-template-name>-<customer>-<device>.ps1
# Expected: 0

# Confirm values are baked
grep -E '^\$ServerCertSha256|^\$Username|^\$Password' /tmp/<your-template-name>-<customer>-<device>.ps1

# Syntax check (PowerShell 5.1 + 7.x)
pwsh -NoProfile -Command "& { $tokens=$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('/tmp/<your-template-name>-<customer>-<device>.ps1',[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count -eq 0){'SYNTAX OK'} else { $errors | %{ '  '+$_.Message } } }"
# Expected: SYNTAX OK

# Confirm ASCII-only (PS 5.1 irm|iex corruption rule — see §7.3)
grep -cP "[^\x00-\x7F]" /tmp/<your-template-name>-<customer>-<device>.ps1
# Expected: 0
```

### 4.6 Deploy to VPS

```bash
# Copy file to VPS
scp /tmp/<your-template-name>-<customer>-<device>.ps1 root@<your-hostname>:<your-portal-web-root>/static/baked/

# Set ownership + permissions
ssh root@<your-hostname> 'chown <nginx-user>:<nginx-group> <your-portal-web-root>/static/baked/<your-template-name>-<customer>-<device>.ps1 && chmod 644 <your-portal-web-root>/static/baked/<your-template-name>-<customer>-<device>.ps1'
```

**Why `<nginx-user>:<nginx-group>`:** the web server user serving static files. Wrong ownership = web server 403s the file. Verify on your deploy (e.g. `ps aux | grep nginx` to see the worker user, then `grep ^user /etc/nginx/nginx.conf`).

**Why `chmod 644`:** web server needs read; nothing needs write.

### 4.7 Verify the URL is served

```bash
curl -ksSL -o /dev/null -w "%{http_code} %{size_download}\n" \
  https://<your-portal-domain>/static/baked/<your-template-name>-<customer>-<device>.ps1
# Expected: 200 <size in bytes>
```

If you get 403: perms wrong. If 404: file not at the path web server expects. If 502/503: web server down — check `ssh root@<your-hostname> 'systemctl status nginx'`.

### 4.8 Ship the URL to the customer

**The canonical customer-facing invocation:**

```
curl.exe -ksSL -o $env:TEMP\setup.ps1 https://<your-portal-domain>/static/baked/<your-template-name>-<customer>-<device>.ps1
powershell -ExecutionPolicy Bypass -NoProfile -File $env:TEMP\setup.ps1
```

**Shipping rules:**
- Send the **URL** to the customer via encrypted channel (portal message, PGP email, SFTP).
- **NEVER email the `.ps1` file itself** — it contains plaintext credentials.
- The customer does NOT need `$Username` or `$Password` — they're baked in.
- The customer does NOT need to install PowerShell — it's built into Windows 10/11.

### 4.9 Post-ship cleanup (on operator host / VPS)

```bash
# Remove working file (contains plaintext creds)
shred -u /tmp/<your-template-name>-<customer>-<device>.ps1

# Optional: remove from VPS too if customer only needs it for one install
ssh root@<your-hostname> 'rm <your-portal-web-root>/static/baked/<your-template-name>-<customer>-<device>.ps1'
```

If the customer needs the URL later (e.g. new device), the file can be re-deployed at any time. There is no rotation requirement on the file itself; only the **LE cert fingerprint** rotates every ~60 days (see §7.5).

---

## 5. Multi-Device Customers

If a customer has 2+ devices (laptop + desktop), generate **one file per device** with the SAME `$Username` is WRONG — each file has its own EAP identity.

```
acme-corp-laptop01 → <your-template-name>-acme-corp-laptop01.ps1
acme-corp-desktop01 → <your-template-name>-acme-corp-desktop01.ps1
```

Both can share the same `$Password` (operator choice) or have different secrets. Either way: **each device has its own EAP identity in FreeRADIUS/MariaDB radcheck.**

To create the second identity: in the portal operator page, add a new device under the customer. Portal generates a new `$Username` + `$Password`. Use those in a new bake.

---

## 6. Cert Rotation (every ~60 days)

### 6.1 What happens

certbot auto-renews the LE cert via your DNS provider's ACME plugin. New cert has new SHA-256 fingerprint. The OLD fingerprint is invalidated.

### 6.2 What operator must do

**If you used Variation A (strict pin) — REQUIRES RE-BAKE:**

1. Get new fingerprint (post-rotation):
   ```bash
   ssh root@<your-hostname> 'openssl x509 -in /etc/letsencrypt/live/<your-vpn-hostname>/cert.pem -noout -fingerprint -sha256'
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

Every failure mode below was observed live, with verified root cause and fix. Verify on your own deploy before applying.

### 7.1 `curl.exe: SEC_E_UNTRUSTED_ROOT` on cert chain

**Symptom:** customer's Windows tried `curl.exe -k -o $env:TEMP\setup.ps1 https://<your-vpn-hostname>/static/<your-template-name>`. Downloaded HTML (35,167 bytes in one real case) — not the expected script size. The HTML contained `input[type=date]`, `table > tbody`, SonicWall-style re-evaluation text, and "To have the rating of this web page re-evaluated".

**Root cause (verified):** `<your-vpn-hostname>` is flagged on Cloudflare's badware / SonicWall Content Filter Server (CFS) for some network paths (specific ISP/corporate firewall, not universal). The operator's host returns `200 <correct-size>` (clean) but the customer's network got the StopBadware HTML. The `<your-portal-domain>` path was clean (different Cloudflare reputation profile).

> **Note:** Whether `<your-vpn-hostname>` returns badware HTML depends on the customer's network. Test before assuming. Always use `<your-portal-domain>` for delivery.

**Fix:**
1. Use `<your-portal-domain>` for delivery URL instead of `<your-vpn-hostname>`. Verified working from customer's network.
2. If both fail: have customer install LE Root X2 manually (admin cmd):
   ```
   certutil -addstore -f Root C:\path\to\isrg-root-x2.pem
   ```

### 7.2 `Invoke-WebRequest` returns HTML instead of .ps1

**Symptom:** PowerShell `Invoke-WebRequest` to `https://<your-vpn-hostname>/static/<your-template-name>` returns Cloudflare badware HTML. Subsequent `& $env:TEMP\setup.ps1` tries to parse `<!DOCTYPE html>` as PowerShell → `Missing argument in parameter list` errors.

**Root cause:** PowerShell 5.1's `Invoke-WebRequest` has known TLS 1.3 + ISRG Root X2 chain issues. Cloudflare returns HTML when the URL is flagged, and PowerShell follows it.

**Fix:**
1. Use `curl.exe` not `Invoke-WebRequest`. `curl.exe` (Win 10 1803+) handles TLS 1.3 + LE chain correctly.
2. Use `<your-portal-domain>` for delivery URL (not `<your-vpn-hostname>`).

### 7.3 PowerShell parse error on em-dash / smart quote

**Symptom:** script downloaded fine but PowerShell 5.1 throws `Missing closing '}' in statement block` at the line of the em-dash.

**Root cause:** Non-ASCII chars in scripts served via `irm|iex` get corrupted by ANSI codepage parsing in PS 5.1. Em-dash U+2014, smart quotes, en-dash, ellipsis all break.

**Fix:**
- Use ASCII only in PowerShell scripts.
- Replace `—` (em-dash) with ` - ` (space-dash-space).
- Replace `–` (en-dash) with ` - `.
- Replace `"`/`"` (smart quotes) with `"`.
- Replace `…` (ellipsis) with `...`.
- Validate with: `grep -cP "[^\x00-\x7F]" script.ps1` → MUST be `0`.

### 7.4 `rasdial: Error 703: The port was disconnected`

**Symptom:** `rasdial` returns 703 immediately, but `Get-VpnConnection -Name <vpn-connection-name>` polls back `Connected` 5-15s later.

**Root cause:** Win10 native IKEv2 client has a known GUI-seed requirement. `rasdial` returns 703 because the EAP profile UI hasn't been seeded yet. The actual connection completes in the background.

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
   ssh root@<your-hostname> 'openssl x509 -in /etc/letsencrypt/live/<your-vpn-hostname>/cert.pem -noout -fingerprint -sha256'
   ```
2. Re-bake the file with the new fingerprint (re-do §4.4).
3. Re-deploy (re-do §4.6).
4. Customer re-runs.

### 7.6 `RasSetCredentials: returned 87` (ERROR_INVALID_PARAMETER)

**Symptom:** Step 6 fails with error 87 from `RasSetCredentials`.

**Root cause:** Profile doesn't exist yet (rare race condition between Steps 3 and 6), OR `$ConnectionName` mismatch.

**Fix:**
1. Verify profile exists: `Get-VpnConnection -Name <vpn-connection-name>` (should not error).
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

**Root cause:** EAP identity or password wrong in FreeRADIUS/MariaDB radcheck.

**Fix:**
1. Verify in MariaDB (MariaDB runs on VPS host directly, not in Docker):
   ```bash
   ssh root@<your-hostname> 'mysql -u root -e "SELECT UserName, Attribute, Value FROM radius.radcheck WHERE UserName=\"$Username\""'
   ```
   Expected output for your test customer:
   ```
   UserName  Attribute          Value
   <customer> Cleartext-Password <eap-secret>
   <customer> NT-Password        <nt-hash>
   ```
2. If password missing or `DISABLED-...` marker: customer is disabled. Re-enable:
   ```sql
   UPDATE radius.radcheck SET Value='<real-password>' WHERE UserName='<customer>' AND Attribute='Cleartext-Password';
   ```
3. Re-run the install script on customer side.

### 7.10 `Error 799 / Error 809` (network blocks UDP 500/4500)

**Symptom:** rasdial hangs for 30s then returns 799 or 809.

**Root cause:** Network blocks IKEv2 UDP ports. Common in hotels, corporate firewalls, mobile carriers.

**Fix:** Try a different network (phone hotspot, home Wi-Fi). No script-side fix — the block is upstream.

### 7.11 `Connected but no <vpn-pool> address`

**Symptom:** `Get-VpnConnection -Name <vpn-connection-name>` shows `Connected`, but `ipconfig /all` doesn't show a `<vpn-pool>` interface.

**Root cause:** Stale state from prior session.

**Fix:**
```
rasdial <vpn-connection-name> /disconnect
rasdial <vpn-connection-name>
```
If second attempt fails: Control Panel → Network Connections → right-click main adapter → Disable, wait 5s, Enable. Then connect.

### 7.12 `[FAIL] Could not download ISRG Root X2`

**Symptom:** Step 0 fails to download `isrg-root-x2.pem` from `<your-portal-domain>`.

**Root cause:** VPS `certs/` dir missing the file, or perms wrong, or VPS web server down.

**Fix:**
1. Verify file exists on VPS:
   ```bash
   ssh root@<your-hostname> 'ls -la <your-portal-web-root>/static/certs/'
   ```
   Expected: `isrg-root-x2.pem` and `root-ye.pem` present, owned by `<nginx-user>:<nginx-group>`.
2. If missing, re-deploy from the source-of-truth chain:
   ```bash
   ssh root@<your-hostname> 'awk "/BEGIN CERT/{n++} n==4{print} /END CERT/{if(n==4)exit}" /etc/letsencrypt/live/<your-vpn-hostname>/fullchain.pem > <your-portal-web-root>/static/certs/isrg-root-x2.pem && chown <nginx-user>:<nginx-group> <your-portal-web-root>/static/certs/isrg-root-x2.pem && chmod 644 <your-portal-web-root>/static/certs/isrg-root-x2.pem'
   ```
3. If file present but download fails: check web server (`ssh root@<your-hostname> 'systemctl status nginx'`) and the access log.
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

**Symptom:** Step 1 fails with `TCP connect to <your-vpn-hostname>:443 timed out after 10s` or similar.

**Root cause:** Customer's network can't reach the VPS directly. Could be:
1. Customer behind strict firewall (proxy required, blocks UDP 500/4500 AND also blocks HTTPS to non-whitelisted hosts).
2. Customer's ISP blocks the VPS IP.
3. `<your-vpn-hostname>` DNS resolving to a blocked/intercepted IP on the customer's network path.

**Fix:**
1. Test from another network (phone hotspot).
2. Verify DNS: `nslookup <your-vpn-hostname> 8.8.8.8` → expect `<your-public-ip>`. Compare with `nslookup <your-vpn-hostname>` (customer's default resolver) — different IPs mean DNS interception.
3. If customer can reach the URL but the script fails: check if HTTPS is being MITMed by corp proxy (corporate CA injected, breaks cert chain). Check: `curl.exe -kv https://<your-vpn-hostname>/ 2>&1 | grep -i "subject:|issuer:"` — if issuer isn't `Let's Encrypt`, there's a proxy in the path.
4. Try `<your-portal-domain>` instead — different Cloudflare IP range, may bypass the block.

---

## 8. Deployment Patterns

### 8.1 Self-test (operator's machine)

- Bake with your test customer's identity + EAP secret.
- Deployed to VPS `<your-portal-web-root>/static/baked/<your-template-name>-<test>-<device>.ps1` (note `-test` suffix — operator-side test artifact, can be removed after verification).
- Ran on your test machine via `curl.exe` + `powershell -File`.
- All 8 steps green, `Connected`, strongSwan SA established.

### 8.2 Real customer (production)

Same workflow. Filename uses actual customer identity (`<your-template-name>-<customer-org>-<deviceNN>.ps1`). Shipped via portal message.

### 8.3 Test / dev (lab-mode)

Use the generic self-serve installer (customer enters creds via prompt at install) instead. See [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) Section 12 for details on the two flows.

---

## 9. Auditing a Past Bake

When a customer reports an issue, retrieve the exact file they were given:

```bash
# Fetch from VPS
ssh root@<your-hostname> 'cat <your-portal-web-root>/static/baked/<your-template-name>-<customer>-<device>.ps1' > /tmp/customer-file.ps1

# MD5 + SHA-256
md5sum /tmp/customer-file.ps1
sha256sum /tmp/customer-file.ps1

# Baked values
grep -E '^\$ServerCertSha256|^\$Username|^\$Password' /tmp/customer-file.ps1

# Was the script file edited after deploy? (modification time)
ssh root@<your-hostname> 'stat <your-portal-web-root>/static/baked/<your-template-name>-<customer>-<device>.ps1'

# Is the customer still in radcheck?
ssh root@<your-hostname> 'mysql -u root -e "SELECT id, UserName, Attribute, Value FROM radius.radcheck WHERE UserName=\"<customer>\""'
```

---

## 10. References

| Document | Purpose |
|---|---|
| [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md) | Master build manual — full architecture, server config, PowerShell script source, failure modes, lessons |
| [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) | Top-level architecture, "Build from scratch" section |
| [`docs/QUICKSTART.md`](QUICKSTART.md) | Gateway-only self-host (no FreeRADIUS/MariaDB) |
| [`docs/DAT-VPN-INT-WIN-HOSTILE-001.md`](DAT-VPN-INT-WIN-HOSTILE-001.md) | Manual PowerShell entry for FortiGate/SonicWall hostile networks |
| Your template script (`<your-template-name>`) | The PowerShell template with editable BAKED-IN CONFIG block |
| `host/vpn-portal/` | Portal web app (customer/operator UI) |
| LE docs | `letsencrypt.org/docs/` | ACME DNS-01 challenge, cert lifecycle |
| strongSwan docs | `docs.strongswan.org/` | IKEv2, eap-radius plugin, strongSwan.conf |

---

**Hand-curated 2026-10-17 for generic use.** This doc is the operator-side playbook for generating per-customer baked Windows installers. For the full VPN stack build (server config, FreeRADIUS, MariaDB, portal, bot), see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) and [`docs/DAT-VPN-INT-WIN-001.md`](DAT-VPN-INT-WIN-001.md). For the generic self-serve installer (customer enters creds at install), use the script template without operator baking.
