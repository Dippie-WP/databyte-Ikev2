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
    ("1G", "L1-L4 testing plan",      "pytest (L1) + DB integrity (L2) + static analysis grep (L3) + E2E smoke cron (L4) — started 2026-06-24",            "🟡 Started",       "2026-07"),
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
     "Split config: customer ≤24h idle / ≤7d absolute. 30 min.", "🟠 High", "🔴 Open", "TODO #1"),
    ("2026-06-24 21:06", "Silent name matching users↔customers",
     "Portal maps users.name (EAP) to customers.name by stripping -laptop suffix. Brittle; rename = silent break.",
     "Add user_id FK column on customers. 1-2h + migration.", "🟡 Med", "🔴 Open", "TODO #2"),
    ("2026-06-24 21:06", "Tier label vs cap mismatch in customer portal",
     "tier display_name='Demo 100MB' but per-customer override data_limit_bytes=500 MiB. UX lie.",
     "Rename tier display_name OR drop override. 5 min.", "🟢 Low", "🔴 Open", "TODO #3"),
    ("2026-06-24 21:14", "Netflix (anti-VPN) won't stream through tunnel",
     "Xneelo ASN 37153 returns non-ZA CDN IPs (Dublin/Virginia/Oregon) from Netflix GeoDNS. Probably on Netflix's anti-VPN blocklist.",
     "NOT FIXABLE on our side. Document as known limitation in DAT-VPN-SOP-001 v1.0.4. Workaround: turn off VPN for streaming.", "ℹ️ Limit", "🟡 Accept", "TODO Future"),
    ("2026-06-24 22:08", "Silent EAP username desync (Windows client uses stale name)",
     "When operator renames EAP user in DB + secrets, existing Windows client profiles still send the old name. Charons 'no EAP key found' = silent auth fail. No re-onboarding prompt.",
     "Short-term: ops/rotate-vpn-credentials.py + manual client update. Long-term: portal_auth should detect rename + force token re-issue. ~1h code + test.", "🟠 High", "🔴 Open", "TODO #4"),
    ("2026-06-24 22:08", "VPS ↔ LXC 903 DB drift (portal UI is 3 days stale)",
     "Two ipsec.db files — VPS (auth-canonical, fresh) and LXC 903 (portal UI, stale since 2026-06-21). No sync mechanism (no cron, no systemd timer, no rsync). User sets diverged.",
     "Add one-way or two-way sync. E.g. LXC 903 → VPS via cron every 5min, or shared NFS mount. ~30min + tests.", "🟡 Med", "🔴 Open", "TODO #5"),
    ("2026-06-24 22:08", "Stale EAP key in rw-eap.conf (eap-demo-phone)",
     "Charon loaded 8 EAP secrets but DB only has 7 users. 'eap-demo-phone' has no matching user. Same drift as DB divergence.",
     "Cleaned up automatically next time ops/rotate-vpn-credentials.py regenerates secrets from DB. Add 'audit unused secrets' check to script.", "🟢 Low", "🔴 Open", "TODO #6"),
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
    # L1-L4 testing plan (high)
    ("L1 pytest integration tests (portal_auth, customer lifecycle, tokens, audit, strongswan sync)", "🔴 High", "2h",  "1G (testing plan 2026-06-24)", "Catches portal_auth login SQL bug retroactively (3-day delay)"),
    ("L2 DB integrity check script",                                                                 "🔴 High", "1h",  "1G",                                "orphaned devices, stale tokens, EAP↔DB consistency"),
    ("L3 static analysis grep (stale IPs in code)",                                                  "🔴 High", "30m", "1G",                                "Pre-commit + CI. Catches 102.182.117.43, 192.168.10.98 in prod"),
    ("L4 E2E smoke cron on LXC 903 (6h)",                                                            "🔴 High", "1h",  "1G",                                "Telegram alert on fail"),
    # CP7 (medium)
    ("CP7: fail2ban portal-login jail (3 retries → 24h ban)",                                        "🟡 Med",  "30m", "1F (CP7)",                           ""),
    ("CP7: AIDE integrity monitoring",                                                               "🟡 Med",  "1h",  "1F (CP7)",                           ""),
    ("CP7: backup /etc/vpn-portal.env + /etc/ssl/cloudflare/* to RustFS",                            "🟡 Med",  "30m", "1F (CP7)",                           ""),
    ("CP7: cert expiry monitor (15y CF Origin Cert — easy to forget)",                               "🟡 Med",  "15m", "1F (CP7)",                           ""),
    ("CP7: INPUT rule tightening (4502 from any→127.0.0.1; 10.99.0.0/24 off INPUT)",                 "🟡 Med",  "15m", "1F (CP7)",                           ""),
    ("CP7: iptables-nft consolidation (empty + policy ACCEPT → migrate)",                            "🟡 Med",  "1h",  "1F (CP7)",                           ""),
    # Other medium
    ("Per-tier bandwidth limits (replace flat 20/20)",                                               "🟡 Med",  "1h",  "1D post-cutover",                    "tier-based columns + JOIN in bandwidth-monitor"),
    ("ipBan service to VPS (currently only on LXC 903)",                                            "🟡 Med",  "30m", "1D post-cutover",                    ""),
    # Low / polish
    ("systemd RuntimeDirectoryMode duplicate key cleanup",                                           "🟢 Low",  "5m",  "polish",                             ""),
    ("CSP report-uri endpoint",                                                                      "🟢 Low",  "15m", "polish",                             ""),
    ("logrotate config for vpn-portal",                                                              "🟢 Low",  "15m", "polish",                             ""),
    ("DAT-VPN-CLIENT-WINDOWS-INSTALLER-001 SOP (formal customer doc)",                               "🟢 Low",  "30m", "docs",                               ""),
    ("nftables migration",                                                                           "🟢 Low",  "2-3h","deferred",                          "5B.6 watchdog fix covers bug nft would have prevented"),
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
