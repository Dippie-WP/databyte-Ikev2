# Windows VPN Client Setup

Self-contained PowerShell scripts that set up an IKEv2/EAP-MSCHAPv2 VPN
connection to the Databyte VPN server on Windows 10/11.

## What's included

| File | Purpose |
|------|---------|
| `setup-windows-vpn.ps1` | Main script. Idempotent. Fetches CA cert from live URL with SHA256 pinning (falls back to bundled cert if offline). Sets crypto. Connects. |
| `connect-databyte-vpn.ps1` | Convenience wrapper. Runs `setup-windows-vpn.ps1` first (so the cert is always fresh), then creates the EAP-MSCHAPv2 profile. |
| `strongswan-ca.crt.pem` | Bundled CA cert (fallback only). The script prefers fetching from the live URL. |

## Run

**Right-click PowerShell → "Run as Administrator"** (or use the `Run as administrator` option), then:

```powershell
cd <path-to-folder>
powershell -ExecutionPolicy Bypass -File .\setup-windows-vpn.ps1
```

The script will:
1. Fetch the strongSwan CA cert from `https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem` (Cloudflare-cached 24h)
2. **Verify SHA256 fingerprint** matches the pinned value `5C:10:B9:6A:97:06:10:29:7C:8D:8F:B3:6B:E3:5A:98:58:CF:F4:10:C8:1E:72:78:7E:25:08:43:B2:71:CE:06` before installing (defence against MITM)
3. Fall back to `strongswan-ca.crt.pem` (bundled) if the live fetch fails — also SHA256-checked
4. Skip re-install if the correct cert is already in `LocalMachine\Root`
5. Create a `DatabyteVPN` IKEv2 connection with **no split tunneling**
6. Set IPsec crypto to match the server (AES256/SHA256/Group14/ECP384)
7. Connect with the operator credentials baked into the script

## Security: cert pinning

The script pins the SHA256 fingerprint of the CA cert, NOT just relying on the
Windows cert chain validation. If a network attacker substitutes a different
cert (e.g. via DNS poisoning or compromised intermediate CA), the script
**refuses to install it** and exits with an error. The bundled fallback is
also SHA256-checked — a stale bundle will not be silently accepted.

**If you rotate the CA cert on the server:**
1. Update `$ExpectedCaSha256` in `setup-windows-vpn.ps1` to the new value
2. Replace `strongswan-ca.crt.pem` with the new cert
3. Push both changes to the repo (the script IS the truth)
4. Customers re-run the script — old cert will be flagged as "fingerprint
   mismatch" and the new one will be installed automatically

## Verify

```cmd
ipconfig /all
:: Look for the DatabyteVPN section — Default Gateway should be the VPS IP

tracert 8.8.8.8
:: First hop should be the VPS, not your home router
```

## Test bandwidth cap

```cmd
iperf3 -c iperf.angolacables.co.ao -p 9200 -t 30
iperf3 -c iperf.angolacables.co.ao -p 9200 -R -t 30
```

Expected: ~17-20 Mbps (cap minus ~10% XFRM/TCP overhead).

## Reconnect

Just re-run the script. It's safe to re-run.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-windows-vpn.ps1
```

Or use the shorter wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File .\connect-databyte-vpn.ps1
```

## Disconnect

```cmd
rasdial DatabyteVPN /disconnect
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Live cert SHA256 mismatch" | Server cert rotated OR MITM | Check `ExpectedCaSha256` against the value on the VPS |
| "Live fetch failed" | No internet OR DNS issue OR portal down | Script falls back to bundled cert; investigate connectivity separately |
| "Bundled cert SHA256 mismatch" | `strongswan-ca.crt.pem` is stale | Re-fetch from live URL or replace with correct cert |
| "Cert already installed" message, but VPN still fails | Old cert pinned by subject, fingerprint might differ | Script now checks SHA256 — will detect mismatch and re-install |
| "Set-VpnConnectionIPsecConfiguration failed" | PowerShell older than 5.1 or missing VPN cmdlets | Update Windows 10/11; PowerShell 5.1 ships in-box |
| rasdial exit code non-zero | Credential wrong OR server unreachable | Verify `zun-operator` password; check `tracert myvpn.databyte.co.za` |
