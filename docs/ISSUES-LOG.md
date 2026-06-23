# ISSUES-LOG

Every bug we hit building strongSwan v1.2. Format: **date — title — what happened — fix — lesson**. Reverse-chronological.

---

## 5A.7 — Server-side MSS clamp (2026-06-18)

**Symptom:** After 5A.6 fix, 5G phone could connect and `ifconfig.me` worked, but other sites (iana.org, wikipedia.org) gave `ERR_TIMED_OUT` after "took too long to respond".

**Root cause:** Path MTU on 5G carriers is often 1280-1400 bytes. ESP+UDP+IP adds 50-70 bytes. The phone's strongSwan app MTU=1400 told the phone to advertise MSS=1360 to remote sites. Remote sites then sent 1360-byte packets back. LXC encapsulated in ESP, sent 1430-byte packets to phone, 5G carrier dropped them. PMTUD failed because ICMP "fragmentation needed" is blocked on CGNAT.

**Fix:**
```bash
iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260
```
Calculation: 1400 (phone MTU) - 40 (TCP+IP) - 70 (ESP+UDP+IP) = 1290, use 1260 for headroom. Persisted via `iptables-save > /etc/iptables/rules.v4`.

**Lesson:**
- **The strongSwan app's phone-side MTU setting is necessary but not sufficient** for 5G.
- Server-side MSS clamping is required because the LXC doesn't know the phone's tunnel MTU.
- The server is the one talking to remote sites (post-MASQ), so the server clamps the MSS in the SYNs it sends on the phone's behalf.

---

## 5A.6 — `install_virtual_ip = no` (gateway mode) (2026-06-18)

**Symptom:** Phone connected, SA ESTABLISHED, VIP 10.99.0.50 assigned, MASQ matching, but `ip -s xfrm state` showed `out 0 bytes, 0 packets`. Phone could only send DNS queries through tunnel, no responses came back. conntrack from 10.99.0.50 = 0 for fresh connections.

**Root cause:** charon's DEFAULT `install_virtual_ip=yes` adds the assigned VIP (e.g. 10.99.0.50) as a LOCAL address on lo. The kernel's local routing table has priority 0, BEFORE table 220 (strongSwan's policy routing). So traffic FROM 10.99.0.50 is routed to the local stack instead of via table 220 → eth0 → MASQ. Packets die at the local stack.

**Smoking gun:**
- `ip addr show lo` → `inet 10.99.0.50/32 scope global lo`
- `ip route get 10.99.0.50` → `local 10.99.0.50 dev lo table local src 10.99.0.50`
- `ip -s xfrm state` → out SA had `oseq 0x0, 0 bytes, 0 packets` despite thousands of incoming packets
- `swanctl --list-sas` showed healthy looking ESTABLISHED SA — **masked the bug**

**Fix:** `/home/zunaid/strongswan/strongswan.d/00-virtual-ip.conf`:
```
charon {
    install_virtual_ip = no
    install_routes = yes
}
```
**Bind-mount required:** This file must be bind-mounted into the container at `/etc/strongswan.d/00-virtual-ip.conf` for charon to read it.

**Lesson:**
- **The strongSwan defaults are tuned for CLIENTS, not gateways.** This is the single most important charon override for any strongSwan gateway deployment.
- **`swanctl --list-sas` is a liar** — it shows ESTABLISHED even when the kernel isn't actually forwarding. **Always cross-check with `ip -s xfrm state` (kernel view).**
- **Don't trust "fix applied" without verification.** A file on the host is NOT loaded by the container unless the docker-compose has a corresponding bind-mount.

---

## 5A.6 follow-on: bind-mount not added → fix not loaded (2026-06-18 17:48)

**Symptom:** After applying the `install_virtual_ip = no` fix, the bug came back. SA in counter was 100KB+ but out counter was 0. 10.99.0.50 was on lo again.

**Root cause:** I added the config file at `/home/zunaid/strongswan/strongswan.d/00-virtual-ip.conf` but **forgot to add a corresponding bind-mount line in `docker-compose.yml`**. The compose file bind-mounts specific files (`debug.conf`, `attr-sql.conf`) into the container, NOT the whole directory. My new file sat on the host, charon never saw it, defaults were used, the bug persisted.

**Fix:** Added to docker-compose.yml:
```yaml
- ./strongswan.d/00-virtual-ip.conf:/etc/strongswan.d/00-virtual-ip.conf:ro
```
Then `docker compose --profile vpn up -d` to recreate the container with the new mount. Verified via `docker exec strongswan ls /etc/strongswan.d/`.

**Lesson:**
- **Docker-compose file-level bind-mounts require explicit mount lines.** A file on the host at `./strongswan.d/foo.conf` will NOT automatically appear in the container at `/etc/strongswan.d/foo.conf`. **Verify with `docker exec container ls /path/`** after every config change.
- **This was the strongest evidence for "we only work with facts and evidence now".** I should have `docker exec strongswan ls /etc/strongswan.d/` after creating the file, not assumed it was loaded.

---

## iOS .mobileconfig silently fails cert validation (2026-06-17)

**Symptom:** Generated `.mobileconfig` profile (IKEv2 + EAP, with the strongSwan CA cert embedded), installed on iPhone, profile shows as installed but IKE never connects. iOS gives no useful error. `swanctl --list-sas` on server shows nothing for the iOS client.

**Root cause (likely):** iOS 26 has stricter cert validation. The strongSwan CA may not pass the chain validation. iOS expects either:
- A publicly-trusted CA (Let's Encrypt, etc.)
- A CA cert with specific Extended Key Usage settings

**Status:** Path shelved to v1.3. v1.3 plan: use certbot + DNS-01 challenge for a Let's Encrypt cert. Skip the self-signed CA for iOS.

**Lesson:**
- iOS native IKEv2 + EAP requires a publicly-trusted cert. Self-signed works for Android strongSwan app and charon-cmd, not for iOS native.
- The strongSwan app on iOS works (uses PSK instead of cert), but the native client is the goal.

---

## charon-cmd is a TEST client, not a production client (2026-06-17)

**Symptom:** Tested "client-side" with `charon-cmd` on the OC host (192.168.10.77). charon-cmd connected to the strongSwan container, SA established, VPN worked. Marked 5A.3 GREEN.

**Root cause:** charon-cmd on the OC host is on the SAME LAN as the strongSwan container. The test path was:
- charon-cmd (192.168.10.77) → strongSwan container (192.168.10.212)
- Both on 192.168.10.0/24, no router, no NAT, no public IP involved

This is **server-correctness only**, not public-path. The real test is: phone on 5G (public IP) → router port-forward → LXC 902 → charon. That involves NAT traversal, MOBIKE, MSS, carrier constraints.

**Fix:** 5A.3 was re-tested with the Android strongSwan app on 5G. That's the real test.

**Lesson:**
- **Always specify the test environment.** "It works" means nothing without "from where, to where, over what path".
- charon-cmd is useful for unit testing (server config is correct) but not for end-to-end public-path tests.

---

## 5G IP rotation causes brief MOBIKE gaps (2026-06-17, ongoing)

**Symptom:** On Vodacom 5G, the public IP rotates every few minutes. During the rotation:
- charon MOBIKE updates the IPsec policy template (1-3 sec)
- During the gap, ESP packets go to the OLD dead IP
- Phone sees broken TCP connections, "ERR_CONNECTION_CLOSED"

**Status:** Workaround for v1.2 — toggle VPN off/on on the phone when websites stop loading. v1.5B will shorten `rekey_time` and add `charon.keep_alive = 20s`.

**Lesson:**
- 5G CGNAT IP rotation is a real issue for long-lived sessions. This is fundamental to carrier networks, not strongSwan.
- For commercial use (5D), MOBIKE keep-alives or shorter rekey times are mandatory.

---

## Build lessons: `libsqlite3-dev` vs `libsqlite3-0` (2026-06-17)

**Symptom:** First build of v1.2 image (with attr-sql) failed at runtime with `charon: unable to load plugin attr-sql: libsqlite3.so.0: cannot open shared object file`.

**Root cause:** The Dockerfile pattern of `apt-get -y remove $DEV_PACKAGES && apt-get -y autoremove` strips BOTH the dev package AND its runtime dependency. We needed `libsqlite3-dev` at compile time (for `sqlite3.h` headers) and `libsqlite3-0` at runtime (for the `.so`). The autoremove removed both.

**Fix:** Explicit re-install after autoremove:
```dockerfile
RUN \
  ...
  apt-get -y remove $DEV_PACKAGES && \
  apt-get -y autoremove && \
  # Re-install libsqlite3 runtime — the dev package pulled it in but autoremove
  # removed it once -dev was gone. The attr-sql plugin .so needs libsqlite3.so.0.
  apt-get -y install --no-install-recommends libsqlite3-0 && \
  ...
```

**Lesson:**
- **`apt-get -y remove $DEV_PACKAGES` is dangerous** when the runtime lib isn't in the base image. Always check what autoremove wants to remove, or use `--no-install-recommends` for dev packages.
- **Test the runtime, not just the build.** The first build "succeeded" — the image was created. The bug only manifested when charon tried to load the plugin.

---

## `--enable-pools` is a no-op flag (2026-06-17)

**Symptom:** Initially assumed I needed `--enable-pools` for charon's IP pool feature.

**Root cause:** `--enable-pools` enables charon-tkm (Trusted Key Manager), NOT charon's IP pool. charon's IP pool is built in (default).

**Lesson:**
- **Read the strongSwan configure help before adding flags.** `--enable-pools` looks like it would enable pool functionality but it's for a different subsystem.
- charon has a built-in IP pool, no flag needed.

---

## DB initializes on FIRST QUERY, not first start (2026-06-17)

**Symptom:** On a fresh deploy, `/var/lib/strongswan/` directory was empty. Ran `swanctl --list-pools`, got an empty list. Couldn't tell if the schema was there.

**Root cause:** attr-sql plugin creates the DB schema lazily — only when the first query is made. Empty dir is normal on a fresh deploy.

**Fix:** `swanctl --list-pools` triggers the schema creation. After that, the DB is there.

**Lesson:**
- **Lazy initialization is a strongSwan pattern.** Don't be alarmed by empty dirs on fresh deploys. Run a query first.

---

## charon `swanctl.conf` `secrets {}` block, NOT the `users` table (2026-06-17)

**Symptom:** Initially tried to insert EAP credentials into the `users` table in SQLite.

**Root cause:** charon has two different auth subsystems:
- `secrets {}` block in swanctl.conf — for EAP-MSCHAPv2 (and PSK, RSA)
- `users` table in SQLite — for the `sql` plugin's different auth model

These are NOT interchangeable. EAP-MSCHAPv2 with attr-sql pool is `secrets {}` + `addresses` table.

**Lesson:**
- **Two tables, two auth models.** For our setup (EAP-MSCHAPv2 + per-user VIP), use:
  - `secrets {}` block in `swanctl.conf` (or `conf.d/*.conf`) for the credentials
  - `identities` + `addresses` tables for the VIP pinning
  - The `users` table is for a different plugin

---

## Daily report pipeline: NUKED 2026-06-15 (out of scope for this repo)

**Why I mention it:** This was a 6h build/test cycle that ended with the daily HTML+PDF report pipeline being removed entirely. Cron entry, gen_dr_html.py, log, output dir, all templates, SOP skill, design spec, historical reports, inbound Telegram copies, mempalace copies — all moved to `.Trash/`. 428KB pre-nuke archive.

**Reason:** Zun's call. "Daily extended report is a time sink, not a decision aid." The short status (xlsx) and runbooks (docx) pipelines are unchanged.

**Why it doesn't affect this repo:** The daily report was a workspace thing, not a strongSwan thing. The strongSwan runbook is docx (DAT-OPS-SEC-002 v1.2), unaffected.

---

## ops-tracker duplicate cleanup (2026-06-16)

**Symptom:** Two ops-tracker services on the Pi: a Docker container AND a FastAPI sibling at `/home/zunaid/operations-tracker/` (no service, just code, abandoned 2026-04-22).

**Root cause:** When the original ops-tracker was rewritten as a FastAPI service, the old directory wasn't cleaned up. The systemd `ops-tracker.service` is actually a `Type=oneshot` `docker start` wrapper, not a FastAPI service. So the systemd unit name is misleading.

**Fix:** Moved the orphan to `/home/zunaid/operations-tracker.bak-20260616-fastapi-orphan-removed`.

**Lesson:** When refactoring a service, check for orphans. systemd unit names don't always reflect what they actually run.

---

## Pre-v1.2: charon-cmd race condition with VICI (2026-06-16)

**Symptom:** Original `start.sh` ran `swanctl --load-creds && swanctl --load-conns && swanctl --load-pools` before the VICI socket was up. Sometimes worked, sometimes didn't.

**Fix:** Baked `start-scripts { creds = ...; conns = ...; pools = ...; }` into `strongswan.conf`. charon runs the start-scripts after VICI is ready. The wrapper `start.sh` is now just `exec ./charon "$@"`.

**Lesson:**
- **Don't try to outsmart charon's lifecycle.** Let charon manage loading.
- The start-scripts option is exactly the right hook.

## 2026-06-23 — CP4 + CP5 audit findings (commit dcc0676, audit follow-up)

### 🔴 CRITICAL — Portal unreachable from Cloudflare ✅ RESOLVED 2026-06-23 07:42 UTC (commit a64211f)

**Bug:** OS firewall `iptables-legacy` INPUT chain had policy DROP and **no rules for TCP 80 or 443**. The Xneelo cloud firewall was open (verified by Zun), but the OS firewall then blocked all external traffic. The portal was unreachable from the internet.

**Resolution:** Inserted two ACCEPT rules at positions 9 + 10 (before RELATED/ESTABLISHED):
```
-A INPUT -p tcp -m tcp --dport 80 -m comment --comment "vpn-portal: HTTP (nginx)" -j ACCEPT
-A INPUT -p tcp -m tcp --dport 443 -m comment --comment "vpn-portal: HTTPS (nginx)" -j ACCEPT
```
Persisted via `netfilter-persistent save` to `/etc/iptables/rules.v4`. Will survive reboot.

**External verification (OC host 192.168.10.77 → 154.65.110.44):**
- TCP :443 → CONNECT_OK
- TCP :80 → CONNECT_OK
- HTTPS /api/health → 200, all 7 security headers
- HTTPS /certs/strongswan-ca.crt.pem → 200, SHA256 matches client fingerprint
- Login → Set-Cookie: Secure; HttpOnly; SameSite=lax

Reference rules snapshot committed at `host/firewall/rules.v4`.

**Original audit entry preserved below for the historical record:**

### 🔴 CRITICAL — Portal unreachable from Cloudflare (RESOLVED — see above)

**Bug (original):** OS firewall `iptables-legacy` INPUT chain had policy DROP and **no rules for TCP 80 or 443**.

**Evidence:**
- `iptables-legacy -L INPUT -n` — 9 rules, none for 80/443
- `policy DROP 62 packets, 4146 bytes` — 62 packets dropped
- `ss conntrack` — zero external connections (only 127.0.0.1 and self-to-self)
- `tcpdump -i any "tcp[tcpflags] & tcp-syn != 0 and tcp dst port 443"` — 0 packets in 12s
- VPS's public IP (154.65.110.44) is on `ens3` (verified via `ip -4 addr`)

**Why missed in CP4:** Self-tests used `curl 127.0.0.1` and `curl <own public IP>` from the VPS itself — both succeed via the kernel's local routing table. The OS firewall is only consulted for external source IPs.

**Proposed fix (AWAITING ZUN APPROVAL):**
- Insert two rules in the correct position:
  ```
  -A INPUT -p tcp -m tcp --dport 80 -j ACCEPT
  -A INPUT -p tcp -m tcp --dport 443 -j ACCEPT
  ```
- Persist via `netfilter-persistent save` (apt) or restore in `/etc/rc.local`
- Defense in depth: Xneelo cloud firewall remains the primary filter (Cloudflare IP allowlist); OS firewall just opens the port on the host

### 🟠 HIGH (next session, not blocking CP4 acceptance)

1. **Customer portal cookie missing `secure` flag** — `/api/portal/login` `set_cookie` call. Fix: add `secure=secure_cookie` parameter.
2. **`/certs/` exposes `strongswan-ca.crt.srl` and `.gitkeep`** — return 404 for non-`.pem`/`.crt` files via nginx `location ~ \.(srl|gitkeep)$ { return 404; }`.
3. **Operator session cleanup is lazy-only** — `purge_expired_sessions()` exists for customer sessions but is never called. Add systemd timer + sqlite query, OR call from auth middleware.

### 🟡 MEDIUM (CP7 scope)

4. **No fail2ban portal jail** — only sshd jail exists. Add portal-login jail with logpath `/var/log/nginx/vpn-portal.access.log`, 3 retries → 24h ban.
5. **No AIDE** — critical files (`/opt/vpn-portal/app.py`, `/etc/vpn-portal.env`, `/etc/ssl/cloudflare/*`, `/etc/nginx/sites-enabled/vpn-portal`) have no integrity baseline.
6. **No backup of `/etc/vpn-portal.env` or `/etc/ssl/cloudflare/*`** — extend `strongswan-db-backup.sh` to include these, push to RustFS encrypted bucket.
7. **No cert expiry monitoring** — Origin Cert valid until 2041-06-19. Add `/opt/scripts/check-cert-expiry.sh` + cron.
8. **Over-broad INPUT rules** — `ACCEPT 0.0.0.0/0 :4502` (charon is 127.0.0.1 only); `ACCEPT 10.99.0.0/24` on INPUT (should be FORWARD only).
9. **iptables-nft empty + policy ACCEPT** — currently using legacy backend, but nft is the default. Consolidate to one backend, document in DEPLOYMENT.md.

### 🟢 LOW (polish, not blocking)

10. **systemd `RuntimeDirectoryMode` duplicate** (0750 in main unit + 0755 in drop-in). Remove 0750 from main unit.
11. **CSP no `report-uri`** — add `report-uri /api/csp-report` + implement endpoint.
12. **logrotate for portal logs not explicitly documented** — covered by `nginx` config `/var/log/nginx/*.log` glob, but no explicit entry.

### New lessons
- **#55:** Self-testing a network service via `curl <own public IP>` succeeds via kernel local routing even when OS firewall would block external sources. The real test is from an external IP. For services behind a cloud firewall, this means asking the user to test from a phone.
- **#56:** "Ports are open" at the cloud layer doesn't imply OS-level iptables is open. The two are independent defense-in-depth layers; both must be configured.
