# VPN Credentials Reset — Customer Notice

**Status:** 📢 **SENT/TO-SEND** (2026-07-06 03:55 SAST, Phase 5 RADIUS cutover)
**Audience:** All 40 VPN customers
**Channel:** WhatsApp broadcast (via customer's stored number) + email backup

---

## Message (English + Afrikaans)

> **Subject:** VPN credentials reset — please re-register
>
> Dear VPN customer,
>
> We upgraded our VPN security infrastructure on **Monday 7 July 2026 at 04:00 SAST**.
> As part of this upgrade, **all existing VPN credentials have been reset**.
>
> Please re-register your device on the customer portal:
>
> 🌐 **https://vpn-portal.databyte.co.za/portal/**
>
> 1. Log in with your existing account email
> 2. Click **Devices** → **Add Device** (or **Re-register**)
> 3. Set a new password (8+ characters)
> 4. Follow the on-screen iPhone / Windows / Android instructions
>
> **Your existing VPN connection will stop working after the upgrade.**
> The new connection uses credentials stored in our central authentication
> server (no more device-local passwords).
>
> Support: WhatsApp +27 ... or support@databyte.co.za
>
> — Databyte Operations

---

## Afrikaans version

> **Onderwerp:** VPN-credentials terugstel — herregistreer asseblief
>
> Beste VPN-kliënt,
>
> Ons het ons VPN-sekuriteitsinfrastruktuur opgradeer op **Maandag 7 Julie 2026 om 04:00 SAST**.
> As deel van hierdie opgradering is **alle bestaande VPN-credentials teruggestel**.
>
> Herregistreer asseblief jou toestel op die kliënteportaal:
>
> 🌐 **https://vpn-portal.databyte.co.za/portal/**
>
> 1. Teken in met jou bestaande rekening-e-pos
> 2. Klik **Toestelle** → **Voeg toestel by** (of **Herregistreer**)
> 3. Stel 'n nuwe wagwoord (8+ karakters)
> 4. Volg die iPhone / Windows / Android instruksies op die skerm
>
> **Jou bestaande VPN-verbinding sal nie meer werk ná die opgradering nie.**
> Die nuwe verbinding gebruik credentials wat in ons sentrale
> verifikasiemasjien gestoor is (geen toestel-lokale wagwoorde meer nie).
>
> Ondersteuning: WhatsApp +27 ... of support@databyte.co.za
>
> — Databyte Operasies

---

## Internal notes

| Channel | Action | Owner |
|---|---|---|
| WhatsApp broadcast | Send via portal admin bulk-message at 03:55 SAST | Zun |
| Email backup | Send to all 40 customer emails on file | Zun |
| Portal banner | Add "Re-register" banner to portal landing | Misha (Phase 7) |
| Status page | Add maintenance notice for ~24h | Zun |

## Rollback path

If cutover fails (>90% reject rate from FreeRADIUS within 1h of flip):
1. Revert rw-eap.conf: `sed -i 's/auth = eap-radius/auth = eap-mschapv2/' /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf`
2. `docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-conns`
3. All 40 customers re-enabled (back to plaintext secrets)
4. Send "false alarm" message

## Verification milestone

After 5 customers re-register successfully:
- FreeRADIUS radpostauth table shows Access-Accept for ≥5 distinct users
- Each Access-Accept has matching `Framed-IP-Address` from rw-pool
- charon-log shows IKE_SA established per user (no AUTH_FAILED)
- All 5 customers confirm via phone/WhatsApp that VPN works

Once 5/40 verified, declare Phase 5 complete. Phase 6 = remaining 35 re-register.

---

**File created:** 2026-07-06 03:55 SAST (Misha, post-cutover)
**Status:** Awaiting Zun approval + send
