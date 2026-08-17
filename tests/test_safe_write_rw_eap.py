"""Tests for host/safe_write_rw_eap.py — TKT-002 single-source-of-truth helper.

These tests cover the four failure modes that caused the production
re-truncation bug:

  1. Happy path: writes content, verifies size, leaves no temp file
  2. Temp size mismatch: refuses rename, preserves backup, raises SafeWriteError
  3. LOCAL: write_bytes failure mid-flow (simulate via Path magic) → no
     half-written live file
  4. REMOTE: SSH transport raises between steps → no live file write

Plus a smoke test that confirms atomic_write_conf_local never opens the
live file in 'w' mode (catches future regressions on the truncate pattern).
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

# Add repo root so we can import the helper directly
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "host"))
from safe_write_rw_eap import (
    SafeWriteError,
    atomic_write_conf_local,
    atomic_write_conf_remote,
)


# --- Fixtures ---

@pytest.fixture
def conf_dir(tmp_path) -> Path:
    """A conf.d/ dir with a healthy 2-block rw-eap.conf (connections + secrets)."""
    d = tmp_path / "swanctl" / "conf.d"
    d.mkdir(parents=True)
    conf = d / "rw-eap.conf"
    conf.write_text(
        "connections {\n"
        "  rw-eap {\n"
        "    version = 2\n"
        "    proposals = aes256-sha256-modp2048\n"
        "  }\n"
        "}\n"
        "\n"
        "secrets {\n"
        "  eap-test-1 {\n"
        '    id     = test-1\n'
        '    secret = "placeholder1"\n'
        "  }\n"
        "}\n"
    )
    return d


# --- LOCAL tests ---

class TestAtomicWriteConfLocal:
    def test_happy_path(self, conf_dir):
        backup = conf_dir / ".backups"
        conf = conf_dir / "rw-eap.conf"
        new_content = conf.read_text() + (
            '\n  eap-test-2 {\n'
            '    id     = test-2\n'
            '    secret = "sekret2"\n'
            '  }\n'
            "}\n"
        )

        result = atomic_write_conf_local(
            new_content, str(conf), str(backup), "testlabel"
        )

        assert result == len(new_content.encode())
        assert conf.read_text() == new_content
        # Backup exists with pre-write content
        backups = list(backup.glob("rw-eap.conf.bak-testlabel-*"))
        assert len(backups) == 1
        # No leftover temp file
        assert list(conf_dir.glob("rw-eap.conf.tmp-*")) == []

    def test_size_mismatch_aborts(self, conf_dir, monkeypatch):
        """If the temp file ends up the wrong size, live file must not change."""
        backup = conf_dir / ".backups"
        conf = conf_dir / "rw-eap.conf"
        original = conf.read_text()
        original_size = len(original.encode())

        # Patch write_bytes so the temp file ends up SHORTER than expected
        real_write_bytes = Path.write_bytes

        def buggy_write_bytes(self, data):
            real_write_bytes(self, data[:-10])  # drop last 10 bytes

        monkeypatch.setattr(Path, "write_bytes", buggy_write_bytes)

        with pytest.raises(SafeWriteError, match="temp file size mismatch"):
            atomic_write_conf_local(
                original + "PADDING_TO_MAKE_IT_LONGER",
                str(conf),
                str(backup),
                "testlabel",
            )

        # Live file unchanged
        assert conf.read_text() == original
        assert conf.stat().st_size == original_size
        # Temp file cleaned up
        assert list(conf_dir.glob("rw-eap.conf.tmp-*")) == []
        # Backup preserved for recovery
        assert list(backup.glob("rw-eap.conf.bak-testlabel-*"))

    def test_never_opens_live_in_w_mode(self, conf_dir, monkeypatch):
        """Regression guard: live conf must never see open(..., 'w')."""
        backup = conf_dir / ".backups"
        conf = conf_dir / "rw-eap.conf"

        # Capture all Path.open() calls
        called_with_w = []

        real_open = Path.open

        def spy_open(self, *args, **kwargs):
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            if "w" in str(mode) and self.name == "rw-eap.conf":
                called_with_w.append((self, mode))
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", spy_open)

        atomic_write_conf_local(
            conf.read_text(), str(conf), str(backup), "testlabel"
        )

        # The LIVE conf file must NEVER be opened in 'w' mode by the helper
        live_writes = [c for c in called_with_w if c[0].name == "rw-eap.conf"]
        # bak file written via write_bytes (atomic), which doesn't go through open(..., 'w')
        # tmp file written via write_bytes too. So zero hits expected.
        assert live_writes == [], f"live file opened in write mode: {live_writes}"


# --- REMOTE tests ---

class _MockSSH:
    """In-memory ssh(cmd_list, stdin_data=None) for testing the remote helper.

    Maintains a dict of {path -> bytes} representing the remote filesystem.
    Handles cat / cp / mkdir / mv / rm / tee / stat / cp / stat.
    """

    def __init__(self, initial_files: dict[str, bytes]):
        self.fs = dict(initial_files)
        self.dirs: set[str] = set()
        self.calls: list[tuple] = []

    def __call__(self, cmd_list, stdin_data=None, timeout=None):
        cmd = list(cmd_list)
        self.calls.append((cmd, stdin_data))
        op = cmd[0]

        if op == "mkdir":
            d = cmd[1]
            self.dirs.add(d)
            return ""
        if op == "cp":
            src, dst = cmd[1], cmd[2]
            self.fs[dst] = self.fs[src]
            return ""
        if op == "tee":
            path = cmd[1]
            self.fs[path] = stdin_data or b""
            return ""
        if op == "mv":
            src, dst = cmd[1], cmd[2]
            self.fs[dst] = self.fs.pop(src)
            return ""
        if op == "rm":
            # Handle 'rm [-f] PATH' — the path is the last non-flag arg
            path_arg = next((c for c in reversed(cmd[1:]) if c != "-f"), None)
            if path_arg:
                self.fs.pop(path_arg, None)
            return ""
        if op == "stat":
            path = cmd[3] if len(cmd) > 3 else cmd[2]
            # command is: stat -c %s <path>
            return str(len(self.fs[path]))
        if op == "cat":
            path = cmd[1]
            return self.fs[path].decode("utf-8")
        raise AssertionError(f"unmocked cmd: {cmd}")


class TestAtomicWriteConfRemote:
    def test_happy_path(self, conf_dir):
        backup = str(conf_dir / ".backups")
        conf_path = str(conf_dir / "rw-eap.conf")
        original = (conf_dir / "rw-eap.conf").read_bytes()

        ssh = _MockSSH({conf_path: original, f"{conf_path}.bak-pre-test": b""})

        new_content = b"new live content here, longer than the original 1234567890"
        result = atomic_write_conf_remote(
            new_content.decode(), ssh, conf_path, backup, "testlabel"
        )

        assert result == len(new_content)
        assert ssh.fs[conf_path] == new_content
        # Backup exists
        backups = [k for k in ssh.fs if "bak-testlabel-" in k]
        assert len(backups) == 1
        assert ssh.fs[backups[0]] == original
        # Temp file cleaned up
        tmp_files = [k for k in ssh.fs if k.startswith(conf_path + ".tmp-")]
        assert tmp_files == []

    def test_size_mismatch_aborts_remote(self, conf_dir):
        """If tee writes fewer bytes than expected, refuse to rename."""
        backup = str(conf_dir / ".backups")
        conf_path = str(conf_dir / "rw-eap.conf")
        original = (conf_dir / "rw-eap.conf").read_bytes()

        class _GlitchySSH(_MockSSH):
            """Returns 5 fewer bytes from tee than we asked for (simulated SSH glitch)."""
            def __call__(self, cmd_list, stdin_data=None, timeout=None):
                if cmd_list and cmd_list[0] == "tee":
                    stdin_data = (stdin_data or b"")[:-5]
                return super().__call__(cmd_list, stdin_data=stdin_data)

        glitch = _GlitchySSH({conf_path: original})

        with pytest.raises(SafeWriteError, match="temp file size mismatch"):
            atomic_write_conf_remote(
                b"0123456789" * 100,  # plenty of content; 5-byte loss detectable
                glitch,
                conf_path,
                backup,
                "testlabel",
            )

        # Live file untouched
        assert glitch.fs[conf_path] == original
        # Temp file cleaned up
        tmp_files = [k for k in glitch.fs if k.startswith(conf_path + ".tmp-")]
        assert tmp_files == []

    def test_ssh_failure_before_rename_keeps_live_file(self, conf_dir):
        """If SSH raises between tee and stat (or anywhere pre-rename), live untouched."""
        backup = str(conf_dir / ".backups")
        conf_path = str(conf_dir / "rw-eap.conf")
        original = (conf_dir / "rw-eap.conf").read_bytes()

        class _BoomSSH(_MockSSH):
            def __call__(self, cmd_list, stdin_data=None, timeout=None):
                if cmd_list and cmd_list[0] == "tee":
                    raise IOError("simulated ssh glitch")
                return super().__call__(cmd_list, stdin_data=stdin_data)

        boom = _BoomSSH({conf_path: original})

        with pytest.raises(IOError, match="simulated ssh glitch"):
            atomic_write_conf_remote(
                "new content here", boom, conf_path, backup, "testlabel"
            )

        # Live file unchanged
        assert boom.fs[conf_path] == original
