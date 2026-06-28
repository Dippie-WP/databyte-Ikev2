---
name: Bug Report
about: Report a bug or unexpected behavior in the VPN gateway
title: '[BUG] '
labels: bug
assignees: ''
---

## Bug Description

<!-- One-paragraph summary of what's broken. -->
<!-- Include the user-visible impact: "Customer X can't connect" / "Quota cut at 50% instead of 100%" / etc. -->

## Steps to Reproduce

1. <!-- Step 1 -->
2. <!-- Step 2 -->
3. <!-- Step 3 -->

## Expected Behavior

<!-- What you expected to happen. -->

## Actual Behavior

<!-- What actually happened. Paste error output / log lines. -->

## Environment

- **Host**: <!-- LXC 903 (homelab) / VPS prod (myvpn.databyte.co.za) / local dev -->
- **Commit / tag**: <!-- git rev-parse HEAD or git describe --tags -->
- **Component**: <!-- charon / portal / quota-monitor / ipBan / docker / nginx / certs / Windows installer / iOS app -->
- **Customer (if applicable)**: <!-- customer name or ID -->
- **Client**: <!-- Android / iOS (strongSwan app) / Windows (PowerShell installer) / Linux -->
- **Client OS version**: <!-- e.g. iOS 18.5, Windows 11 23H2, Ubuntu 24.04 -->
- **Connection type**: <!-- 5G / WiFi / LAN / Ethernet / CGNAT -->

## Logs

<!-- Paste relevant log lines. Format: -->
<!-- - charon: docker exec strongswan journalctl -u charon --no-pager -n 50 -->
<!-- - portal: journalctl -u vpn-portal --no-pager -n 50 -->
<!-- - quota: cat /var/log/quota-monitor.log | tail -50 -->
<!-- - ipBan: cat /opt/ipban/log.txt | tail -30 -->
<!-- - auth.log: grep <customer-name> /var/log/auth.log | tail -20 -->

```text
PASTE LOGS HERE
```

## Severity

- [ ] **Blocker** — service down, all customers disconnected
- [ ] **High** — degraded service, multiple customers affected
- [ ] **Medium** — single customer affected or workaround exists
- [ ] **Low** — cosmetic, typo, doc nit

## Diagnostic Evidence (per cross-check protocol)

<!-- Per Misha's diagnosis protocol (5-question gate): before filing "X is broken",
     verify these 5 things. Answer each. -->

1. **Is the customer listed in `swanctl --list-sas`?** (active SA check)
2. **Does the customer's `data_used_bytes` match what the iOS/Windows app shows?** (drift check)
3. **Does `rw-eap.conf` have a `KILLED-<hash>` or `BLOCKED-<hash>` secret?** (cut state check)
4. **Is the customer's charon log showing AUTH_FAILED or EAP_FAILURE?** (auth path check)
5. **Did the issue start after a specific commit / deploy / config change?** (regression check)

## Workaround

<!-- If you found a workaround while filing the bug, note it here. -->

## Related

<!-- Links to related issues, PRs, commits, tracker entries. -->
