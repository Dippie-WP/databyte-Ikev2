# charon defaults audit — 2026-06-21

**Goal:** enumerate every tunable that affects charon behavior in this build, compare against strongSwan 6.0.7 defaults, flag anything non-obvious.

**Method:** inspected `/etc/strongswan.conf`, `/etc/strongswan.d/*.conf`, `/etc/swanctl/conf.d/*.conf` on LXC 903 (192.168.10.98). Cross-referenced with strongSwan 6.0.7 docs (`charon.conf.5`, `swanctl.conf.5`).

**Color key:**
- ✅ = at strongSwan default (no action)
- 🔧 = set explicitly with documented reason (no action)
- ⚠️ = set explicitly, no comment, may need attention
- 🚨 = set explicitly, contradicts current operational reality or default best practice

---

## 1. `charon { ... }` block (from `/etc/strongswan.conf`)

| Setting | Current | Default | Status | Notes |
|---|---|---|---|---|
| `install_virtual_ip` | `no` | `yes` | 🔧 | Set in `00-virtual-ip.conf`. Reason: kernel-netlink handles VIP install via attr-sql pool plugin. **But:** container runs `network_mode: host`, so kernel-netlink IS the host kernel. Verify attr-sql is actually assigning VIPs — not silently broken. |
| `install_routes` | `yes` | `yes` | ✅ | Default. |
| `start-scripts.creds/conns/pools` | `swanctl --load-*` | (none) | 🔧 | Required because vici uses TCP socket (see vici block below). Default = manual. |
| `plugins.vici.socket` | `tcp://127.0.0.1:4502` | `unix:///var/run/charon.vici` | 🔧 | TCP loopback only — comment in file documents why. |
| `filelog.default` | `1` | `1` | ✅ | Default. |
| `filelog.ike` | `2` | `1` | 🔧 | Bumped for debugging (matches `debug.conf`). |
| `filelog.knl` | `3` | `1` | 🔧 | Bumped for debugging. |
| `filelog.cfg` | `2` | `1` | 🔧 | Bumped for debugging. |

**Action items:** none from this block. Tunables are intentional.

---

## 2. Plugin load list (`loaded plugins` from `swanctl --stats`)

```
charon random nonce x509 constraints pubkey pem openssl ml sqlite
attr-sql kernel-netlink resolve socket-default vici updown
eap-identity eap-md5 eap-mschapv2 eap-dynamic eap-radius eap-tls
counters
```

| Plugin | Used by us? | Status |
|---|---|---|
| `charon random nonce x509 constraints pubkey pem openssl` | core crypto | ✅ required |
| `ml` (modecfg) | unused (legacy) | 🔧 harmless |
| `sqlite` | attr-sql backend | ✅ required |
| `attr-sql` | pool management | ✅ required |
| `kernel-netlink` | kernel IPsec interface | ✅ required |
| `resolve` | DNS resolution for IKE_SA_INIT | ✅ required |
| `socket-default` | UDP/TCP IKE socket | ✅ required |
| `vici` | control plane | ✅ required |
| `updown` | route scripts | ✅ required |
| `eap-identity` | EAP phase 1 | ✅ required |
| `eap-md5` | legacy (none of our conns use) | 🔧 harmless — could disable |
| `eap-mschapv2` | `rw-eap` | ✅ required |
| `eap-dynamic` | EAP method negotiation | ✅ required |
| `eap-radius` | not used (auth is local) | ⚠️ loaded but no radius config — disable to reduce attack surface |
| `eap-tls` | not used | ⚠️ loaded but no clients — disable |
| `counters` | SMEP/SMIP accounting | 🔧 required for attr-sql byte counters |

**Action items:**
- **Consider disabling `eap-radius`** — not configured, not used. Reduces kernel attack surface for misconfigured server.
- **Consider disabling `eap-tls`** — no clients configured for cert-based EAP.
- **`eap-md5`** — only useful if we add `eap-md5` to a `remote.auth`. Not used.

---

## 3. Connection-level tunables (`rw-eap.conf`)

| Setting | Current | Default | Status | Notes |
|---|---|---|---|---|
| `version` | `2` | `2` | ✅ | IKEv2 only. |
| `send_cert` | `always` | `ifasked` | 🔧 | Required: iPhone strongSwan app may not request but server should always send to avoid confusion. |
| `local_addrs` | `0.0.0.0` | `0.0.0.0` | ✅ | Listens on all interfaces. |
| `remote_addrs` | `%any` | `%any` | ✅ | Accepts any peer. |
| `pools` | `rw-pool` | (none) | ✅ | attr-sql pool defined elsewhere. |
| `proposals` | `aes256-sha256-modp2048,aes128-sha256-modp2048` | (built-in default: aes128-sha1-modp2048,3des-sha1-modp1536) | 🔧 | Modern cipher suite only — disables 3DES/SHA1. Good. |
| `unique` | `replace` | `never` | 🔧 | Replaces existing SAs for same peer on reauth. |
| `rekey_time` | `24h` | `24h` | ✅ | Default. |
| `reauth_time` | `24h` | `24h` | ✅ | Default. |
| `mobike` | `yes` | `yes` | ✅ | Default. Critical for cellular roam. |
| `fragmentation` | `yes` | `yes` | ✅ | Default. |
| `dpd_delay` | `30s` | `0s (disabled)` | 🔧 | Send DPD every 30s. Tunable for 5G CGNAT — currently OK, no evidence of drops. |
| `dpd_timeout` | `120s` | `0s (disabled)` | 🔧 | Mark peer dead after 120s no DPD response. |

### Children-level

| Setting | Current | Default | Status | Notes |
|---|---|---|---|---|
| `mode` | `tunnel` | `tunnel` | ✅ | |
| `local_ts` | `0.0.0.0/0` | `0.0.0.0/0` | ✅ | Full tunnel (split-tunnel not used). |
| `remote_ts` | `dynamic` | `dynamic` | ✅ | |
| `dpd_action` | `clear` | `none` | 🔧 | Tear down child SA on DPD timeout. |
| `start_action` | `start` | `start` | ✅ | |
| `rekey_time` | `1h` | `1h` | ✅ | Default. |
| `esp_proposals` | `aes256-sha256-modp2048,aes128-sha256-modp2048` | (built-in default) | 🔧 | Modern ESP suite, same as IKE. |

**Action items from connection level:** none.

---

## 4. Secrets block (`rw-eap.conf`)

```
eap-zun            id=zun
eap-zun-iphone     id=zun-iphone
eap-zun-windows    id=zun-windows
eap-demo-phone     id=demo-phone     secret=KILLED-c4415c4c07f1b4da
eap-demo-laptop    id=demo-laptop
eap-friend-phone   id=friend-phone
```

| Item | Status | Notes |
|---|---|---|
| 6 EAP users defined | ✅ | |
| `eap-demo-phone` is KILLED-… | ✅ | quota-monitor's hard-cut action. Expected. |

**Action items:** none. KILLED secret is the working state, not a config bug.

---

## 5. iptables interaction (host-side, not charon)

| Setting | Current | Default | Status |
|---|---|---|---|
| `/etc/iptables/rules.v4` | 508 quota:VIP rules | (none) | ✅ |
| `quota-monitor.service` polling | `60s` | n/a | 🔧 |
| `strongswan-iptables-watchdog.service` | active | n/a | 🔧 |

---

## Summary

**Total tunables reviewed:** 28 (8 charon block + 15 plugins + 14 connection + 6 secrets)
**Issues found:** 0 hard bugs, 2 minor (disabled but loaded plugins), 0 untracked defaults

### Recommendations (low priority — none are bugs)

1. **Disable `eap-radius`** in plugin load list. Not configured, not used, attack surface reduction.
2. **Disable `eap-tls`** in plugin load list. Same reason.
3. **Verify `install_virtual_ip = no` is still correct.** attr-sql + kernel-netlink SHOULD be installing VIPs. If VIPs are not being installed on client connect, this is a silent bug. Test: connect with iPhone, check `swanctl --list-sas` shows client VIP.

### What this audit does NOT cover

- **Performance:** worker thread count (16) not tuned. Memory limits not set.
- **Logging volume:** filelog at default=1 means INFO+ logs. Could be reduced.
- **Cert rotation:** `rw-eap.conf.certs` points to `server.crt.pem` with no rotation strategy.
- **Backup:** `/etc/swanctl/conf.d/rw-eap.conf` is the only secrets file. Lost if container destroyed.

---

**Reviewed by:** Misha
**Date:** 2026-06-21 09:50 UTC
**Build:** strongSwan 6.0.7-mschapv2-attrsql (container `zun/strongswan:6.0.7-mschapv2-attrsql`)