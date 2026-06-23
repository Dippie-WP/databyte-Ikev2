#!/usr/bin/env python3
"""
workspace_files_enumerator.py
-------------------------------
Enumerate files in an OpenClaw workspace that are SAFE to back up to
RustFS (LAN S3-compatible storage). Excludes:
  - Sensitive (NEVER backup): credentials, .demo_vpn_creds, *.mobileconfig,
                              .env, *.pfx, *.p12, **/id_rsa*, **/id_ed25519*
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
import re
import sys
from pathlib import Path

# ---- Configuration ----
SENSITIVE_NAMES = {
    ".demo_vpn_creds",
    ".env",
}

SENSITIVE_SUFFIXES = {
    ".mobileconfig",
    ".pfx",
    ".p12",
}

EXCLUDE_DIRS = {
    "credentials",
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
    # memory/.dreams/ — old migrated agent memory, contains bot tokens
    # (caught by content scan too, but excluded at dir level for safety)
    ".dreams",
}

# Filename patterns (substring match) to exclude
EXCLUDE_SUBSTRINGS = (
    ".bak-",  # catches app.py.bak-v13pre, *.bak-20260616
    ".log",
    "DS_Store",
    "Thumbs.db",
)

# SSH key basenames (defensive — none in workspace, but cover)
SSH_KEY_BASENAMES = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_xmss",
)

# Telegram bot tokens look like: 8-10 digits, colon, then 35 chars of [A-Za-z0-9_-]
TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b[0-9]{8,}:[A-Za-z0-9_-]{30,}\b")

# Files larger than this are not scanned for content (perf)
MAX_CONTENT_SCAN_BYTES = 10_000_000  # 10 MB


def has_sensitive_content(path: Path) -> bool:
    """Return True if file contains patterns that look like Telegram bot tokens.

    Scans text files (any size up to MAX_CONTENT_SCAN_BYTES). Skips binary.
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size > MAX_CONTENT_SCAN_BYTES:
            return False
        # Read as bytes; skip if too many non-text bytes
        with path.open("rb") as f:
            data = f.read()
        if b"\x00" in data:
            # likely binary
            return False
        text = data.decode("utf-8", errors="ignore")
        return bool(TELEGRAM_BOT_TOKEN_RE.search(text))
    except (OSError, PermissionError):
        return False


def enumerate(workspace: Path) -> list[str]:
    """Walk workspace, return list of relative paths to safe files."""
    kept: list[str] = []
    excluded_for_content: list[str] = []
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
            rel_str = str(rel)
            # Content scan: skip files that contain bot tokens
            if has_sensitive_content(full):
                excluded_for_content.append(rel_str)
                continue
            kept.append(rel_str)
    if excluded_for_content:
        print(
            f"# Excluded {len(excluded_for_content)} files for sensitive content (bot tokens):",
            file=sys.stderr,
        )
        for f in excluded_for_content:
            print(f"#   {f}", file=sys.stderr)
    return sorted(kept)

def has_control_chars(name: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)


def is_safe_filename(name: str) -> bool:
    if not name:
        return False
    if name in SENSITIVE_NAMES:
        return False
    if has_control_chars(name):
        return False
    if any(name.endswith(s) for s in SENSITIVE_SUFFIXES):
        return False
    if any(p in name for p in EXCLUDE_SUBSTRINGS):
        return False
    # SSH keys: only if file starts with id_rsa* / id_ed25519* etc.
    if any(name.startswith(k) for k in SSH_KEY_BASENAMES):
        return False
    return True


def enumerate(workspace: Path) -> list[str]:
    """Walk workspace, return list of relative paths to safe files."""
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
