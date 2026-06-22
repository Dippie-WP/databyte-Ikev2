# Windows VPN Client Setup

Self-contained PowerShell script that sets up an IKEv2/EAP-MSCHAPv2 VPN
connection to the Databyte VPN server on Windows 10/11.

## What's included

- `setup-windows-vpn.ps1` — main script. Idempotent.
- `strongswan-ca.crt.pem` — bundled CA cert (client copy).

## Run

**Right-click PowerShell → "Run as Administrator"** (or use the `Run as administrator` option), then:

```powershell
cd <path-to-folder>
powershell -ExecutionPolicy Bypass -File .\setup-windows-vpn.ps1
```

The script will:
1. Install the strongSwan CA cert into `LocalMachine\Root`
2. Create a `DatabyteVPN` IKEv2 connection with **no split tunneling**
3. Set IPsec crypto to match the server (AES256/SHA256/Group14/ECP384)
4. Connect with the operator credentials baked into the script

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

## Disconnect

```cmd
rasdial DatabyteVPN /disconnect
```

## Customize for a different operator

Edit the top of `setup-windows-vpn.ps1` (the `CONFIG` block):
- `$ServerHostname` — your server's DNS name
- `$ConnectionName` — Windows connection name
- `$Username` / `$Password` — EAP creds
- `$CaCertPath` — path to your CA cert

## Security note

This script contains **plaintext credentials**. Do not commit your customized
copy to a public repo. The bundled version uses production credentials and is
intentional for the operator's own use.

## Why this script is needed

Native Windows IKEv2 + EAP-MSCHAPv2 has three known issues with strongSwan
servers using self-signed CAs:

1. **Cert trust**: Windows hangs if the CA cert isn't in the Trusted Root
   store. Fixed by Step 1 of this script.
2. **Default gateway**: Without `-SplitTunneling:$false`, internet traffic
   bypasses the tunnel. Fixed by omitting `-SplitTunneling` in Step 2.
3. **Crypto defaults**: Windows defaults to weak DH (Group2) and 3DES/MD5.
   Fixed by Step 3 (`Set-VpnConnectionIPsecConfiguration` + registry tweak).
