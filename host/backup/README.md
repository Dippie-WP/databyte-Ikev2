# backup-workspace — Daily OpenClaw Workspace Backup to RustFS

Disaster-recovery backup of `~/.openclaw/workspace` to the LAN-attached
RustFS (S3-compatible) bucket.

## Why this exists

OpenClaw holds my long-term state (MEMORY.md, TOOLS.md, HEARTBEAT.md, daily
memory files, runbooks, project files, skills). Losing the workspace means
losing months of accumulated context. This is the same pattern as the
VPN portal backup, applied to the OpenClaw host itself.

## What's backed up

**Total: ~672 files, ~47 MB** (compressed at S3 level by RustFS).

| Category | Count | Notes |
|---|---|---|
| Top-level state | 30+ | MEMORY.md, TOOLS.md, SOUL.md, AGENTS.md, IDENTITY.md, USER.md, HEARTBEAT.md, DECISIONS.md, etc. |
| Daily memory | 100+ | `memory/YYYY-MM-DD.md` |
| Project files | varies | `projects/`, `docs/`, `runbooks/`, `scripts/` |
| Skills | 50+ | `skills/*.skill` |
| References | 7 | PDF + markdown research material |
| Dashboards | varies | Grafana dashboard JSON |

## What's NOT backed up (and why)

### Sensitive (NEVER backup)

| Pattern | Reason |
|---|---|
| `credentials/` | Telegram bot tokens |
| `.demo_vpn_creds` | VPN PSK for the lab |
| `*.mobileconfig` | VPN profiles (contain PSK / password) |
| `*.pfx`, `*.p12` | Private key bundles |
| `.env` | Secret env files |
| `**/id_rsa*`, `**/id_ed25519*` | SSH private keys (defensive) |
| `memory/.dreams/` | Old migrated agent memory; contains bot tokens |
| Any file containing `[0-9]{8,}:[A-Za-z0-9_-]{30,}` regex match | Catches Telegram bot tokens wherever they appear |

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
| `ops-tracker/node_modules/`, `ops-tracker-react/{node_modules,dist}/` | Build deps |
| `*.log`, `*.log.*` | Regen from running services |

### Cruft

| Pattern | Notes |
|---|---|
| `tmp.bak-*`, `http.bak-*` | Old backup attempts |
| `app.py.bak-v13pre` | Old backup of portal app |
| `zitadel-compose.bak-*` | Abandoned Zitadel experiment |
| Files with control chars in name | Workspace-root corruption remnants (`,\n    f`, etc.) |

## How it works

```
1. workspace_files_enumerator.py walks /root/.openclaw/workspace
2. Excludes dirs/files matching EXCLUDE_DIRS / SENSITIVE_NAMES / SENSITIVE_SUFFIXES
3. Scans text content (≤10 MB) for bot token regex
4. Outputs a list of safe relative paths
5. backup-workspace.sh does `rclone copy --files-from=<list>` to RustFS
6. Post-flight: spot-checks key files + re-scans for sensitive content
```

The `--files-from` approach (vs `--exclude` patterns) handles weird
filenames with control characters more robustly — they never get
enumerated in the first place.

## Install

```bash
# 1. Install script + enumerator
sudo install -m 0755 backup-workspace.sh /usr/local/bin/
sudo install -m 0755 workspace_files_enumerator.py /usr/local/bin/

# 2. Install systemd units
sudo install -m 0644 backup-workspace.service /etc/systemd/system/
sudo install -m 0644 backup-workspace.timer /etc/systemd/system/
sudo install -d -m 0755 /var/log/workspace-backup

# 3. Enable daily run
sudo systemctl daemon-reload
sudo systemctl enable --now backup-workspace.timer
```

## Verify

```bash
# Next scheduled run
systemctl list-timers backup-workspace.timer

# Manual one-shot
sudo systemctl start backup-workspace.service
sudo journalctl -u backup-workspace.service --no-pager

# List today's backup
rclone lsf rustfs:open-claw-push/workspace-backups/2026-06-23/ | head -10
rclone size rustfs:open-claw-push/workspace-backups/2026-06-23/
```

## Restore procedure

```bash
# Pull today's backup to a temp dir
mkdir -p /tmp/restore
rclone copy rustfs:open-claw-push/workspace-backups/2026-06-23/ /tmp/restore/

# Inspect (don't overwrite your live workspace blindly!)
ls /tmp/restore/
diff -r /tmp/restore/MEMORY.md ~/.openclaw/workspace/MEMORY.md

# If you want to RESTORE OVER existing workspace (destructive!):
#   1. Backup current workspace first (defensive)
#      rclone sync ~/.openclaw/workspace rustfs:open-claw-push/workspace-backups/_pre-restore-$(date -u +%Y-%m-%d)/
#   2. Restore
#      rclone sync /tmp/restore/ ~/.openclaw/workspace/
```

## Lessons

### #83 — Pre-backup audit caught 4 leaks

A naïve `rclone copy` with no exclusions would have backed up:
- 16 `*.mobileconfig` files (contain VPN PSK + EAP password)
- `.demo_vpn_creds` (VPN PSK)
- `credentials/telegram-tokens.md` (Telegram bot tokens)
- `memory/.dreams/short-term-recall.json.migrated` (Qwen bot token in old migrated memory)
- 3 script files with hardcoded Telegram bot tokens in `archives/` and `reports/`

A simple `rclone size` dry-run doesn't surface these — you have to **search
the source tree for known sensitive patterns** and verify your exclusion
list catches them all. The post-backup grep check (in the script) is the
last line of defense.

### #84 — `set -euo pipefail` + grep returns 1 = script exits 1

`grep` returns 1 when no match. With `pipefail`, the pipeline returns the
rightmost non-zero. Combined with `set -e`, an assignment like
`SENSITIVE_HITS=$(... | grep ...)` exits the script if grep finds nothing.

Fix: `SENSITIVE_HITS=$(... | grep ...) || true`. Always think about the
exit code of every command in a `set -e` script.

### #85 — `sed -i` creates `.duplicate-tmp` files on some systems

GNU sed (default) replaces in-place, but with a copy-then-rename under the
hood. If the rename fails (permissions, etc.), you can be left with a
`.duplicate-tmp` file alongside the original. Always clean these up
after `sed -i` operations on files with secrets.

### #86 — Content-based scanning catches what pattern-matching misses

Pattern-based exclusion (`*.mobileconfig`) is fast but rigid.
Content-based scanning (`grep -E "[0-9]{8,}:[A-Za-z0-9_-]{30,}"`) catches
tokens wherever they appear — even in JSON migration files, archives, or
unexpected code paths. The enumerator does BOTH: pattern-based for speed
+ a content scan for safety. The two-layer approach is more robust than
either alone.
