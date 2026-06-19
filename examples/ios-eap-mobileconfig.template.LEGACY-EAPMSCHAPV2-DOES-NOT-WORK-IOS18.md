# iOS native IKEv2 + EAP-MSCHAPv2 via `.mobileconfig` — DOES NOT WORK on iOS 18+

**Status:** DEPRECATED. Do not use this approach for EAP-MSCHAPv2 clients.

## Why it's deprecated

iOS 18+ native VPN Settings + `.mobileconfig` is **fundamentally broken for EAP-MSCHAPv2**, regardless of how the profile is configured. We tested **8 different mobileconfig variations** (v3-v10) with different combinations of:

- `AuthenticationMethod: Certificate` vs `None`
- `ExtendedAuthEnabled: 1` (integer) vs `true` (bool)
- `AuthPassword` baked in vs omitted (force iOS prompt)
- `AuthName`/`AuthPassword` vs `Username`/`Password` keys
- PKCS#7 signed vs UNSIGNED XML
- Unique `PayloadIdentifier`/`PayloadUUID` per profile
- 2048-bit vs 4096-bit CA cert (matching the one iPhone has trusted)
- 1280-byte MTU + NATKeepAliveInterval=20 + MOBIKEEnabled (Apple's iOS-side recommended options)

**Every variation had the same failure mode:**
1. iOS sends `EAP identity 'demo-phone'` ✓
2. Server sends `EAP/REQ/MSCHAPV2` (challenge) ✓
3. iOS never sends `EAP/RES/MSCHAPV2` ✗ — silent, no UI prompt, no error
4. iOS opens 5+ parallel IKE_SAs in retry storm → server hits `per-IP half-open IKE_SA limit of 5 reached`
5. All SAs timeout, iOS backs off, retries, same loop → "Connecting…" forever

Per the strongSwan discussion #2612 reference, a working EAP-MSCHAPV2 handshake has 4 messages: ID → MSCHAPV2 req → MSCHAPV2 challenge response → EAP/SUCC. iOS only does 2 (ID, then nothing).

## What to use instead

**strongSwan iOS app** (free, official strongSwan client, App Store). It has a working EAP-MSCHAPv2 implementation and bypasses the iOS native bug entirely. See the README "iPhone / iPad" section for setup steps.

## If you need iOS native (EAP-TLS path, 5D)

The v3 template previously here can still work for **EAP-TLS** (per-device client certs, 5D path), because EAP-TLS doesn't need the EAP-MSCHAPv2 dialog — the cert IS the auth. The mobileconfig flow is the right approach for EAP-TLS.

## Reference: v3 mobileconfig structure (kept for 5D EAP-TLS)

If you need to regenerate the v3 template for EAP-TLS:

1. Replace `<string>***</string>` with a `${AUTH_PASSWORD}` template variable
2. Change `AuthenticationMethod` from `Certificate` to `Certificate` (same — it's `Certificate` for EAP-TLS too, since the cert IS the auth method)
3. Use a per-profile unique `PayloadIdentifier` (e.g. `com.homelab.vpn.<device>.ikEv2`) and a per-profile unique CA `PayloadUUID` (iOS 18 merges profiles with the same `PayloadIdentifier`)
4. Sign the profile (PKCS#7 DER via `openssl smime -sign -outform der -nodetach`) before distribution

## Lessons from 8 iterations (2026-06-19)

- iOS 18 merges installed profiles with the same `PayloadIdentifier` — new profile's settings are ignored
- `AuthPassword` in mobileconfig may not be honored by iOS 18 native VPN; bake the user, let iOS prompt
- CA cert must match the one iPhone has trusted; otherwise iOS silently fails cert validation
- Server cert must use PKCS#1 v1.5 (`sha256WithRSAEncryption`), NOT RSASSA-PSS, for iOS native IKEv2
- StrongSwan official iOS profile example uses `AuthenticationMethod: Certificate` for EAP-MSCHAPv2 — this is **WRONG** for iOS 18+, should be `None` (per pfSense bug #13878). Doesn't matter — the EAP-MSCHAPv2 flow is broken anyway
