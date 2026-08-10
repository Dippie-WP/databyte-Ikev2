#!/usr/bin/env python3
"""
workspace_files_enumerator.py
-------------------------------
Enumerate files in an OpenClaw workspace for backup to RustFS (LAN S3-compatible).

Policy (2026-07-29, Zun directive msg #29460):
    - Back up ENTIRE workspace INCLUDING credentials, secrets, SSH keys,
      .mobileconfig, .env, .pfx, .p12, .demo_vpn_creds, memory/.dreams/, etc.

Excludes (regenerable bloat + cruft ONLY — no safety excludes):
  - Regenerable: .git, __pycache__, node_modules, dist, .cache, .next,
                 mempalace_env, reports/pdf-tool, reports/weather-beacon-versions
  - Cruft: *.bak-*, tmp.bak-*, http.bak-*, app.py.bak-v13pre, *.log, *.log.*
  - Corrupt: files with control chars in name

Output: newline-separated list of relative paths (suitable for
        `rclone copy --files-from`).

Usage:
    python3 workspace_files_enumerator.py /root/.openclaw/workspace
"""

import os
import sys
from pathlib import Path

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    "dist",
    ".cache",
    ".next",
    ".pioenvs",
    ".platformio",
    "build",
    ".esphome",
    "mempalace_env",
    "pdf-tool",
    "weather-beacon-versions",
    "tmp.bak-20260616-cruft",
    "http.bak-20260616-cruft",
}

# Filename patterns (substring match) to exclude
EXCLUDE_SUBSTRINGS = (
    ".bak-",  # catches app.py.bak-v13pre, *.bak-20260616
    ".log",
    "DS_Store",
    "Thumbs.db",
)


def has_control_chars(name: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)


def is_safe_filename(name: str) -> bool:
    if not name:
        return False
    if has_control_chars(name):
        return False
    if any(p in name for p in EXCLUDE_SUBSTRINGS):
        return False
    return True


def enumerate(workspace: Path) -> list[str]:
    """Walk workspace, return list of relative paths to include in backup."""
    kept: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        # Prune excluded directories
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if not is_safe_filename(name):
                continue
            full = Path(dirpath) / name
            try:
                rel = full.relative_to(workspace)
            except ValueError:
                continue
            kept.append(str(rel))
    return sorted(kept)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <workspace_dir>", file=sys.stderr)
        return 1
    workspace = Path(sys.argv[1])
    if not workspace.is_dir():
        print(f"ERROR: {workspace} is not a directory", file=sys.stderr)
        return 2
    files = enumerate(workspace)
    try:
        for f in files:
            print(f)
    except BrokenPipeError:
        # When piped to head/tail — fine, just exit
        sys.stderr.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
