# backup-workspace — OpenClaw Workspace Backup to RustFS

Disaster-recovery backup of `~/.openclaw/workspace` to the LAN-attached
RustFS (S3-compatible) bucket.

## Policy (current as of 2026-07-29, msg #29460)

- **3x daily** at 06:00, 12:00, 22:00 SAST (= 04:00, 10:00, 20:00 UTC).
- **Single rolling destination** — always keep latest, overwrite old.
- **Entire workspace, including credentials** — `credentials/`,
  `.env`, `*.mobileconfig`, `*.pfx`/`*.p12`, `**/id_rsa*`/`**/id_ed25519*`,
  `memory/.dreams/`, and `.demo_vpn_creds` are all backed up.

## Why this exists

OpenClaw holds my long-term state (MEMORY.md, TOOLS.md, HEARTBEAT.md, daily
memory files, runbooks, project files, skills, credentials, secrets). Losing
the workspace means losing months of accumulated context. This is the same
pattern as the VPN portal backup, applied to the OpenClaw host itself.

## What's backed up

**Total:** entire workspace minus regenerable bloat + cruft.

| Category | Count (approx) | Notes |
|---|---|---|
| Top-level state | 30+ | MEMORY.md, TOOLS.md, SOUL.md, AGENTS.md, IDENTITY.md, USER.md, HEARTBEAT.md, DECISIONS.md, openclaw.json |
| Daily memory | 100+ | `memory/YYYY-MM-DD.md` |
| Project files | varies | `projects/`, `docs/`, `scripts/` |
| Skills | 50+ | `skills/*.skill` |
| References | 7 | PDF + markdown research material |
| Dashboards | varies | Grafana dashboard JSON |
| **Credentials** | varies | `credentials/`, `.env`, `*.mobileconfig`, SSH keys, secrets |

## What's NOT backed up (and why)

### Regenerable (not source-of-truth)

| Pattern | Why |
|---|---|
| `.git/` | Version control, regenerable from remote |
| `**/__pycache__/` | Python bytecode |
| `**/node_modules/` | Node modules |
| `**/dist/` | Build artifacts |
| `mempalace_env/` (365 MB) | Python venv, regenerable with `pip install -r requirements.txt` |
| `reports/pdf-tool/` (54 MB) | Old PDF binaries |
| `reports/weather-beacon-versions/` (187 MB) | Old versioned binaries |
| `*.log`, `*.log.*` | Regen from running services |

### Cruft

| Pattern | Notes |
|---|---|
| `tmp.bak-*`, `http.bak-*` | Old backup attempts |
| `app.py.bak-v13pre` | Old backup of portal app |
| Files with control chars in name | Workspace-root corruption remnants |

## Schedule

```
OnCalendar=*-*-* 04,10,20:00:00 UTC   # 3x daily, rolling
```

3x daily at 06:00, 12:00, 22:00 SAST. Single rolling destination.

## How it works

```
1. workspace_files_enumerator.py walks /root/.openclaw/workspace
2. Excludes dirs/files matching EXCLUDE_DIRS / EXCLUDE_SUBSTRINGS
3. Outputs a sorted list of relative paths
4. backup-workspace.sh does `rclone copy --files-from=<list>` to RustFS
5. Post-flight: spot-checks key state files + openclaw.json + .gitignore
```

The `--files-from` approach (vs `--exclude` patterns) handles weird
filenames with control characters more robustly — they never get
enumerated in the first place.

## Destination

```
rustfs:open-claw-push/workspace-backups/   (FIXED, rolling)
```

No dated subfolders. Each run overwrites the previous. Latest wins.

## Install

```bash
# 1. Install script + enumerator
sudo install -m 0755 backup-workspace.sh /usr/local/bin/
sudo install -m 0755 workspace_files_enumerator.py /usr/local/bin/

# 2. Install systemd units
sudo install -m 0644 backup-workspace.service /etc/systemd/system/
sudo install -m 0644 backup-workspace.timer /etc/systemd/system/
sudo install -d -m 0755 /var/log/workspace-backup

# 3. Enable + start
sudo systemctl daemon-reload
sudo systemctl enable --now backup-workspace.timer
```

## Verify

```bash
# Next scheduled runs (should show 3 daily)
systemctl list-timers backup-workspace.timer

# Manual one-shot (logs to journald)
sudo systemctl start backup-workspace.service
sudo journalctl -u backup-workspace.service --no-pager

# Inspect current rolling backup
rclone lsf rustfs:open-claw-push/workspace-backups/ | head -10
rclone size rustfs:open-claw-push/workspace-backups/
```

## Restore procedure

```bash
# Pull current snapshot to a temp dir
mkdir -p /tmp/restore
rclone copy rustfs:open-claw-push/workspace-backups/ /tmp/restore/

# Inspect (don't overwrite your live workspace blindly!)
ls /tmp/restore/
diff -r /tmp/restore/MEMORY.md ~/.openclaw/workspace/MEMORY.md

# If you want to RESTORE OVER existing workspace (destructive!):
#   1. Defensive backup of current state FIRST
#      rclone sync ~/.openclaw/workspace rustfs:open-claw-push/workspace-backups/_pre-restore-$(date -u +%Y-%m-%d)/
#   2. Restore
#      rclone sync /tmp/restore/ ~/.openclaw/workspace/
```

## Lessons (historical)

### #83 — Pre-backup audit caught 4 leaks

A naïve `rclone copy` with no exclusions would have backed up:
- 16 `*.mobileconfig` files (contain VPN PSK + EAP password)
- `.demo_vpn_creds` (VPN PSK)
- `credentials/telegram-tokens.md` (Telegram bot tokens)
- `memory/.dreams/short-term-recall.json.migrated` (Qwen bot token in old migrated memory)
- 3 script files with hardcoded Telegram bot tokens in `archives/` and `reports/`

These were EXCLUDED from the backup as a SAFETY measure.

**Reversed 2026-07-29** per Zun directive msg #29460 — credentials are now
INCLUDED in the backup. The regenerable bloat + cruft excludes remain.

### #86 — Content-based scanning was a defense layer

The two-layer safety (pattern-based + content-scan) was robust, but
no longer needed: Zun has decided the entire workspace, including all
secrets, belongs in the RustFS rolling backup.

---

**Change log:**
- 2026-07-29 09:39 UTC — Zun directive (msg #29460): 3x daily, full workspace + credentials, rolling.
- 2026-06-27 — Initial policy: daily 04:00 UTC, credentials excluded.
