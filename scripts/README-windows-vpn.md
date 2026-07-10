# Windows VPN Client Setup

Self-contained PowerShell scripts that set up an IKEv2/EAP-MSCHAPv2 VPN
connection to the Databyte VPN server on Windows 10/11.

## What's in `scripts/`

| File | Version | Purpose | MD5 | Status |
|---|---|---|---|---|
| `setup-databyte-vpn.ps1` | v2.6.5 | **Generic canonical installer.** Self-serve portal flow. Customer supplies creds at install time via GUI prompt or via an `installer_tokens.py` URL. | `fc6a83d18b195bf3cbba1558f87f912a` | HARDLOCKED (no filename/URL/method changes; v2.6.x patch revisions allowed) |
| `setup-databyte-vpn-windows.ps1` | v1.0.0 | **Operator template for per-customer baked installers.** Operator edits `BAKED-IN CONFIG` block per customer, saves as `setup-databyte-vpn-<customer>-<device>.ps1`, ships URL. No prompt. | `5541343b9c5efe3b3b9257dbd3332805` (template) | Co-exists with v2.6.5; per-customer MD5 differs |

**Archived (DO NOT use, kept for git history only):**

- `setup-windows-vpn.ps1` — old pre-HARDLOCK script
- `connect-databyte-vpn.ps1` — old convenience wrapper
- `test-win-5g-setup.ps1` — old test variant
- `setup-databyte-vpn-zun.ps1` — old personal copy
- All `setup-databyte-vpn-baked-*.ps1` drafts

These are in `scripts/_archive-2026-06-24/` (older) or removed entirely (newer).

## Run (customer-facing flow)

Both scripts follow the same customer invocation pattern. PowerShell as Administrator:

```powershell
# v2.6.5 generic canonical
curl.exe -ksSL -o $env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1
& $env:TEMP\setup.ps1

# v1.0.0 baked per-customer
curl.exe -ksSL -o $env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/baked/setup-databyte-vpn-<customer>-<device>.ps1
& $env:TEMP\setup.ps1
```

**Why vpn-portal and not myvpn for the delivery URL:** `myvpn.databyte.co.za` is flagged on Cloudflare's badware list for some customer network paths. `vpn-portal.databyte.co.za` is clean. The VPN connection target stays `myvpn.databyte.co.za` regardless (the LE cert SAN covers both hostnames). See `docs/DAT-VPN-INT-WIN-001.md § 12.6` for the full finding.

**Why `curl.exe` and not `Invoke-WebRequest`:** PowerShell 5.1's `Invoke-WebRequest` has known TLS 1.3 + ISRG Root X2 chain issues. `curl.exe` (Windows 10 1803+) handles it correctly. Both scripts use `curl.exe` internally for HTTPS fetches.

## v2.6.5 — what the script does

The `setup-databyte-vpn.ps1` v2.6.5 script:

1. **STEP 1 — Remove stale profiles** (any prior `DatabyteVPN` + cmdkey entries)
2. **STEP 2 — Download Let's Encrypt CA** (cert chain bootstrap)
3. **STEP 3 — `New-VpnConnection`** (IKEv2, custom crypto)
4. **STEP 4 — `New-EapConfiguration`** (EAP-MSCHAPv2 schema)
5. **STEP 5 — Registry** (`AssumeUDPEncapsulationContextOnSendRule=2`, `NegotiateDH2048_AES256=2`)
6. **STEP 6 — `RasSetCredentials`** (bind creds via Win32 P/Invoke; canonical method)
7. **STEP 7 — Connect + poll** (rasdial, then poll `Get-VpnConnection` until `Connected`)

After install, customer uses:

```cmd
rasdial DatabyteVPN                   # Connect
rasdial DatabyteVPN /disconnect       # Disconnect
```

No GUI prompt after install. Credentials are saved in Windows Credential Manager via `RasSetCredentials`.

## v1.0.0 baked — what the script does (DIFFERENCES from v2.6.5)

The `setup-databyte-vpn-windows.ps1` template:

- **STEP 0 — Self-bootstrap ISRG Root X2** (NEW). Downloads LE root from `https://vpn-portal.databyte.co.za/static/certs/isrg-root-x2.pem` and installs via `certutil -addstore -f Root`. Skips if X2 already trusted. Required for Win 10 <1903 + Win 11 builds missing X2.
- **STEP 1 — Verify server cert** (issuer + optional SHA-256 fingerprint pin if `$ServerCertSha256` is baked to a real value).
- **STEPS 2–7 — Same as v2.6.5** (cert verify, profile create, IPsec, registry, RasSetCredentials, rasdial). Crypto identical (AES128/SHA256128/Group14/SHA256/PFS2048).
- **Credentials**: BAKED IN at operator edit time (no GUI prompt at install).
- **Token fetch**: REMOVED (no portal token round-trip).

Per-customer files served from `/opt/vpn-portal/www/static/baked/` on VPS.

## Operator workflow — baking a per-customer file

1. Copy the template:
   ```bash
   cp /root/projects/strongswan-vpn-gateway/scripts/setup-databyte-vpn-windows.ps1 /tmp/setup-databyte-vpn-acme-corp-laptop01.ps1
   ```
2. Pull the customer's credentials from the portal operator page.
3. Pull the current LE cert SHA-256 fingerprint:
   ```bash
   ssh root@vps-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -fingerprint -sha256'
   ```
4. Edit the file — replace the three `REPLACE-ME-*` values in the `BAKED-IN CONFIG` block.
5. Save as `setup-databyte-vpn-<customer>-<device>.ps1`.
6. Deploy to VPS:
   ```bash
   scp setup-databyte-vpn-<customer>-<device>.ps1 root@vps-01:/opt/vpn-portal/www/static/baked/
   ssh root@vps-01 'chown vpn-portal:vpn-portal /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1 && chmod 644 /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1'
   ```
7. Verify:
   ```bash
   curl -ksSL -o /dev/null -w "%{http_code} %{size_download}\n" https://vpn-portal.databyte.co.za/static/baked/setup-databyte-vpn-<customer>-<device>.ps1
   ```
8. Ship the URL to the customer (encrypted email, portal message, or SFTP). **Never email the .ps1 file itself** — it contains plaintext credentials.

## Security: cert pinning

Both scripts pin the server cert. v2.6.5 pins by **issuer** (Let's Encrypt expected). v1.0.0 baked additionally pins by **SHA-256 fingerprint** when `$ServerCertSha256` is set to a real value (not the `REPLACE-ME` placeholder).

If a network attacker substitutes a different cert (DNS poisoning, compromised intermediate CA), the script **refuses to install / connect** and exits with an error.

**If you rotate the LE cert on the server:**

1. certbot auto-renews every ~60 days via `dns-cloudflare` plugin.
2. Update `$ServerCertSha256` in the v1.0.0 template (or just leave `REPLACE-ME` for issuer-only validation).
3. Re-bake customer files at next onboarding touch.

## Verify (after connect)

```cmd
ipconfig /all
:: Look for the DatabyteVPN section — Default Gateway should be 154.65.110.44

tracert 8.8.8.8
:: First hop should be the VPS, not your home router

curl -s https://ifconfig.me
:: Public IP should be 154.65.110.44
```

## Disconnect / reconnect

```cmd
rasdial DatabyteVPN /disconnect
rasdial DatabyteVPN
```

Idempotent — safe to re-run.

## Test bandwidth cap

```cmd
iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30
iperf3 -c iperf.angolacables.co.ao -p 9200 -R -t 30
```

Expected: ~17–20 Mbps (cap minus ~10% XFRM/TCP overhead).

## Master documentation

- `docs/DAT-VPN-INT-WIN-001.md` — Internal build manual (server + client)
- `reports/DAT-VPN-INT-WIN-001-v1.1.0.docx` — ISO 9001 formatted version
- `reports/DAT-VPN-EXT-WIN-001-v1.0.0.docx` — Customer-facing quickstart (one-liner + troubleshooting)
- `reports/DAT-VPN-WINDOWS-CLIENT-MASTER-001-archived-2026-07-07.md` — pre-master consolidated doc (archived)