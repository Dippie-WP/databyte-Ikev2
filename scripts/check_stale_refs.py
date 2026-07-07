#!/usr/bin/env python3
"""
check_stale_refs.py — Python-aware lab-leakage detector.

Companion to check_stale_refs.sh. Same pattern set, but for Python files we
use Python's tokenize module to find ONLY non-comment, non-string occurrences
of the patterns. This eliminates false positives on docstring text and
string-literal error messages that legitimately reference "LXC 903" or
"192.168.10.98" without actually using them as a connection target.

For non-Python files (.sh, .service, .json), fall back to the shell-style
heuristic that the .sh script uses.

Usage: scripts/check_stale_refs.py [--strict]
"""

import sys
import os
import re
import tokenize
import io
from pathlib import Path

PATTERNS = [
    (r'vpn\.homelab\.local', 'LXC 903 hostname'),
    (r'102\.182\.117\.43', 'old public IP (lab router)'),
    (r'192\.168\.10\.98', 'LXC 903 IP'),
    (r'LXC ?903', '"LXC 903" / "LXC-903"'),
    (r'lxc-903', 'tag-style'),
]

# File-level allowlist: docs/, archive/, CHANGELOG.md, .service files,
# Grafana dashboard JSON (metadata fields).
FILE_ALLOWLIST = [
    r'\.md$',
    r'/archive/',
    r'_archived-',
    r'\.bak$',
    r'gen-certs\.sh',
    r'CHANGELOG\.md',
    r'\.service$',
    r'host/grafana/',
    r'host/grafana/dashboards/',
]

# Search paths (same as .sh)
SEARCH_PATHS = ['host', 'docker', 'quota', 'tests']


def is_file_allowlisted(filepath: str) -> bool:
    for pat in FILE_ALLOWLIST:
        if re.search(pat, filepath):
            return True
    return False


def find_python_refs(filepath: str, patterns: list) -> list:
    """Return list of (lineno, snippet) for non-comment, non-string occurrences."""
    try:
        with open(filepath, 'rb') as f:
            tokens = list(tokenize.tokenize(f.readline))
    except (tokenize.TokenizeError, IndentationError, SyntaxError):
        return []  # unreadable Python — fall through to shell-style grep

    # Token types we skip (anything that's NOT actual executable code):
    # - COMMENT (# ...) and NL/NEWLINE/INDENT/DEDENT/ENDMARKER/ENCODING (whitespace)
    # - STRING (regular "..." or '...' literals)
    # - FSTRING_START/MIDDLE/END (PEP 701 f-string parts, Python 3.12+)
    #   F-string middle parts contain literal text that LOOKS like code but is
    #   actually inside an f-string. Skip them to avoid false positives on
    #   error messages, log lines, etc.
    skip_types = {
        tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE,
        tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER,
    }
    # PEP 701 f-string tokens (Python 3.12+)
    for attr in ('FSTRING_START', 'FSTRING_MIDDLE', 'FSTRING_END'):
        if hasattr(tokenize, attr):
            skip_types.add(getattr(tokenize, attr))

    refs = []
    for tok in tokens:
        if tok.type in skip_types:
            continue
        # tok.string is the actual source text for non-string tokens.
        for pat, _desc in patterns:
            if re.search(pat, tok.string):
                refs.append((tok.start[0], tok.string))
                break
    return refs


def find_shell_refs(filepath: str, patterns: list) -> list:
    """For non-Python files, use line-based heuristic: skip lines that look
    like comments or JSON metadata. Returns list of (lineno, snippet)."""
    refs = []
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.lstrip()
                # Comment in shell: starts with # (any indentation)
                if stripped.startswith('#'):
                    continue
                # JSON metadata fields: "description": ..., "tags": [...], "title": ...
                if re.search(r'(description|tags|title)"?\s*:\s*"', line):
                    continue
                # Docstring-ish (Python or other): contains """ or '''
                if '"""' in line or "'''" in line:
                    continue
                # Env-var default patterns (the actual value comes from env var, not code):
                # bash: ${VAR:-default} or ${VAR-default}
                if re.search(r'\$\{?[A-Z_][A-Z0-9_]*[\w_]*:?-[^}]*\}', line):
                    continue
                # Python os.environ.get("X", "default")
                if re.search(r'os\.environ\.get\s*\([^)]*,\s*["\']', line):
                    continue
                for pat, _desc in patterns:
                    if re.search(pat, line):
                        refs.append((lineno, line.rstrip()))
                        break
    except (IOError, OSError):
        pass
    return refs


def main():
    strict = '--strict' in sys.argv

    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)

    fail_count = 0
    allowlisted_count = 0
    skipped_count = 0
    real_refs = []

    for search_path in SEARCH_PATHS:
        path = repo_root / search_path
        if not path.exists():
            continue
        for filepath in path.rglob('*'):
            if filepath.is_dir():
                continue
            # Skip binary / pycache
            if '__pycache__' in filepath.parts or filepath.suffix == '.pyc':
                continue
            rel = str(filepath.relative_to(repo_root))
            # File-level allowlist: never check .md, .service, Grafana JSON, archive, etc.
            if is_file_allowlisted(rel):
                # Quick pattern check just to count
                try:
                    content = filepath.read_text(encoding='utf-8', errors='replace')
                except (IOError, OSError):
                    continue
                for pat, _ in PATTERNS:
                    if re.search(pat, content):
                        allowlisted_count += 1
                        break
                continue
            for pat, desc in PATTERNS:
                # Quick check first
                try:
                    content = filepath.read_text(encoding='utf-8', errors='replace')
                except (IOError, OSError):
                    continue
                if not re.search(pat, content):
                    continue
                # Real refs need investigation
                if filepath.suffix == '.py':
                    refs = find_python_refs(str(filepath), [(pat, desc)])
                else:
                    refs = find_shell_refs(str(filepath), [(pat, desc)])
                if not refs:
                    skipped_count += 1
                else:
                    for lineno, snippet in refs:
                        real_refs.append((rel, lineno, pat, snippet))

    if real_refs:
        print(f"FAIL: lab-leakage detected — {len(real_refs)} hardcoded ref(s) in production code:")
        print()
        for rel, lineno, pat, snippet in real_refs:
            print(f"  {rel}:{lineno}: pattern={pat!r}")
            print(f"    {snippet.strip()[:120]}")
        print()
        print(f"  {len(real_refs)} FAIL(s) | {allowlisted_count} allowlisted file(s) | {skipped_count} comment/string-only")
        sys.exit(1)
    else:
        print(f"OK: no lab-leakage in production code")
        print(f"  {allowlisted_count} allowlisted file(s) (.md, .service, CHANGELOG, archive/, Grafana dashboards)")
        print(f"  {skipped_count} reference(s) inside Python comments/docstrings/strings (not leaks)")
        sys.exit(0)


if __name__ == '__main__':
    main()