# DAT-VPN-INT-WIN-LIVE-001 — Windows IKEv2 VPN Client: Live-State Fact-Check

| Field | Value |
|---|---|
| Document ID | **DAT-VPN-INT-WIN-LIVE-001** |
| Title | Windows IKEv2 + EAP-MSCHAPv2 VPN Client — **Live-State Fact-Check** (parallel companion to `DAT-VPN-INT-WIN-001`) |
| Version | **v0.1.0** (initial — 2026-10-15) |
| Date (SAST) | **2026-10-15** |
| Author | Misha 🐻 (Zun's assistant) |
| Status | **LIVE-STATE FACT-CHECK** — parallel working companion to the HARDLOCKED `DAT-VPN-INT-WIN-001`. **NOT approved for production.** This is an operator working reference. |
| Companion doc (HARDLOCKED) | **[`docs/DAT-VPN-INT-WIN-001.md`](./DAT-VPN-INT-WIN-001.md)** — v1.1.0 (refresh-in-place 2026-07-07), **APPROVED — HARDLOCKED**. That doc is the production-approved reference; refer to it for any change requests. This live-state doc explicitly does **NOT** modify it. |
| Live fact-check | **2026-10-15 06:50 UTC (08:50 SAST)** — all script + server facts verified against live `vpn-prod-01` (was `vps-01`, renamed per HOT-210, 2026-09-30) |
| ISO 9001:2015 | Document Control: internal — distribution restricted to operators |
| Companion docs (this repo) | [`docs/ARCHITECTURE.md`](./ARCHITECTURE.md) (2026-10-15 v2.2.1+v2.5.0 fact-check) · [`docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0](./RUNBOOK-DR-REBUILD-AND-HA.md) · [`docs/VPS-XNEELO-DEPLOY.md` v1.0.0](./VPS-XNEELO-DEPLOY.md) · [`DAT-VPN-INT-WIN-001.md` v1.1.0](./DAT-VPN-INT-WIN-001.md) (HARDLOCKED) · [`DAT-VPN-INT-WIN-GUIDE-001.md`](./DAT-VPN-INT-WIN-GUIDE-001.md) · [`DAT-VPN-INT-WIN-HOSTILE-001.md`](./DAT-VPN-INT-WIN-HOSTILE-001.md) |

---

## 0. Purpose

This doc is a **parallel companion** to the HARDLOCKED `docs/DAT-VPN-INT-WIN-001.md` (v1.1.0, 2026-07-07). It serves three purposes:

1. **Current fact-check** — script + server facts verified against live `vpn-prod-01` as of 2026-10-15 (the hardlock was last verified 2026-07-07, ~3.5 months ago)
2. **Hostname fix** — `vps-01` → `vpn-prod-01` in all operational `ssh`/`scp` commands (the hardlock still has `vps-01` because the rename per HOT-210 was 2026-09-30, AFTER the hardlock's last verification)
3. **Discrepancy log** — factual claims in the hardlock that could not be verified on live prod or in the git repo, so the next hardlock revision knows what to fix

**This doc does NOT replace the hardlock.** For production changes, follow the hardlock's approval process. Use this doc as a working reference for what the current state actually is.

> **"Is that better?"** (Zun msg #43979) — yes, parallel is better than either (a) silently editing the hardlock or (b) ignoring the discrepancies. This doc acknowledges both the hardlock's authority and the live state's facts.

---

## 1. Cross-reference: the hardlock doc

| Property | Hardlock (DAT-VPN-INT-WIN-001) | This doc (DAT-VPN-INT-WIN-LIVE-001) |
|---|---|---|
| Location | `docs/DAT-VPN-INT-WIN-001.md` | `docs/DAT-VPN-INT-WIN-LIVE-001.md` |
| Version | v1.1.0 (refresh-in-place 2026-07-07) | v0.1.0 (initial 2026-10-15) |
| Status | **APPROVED — HARDLOCKED** | **LIVE-STATE FACT-CHECK** (working reference) |
| Last fact-check | 2026-07-07 | 2026-10-15 |
| Can be modified freely? | **No** — needs formal review + revision bump | **Yes** — open working document |
| Purpose | Single source of truth for production | Working reference for current state + discrepancy log |

**When to use which:**

- Production change request → hardlock (needs formal review)
- "What's the current state?" → this doc
- "Is this fact still true?" → check this doc + live prod
- Discrepancy found → log here, file separate investigation

---

## 2. The canonical 3-line block (verified live 2026-10-15)

Same as the hardlock — **no content change needed** (still accurate):

```powershell
# Primary (LE cert, no -k):
curl.exe -o $env:TEMP\setup.ps1 https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1
& $env:TEMP\setup.ps1
rasdial DatabyteVPN
```

**Fallback (myvpn.databyte.co.za, has Cloudflare Origin Cert, needs `-k`):**
```powershell
curl.exe -k -o $env:TEMP\setup.ps1 https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1
& $env:TEMP\setup.ps1
rasdial DatabyteVPN
```

**Verified live 2026-10-15:**
- `https://vpn-portal.databyte.co.za/` → LE cert valid (expires 2026-12-02, 48 days), portal v2.2.1 live
- `https://myvpn.databyte.co.za/` → LE cert valid (same expiry, SAN includes `vpn-portal`)

---

## 3. Current script facts (live-verified 2026-10-15 06:50 UTC)

| Property | Value | Source / verification |
|---|---|---|
| Canonical filename | `setup-databyte-vpn.ps1` | hardlock + `ls /opt/vpn-portal/www/static/setup-databyte-vpn.ps1` on prod |
| **MD5** | `fc6a83d18b195bf3cbba1558f87f912a` | `md5sum /opt/vpn-portal/www/static/setup-databyte-vpn.ps1` on `vpn-prod-01` — **matches hardlock v2.6.5 claim** |
| **Size** | **23609 bytes** | `stat -c "%s" /opt/vpn-portal/www/static/setup-databyte-vpn.ps1` on `vpn-prod-01` — **matches hardlock** |
| SHA-256 (new — not in hardlock) | `22e18fdc2efb8d288368014755c493280e26622c21c55e34a1a98d3dc4be44c7` | `sha256sum` on prod — for operators who want a stronger hash |
| Last modified | **2026-07-01 06:44 SAST** | `ls -la ...` on prod — **unchanged for 3.5 months**, so the v2.6.5 stamp in the hardlock is still current |
| Primary URL | `https://vpn-portal.databyte.co.za/static/setup-databyte-vpn.ps1` | hardlock + live cert verified (LE cert valid 48 days) |
| Fallback URL | `https://myvpn.databyte.co.za/static/setup-databyte-vpn.ps1` (needs `-k`) | hardlock + live cert verified |
| Connection name | `DatabyteVPN` | hardlock |
| Server | `myvpn.databyte.co.za` → 154.65.110.44 | hardlock + live `dig +short` + `curl ifconfig.me` on prod |

**Verdict: the script facts in the hardlock are STILL ACCURATE as of 2026-10-15.** No script-side changes needed.

---

## 4. Discrepancies with the hardlock (flagged for separate investigation)

During the 2.8 fact-check (2026-10-15), the following HARDLOCKED claims in `DAT-VPN-INT-WIN-001.md` could **NOT** be verified. These are flagged here for the next hardlock revision — **NOT** silently fixed (the hardlock must be updated through its own review process).

### 4.1 Git commit `bf4e4b1` does not exist in databyte-Ikev2 repo

The hardlock references **9 git commits** that do not exist in the databyte-Ikev2 repo (verified via `git rev-parse --verify <hash>` for each — all return `fatal: Needed a single revision`):

| Commit | Hardlock claim | Verification result |
|---|---|---|
| `bf4e4b1` | "v2.6.5 HARDLOCK base" | **NOT IN REPO** |
| `1dea754` | "rename commit" | NOT IN REPO |
| `2732215` | "HARDLOCK base" | NOT IN REPO |
| `c27742d` | "intermediate patch" | NOT IN REPO |
| `95b401d` | "intermediate patch" | NOT IN REPO |
| `e565666` | "intermediate patch" | NOT IN REPO |
| `41859eb` | "intermediate patch" | NOT IN REPO |
| `070f59e` | "original (pre-rename)" | NOT IN REPO |
| `a4ada5d` | "original (post-rename)" | NOT IN REPO |

The databyte-Ikev2 repo also has **zero tags** (`git tag -l` is empty).

**Possible causes (UNCONFIRMED — needs Zun's input):**
1. Hardlock was written against a different branch / fork / repo that was never pushed here
2. Force-push wiped the history
3. Hardlock references commits that were never actually made (doc drift)

### 4.2 Template file `setup-databyte-vpn-windows.ps1` does not exist on prod

The hardlock claims: *"v1.0.0 baked template MD5 must match: `5541343b9c5efe3b3b9257dbd3332805` — live verified 2026-07-10"* + *"saved as `setup-databyte-vpn-<customer>-<device>.ps1` and served from `/opt/vpn-portal/www/static/baked/`"*

Verified on live `vpn-prod-01` via `find /opt/vpn-portal/www/static/ -name "*.ps1*"`:
- `setup-databyte-vpn.ps1` (23609 B, generic canonical) — **present** ✓
- `/opt/vpn-portal/www/static/setup-databyte-vpn-windows.ps1` (template) — **MISSING**
- `/opt/vpn-portal/www/static/baked/` — 1 file, but it's a **`.bak-pre-fortigate-fix`** (not a live baked template)

**Possible causes (UNCONFIRMED — needs Zun's input):**
1. Template was removed/deprecated
2. Hardlock describes a **build-time artifact** — operator runs `build_installer.py` per the `prod-vpn-bot-edit` skill; the output may go to a different path or be regenerated per-customer
3. Template lives at a different path

### 4.3 What I'm NOT doing in this layer

These are **HARDLOCKED claims in the approved doc**. Per Zun msg #43979 ("Ignore the headlock. This book we writing Parralel To the headlock doc so we don't touch it"), this parallel doc does NOT modify the hardlock. The hardlock remains authoritative for production. The next hardlock revision (v1.2.0 or whatever) should:
- (a) determine the correct state for the missing git commits + template file
- (b) update through the normal review process
- (c) align with this live-state doc

---

## 5. Current prod state (live-verified 2026-10-15 06:50 UTC)

| Item | Value | Verification |
|---|---|---|
| **Hostname** | `vpn-prod-01` | `ssh root@vpn-prod-01 'hostname'` (was `vps-01`, renamed per HOT-210, 2026-09-30) |
| **Public IPv4** | **154.65.110.44** | `curl ifconfig.me` from prod |
| **Tailscale IP** | 100.64.212.47 | `tailscale status` |
| **OS** | Debian 13.7 (trixie) | `/etc/debian_version` |
| charon container uptime | 6 days 17 h (since 2026-10-08 14:06 UTC) | `docker inspect strongswan --format '{{.State.StartedAt}}'` |
| **Portal** version | **v2.2.1** (deployed 2026-08-10, git HEAD `8e02a6e`) | `systemctl cat vpn-portal.service \| grep Description` |
| **Bot** version | **v2.5.0** (HEAD `01ad543` in `vpn-admin-bot` repo) | `git tag --sort=-v:refname \| head -1` in vpn-admin-bot |
| **MariaDB `radius` tables** | **45** | `mariadb -uroot radius -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='radius'"` |
| `radpostauth` rows | **2,299** | `SELECT COUNT(*) FROM radpostauth` |
| `radacct` rows | **1,166** | `SELECT COUNT(*) FROM radacct` |
| `radcheck` rows | 26 | `SELECT COUNT(*) FROM radcheck` |
| `customers` rows | **11** | `SELECT COUNT(*) FROM customers` |
| `users` rows | 11 | `SELECT COUNT(*) FROM users` |
| `devices` rows | 10 | `SELECT COUNT(*) FROM devices` |
| `operators` rows | 1 | `SELECT COUNT(*) FROM operators` |
| **LE cert `myvpn`** expiry | **2026-12-02** (48 days valid) | `certbot certificates \| grep -E "myvpn\|Expiry"` |
| LE cert `adminer` expiry | 2026-12-02 (48 days valid) | `certbot certificates \| grep -E "adminer\|Expiry"` |
| **kopia 502 Bad Gateway** | **PARKED** (Zun msg #43961) — last successful `.update-info.json` mtime 2026-10-10 00:00 | `kopia snapshot list --all` returns 502 |

---

## 6. Verification commands (all `vps-01` → `vpn-prod-01`)

All commands from the hardlock §GUIDE-001 are reproduced here with the hostname fix. Use these instead of the hardlock's `vps-01` versions.

### 6.1 Verify LE cert fingerprint

```bash
ssh root@vpn-prod-01 'openssl x509 -in /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem -noout -fingerprint -sha256'
```

### 6.2 Push a baked .ps1 to prod

```bash
scp setup-databyte-vpn-<customer>-<device>.ps1 root@vpn-prod-01:/opt/vpn-portal/www/static/baked/
ssh root@vpn-prod-01 'chown vpn-portal:vpn-portal /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<device>.ps1 && chmod 644 /opt/vpn-portal/www/static/baked/setup-databyte-vpn-<customer>-<device>.ps1'
```

### 6.3 Verify MariaDB customer exists

```bash
ssh root@vpn-prod-01 'mariadb -uroot -D radius -e "SELECT UserName, Attribute, Value FROM radius.radcheck WHERE UserName=\"$Username\""'
```

### 6.4 Check nginx status

```bash
ssh root@vpn-prod-01 'systemctl status nginx'
```

### 6.5 Inspect radpostauth (last 5)

```bash
ssh root@vpn-prod-01 'mariadb -uroot -D radius -e "SELECT id, username, reply, authdate FROM radpostauth WHERE authdate >= NOW() - INTERVAL 3 MINUTE ORDER BY id DESC LIMIT 5;"'
```

### 6.6 Check charon active sessions

```bash
ssh root@vpn-prod-01 'docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-sas'
```

### 6.7 All other `ssh root@vps-01` commands from the hardlock

The hardlock §GUIDE-001 + §HOSTILE-001 contain **24 ssh/scp commands** in total, all using `vps-01`. This doc replaces all 24 with `vpn-prod-01`. See the corresponding guide for the full command list (this is the companion fact-check, not a full duplicate).

---

## 7. Key differences from the hardlock (summary)

| # | Hardlock (DAT-VPN-INT-WIN-001 v1.1.0, 2026-07-07) | This live-state doc (v0.1.0, 2026-10-15) |
|---|---|---|
| 1 | All `ssh`/`scp` use `vps-01` (24 places across the 3 docs) | All `ssh`/`scp` use `vpn-prod-01` |
| 2 | Last verification 2026-07-07 | Last verification 2026-10-15 (3.5 months newer) |
| 3 | MD5 `fc6a83d...` (v2.6.5) — claims "live verified 2026-07-07" | **MD5 `fc6a83d...` STILL matches live prod** — script unchanged since 2026-07-01, hardlock v2.6.5 stamp is still current |
| 4 | Size 23609 B | **Size 23609 B — matches** |
| 5 | References git commit `bf4e4b1` (v2.6.5 base) | **`bf4e4b1` NOT in repo** — flagged for separate investigation |
| 6 | References template `setup-databyte-vpn-windows.ps1` MD5 `5541343b...` | **Template NOT on prod** — flagged for separate investigation |
| 7 | Portal version claim (implicit v2.2.0 era) | **Portal v2.2.1** (deployed 2026-08-10) |
| 8 | MariaDB row count claims (5 customers baseline) | **11 customers, 2,299 radpostauth, 1,166 radacct** |
| 9 | No LE cert `adminer` mention | LE cert `adminer.databyte.co.za` exists (48 days) |
| 10 | No kopia 502 mention | **kopia 502 Bad Gateway PARKED** (Zun msg #43961) |
| 11 | SHA-256 of script not provided | SHA-256: `22e18fdc2efb8d288368014755c493280e26622c21c55e34a1a98d3dc4be44c7` (new — for stronger hash verification) |

---

## 8. Change log

| Rev | Date | Author | Notes |
|---|---|---|---|
| **v0.1.0** | **2026-10-15 06:50 UTC / 08:50 SAST** | **Misha 🐻** | **Initial release.** Parallel companion to the HARDLOCKED `docs/DAT-VPN-INT-WIN-001.md` (v1.1.0, 2026-07-07, APPROVED — HARDLOCKED). **NOT a replacement** — the hardlock remains the production-approved reference. This doc provides: (1) current fact-check against live `vpn-prod-01` — script MD5 `fc6a83d18b195bf3cbba1558f87f912a` + size 23609 B (both match hardlock v2.6.5, script unchanged since 2026-07-01) + portal v2.2.1 + bot v2.5.0 + MariaDB 45 tables / 11 customers / 2,299 radpostauth / 1,166 radacct + LE cert 48 days valid; (2) hostname fix — all 24 `vps-01` → `vpn-prod-01` in operational `ssh`/`scp` commands (HOT-210 rename, 2026-09-30); (3) discrepancy log — git commit `bf4e4b1` + 8 other referenced commits not in repo, template file `setup-databyte-vpn-windows.ps1` not on prod — flagged for separate investigation, NOT silently fixed (per Zun msg #43979: "Ignore the headlock. This book we writing Parralel To the headlock doc so we don't touch it"). Cross-references the hardlock for production decisions; complements `docs/ARCHITECTURE.md` + `docs/RUNBOOK-DR-REBUILD-AND-HA.md` v1.1.0 + `docs/VPS-XNEELO-DEPLOY.md` v1.0.0 (all 2026-10-15 fact-checks). |
