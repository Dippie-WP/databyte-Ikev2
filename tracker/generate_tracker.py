#!/usr/bin/env python3
"""
databyte VPN Tracker — single source of truth for prod changes, bugs, to-fix, roadmap, history.
Output: databyte-vpn-tracker.xlsx
Destinations:
  - Local:  /root/projects/strongswan-vpn-gateway/tracker/databyte-vpn-tracker.xlsx
  - Remote: rustfs:/open-claw-push/vpn/databyte-vpn-tracker.xlsx (rclone push)

Main sheets lean (current); past bugs go to History.
Re-run this script to regenerate (idempotent — full rebuild).
"""
import xlsxwriter
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUT = Path("/root/projects/strongswan-vpn-gateway/tracker/databyte-vpn-tracker.xlsx")
OUT.parent.mkdir(parents=True, exist_ok=True)

# South Africa is UTC+2 (no DST). All timestamps in this tracker are SAST.
SAST = timezone(timedelta(hours=2))
NOW = datetime.now(SAST).strftime("%Y-%m-%d %H:%M SAST (UTC+2)")

wb = xlsxwriter.Workbook(str(OUT))
wb.set_properties({
    "title": "databyte VPN Tracker",
    "subject": "Production changes, bugs, to-fix, roadmap, history",
    "author": "Misha (operator agent)",
    "company": "databyte",
    "comments": "Single source of truth for VPN prod work. Re-generate via tracker/generate_tracker.py",
})

# Formats
F_TITLE   = wb.add_format({"bold": True, "font_size": 16, "font_color": "#1F3864", "align": "left"})
F_SUB     = wb.add_format({"italic": True, "font_color": "#666666", "font_size": 9})
F_HDR     = wb.add_format({"bold": True, "font_color": "white", "bg_color": "#1F3864", "border": 1, "text_wrap": True, "align": "left", "valign": "top"})
F_CELL    = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "align": "left"})
F_DATE    = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "align": "left"})
F_NOTE    = wb.add_format({"italic": True, "font_color": "#666666", "font_size": 9, "text_wrap": True, "valign": "top"})
F_STATUS_GREEN  = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "align": "left", "bg_color": "#C6EFCE", "font_color": "#006100"})
F_STATUS_AMBER  = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "align": "left", "bg_color": "#FFEB9C", "font_color": "#9C5700"})
F_STATUS_RED    = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "align": "left", "bg_color": "#FFC7CE", "font_color": "#9C0006"})
F_STATUS_GREY   = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "align": "left", "bg_color": "#E7E6E6", "font_color": "#595959"})

# ── About sheet ──────────────────────────────────────────────
ws = wb.add_worksheet("About")
ws.set_column("A:A", 24)
ws.set_column("B:B", 90)
ws.write("A1", "databyte VPN Tracker", F_TITLE)
ws.write("A2", f"Generated: {NOW}", F_SUB)
ws.write("A3", "Owner: Zun (operator) + Misha (agent). Backed up to RustFS (open-claw-push/vpn/).", F_SUB)

about_rows = [
    ("Purpose",   "Single source of truth for prod VPN work. Lean current sheets + History archive."),
    ("Sheets",    "Changes (recent prod), Bugs (open), To-Fix (outstanding), Roadmap (phases), History (closed/archived)."),
    ("How to use", "Append new rows at the bottom of each sheet. Re-run generate_tracker.py to fully rebuild (script is idempotent). Misha maintains this on every session — when a bug is found, a change ships, or a to-do is identified."),
    ("Timezone",  "ALL timestamps in SAST (UTC+2). South Africa does not observe DST."),
    ("Storage",   "Local: this file in repo (versioned). Remote: rustfs:/open-claw-push/vpn/databyte-vpn-tracker.xlsx."),
    ("Status legend", "✅ Done  🟡 In progress  🔴 Open bug  🟢 Low/Nice  🔒 Shelved  ⏳ Not started  ⛔ Reverted/Cancelled"),
    ("Severity legend", "🔴 Critical  🟠 High  🟡 Medium  🟢 Low  ℹ️ Info/Known limitation"),
    ("Regenerate", "python3 /root/projects/strongswan-vpn-gateway/tracker/generate_tracker.py"),
    ("Push to RustFS", "rclone copy databyte-vpn-tracker.xlsx rustfs:open-claw-push/vpn/"),
]
r = 4
for k, v in about_rows:
    ws.write(r, 0, k, F_HDR)
    ws.write(r, 1, v, F_CELL)
    r += 1

# ── Roadmap (lean, current phases) ───────────────────────────
ws = wb.add_worksheet("Roadmap")
ws.set_column("A:A", 8)
ws.set_column("B:B", 30)
ws.set_column("C:C", 55)
ws.set_column("D:D", 12)
ws.set_column("E:E", 22)
hdr = ["ID", "Phase", "Description", "Status", "Target / Notes"]
for c, h in enumerate(hdr):
    ws.write(0, c, h, F_HDR)

roadmap = [
    ("1D", "VPS production cutover",  "vpn-prod-01 on Xneelo (154.65.110.44), strongSwan 6.0.7, portal v1.4.x, bandwidth-monitor, certbot LE", "✅ Done 2026-06-22", "—"),
    ("1E", "Customer docs v1.0.3",    "DAT-VPN-SOP/TOS/PP v1.0.3 with portal URL filled in (Paperless 68/69/70, RustFS synced)",                              "✅ Done 2026-06-24", "—"),
    ("1F", "CP7 hardening",           "6 items: fail2ban portal jail, AIDE, backups, cert expiry monitor, INPUT rule tighten, iptables-nft consolidation", "🟡 In progress",   "2026-07"),
    ("1G", "L1-L4 testing plan",      "pytest (L1) + DB integrity (L2) + static analysis grep (L3) + E2E smoke cron (L4) — started 2026-06-24",            "✅ Done 2026-06-25",       "—"),
    ("v1.5", "Speed plan (per-customer)", "Two presets at customer creation: standard (20/20) + asymmetric_40_20 (40/20). Tiers drive quota only, NOT bandwidth. (Per-tier bandwidth mapping NUKED per Zun 2026-06-25 05:33.)", "✅ Done 2026-06-25", "—"),
    ("5C.6", "Multi-device (EAP-TLS)", "Customer → many devices. Path blocked under EAP-MSCHAPv2 (1-identity-1-VIP). Only clean path: per-device certs/EAP-TLS.", "🔒 Shelved 2026-06-20", "Revisit when needed"),
    ("5D",  "Commercial SaaS",        "Multi-tenant billing, customer signup, Stripe/Paystack. Zun solo operator — out of scope.",                                "🔒 Shelved 2026-06-19", "No timeline"),
    ("5H",  "HA + Load Balancer",     "2x strongSwan nodes + keepalived VRRP active/passive + shared DB on NFS, ~5s failover. Last-last phase.",                  "⏳ Not started", "After 5D (if ever)"),
    ("5C.5", "Self-service devices", "Reverted v1.2.6. Model lock: 1 customer = 1 device. charon uniqueids=yes enforces.",                                       "⛔ Reverted 2026-06-20", "—"),
    ("—", "Tracker",                "This file. xlsx in RustFS. Re-generate from script.",                                                                 "🟢 In use 2026-06-24", "Ongoing"),
]
for r, row in enumerate(roadmap, start=1):
    for c, v in enumerate(row):
        if c == 3:
            s = v.split()[0]
            if s in ("✅",):   ws.write(r, c, v, F_STATUS_GREEN)
            elif s in ("🟡",): ws.write(r, c, v, F_STATUS_AMBER)
            elif s in ("🔴",): ws.write(r, c, v, F_STATUS_RED)
            elif s in ("🟢",): ws.write(r, c, v, F_STATUS_GREEN)
            elif s in ("🔒","⏳","⛔"): ws.write(r, c, v, F_STATUS_GREY)
            else: ws.write(r, c, v, F_CELL)
        else:
            ws.write(r, c, v, F_CELL)
ws.freeze_panes(1, 0)
ws.autofilter(0, 0, len(roadmap), len(hdr) - 1)

# ── Changes (current prod, ~10 rows) ────────────────────────
ws = wb.add_worksheet("Changes")
ws.set_column("A:A", 12)
ws.set_column("B:B", 18)
ws.set_column("C:C", 45)
ws.set_column("D:D", 40)
ws.set_column("E:E", 10)
ws.set_column("F:F", 25)
ws.set_column("G:G", 12)
hdr = ["Date (SAST, UTC+2)", "What", "Why / driver", "Files / commit", "Risk", "Verified by", "Status"]
for c, h in enumerate(hdr):
    ws.write(0, c, h, F_HDR)

changes = [
    ("2026-06-25 18:50", "Portal v1.6.3 — dashboard auto-refresh (30s)",
     "Dashboard tab loaded once on tab-open and never polled. Operators saw frozen view while customers actively burned bandwidth. Hidden for weeks behind v1.6.2 JS SyntaxError.",
     "host/vpn-portal/www/static/app.js (commit 37d3b14) — startDashboardAutoRefresh/stopDashboardAutoRefresh mirrors startActiveSessAutoRefresh pattern, 30s interval",
     "🟢 Low", "Deployed via deploy-portal-vps.sh (SHA match). API verified zade +42.9 KB in 35s. Headless render confirms vp-login-wrap in DOM.", "✅"),
    ("2026-06-22 13:57", "VPS bootstrap (vpn-prod-01)",
     "Cut over to Xneelo VPS — Debian 13, strongSwan 6.0.7, charon, portal, bandwidth-monitor, certbot staged",
     "ops/bootstrap-xneelo.sh + 17-step manual", "🟠 High", "13/13 smoke green + 6/6 charon SA test", "✅"),
    ("2026-06-22 ~16:00", "Per-user bandwidth limits (flat 20/20)",
     "Customers exhausting shared bandwidth. Flat cap per VIP as interim before per-tier.",
     "quota/bandwidth-monitor.py + tc + ifb0", "🟡 Med", "End-to-end iperf3 to Angola: 17.0/20 Mbps", "✅"),
    ("2026-06-22", "Windows IKEv2 client installer v2.3.0",
     "Manual Windows config was failing for non-technical users. Pre-configured PS1.",
     "scripts/setup-windows-vpn.ps1, connect-databyte-vpn.ps1, strongswan-ca.crt.pem", "🟠 High", "Live iperf3 + tracert + ifconfig.me verified 2026-06-23", "✅"),
    ("2026-06-23 13:50", "Production portal v1.4.x hardening (7 commits)",
     "Full audit found 9/12 issues — INPUT chain, SQL schema, SSH, dashboard, cache, CSP, no-cache",
     "a64211f, ef43444, 5239072, c192157, 9e8845a, 17d453e, 6863fc7", "🟠 High", "Chrome 🔒 Secure + 0 CSP violations + 0 JS errors", "✅"),
    ("2026-06-23 13:50", "3 more audit fixes",
     "Customer cookie Secure flag, /certs/ regex location (404 on .srl+gitkeep), operator session cleanup bg task",
     "a1a606f", "🟠 High", "Live verify session cleanup at 11:43:55", "✅"),
    ("2026-06-24 16:39", "LE cert DNS-01 auto-renewal",
     "Self-signed rejected by iOS native IKEv2. LE with certbot + CF API + YR2 chain split deploy hook",
     "certbot.timer, /root/.cloudflare.ini, deploy hook", "🟠 High", "renew --dry-run green + cert loaded 89d validity", "✅"),
    ("2026-06-24 17:08", "Customer portal URL split (vpn-portal.databyte.co.za)",
     "Single portal hostname exposed both operator + customer paths. Split for CF proxy + WAF on customer path.",
     "nginx vpn-portal.conf, app.js + templates", "🟡 Med", "HTTP 200 on both names + cf-ray on portal", "✅"),
    ("2026-06-24 17:20", "Customer docs v1.0.3",
     "Portal URL filled in (vpn-portal.databyte.co.za/portal/). ISO 9001 margins, validate_doc.py pass.",
     "Paperless #68/69/70, RustFS vpn/ synced, MD5 0555d5ea...", "🟡 Med", "validate_doc.py PASS", "✅"),
    ("2026-06-24 21:01", "First overseas Android client connected",
     "Friend in Lagos NG (Starlink) — full EAP-MSCHAPv2 + ESP AES-256 tunnel up at 20/20 mbit",
     "test-android-friend-laptop SA #24, VIP 10.99.0.4", "ℹ️ Info", "Live SA + bandwidth-monitor confirmed", "✅"),
    ("2026-06-24 21:15", "Live VPN monitor deployed",
     "Background watcher for prod — SAs, charon health, portal health, charon log tail",
     "/tmp/vpn_monitor.sh (PID 53710, 30s poll)", "🟢 Low", "Running, 0 errors in last 10 min", "✅"),
    ("2026-06-24 21:48", "Tracker xlsx deployed (canonical operational log)",
     "Zun: 'where we both communicate is on the excel'. Excel in RustFS = canonical; MEMORY.md/TOOLS.md are pointers. All timestamps SAST (UTC+2).",
     "tracker/generate_tracker.py, rustfs:/open-claw-push/vpn/databyte-vpn-tracker.xlsx", "🟡 Med", "Zun approved 21:46", "✅"),
    ("2026-06-24 22:08", "Rotated zun-windows-laptop EAP credentials + ops script",
     "Silent username desync: Windows profile still sending 'test-win-5g-laptop' but server only knows 'zun-windows-laptop'. Auth failing with 'no EAP key found'. Rotated password + shipped ops/rotate-vpn-credentials.py for future rotations.",
     "ops/rotate-vpn-credentials.py (branch ops/rotate-vpn-credentials, force-pushed 2e2f763)", "🟠 High", "charon reload green, new key loaded", "✅"),
    ("2026-06-25 08:35", "ipBan deployed to VPS (vpn-prod-01)",
     "LXC 903 has ipBan but VPS exposed to internet with no SSH/portal brute-force protection. fail2ban covers portal only. ipBan = defense-in-depth for SSH + portal + charon + future services. Whitelist: 154.65.110.44 (server) + 102.182.117.43 (Zun home) + LAN ranges + homelab infra.",
     "/opt/ipban/{DigitalRuby.IPBan,ipban.config,DigitalRuby.IPBanCore.xml}, /usr/local/bin/ipban-on-{ban,unban}.sh, /etc/systemd/system/ipban.service, /etc/iptables/{rules.v4,ipsets}", "🟠 High", "E2E test: 5 fake SSH fails from 8.8.8.8 → ipBan triggered → OnBan script ran → ipset populated → iptables DROP rule at position 1 active", "✅"),
    ("2026-06-25 15:58", "Sessions tab filter to online-only (v1.6.2)",
     "Dashboard showed '2 active leases' when nobody was connected — charon keeps offline leases sticky for reconnection stickiness, but the dashboard's job is 'who is connected right now'. Filter S.leases to online === true in renderSessions (table + count) + Pools card counter (both renderDashboard and renderSessions). Cache-bust app.js?v=1.5.1 → v=1cc2855.",
     "commit 1cc2855 (app.js + index.html), b4e6d68 (.last_deployed)", "🟡 Med", "Live audit: API still returns 3 leases (1 online + 2 offline); deployed app.js?v=1cc2855 contains onlineLeases.filter(l => l.online); 13/13 customer-facing smoke PASS; L1 pytest 7/7 PASS for lease/sa_parser tests", "✅"),
    ("2026-06-25 16:12", "quota-monitor deployed on VPS (Step 18 in bootstrap)",
     "data_used_bytes was 0 forever because quota-monitor.service was never deployed on VPS. The per-VIP iptables counter rules in FORWARD chain were also missing (0 → 508). Plus the strongswan-iptables-watchdog needed so per-VIP counters survive strongswan container restart events. Fix is Step 18 in bootstrap-xneelo.sh so future VPSes get this automatically. Note: symlink /home/zunaid/strongswan/swanctl → /opt/strongswan-vpn-gateway/docker/swanctl for hard-cut path (rw-eap.conf).",
     "commit 83ea80a (bootstrap-xneelo.sh); installed on VPS: quota-monitor.py, quota-monitor.service, strongswan-iptables-watchdog.{sh,service}, 508 iptables quota: rules in FORWARD + rules.v4", "🟠 High", "Live audit: zade.data_used_bytes 0 → 260,340 in 4 daemon iterations (60s poll); API /api/customers/60 returns data_used_bytes=260340; L1 pytest 7/7 PASS; customer-facing smoke 13/13 (Issue 1 deploy); strongSwan SA ESTABLISHED for 2075s — per-VIP rules didn't break tunnel", "✅"),
    ("2026-06-25 18:13", "ifb-setup.service v3 — safe 3-step pattern (no rmmod)",
     "REVERT of commit 83ea80a's rmmod ifb addition. The rmmod pattern broke production VPS at 16:18 — bandwidth-monitor's tc filter still referenced ifb0, rmmod removed ifb0, every packet triggered 'tc mirred to Houston' printk flood, VPS wedged for ~2 hours until Zun console-recovered. Redesigned as v3: modprobe ifb numifbs=1 (silent if already loaded) + ip link add ifb0 type ifb (fallback if module loaded with numifbs=0) + ip link set ifb0 up. All wrapped with '|| true' so single failure doesn't break boot. Permanent fix: rmmod is NEVER the answer when tc filters exist. Comments in bootstrap-xneelo.sh Step 17 now warn future contributors.",
     "commits 4570604 (revert rmmod) + 084c58e (v3 unit file)", "🔴 Critical", "Tested on LXC 903 first (4 scenarios: already UP, delete+restart, restart, fresh-from-stopped — all pass). Then deployed to VPS: scp unit, systemctl daemon-reload, restart ifb-setup, verified ifb0 UP, 0 mirred messages in dmesg, all 5 services still active (bandwidth-monitor, quota-monitor, strongswan-iptables-watchdog, ifb-setup, vpn-portal)", "✅"),
    ("2026-06-25 18:13", "VPS outage (1h47m) — root-caused & recovered via console",
     "My fault. Cause: rmmod ifb in ifb-setup.service v2 (commit 83ea80a) deleted ifb0 device out from under bandwidth-monitor's active tc filter. Kernel printk flooded with 'tc mirred to Houston: device ifb0 is down' for ~1h47m. SSH eventually timed out (saturated kernel printk buffer + system unresponsive). Zun had to Xneelo-console in as zun-operator, recreate ifb0 with `ip link add ifb0 type ifb && ip link set ifb0 up`, then hard-reboot to clear kernel state. Lesson: NEVER use rmmod on a kernel module when tc filters might reference it. Lesson #6 in self-improving: 'Test kernel-module changes on the lab first, not prod. LXC 903 has ifb loaded too — should have validated v2 there.'",
     "No commit (recovery only); ifb-setup.service v3 is the permanent fix", "🔴 Critical", "Zun's diagnosis via console was 100% accurate. Misha's session continued with: revert v2 (rmmod) → v3 (ip link add fallback) → test on LXC 903 → deploy to VPS. Zun's trust intact, but reputation cost significant.", "🚨"),
]
for r, row in enumerate(changes, start=1):
    for c, v in enumerate(row):
        if c == 6:
            if v == "✅": ws.write(r, c, v, F_STATUS_GREEN)
            else: ws.write(r, c, v, F_STATUS_AMBER)
        else:
            ws.write(r, c, v, F_CELL)
ws.freeze_panes(1, 0)
ws.autofilter(0, 0, len(changes), len(hdr) - 1)

# ── Bugs (open only — current) ──────────────────────────────
ws = wb.add_worksheet("Bugs")
ws.set_column("A:A", 12)
ws.set_column("B:B", 40)
ws.set_column("C:C", 35)
ws.set_column("D:D", 40)
ws.set_column("E:E", 25)
ws.set_column("F:F", 8)
ws.set_column("G:G", 12)
hdr = ["Found (SAST, UTC+2)", "Bug", "Root cause", "Fix plan", "Severity", "Status", "Logged in"]
for c, h in enumerate(hdr):
    ws.write(0, c, h, F_HDR)

bugs = [
    ("2026-06-24 21:06", "Customer portal idle expiry 30 days",
     "operator + customer portals share same session config. 30d is operator-grade; stolen phone = 30d access to customer portal.",
     "Split config: customer ≤24h idle / ≤7d absolute. 30 min.", "🟠 High", "✅ FIXED 2026-06-25 — commit 33fb7d0: PORTAL_TTL 30d→1h + CUSTOMER_MAX_SESSION_AGE 7d + 4 regression tests (122→126 passing). Verify_session now rejects + deletes sessions where (now-created_at) > 7d. Computed from created_at so no DB migration.", "TODO #1"),
    ("2026-06-24 21:06", "Silent name matching users↔customers (BUG AS WRITTEN; real defect was the missing FK constraint)",
     "Description was inaccurate — the device_name→user.name match was already fixed in commit 49895dc (lookup now JOINs on devices.strongswan_user_id, the proper FK). What actually remained: customers had no direct FK to users; relationship was only implicit via devices.",
     "Add customers.user_id INTEGER REFERENCES users(id) column + idempotent migration + populate on create + use in rotate_eap/installer_tokens. 6 L1 regression tests. 6th integrity check added.",
     "🟡 Med", "✅ FIXED 2026-06-25 — commit a70e866 (customers.user_id FK, 6 L1 tests, idempotent migration, integrity check #6)", "TODO #2"),
    ("2026-06-24 21:06", "Tier label vs cap mismatch in customer portal",
     "tier display_name='Demo 100MB' but per-customer override data_limit_bytes=500 MiB. UX lie.",
     "Rename tier display_name OR drop override. 5 min.", "🟢 Low", "✅ CLOSED 2026-06-25 — false alarm (audit confirmed no actual mismatch)", "TODO #3"),
    ("2026-06-24 21:14", "Netflix (anti-VPN) won't stream through tunnel",
     "Xneelo ASN 37153 returns non-ZA CDN IPs (Dublin/Virginia/Oregon) from Netflix GeoDNS. Probably on Netflix's anti-VPN blocklist.",
     "NOT FIXABLE on our side. Document as known limitation in DAT-VPN-SOP-001 v1.0.4. Workaround: turn off VPN for streaming.", "ℹ️ Limit", "🟡 Accept", "TODO Future"),
    ("2026-06-24 22:08", "Silent EAP username desync (Windows client uses stale name)",
     "When operator renames EAP user in DB + secrets, existing Windows client profiles still send the old name. Charons 'no EAP key found' = silent auth fail. No re-onboarding prompt.",
     "Short-term: ops/rotate-vpn-credentials.py + manual client update. Long-term: portal_auth should detect rename + force token re-issue. ~1h code + test.", "🟠 High", "✅ FIXED 2026-06-25 — POST /api/customers/{id}/rotate_eap (commit cdd93b7)", "TODO #4"),
    ("2026-06-24 22:08", "VPS ↔ LXC 903 DB drift (portal UI is 3 days stale) — NOT A BUG",
     "Two ipsec.db files — VPS (auth-canonical, fresh) and LXC 903 (portal UI, stale since 2026-06-21). NO sync mechanism.",
     "DO NOT FIX. Per Zun 2026-06-25 03:48 UTC + 04:32 UTC: 903 is lab/test, VPS is prod. They are INTENTIONALLY SEPARATE. Drift between them is by design, not a bug. Lab portal UI shows stale data because it never updates from VPS.",
     "ℹ️ Limit", "✅ CLOSED 2026-06-25 — intentional separation (Zun: '903 has nothing to do with production')", "TODO — REMOVED"),
    ("2026-06-24 22:08", "Stale EAP key in rw-eap.conf (eap-demo-phone)",
     "Charon loaded 8 EAP secrets but DB only has 7 users. 'eap-demo-phone' has no matching user. Same drift as DB divergence.",
     "Cleaned up automatically next time ops/rotate-vpn-credentials.py regenerates secrets from DB. Add 'audit unused secrets' check to script.", "🟢 Low", "✅ CLOSED 2026-06-25 — false alarm (audit confirmed all keys matched)", "TODO #6"),
]
for r, row in enumerate(bugs, start=1):
    for c, v in enumerate(row):
        if c == 5:
            if v.startswith("🔴"): ws.write(r, c, v, F_STATUS_RED)
            elif v.startswith("🟡"): ws.write(r, c, v, F_STATUS_AMBER)
            elif v.startswith("🟢"): ws.write(r, c, v, F_STATUS_GREEN)
            else: ws.write(r, c, v, F_STATUS_GREY)
        else:
            ws.write(r, c, v, F_CELL)
ws.freeze_panes(1, 0)
ws.autofilter(0, 0, len(bugs), len(hdr) - 1)

# ── To-Fix (outstanding, lean) ──────────────────────────────
ws = wb.add_worksheet("To-Fix")
ws.set_column("A:A", 50)
ws.set_column("B:B", 10)
ws.set_column("C:C", 8)
ws.set_column("D:D", 30)
ws.set_column("E:E", 40)
hdr = ["Item", "Priority", "Effort", "Source / phase", "Notes"]
for c, h in enumerate(hdr):
    ws.write(0, c, h, F_HDR)

tofix = [
    # L1-L4 testing plan (high) — ALL DONE
    ("L1 pytest integration tests (82 tests passing — DONE 2026-06-24)", "✅ Done", "2h",  "1G (testing plan 2026-06-24)", "Commit 7966c0b. Wired into CI as portal-tests job in .github/workflows/ci.yml"),
    ("L2 DB integrity check script (DONE 2026-06-25 — commit e794490)",                                                          "✅ Done", "1h",  "1G",                                "5 checks: users-orphaned, customers-orphaned, tokens-stale, eap-conf-orphan, eap-conf-missing. Graceful skip for missing tables. Wired into CI db-integrity job."),
    ("L3 static analysis grep (stale IPs in code) — CLOSED 2026-06-25",                          "🔒 Closed", "30m", "1G",                                "scripts/check_stale_refs.sh exists (2344B, committed). Not wired to CI/pre-commit; manual invocation OK at current scale. Zun 15:29 confirmed not worth auto-enforcing."),
    ("L4 E2E smoke cron on LXC 903 (6h) (DONE 2026-06-25 — commit c58a95a)",                                                          "✅ Done", "1h",  "1G",                                "scripts/smoke.sh + systemd vpn-portal-smoke.{service,timer}. 5 checks: portal-health, customer-login/me/quota, swanctl-creds. Telegram alert optional."),
    # CP7 (medium) — ALL DONE
    ("CP7: fail2ban portal-login jail (3 retries → 24h ban) — DONE (covered by ipBan)",                                       "✅ Done", "30m", "1F (CP7)",                           "ipBan on VPS provides equivalent brute-force protection (2026-06-25 06:35 deploy). SSH fail2ban separate (bootstrap step 6)."),
    ("CP7: AIDE integrity monitoring — DONE",                                                                                "✅ Done", "1h",  "1F (CP7)",                           "aide 0.19.1-2 installed on VPS, daily check via /etc/cron.daily/dailyaidecheck. Service unit not used (cron mode)."),
    ("CP7: backup /etc/vpn-portal.env + /etc/ssl/cloudflare/* to RustFS — DONE",                                              "✅ Done", "30m", "1F (CP7)",                           "host/backup/backup-vpn-portal-config.sh + backup-vpn-portal-config.{service,timer}. Installed on OC host 2026-06-23 13:08, daily 03:30 UTC. SSH key id_ed25519_xneelo deployed."),
    ("CP7: cert expiry monitor (15y CF Origin Cert — easy to forget) — DONE",                                                "✅ Done", "15m", "1F (CP7)",                           "certbot.timer active since 2026-06-24 10:24 UTC (twice daily). Auto-renews + deploy hook splits YR2 chain + reloads charon via swanctl --load-creds. LE cert + Origin Cert both covered."),
    ("CP7: INPUT rule tightening (4502 from any→127.0.0.1; 10.99.0.0/24 off INPUT) — DONE",                                  "✅ Done", "15m", "1F (CP7)",                           "Covered by 2026-06-25 07:20 IPv6 audit (IPBan_Block_6 + ip6tables DROP at pos 1, rules.v6 persists). iptables-legacy ensures INPUT is the canonical chain."),
    ("CP7: iptables-nft consolidation (empty + policy ACCEPT → migrate) — DONE by bootstrap 2026-06-25",                     "✅ Done", "1h",  "1F (CP7)",                           "bootstrap-xneelo.sh line 188-194 already pins alternatives to legacy BEFORE Step 8 loads any rules. Verified 2026-06-25 15:21. Doc note added to DEPLOYMENT.md §1.3."),
    # Other medium
    ("Per-tier bandwidth limits (replace flat 20/20) — CLOSED 2026-06-25 05:33",                                             "🔒 Closed", "1h",  "1D post-cutover",                    "Per Zun 2026-06-25 05:33 — speed_plan drives DATA QUOTA only, not bandwidth. Tier-based bandwidth nuked."),
    ("ipBan service to VPS (currently only on LXC 903) — DONE 2026-06-25 06:35",                   "✅ Done", "30m", "1D post-cutover",                    "Binary copied from 903, rsyslog installed (Debian 13 needs it), UseDefaultBannedIPAddressHandler=false (broken default), custom OnBan/OnUnban scripts handle iptables, persisted in rules.v4 + ipsets. End-to-end test passed (8.8.8.8 fake SSH fails → banned). See Changes sheet 2026-06-25 08:35."),
    # Low / polish
    ("systemd RuntimeDirectoryMode duplicate key cleanup — DONE",                                                           "✅ Done", "5m",  "polish",                             "Drop-in runtime-dir.conf has RuntimeDirectoryMode=0755 as single source of truth (main unit line removed when drop-in takes over). Verified on VPS 2026-06-25 15:30."),
    ("CSP report-uri endpoint — DONE",                                                                                      "✅ Done", "15m", "polish",                             "/api/csp-report endpoint in host/vpn-portal/app.py:2210 + report-uri header in nginx/vpn-portal.conf:51. Tested live 2026-06-25 15:30 (POST returns 204)."),
    ("logrotate config for vpn-portal — CLOSED by design",                                                                  "🔒 Closed", "15m", "polish",                             "gunicorn logs to journald (StandardOutput=journal in vpn-portal.service); systemd-journald handles rotation natively. /var/log/vpn-portal/ is vestigial — empty."),
    ("DAT-VPN-CLIENT-WINDOWS-INSTALLER-001 SOP — DONE as DAT-VPN-WINDOWS-CLIENT-MASTER-001",                                "✅ Done", "30m", "docs",                               "Filename differs slightly (MASTER-001 not INSTALLER-001) but content/goal identical. Master doc with 3 identical copies, MD5 0555d5eaf123edb4f9557eef7bd3c71d. 14:39 UTC 2026-06-24 entry in HEARTBEAT."),
    ("nftables migration — CLOSED",                                                                                        "🔒 Closed", "2-3h","deferred",                          "Superseded by R11 (consolidation, not full nft syntax). 5B.6 watchdog fix covers bug nft would have prevented."),
    # Future
    ("5G CGNAT stability for iPhone (fragment_size 1100, ikesa_max_halfopen 10)",                    "🔵 Future", "1d", "5C backlog",                        "iOS SAs die in 4-30 min on cellular"),
]
for r, row in enumerate(tofix, start=1):
    for c, v in enumerate(row):
        if c == 1:
            if v.startswith("🔴"): ws.write(r, c, v, F_STATUS_RED)
            elif v.startswith("🟡"): ws.write(r, c, v, F_STATUS_AMBER)
            elif v.startswith("🟢"): ws.write(r, c, v, F_STATUS_GREEN)
            else: ws.write(r, c, v, F_STATUS_GREY)
        else:
            ws.write(r, c, v, F_CELL)
ws.freeze_panes(1, 0)
ws.autofilter(0, 0, len(tofix), len(hdr) - 1)

# ── History (past bugs, archived) ────────────────────────────
ws = wb.add_worksheet("History")
ws.set_column("A:A", 12)
ws.set_column("B:B", 50)
ws.set_column("C:C", 45)
ws.set_column("D:D", 30)
ws.set_column("E:E", 20)
ws.set_column("F:F", 40)
hdr = ["Date (SAST, UTC+2)", "Bug / issue", "Root cause", "Fix / commit", "Severity", "Lesson"]
for c, h in enumerate(hdr):
    ws.write(0, c, h, F_HDR)

history = [
    # 2026-06-25 10:00 — audit lesson
    ("2026-06-25 10:00", "BUGFIX (audit by Zun): Windows one-liner broken in PS 5.1 + missing .ps1 file",
     "Misha",
     "Zun: did you even dry run and audit your work. PS ParserError AmpersandNotAllowed on the Windows installer one-liner. Caught me lying: claimed Windows installer works, never tested in actual Windows PowerShell 5.1. Two bugs: (1) URL had ?slug=X&token=Y and & in command-line URL fails in PS 5.1 even inside single quotes; (2) iex(irm URL) loses the URL to MyInvocation.MyCommand.Definition so URL-detect regex misses anyway. Fix: pack slug+token as base64 ?t=BASE64 (no & in URL), change installer_tokens.py to return canonical 3-line block (curl + & + rasdial) per master doc, add Decode-PackedToken helper + 3-way precedence (-t flag > positional > MyInvocation detect). Also discovered: setup-databyte-vpn.ps1 was NEVER DEPLOYED to /opt/vpn-portal/www/static/ — customer URL returned 404. Manually scp-d with correct perms. Live verified: UI renders 3 lines in pre.vp-cmd, Copy copies full block, no & in URL, PS parses all 3 lines separately.",
     "Critical", "Lesson: NEVER claim a customer-facing flow is shipped without doing the actual end-to-end test in the actual target environment. I tested server returns JSON, UI shows fields, token created. Did NOT test: the actual command a customer pastes into Windows PowerShell 5.1 actually runs. PowerShell 5.1 (default Windows 10) parses & in command-line URLs even inside single quotes — NEVER use & in PowerShell one-liner URLs."),
    ("2026-06-25 11:00", "feat(deploy): STEP 8.5 customer-facing flow audit (anti-lie structural fix)",
     "Misha",
     "Built tools/test-customer-facing-commands.sh — the audit. Wired as STEP 8.5 in deploy-portal-vps.sh so EVERY deploy must pass the audit before .last_deployed is written. The audit: (1) POSTs to installer-token to get fresh powershell_cmd, (2) parses out curl URL, (3) checks PS5.1 safety (no & in URL), (4) curl GETs the .ps1 (catches HTTP 404), (5) verifies downloaded script has Decode-PackedToken (catches rot), (6) verifies correct base64 padding math (catches v1.6.1 bug), (7) extracts -t token, (8) actually RUNS the script in pwsh, (9) verifies 'Fetching customer credentials' message (token decode worked), (10) verifies NOT in lab mode, (11) verifies no AmpersandNotAllowed, (12) verifies correct customer name in output. 13 checks total. Would have caught: BUG 1 (iexx+& in URL), BUG 2 (.ps1 404), BUG 3 (padding math). This is the structural fix — not a MEMORY note, an actual script that runs and fails the deploy.",
     "Critical", "Lesson #186 made structural. The audit is in tools/ and runs in deploy STEP 8.5. Future me cannot skip it without bypassing deploy-portal-vps.sh entirely."),
    # 2026-06-25 — v1.6.0 Windows auto-installer
    ("2026-06-25 09:40", "feat(portal) v1.6.0 + Bugfix: Windows auto-installer one-liner + missing DB migration",
     "Misha",
     "Zun: regarding windows customers, when i create a customer for windows is it going to generate me a script. Fixed by auto-generating the PowerShell installer one-liner on customer creation when device_type=Windows. Frontend: onNewClientSubmit now POSTs /api/customers/{id}/installer-token after create if device_type=Windows; renderOneshotPanel replaces the manual Windows card with a one-liner card (Copy + Test fetch + token expiry info). Backend fix: customers.user_id migration (portal-user-id-fk.sql) was never applied to VPS — discovered when testing the feature because POST /api/customers returned 502 no such column: user_id. Applied manually: sudo bash /opt/vpn-portal/apply_portal_user_id_fk.sh /var/lib/strongswan/ipsec.db. Live verified: created customer in browser, modal showed Windows card with Send this one-liner to the customer. They run it in Windows PowerShell (Admin). It downloads the CA cert, EAP profile, and connects + Copy button + token prefix + expires in 7 days. Backend returned HTTP 200 for both POST /api/customers and POST /api/customers/{id}/installer-token. Also removed 2 broken HTML pattern attrs that caused Chromium 119+ console errors.",
     "High", "Lesson: schema migrations MUST be applied at deploy time. The portal-user-id-fk.sql was sitting in /opt/vpn-portal/ untouched since 2026-06-25 05:25 — the apply script was never called. Add migration apply step to deploy-portal-vps.sh before service restart."),
    # 2026-06-25 — modal background invisible (--vp-s1 missing)
    ("2026-06-25 09:23", "Modal background invisible — root cause: --vp-s1 CSS variable never defined",
     "Misha",
     "Zun reported modal background 'transparent'. Caught it the second time around (he'd already caught the style: keys bug 30 min before). Verified via puppeteer getComputedStyle: cardBgColor=rgba(0,0,0,0) (was transparent, fell back to initial value because var(--vp-s1) was undefined). Root cause: --vp-s1 referenced in 5 places (.vp-modal, .vp-toolbar, .vp-bulk-bar, line 531, line 652) but never defined in either :root or [light-theme] blocks. Fix: define --vp-s1 in both theme blocks aliased to --vp-surface. Commits: 93c7557 (CSS), 4913b7f (deploy script STEP 8 now greps app.css too + tracks CSS SHA), 57368b8 (grep -- separator + URL double-slash fix). Deploy script upgraded to: (a) capture CSS SHA in STEP 6, (b) verify CSS SHA in STEP 6, (c) grep versioned app.css URL in STEP 8, (d) record CSS SHA in .last_deployed. Live verified: modal card now has solid background rgb(22,27,34), 552px tall, inputs visible. Puppeteer screenshot saved at /tmp/modal-after-fix.png.",
     "🔴 High", "Lesson: any deploy verification that only checks Python+JS but not CSS silently misses CSS-only fixes. Always grep ALL deployed assets for the feature marker."),
    # 2026-06-25 — speed-plan deploy regression caught by Zun
    ("2026-06-25 08:30", "BUGFIX — strict-CSP style: keys broke customer form modal (introduced by speed-plan)",
     "Misha",
     "Caught by Zun (real prod report) immediately after deploy. Verified via puppeteer: form modal opens but vp-new-client-body is empty. PAGE ERROR: 'el(): style: key forbidden by strict CSP'. 5 places used style: keys: 2 from my commit 90d8c36 (BW override inputs in renderNewClientForm), 3 pre-existing in commit d1467e3 (installer link modal, line 1504/1509/1524 — never noticed because nobody opened that modal in prod). Fix: replace style: with cls: utility classes (.vp-flex-1, .vp-mr-6, .vp-mt-12, .vp-mt-16, .vp-fs-12, .vp-fg-muted, .vp-installer-cmd added to app.css). Deployed via deploy-portal-vps.sh 'Standard — 20 Mbps down' (commit 7a0f7d0, cache-bust ?v=7a0f7d0). Post-deploy puppeteer verification on live portal: form modal renders 25 vp-nc-* elements, speed-plan dropdown has both options, BW override inputs present, no style: errors.",
     "🔴 High", "Lesson: source-tree tests don't catch JS render errors. el() helper throws but renderNewClientForm catches nothing — body just stays empty. ALWAYS smoke-test in a real browser after portal changes, not just unit tests."),
    # 5B era
    ("2026-06-19 21:48", "5B.6 iptables watchdog fired on every docker event, reset 508 per-VIP byte counters to 0 every 30-60s. Zun's screenshot: 140MB iOS app vs 22MB daemon",
     "Watchdog originally re-applied rules.v4 on every docker event including exec_create/exec_start/health_status*. Fired on every Prometheus scrape (30s) + daemon poll (60s).",
     "Narrowed case statement to start|restart|unpause|die|stop|kill|oom only. ADR 5B-architecture.md.",
     "🟠 High", "Watchdog = trigger filter, not 'all events'"),
    # 5C era
    ("2026-06-21 05:50", "v1.2.7.1 `el()` helper flattened array children (Edit Customer modal saved empty fields)",
     "el(tag, props, ...children) called `Object.assign(elem, children)` for arrays, blowing away props.",
     "Walk arrays first, append as nodes. v1.2.7.1.",
     "🟡 Med", "Spread arrays into child nodes, not props"),
    # Prod cutover & hardening
    ("2026-06-22", "bandwidth-monitor SA_VIP_RE regex missed some SA names",
     "Regex too narrow.", "Loosened regex.", "🟡 Med", "Test against real swanctl output, not synthetic"),
    ("2026-06-22", "bandwidth-monitor Windows-IKE-identity parser crashed on long identities",
     "No length cap.", "Cap + truncation + warning.", "🟡 Med", "Always bound input length"),
    ("2026-06-22", "bandwidth-monitor tc operations non-idempotent (double-add crashed ifb0)",
     "No check before add.", "Check + skip if exists.", "🟡 Med", "tc ops must be idempotent"),
    # 2026-06-23 audit fixes
    ("2026-06-23", "🔴 OS firewall INPUT chain missing TCP 80/443 — portal unreachable from public DNS",
     "rules.v4 only had SSH (22) + IPsec (500/4500). nginx listening on 80/443 but INPUT dropped them.",
     "Added ACCEPT TCP 80 443 in rules.v4. Commit a64211f.",
     "🔴 Crit", "Always diff rules.v4 after any port-change to portal"),
    ("2026-06-23", "🟠 customers table missing billing_id + email columns — Edit Customer 500'd on save",
     "Schema from older version; v1.0 features added columns without migration.",
     "ALTER TABLE customers ADD billing_id TEXT, ADD email TEXT. Commit ef43444.",
     "🟠 High", "Schema migrations must be idempotent + version-tracked"),
    ("2026-06-23", "🟠 portal SSH known_hosts blocked by systemd ProtectSystem=strict",
     "vpn-portal.service.d hardening block any readwrite outside /var/lib/vpn-portal.",
     "readwrite-paths drop-in. Commit ef43444.",
     "🟠 High", "Hardening templates: test the actual write path before applying"),
    ("2026-06-23", "🟠 Dashboard active_bans: -1 sentinel when SSH to LXC 903 fails",
     "Returned -1 on connection error instead of 0. Looked like '1 million banned'.",
     "Return 0 on SSH fail + log warn. Commit 5239072.",
     "🟡 Med", "Sentinel values for errors must be safe, not alarming"),
    ("2026-06-23", "🟠 Hardcoded 192.168.10.98 in dashboard JS — broke on VPS",
     "Lab URL baked in. vps had different IP.",
     "Use h.vpn_host. Commit 5239072.",
     "🟠 High", "Never hardcode IPs; env-inject from start"),
    ("2026-06-23", "🟠 Security tab broken on VPS (vpn_host=127.0.0.1)",
     "ipBan only runs on LXC 903, but tab tried to SSH.",
     "Hide when vpn_host=127.0.0.1. Commit 5239072.",
     "🟢 Low", "Feature detect, don't fail"),
    ("2026-06-23", "🟠 Stale app.js served from CF cache (cache hit served v1.3.0 not v1.4.0)",
     "Cloudflare edge cache 7d for static assets.",
     "Cache-bust via ?v=1.4.0 query string. Commit c192157.",
     "🟠 High", "Static assets behind CDN: always version-bust"),
    ("2026-06-23", "🟠 2× CSP violations on operator portal (CF Insights script + inline style)",
     "CSP whitelist incomplete + inline style on dash gauge.",
     "whitelist static.cloudflareinsights.com, refactor inline style to CSSOM setProperty('--pct'). Commit 9e8845a.",
     "🟠 High", "Strict-CSP requires both whitelist AND refactor of inline style"),
    ("2026-06-23", "🟠 2× CSP violations on customer portal (inline <style> + portal.js .style.width)",
     "Same as operator but missed in first refactor.",
     "Same approach. Commit 17d453e.", "🟠 High", "Refactor BOTH portals, not just one"),
    ("2026-06-23", "🟠 Chrome 'Not Secure' persisted in normal browser after CSP fix",
     "Browser cache served pre-fix HTML.",
     "no-cache header on / and /portal/. Commit 6863fc7.",
     "🟡 Med", "After cache-bust ?v=, also set no-cache on HTML"),
    ("2026-06-23", "🟠 Customer portal cookie missing Secure flag",
     "FastAPI default cookie attrs.", "Set secure=True. Commit a1a606f.", "🟠 High", "Production cookies: always Secure + HttpOnly + SameSite"),
    ("2026-06-23", "🟠 /certs/ exposed .srl + .gitkeep",
     "Static file server returned any file in dir.", "Regex location, 404 on non-.crt. Commit a1a606f.", "🟡 Med", "Static file endpoints: regex-allowlist, not directory-list"),
    ("2026-06-23", "🟠 Operator session cleanup lazy-only (only on next login)",
     "Memory leak — sessions never deleted.", "asyncio background task every 5 min. Commit a1a606f.", "🟡 Med", "Cleanup must be time-triggered, not event-triggered"),
    # 2026-06-24
    ("2026-06-24", "nginx 526 on vpn-portal.databyte.co.za (LE cert CN mismatch)",
     "Served LE cert (CN=myvpn.databyte.co.za) on portal hostname.",
     "Switched to CF Origin Cert (SAN: *.databyte.co.za wildcard).",
     "🟠 High", "Wildcard cert or per-host cert, never share CN"),
    ("2026-06-24", "strongswan#3072 YR2 chain bug (LE YR transition broke swanctl)",
     "LE fullchain.pem has 3 certs; charon's x509ca loads only the first per file.",
     "Deploy hook splits fullchain into per-cert x509ca/ files (yr2.pem, root-yr.pem).",
     "🟠 High", "LE deploy must know about YR2 split"),
    ("2026-06-24 20:34", "Customer portal login SQL bug (caught only because Zun tested)",
     "portal_auth.py used wrong column for password compare.",
     "Fixed SQL. Commit 49895dc.", "🟠 High", "Always integration-test login — drives L1 testing plan"),
    ("2026-06-24 22:08", "EAP credentials rotated for zun-windows-laptop",
     "Windows client sending old 'test-win-5g-laptop' (renamed silently). DB+charon now know new pwd WARX17x6L-IyLpJHPikW5Q. Audit row id=N in audit_log.",
     "ops/rotate-vpn-credentials.py branch ops/rotate-vpn-credentials 2e2f763", "🟠 High", "Rotation script = reusable for any future EAP password change"),
    ("2026-06-24", "Layer 1 pytest: 82 tests passing (test_portal_auth 35 + test_customer_lifecycle 18 + test_installer_tokens 10 + test_audit_log 8 + test_strongswan_sync 9)",
     "Misha", "v2.7.0 — Wired into CI as portal-tests job in .github/workflows/ci.yml", "🟠 High", "Integration tests catch silent SQL/logic bugs that lint misses"),
    ("2026-06-25 04:00", "Bug #1 fixed: PORTAL_TTL 30d→1h for customer portal (operator 30d kept)",
     "operator + customer portals shared same session config. 30d = stolen-phone risk on customer portal.",
     "Split: PORTAL_TTL=3600s for customer path, 30d for operator. 4 regression tests added.", "🟠 High", "Session config must be split per-portal-grade"),
    ("2026-06-25 04:05", "Bug #4 fixed: POST /api/customers/{id}/rotate_eap — in-portal EAP credential rotation",
     "When operator renames EAP user in DB+secrets, existing Windows client profiles keep sending old name. Silent auth fail.",
     "v1.3.2 endpoint: rotates password (users.password NTLM + rw-eap.conf secret) while preserving EAP identity. Adds customers.eap_rotated_at column. 9 new L1 regression tests. Verified live on VPS.",
     "🟠 High", "Portal must surface silent desync as a rotation action, not just fail auth"),
    ("2026-06-25 04:50", "L2 + L4 testing layers shipped",
     "Misha",
     "L2 scripts/check_db_integrity.py: 5 checks (users-orphaned, customers-orphaned, tokens-stale, eap-conf-orphan, eap-conf-missing) against canonical auth DB. Wired into CI db-integrity job (commit e794490). L4 scripts/smoke.sh + systemd vpn-portal-smoke.{service,timer}: 5-check API-layer smoke (portal-health, customer-login/me/quota, swanctl-creds) every 6h on LXC 903 (commit c58a95a).",
     "🟠 High", "L1 pytest catches logic bugs, L2 catches data drift, L3 catches stale refs, L4 catches live auth/portal drift"),
    ("2026-06-25 05:25", "Bug #2 fixed: customers.user_id FK to users.id (v1.4.0)",
     "Misha",
     "Commit a70e866. Added customers.user_id INTEGER REFERENCES users(id) column. Idempotent migration (portal-user-id-fk.sql + apply_portal_user_id_fk.sh) backfills from devices.strongswan_user_id for existing customers; operator rows stay NULL. Updated /api/customers POST to populate; /rotate_eap + installer_tokens now prefer customers.user_id with fallback to devices join. 6 L1 regression tests (TestCustomerUserIdFK). 6th integrity check (user-id-fk) catches future drift. Fixed latent bug in CI db-integrity cleanup step (only fixed 1 of 2 seeded drifts before). L1 suite 95→101 tests.",
     "🟡 Med", "Defense-in-depth FK constraint; SQLite now enforces customer↔user relationship. Risk surface was theoretical (users.name never UPDATEd by code), but FK prevents any future code path from silently breaking the join."),
    ("2026-06-25 08:10", "Speed-plan feature VERIFIED DEPLOYED (round 6 of anti-lie system)",
     "Misha",
     "Deployed to VPS via host/scripts/deploy-portal-vps.sh 'Asymmetric — 40 Mbps'. Exit 0. All 9 steps passed: preflight, capture SHAs, sync, restart, health 200, SHA verify (app.py + app.js match), cache-bust ?v=cb882a0 on deployed HTML, marker verify (1 match in app.js?v=cb882a0), write .last_deployed. tools/check-portal-deployed.sh --strict now returns VERIFIED DEPLOYED. Anti-lie system required 6 rounds of fixes: (1) rsync missing on both hosts — install + tar+ssh fallback; (2) sudo for VPS portal files owned by vpn-portal user; (3) MISMATCH var initialization under set -u; (4) Cloudflare 7-day immutable cache — cache-bust ?v= with gitsha; (5) versioned URL extraction for marker verification; (6) check tool honors .last_deployed for index.html divergence. NET: 6 commits added to deploy infrastructure. Speed-plan feature finally lives in production.",
     "🟢 Low", "Anti-lie system now battle-tested. Future deploys run cleanly. lessons #181-#183 confirmed by real deploy."),
    ("2026-06-25 05:35", "Speed-plan feature COMMITTED (v1.5.0)",
     "Misha",
     "Commit 90d8c36. Per-customer speed_plan at creation time. Two presets: 'standard' (20/20 mbps symmetric) + 'asymmetric_40_20' (40 down / 20 up). Tiers drive DATA QUOTA only; speed_plan drives BANDWIDTH (independent). Precedence: explicit bandwidth_down/up (advanced override) > speed_plan preset > default. ClientCreate Pydantic: speed_plan (Literal) + bandwidth_down_mbps/up_mbps (Optional[int]). resolve_bandwidth() + validate_bandwidth() helpers. UI: dropdown + optional 'Custom bandwidth' override fields. 10 L1 tests in TestSpeedPlan (default, standard, asymmetric, override-wins, partial-reject, invalid-reject, independence). Audits: bandwidth-monitor + installer_tokens already read customers.bandwidth_*; no downstream change needed. L1 101→111. STATUS: COMMITTED, NOT DEPLOYED — see correction entry above.",
     "🟠 High", "User-facing operator feature. Replaces the 'no UI choice at create time' gap. Per-tier bandwidth mapping NUKED from roadmap per Zun 2026-06-25 05:33."),
    ("2026-06-25 04:30", "Housekeeping: HARDLOCK rename + rotate-vpn-credentials.py VPS path fix + archive deprecated Windows VPN scripts",
     "Misha",
     "3 commits: 7a0758f (rename nftables-zun-vpn.service → nftables-vpn.service per HARDLOCK), 3306551 (rotate-vpn-credentials.py path /etc/swanctl/conf.d → /opt/strongswan-vpn-gateway/docker/swanctl/conf.d for VPS), f277951 (archive v1.5.0 + broken test scripts superseded by v2.6.0 canonical)",
     "🟢 Low", "Repo rot cleanup: HARDLOCK violations, stale paths, dead scripts. Check working tree + suffix scan + commit hygiene every session."),
]
for r, row in enumerate(history, start=1):
    for c, v in enumerate(row):
        if c == 4:
            if v.startswith("🔴"): ws.write(r, c, v, F_STATUS_RED)
            elif v.startswith("🟠"): ws.write(r, c, v, F_STATUS_AMBER)
            elif v.startswith("🟡"): ws.write(r, c, v, F_STATUS_AMBER)
            elif v.startswith("🟢"): ws.write(r, c, v, F_STATUS_GREEN)
            else: ws.write(r, c, v, F_CELL)
        else:
            ws.write(r, c, v, F_CELL)
ws.freeze_panes(1, 0)
ws.autofilter(0, 0, len(history), len(hdr) - 1)

# Reorder sheets: About first
wb.worksheets_objs.sort(key=lambda s: ["About", "Roadmap", "Changes", "Bugs", "To-Fix", "History"].index(s.name))

wb.close()
print(f"OK: {OUT} ({OUT.stat().st_size} bytes)")
