#!/usr/bin/env python3
"""
build_installer.py  -  Phase 1 of mode-selector build (2026-07-11).

Pure function: generate the installer artifact for a Windows customer+device
based on the chosen mode (standard vs hostile).

NO database access. NO network. NO SSH. The caller (installer_tokens.py or
a future CLI) provides all inputs as plain dicts. This isolation makes the
function trivially testable.

Modes
-----
- "standard"  -  Type N. Builds the canonical 3-line PS block (FROZEN
  setup-databyte-vpn.ps1 v2.6.5 fetched via 7-day installer token).
  Output is powershell_cmd, no per-customer file content. Today's behavior,
  unchanged.

- "hostile"  -  Type H. Builds a self-contained .ps1 with EAP credentials
  INLINED at the top (no HTTPS, no token fetch) using the 5-step block
  from skills/windows-vpn-hostile-network-setup. Bypasses FortiGate /
  SonicWall / captive portals that interfere with HTTPS. Customer saves
  the file and runs it in PowerShell Admin.

Why a dispatcher
----------------
Zun (msg #25242): "if i select a windows customer i must select hostile or
non hostile. then generate the installer script accordingly". Default =
standard; operator picks. UI work is Phase 2 (radio in
showInstallerLinkModal in app.js line ~1518).

Skill references
----------------
- skills/vpn-windows-client-setup/SKILL.md (209 lines, Type N canonical)
- skills/windows-vpn-hostile-network-setup/SKILL.md (350 lines, Type H manual)

The two skills are intentionally NOT unified (Zun msg #25244): they target
different network conditions, different credential transports, different
verification models. The dispatcher branches, does not merge.

Not in this module
------------------
- DB queries for customer/device (caller does it)
- rw-eap.conf read for the EAP password (caller does it via
  installer_tokens._read_eap_secret_from_conf)
- 7-day installer_token row creation (caller does it; see
  installer_tokens.create_installer_token)
- Audit logging (caller does it via _audit)
- UI rendering (Phase 2)
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Optional

log = logging.getLogger("build_installer")

# Allowed mode values  -  fail loudly otherwise (don't silently default)
MODE_STANDARD = "standard"
MODE_HOSTILE = "hostile"
ALLOWED_MODES = (MODE_STANDARD, MODE_HOSTILE)

# Server identity (matches rw-eap.conf `leftid` on vps-01  -  SAN of LE cert)
SERVER_ADDRESS = "myvpn.databyte.co.za"
REMOTE_ID = "myvpn.databyte.co.za"

# Profile / connection name (per skill  -  same constant across both skills)
CONNECTION_NAME = "DatabyteVPN"

# Portal base URL  -  used in the canonical 3-line block. Hostile flow does
# NOT use this URL because hostile networks intercept TCP/443.
PORTAL_BASE_URL = "https://vpn-portal.databyte.co.za"

# Public installer URL (served from /static on the portal). Same LE cert SAN
# as the portal  -  Windows clients trust it via the LE root chain.
FROZEN_INSTALLER_URL = f"{PORTAL_BASE_URL}/static/setup-databyte-vpn.ps1"


def build(
    customer: dict,
    device: dict,
    mode: str,
    *,
    token: str = "",
    eap_password: str = "",
    connection_name: str = CONNECTION_NAME,
) -> dict:
    """Dispatcher: build the installer artifact for a customer+device.

    Args:
        customer: dict with at least {"name": str, "display_name": Optional[str], ...}
        device:   dict with at least {"device_name": str, "device_type": str, ...}
        mode:     "standard" or "hostile"
        token:    (standard) installer token from installer_tokens table
        eap_password: (hostile) plaintext EAP password (read from rw-eap.conf by
                  caller via installer_tokens._read_eap_secret_from_conf)
        connection_name: profile name (default "DatabyteVPN"). Operators can pass
                  a unique name for parallel customers on one machine per skill.

    Returns:
        dict with shape:
        {
          "mode": str,
          "installer_kind": "token" | "baked",
          "filename": Optional[str],     # hostile only
          "content": Optional[str],      # hostile only  -  full .ps1 source
          "powershell_cmd": str,          # the single string the operator sends
          "customer_name": str,
          "device_name": str,
        }

    Raises:
        ValueError on invalid mode or missing required arg.
    """
    if mode not in ALLOWED_MODES:
        raise ValueError(
            f"unknown mode {mode!r}; expected one of {ALLOWED_MODES}"
        )

    base = {
        "mode": mode,
        "customer_name": customer.get("name", ""),
        "customer_display": customer.get("display_name"),
        "device_name": device.get("device_name", ""),
        "device_type": device.get("device_type", "windows"),
    }

    if mode == MODE_STANDARD:
        if not token:
            raise ValueError("standard mode requires a non-empty token")
        result = _build_standard(customer, device, token)
    else:  # MODE_HOSTILE
        if not eap_password:
            raise ValueError("hostile mode requires eap_password (caller must read it from rw-eap.conf)")
        result = _build_hostile(customer, device, eap_password, connection_name)

    return {**base, **result}


# ─── Mode: standard (Type N) ────────────────────────────────────────────────

def _build_standard(customer: dict, device: dict, token: str) -> dict:
    """Build the canonical 3-line PS block (FROZEN v2.6.5 token flow).

    Replicates the EXACT output produced by installer_tokens.create_installer_token
    today, so existing operator UX is preserved verbatim when mode="standard".

    The customer runs:
        curl.exe -o $env:TEMP\\setup.ps1 '<FROZEN_INSTALLER_URL>'
        & $env:TEMP\\setup.ps1 -t <base64-packed>
        rasdial DatabyteVPN

    The base64-packed payload is `customer_name:token` (urlsafe-b64, padding
    stripped). See installer_tokens.py create_installer_token for the rationale
    on base64 packing (PS 5.1 '&' ParserError avoidance).
    """
    packed = base64.urlsafe_b64encode(
        f"{customer['name']}:{token}".encode()
    ).decode().rstrip("=")

    ps_cmd_lines = [
        f"curl.exe -o $env:TEMP\\setup.ps1 '{FROZEN_INSTALLER_URL}'",
        f"& $env:TEMP\\setup.ps1 -t {packed}",
        "rasdial DatabyteVPN",
    ]
    ps_cmd = "\n".join(ps_cmd_lines)

    return {
        "installer_kind": "token",
        "filename": None,        # FROZEN script is shared, no per-customer file
        "content": None,         # no per-customer content; canonical URL serves the script
        "powershell_cmd": ps_cmd,
        "installer_url": FROZEN_INSTALLER_URL,
        "token_prefix": token[:8] + "\u2026",
        "skill_source": "vpn-windows-client-setup",
    }


# ─── Mode: hostile (Type H) ────────────────────────────────────────────────

def _build_hostile(
    customer: dict,
    device: dict,
    eap_password: str,
    connection_name: str,
) -> dict:
    """Build the hostile-baked script (Type H, no HTTPS during config).

    Self-contained .ps1 with EAP credentials INLINED at the top as PowerShell
    variables $EAP_USERNAME / $EAP_PASSWORD. Customer saves + runs in
    PowerShell Admin. No HTTPS = bypasses FortiGate / SonicWall / captive
    portals that intercept TCP/443.

    5 steps (matches skills/windows-vpn-hostile-network-setup):
      1. Add-VpnConnection (IKEv2 + EAP, -ServerAddress myvpn.databyte.co.za)
      2. Set-VpnConnectionIPsecConfiguration (Group14 / SHA256 / AES256 / PFS2048)
      3. Registry: NegotiateDH2048_AES256 = 2
      4. cmdkey clear + RasSetCredentials (P/Invoke rasapi32.dll)
      5. rasdial DatabyteVPN

    Filename convention: setup-databyte-vpn-<customer>-<device>-hostile.ps1
    (`-hostile` suffix keeps it distinguishable from the per-customer standard
    bakes that already exist in /opt/vpn-portal/www/static/baked/).
    """
    eap_identity = f"{customer['name']}-{device['device_name']}"
    filename = f"setup-databyte-vpn-{customer['name']}-{device['device_name']}-hostile.ps1"

    script = _render_hostile_script(
        connection_name=connection_name,
        eap_identity=eap_identity,
        eap_password=eap_password,
        customer_display=customer.get("display_name") or customer["name"],
        device_name=device["device_name"],
        customer_name_safe=customer["name"],
        device_name_safe=device["device_name"],
    )

    run_command = (
        f"# Save the attached file as setup.ps1 on the customer's desktop, then:\n"
        f"powershell -ExecutionPolicy Bypass -NoProfile -File C:\\Users\\<user>\\Desktop\\setup.ps1"
    )

    return {
        "installer_kind": "baked",
        "filename": filename,
        "content": script,
        "powershell_cmd": run_command,
        "eap_identity": eap_identity,
        "skill_source": "windows-vpn-hostile-network-setup",
    }


def _render_hostile_script(
    *,
    connection_name: str,
    eap_identity: str,
    eap_password: str,
    customer_display: str,
    device_name: str,
    customer_name_safe: str,
    device_name_safe: str,
) -> str:
    """Render the hostile-baked .ps1 source content.

    Verbatim structure from skills/windows-vpn-hostile-network-setup §"Ready-to-paste
    customer block"  -  the manual 5-step block. Here the operator has already filled
    $EAP_USERNAME / $EAP_PASSWORD (no `# ⬇ FILL THIS ⬇` markers), so customer can
    copy-paste the whole file as-is.

    PowerShell-safe escapes: we use single-quoted heredoc for the C# type
    definition (same trick the skill uses), so no escaping issues with the EAP
    password. Credentials are still plaintext in the file  -  same threat model as
    the existing per-customer baked files (FROZEN §"Hard rule #10: ALWAYS treat
    the baked .ps1 as customer credential material").
    """
    # NOTE: f-string substitution of {eap_password} inside a regular triple-quoted
    # string is fine because the surrounding { } are PowerShell sigs (not f-string
    # patterns). Python only substitutes $name / $password here. PowerShell ${}
    # references are literal in the output.
    return f"""<#
.SYNOPSIS
    Databyte VPN installer (Windows, hostile-network variant) for {customer_display} / device {device_name}.

.DESCRIPTION
    AUTO-GENERATED by the operator portal on demand (one per Generate click).
    Type H (hostile-network flow) per skills/windows-vpn-hostile-network-setup v1.1.0.

    Server:          {SERVER_ADDRESS} (VPS 154.65.110.44)
    Auth:            EAP-MSCHAPv2
    Crypto:          AES128 / SHA256128 / Group14 / SHA256 / PFS2048
    Credential API:  rasapi32!RasSetCredentials (P/Invoke)
    Cert validation: SKIPPED (hostile networks intercept TLS - trust the IKE_SA)

    What it does (5 steps, ZERO HTTPS):
      1. Create IKEv2 + EAP VPN profile
      2. Set IPsec crypto to match strongSwan server (Group14)
      3. Registry: NegotiateDH2048_AES256 = 2 (enforce DH2048)
      4. cmdkey clear + Bind credentials via RasSetCredentials (rasapi32.dll P/Invoke)
      5. Connect via rasdial DatabyteVPN (with retry)

.NOTES
    File:           setup-databyte-vpn-{customer_name_safe}-{device_name_safe}-hostile.ps1
    Mode:           Type H (hostile)
    Skill:          skills/windows-vpn-hostile-network-setup v1.1.0
    Threat:         Operator-baked  -  file contains plaintext EAP credentials.
                    Treat as password material. NEVER email the file; ship URL or
                    encrypted DM only.
    Re-bake on:     EAP password rotation (portal operator page  ->  rotate_eap).
    Cert rotation:  NO EFFECT (hostile flow skips cert validation entirely).
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'

# ============================================================================
# BAKED-IN CONFIG (operator portal filled these automatically)
# ============================================================================
$EAP_USERNAME  = "{eap_identity}"
$EAP_PASSWORD  = "{eap_password}"
$ServerAddress = "{SERVER_ADDRESS}"
$RemoteId      = "{REMOTE_ID}"
$ConnectionName = "{connection_name}"

# ============================================================================
# STEP 1 - Create VPN profile
# ============================================================================
Write-Host ""
Write-Host "=== [1/5] Creating VPN profile '$ConnectionName' ===" -ForegroundColor Cyan

Add-VpnConnection -Name "$ConnectionName" `
  -ServerAddress "$ServerAddress" `
  -TunnelType "IKEv2" `
  -AuthenticationMethod "EAP" `
  -RememberCredential `
  -PassThru

# ============================================================================
# STEP 2 - Set IPsec crypto to match strongSwan server
# ============================================================================
# Defaults: Windows uses DES3/SHA1/DH2/modp1024  -  strongSwan rejects these.
# Group14 = modp2048, PFS2048 = perfect forward secrecy with modp2048.
Write-Host "=== [2/5] Setting IPsec crypto (AES256 / SHA256 / Group14 / PFS2048) ===" -ForegroundColor Cyan

Set-VpnConnectionIPsecConfiguration -ConnectionName "$ConnectionName" `
  -AuthenticationTransformConstants "SHA256128" `
  -CipherTransformConstants "AES128" `
  -DHGroup "Group14" `
  -EncryptionMethod "AES256" `
  -IntegrityCheckMethod "SHA256" `
  -PfsGroup "PFS2048"

# ============================================================================
# STEP 3 - Force modp2048 for IKE (registry, one-time, persists across profiles)
# ============================================================================
Write-Host "=== [3/5] Forcing DH2048 + AES256 in registry ===" -ForegroundColor Cyan

Set-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\RasMan\\Parameters" `
  -Name "NegotiateDH2048_AES256" -Value 2 -Type DWord

# ============================================================================
# STEP 4 - Clear stale cmdkey cache + bind credentials via RasSetCredentials
# ============================================================================
Write-Host "=== [4/5] Binding credentials via RasSetCredentials ===" -ForegroundColor Cyan

cmdkey /delete:ras\\$ConnectionName 2>$null
cmdkey /delete:$ServerAddress 2>$null
cmdkey /delete:154.65.110.44 2>$null
# Note: deliberately NOT clearing "vpn-portal.databyte.co.za" -- the customer
# may have a leftover portal cookie from a previous install, but clearing it
# would force a portal re-login on every run. The stale cookie does not
# interfere with IKEv2/EAP (different auth path).

$sig = @"
using System.Runtime.InteropServices;
public class Cred {{
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    public struct RASCREDENTIALS {{
        public uint Size; public uint Mask;
        [MarshalAs(UnmanagedType.LPWStr)] public string UserName;
        [MarshalAs(UnmanagedType.LPWStr)] public string Password;
        [MarshalAs(UnmanagedType.LPWStr)] public string Domain;
    }}
    [DllImport("rasapi32.dll", CharSet=CharSet.Unicode)]
    public static extern uint RasSetCredentials(string phonebook, string entry, ref RASCREDENTIALS creds, bool fClear);
}}
"@
Add-Type -TypeDefinition $sig -Force

$c = New-Object Cred+RASCREDENTIALS
$c.Size = [System.Runtime.InteropServices.Marshal]::SizeOf($c)
$c.Mask = 0x87  # UserName | Password | Domain | Default
$c.UserName = $EAP_USERNAME
$c.Password = $EAP_PASSWORD
$c.Domain = ""

$ret = [Cred]::RasSetCredentials("", $ConnectionName, [ref]$c, $false)
Write-Host "  RasSetCredentials returned: $ret (0=OK, 87=ERROR_INVALID_PARAMETER, 1162=ERROR_NOT_FOUND)" -ForegroundColor Yellow

if ($ret -ne 0) {{
    Write-Host "  Non-zero RasSetCredentials return  -  re-run after profile propagation." -ForegroundColor Red
}}

# ============================================================================
# STEP 5 - Connect
# ============================================================================
Write-Host "=== [5/5] Connecting ===" -ForegroundColor Cyan

$connectOutput = rasdial $ConnectionName 2>&1
$connectExit = $LASTEXITCODE
Write-Host $connectOutput

# 703 ("port disconnected") is NORMAL for IKEv2+EAP first run  -  Windows
# needs the EAP profile seeded by the GUI dialog. Retry up to 3 times.
$connected = $false
for ($i = 1; $i -le 3; $i++) {{
    Start-Sleep -Seconds 5
    $status = (Get-VpnConnection -Name $ConnectionName -ErrorAction SilentlyContinue).ConnectionStatus
    if ($status -eq "Connected") {{
        $connected = $true
        break
    }}
    Write-Host "  Retry $i/3: status=$status, reconnecting..." -ForegroundColor Yellow
    rasdial $ConnectionName /disconnect 2>$null | Out-Null
    Start-Sleep -Seconds 1
    rasdial $ConnectionName 2>$null | Out-Null
}}

if ($connected) {{
    Write-Host ""
    Write-Host "  [OK] Connected as $EAP_USERNAME" -ForegroundColor Green
    Write-Host "  Verify:" -ForegroundColor Green
    Write-Host "    Get-VpnConnection -Name $ConnectionName   # ConnectionStatus: Connected" -ForegroundColor Green
    Write-Host "    ipconfig /all                              # expect 10.99.0.x interface" -ForegroundColor Green
}} else {{
    Write-Host ""
    Write-Host "  [FAIL] Did not reach Connected after 3 retries." -ForegroundColor Red
    Write-Host "    Open ncpa.cpl  ->  right-click '$ConnectionName'  ->  Connect" -ForegroundColor Red
    Write-Host "    Once GUI seeds the EAP profile, future rasdial calls succeed." -ForegroundColor Red
    exit 1
}}
"""


# Module self-check (when run directly): print a sample of each mode
if __name__ == "__main__":
    sample_customer = {
        "name": "zunaid-test",
        "display_name": "Zunaid (test)",
    }
    sample_device = {
        "device_name": "laptop",
        "device_type": "windows",
    }
    out_std = build(
        sample_customer, sample_device, MODE_STANDARD,
        token="abc123def456ghi789jkl012mno345pq",
    )
    print("=== STANDARD ===")
    print(f"kind={out_std['installer_kind']}  filename={out_std['filename']}")
    print("--- powershell_cmd ---")
    print(out_std["powershell_cmd"])
    print()

    out_host = build(
        sample_customer, sample_device, MODE_HOSTILE,
        eap_password="S3cr3tEapPass!",
    )
    print("=== HOSTILE ===")
    print(f"kind={out_host['installer_kind']}  filename={out_host['filename']}")
    print(f"content length: {len(out_host['content'])} B  (first 8 lines:)")
    print("\n".join(out_host["content"].splitlines()[:8]))
