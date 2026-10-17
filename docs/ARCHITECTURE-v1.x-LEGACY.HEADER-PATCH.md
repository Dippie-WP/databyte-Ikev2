# Patch — apply to `docs/ARCHITECTURE-v1.x-LEGACY.md` after `git mv`

After you do `git mv docs/ARCHITECTURE.md docs/ARCHITECTURE-v1.x-LEGACY.md`, replace the existing top-of-file block (the giant "⚠ Live deployment context..." callout, the "🟢 Verified-live 2026-10-15" table, AND the "Key architecture shifts since the v1.x design" table) with this single line:

```markdown
# Architecture (v1.x LXC 903 lab — LEGACY, preserved for historical reference)

> ⚠ **DO NOT use as a guide for current prod state.** This document preserves the v1.x LXC 903 lab architecture as it was designed and deployed (192.168.10.98, pve2, iptables-legacy, SQLite-at-charon). For the current production architecture (Xneelo VPS, MariaDB, nftables, FreeRADIUS, v2.2.1 portal + v2.5.0 bot), see [docs/ARCHITECTURE.md](ARCHITECTURE.md).
>
> Lab (LXC 903 @ 192.168.10.98) and prod (VPS @ 154.65.110.44) are intentionally separate — no sync, never will be.

---

## Original v1.x LXC 903 lab design (legacy body, preserved verbatim below this line)
```

Everything else in the file (the "Network topology" diagram, "What runs where", "Data flow", "Why these specific design choices", "Quota layer" — i.e. the 29K of legacy content) stays unchanged. You're just relabeling the file and pointing readers to ARCHITECTURE.md for current state.

---

## How to land the whole change as one PR

```bash
cd ~/path/to/databyte-Ikev2

# 1. Replace the new files I drafted
cp /root/.openclaw/workspace/databyte-ikev2-doc-redraft/README.md        README.md
cp /root/.openclaw/workspace/databyte-ikev2-doc-redraft/docs/ARCHITECTURE.md   docs/ARCHITECTURE.md

# 2. Rename the legacy file
git mv docs/ARCHITECTURE.md docs/ARCHITECTURE-v1.x-LEGACY.md

# 3. Apply the header patch to the legacy file
#    (open it, replace the giant live-state + shifts table at the top with the snippet above)

# 4. Also fix the GitHub About section (UI only, no file change):
#    github.com → Dippie-WP/databyte-Ikev2 → ⚙ next to About
#    Description: "Self-hosted IKEv2 VPN stack — strongSwan + FreeRADIUS + FastAPI portal + Telegram bot. Production: vpn-prod-01 (Xneelo VPS), Debian 13."
#    Topics: ikev2, strongswan, freeradius, mariadb, vpn, self-hosted
#    Website: https://myvpn.databyte.co.za

# 5. Verify + push
git diff --stat
git add -A
git commit -m "docs: split ARCHITECTURE.md into current (prod) + v1.x-LEGACY; tighten README to landing page"
git push origin HEAD
```

5 changes, one PR, all live-verified.
