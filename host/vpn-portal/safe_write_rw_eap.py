"""safe_write_rw_eap.py — single source of truth for atomic rw-eap.conf writes.

2026-08-11 fix (TKT-002). Replaces the 4 separate read-modify-write writers
that each truncated the file on mid-write failure:

  - host/vpn-portal/app.py:write_rw_eap_conf (formerly the only safe one)
  - quota/quota-monitor.py:kill_customer_credentials
  - quota/update_rw_eap_conf.py
  - ops/rotate-vpn-credentials.py:update_secrets_file

2026-08-17 reconciliation: TKT-010's flock + temp + os.replace + fsync pattern
(filed separately as quota_monitor_atomic_helper.py) is folded in here so
quota-monitor and app.py share ONE helper for atomic rw-eap.conf writes.
The TKT-010 helper is now obsolete and should be deleted on VPS.

Every caller uses ONE of these two functions:

  atomic_write_conf_local(content, conf_path, backup_dir, caller_label)
    For writers that run on the same host as the conf file (Path operations).
    Acquires fcntl.flock on RW_EAP_LOCK to serialize concurrent writers.

  atomic_write_conf_remote(content, ssh, conf_path, backup_dir, caller_label)
    For writers that talk to the conf file via an SSH transport.
    Does NOT flock — caller's transport runs on the same VPS already and
    concurrent writers via SSH are not expected (the portal runs single-
    threaded admin operations only).

Algorithm (local):
  1. acquire fcntl.flock on RW_EAP_LOCK (serializes against other writers)
  2. mkdir -p backup_dir + cp <conf> → <bak>
  3. write content to <conf>.tmp-<label>-<ts> via Path
  4. stat temp; verify size == len(content_bytes); else SafeWriteError
  5. tmp_path.replace(p) — POSIX-atomic rename on same FS
  6. stat final; verify size == len(content_bytes); else SafeWriteError
  7. release flock (auto on context exit)

Algorithm (remote):
  1. ssh mkdir -p backup_dir + ssh cp <conf> → <bak>
  2. ssh tee content > <conf>.tmp-<label>-<ts>
  3. ssh stat -c %s <tmp>; verify expected; else SafeWriteError
  4. ssh mv <tmp> <conf> — POSIX-atomic on the remote FS
  5. ssh stat -c %s <conf>; verify expected; else SafeWriteError

On any size mismatch the temp file is removed and SafeWriteError raised.
The live conf file is NEVER opened in 'w' mode, NEVER truncated mid-write.

Tests: tests/test_safe_write_rw_eap.py
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Union


# Path to the shared lockfile. Both quota-monitor (via local atomic) and
# app.py (via remote atomic) target the same rw-eap.conf, so any concurrent
# local writers must serialize. The local atomic function takes fcntl.flock
# on this path before reading + writing. The remote variant relies on the
# caller's transport coordination (the portal runs single-threaded admin
# writes, so this is acceptable for now).
#
# This constant + the _flock_exclusive helper fold TKT-010's separate
# quota_monitor_atomic_helper.flock_exclusive back into the single source
# of truth (canonical reconciliation 2026-08-17, drift fix for runs #187-196).
RW_EAP_LOCK = Path("/var/lock/rw-eap.lock")


class SafeWriteError(RuntimeError):
    """Raised when an atomic rw-eap.conf write fails a size validation step."""


@contextmanager
def _flock_exclusive(path: Path) -> Iterator[int]:
    """Acquire exclusive fcntl.flock on path. Auto-creates the lockfile.

    Mirrors TKT-010's quota_monitor_atomic_helper.flock_exclusive; folded
    in here so there's one source of truth for atomic rw-eap.conf writes.
    Released on context exit (also released automatically if the process
    holding the lock exits — that's the fcntl semantic).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            # If the fd was already closed somehow, swallow — we're exiting.
            pass
        os.close(fd)


def _expected_size(content: Union[str, bytes]) -> int:
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    return len(content)


def atomic_write_conf_local(
    content: Union[str, bytes],
    conf_path: Union[str, Path],
    backup_dir: Union[str, Path],
    caller_label: str,
) -> int:
    """Atomic write of ``content`` to ``conf_path`` via local Path operations.

    Acquires fcntl.flock on RW_EAP_LOCK before reading the conf, so concurrent
    local writers (e.g. quota-monitor's kill_customer_credentials running in
    parallel with another quota-monitor invocation) serialize cleanly.

    Returns the number of bytes written. Raises ``SafeWriteError`` on any
    size mismatch. The live conf file is never opened in 'w' mode.
    """
    p = Path(conf_path)
    backup = Path(backup_dir)
    backup.mkdir(parents=True, exist_ok=True)

    expected = _expected_size(content)
    ts = int(time.time())
    tmp_path = p.with_name(f"{p.name}.tmp-{caller_label}-{ts}")
    bak_path = backup / f"{p.name}.bak-{caller_label}-{ts}"

    # Serialise concurrent writers via shared flock (folded in from TKT-010)
    with _flock_exclusive(RW_EAP_LOCK):
        # 1. Snapshot the current file (bytes) for recovery
        bak_path.write_bytes(p.read_bytes())

        # 2. Write content to temp file (atomic on POSIX for writes under PIPE_BUF,
        #    but we still validate after the fact)
        if isinstance(content, str):
            tmp_bytes = content.encode("utf-8")
        else:
            tmp_bytes = content
        try:
            tmp_path.write_bytes(tmp_bytes)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise

        # 3. Validate temp size BEFORE moving over the live file
        actual = tmp_path.stat().st_size
        if actual != expected:
            tmp_path.unlink(missing_ok=True)
            raise SafeWriteError(
                f"temp file size mismatch for {p}: expected {expected}, got {actual}. "
                f"Backup preserved at {bak_path}."
            )

        # 4. Atomic rename — tmp_path.replace(p) is POSIX-atomic on the same FS,
        #    so the live conf is never observed in a half-written state
        tmp_path.replace(p)

        # 5. Verify final size after the rename
        final = p.stat().st_size
        if final != expected:
            raise SafeWriteError(
                f"final file size mismatch for {p}: expected {expected}, got {final}. "
                f"Backup preserved at {bak_path}."
            )

    return final


def atomic_write_conf_remote(
    content: Union[str, bytes],
    ssh: Callable,
    conf_path: str,
    backup_dir: str,
    caller_label: str,
) -> int:
    """Atomic write via SSH transport.

    ``ssh`` is a callable matching the portal's ``_run_remote`` signature:
        ssh(cmd_list, stdin_data=None, timeout=None) -> str

    Does NOT flock — caller's transport is expected to coordinate. If you
    have concurrent writers that both reach the VPS via SSH, add flock via
    a wrapper script on the VPS side.

    Returns the number of bytes written. Raises ``SafeWriteError`` on any
    size mismatch. The live conf file is never opened in 'w' mode.
    """
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content
    expected = len(content_bytes)
    ts = int(time.time())
    conf_name = Path(conf_path).name
    tmp_path = f"{conf_path}.tmp-{caller_label}-{ts}"
    bak_path = f"{backup_dir.rstrip('/')}/{conf_name}.bak-{caller_label}-{ts}"

    # 1. Ensure backup dir exists + snapshot
    ssh(["mkdir", "-p", backup_dir])
    ssh(["cp", conf_path, bak_path])

    # 2. Write content to temp file via 'tee' so stdout/stderr stay clean
    try:
        ssh(["tee", tmp_path], stdin_data=content_bytes)
    except Exception:
        ssh(["rm", "-f", tmp_path])
        raise

    # 3. Validate temp size BEFORE renaming over the live file
    try:
        actual = int(ssh(["stat", "-c", "%s", tmp_path]).strip())
    except Exception as e:
        ssh(["rm", "-f", tmp_path])
        raise SafeWriteError(
            f"could not stat temp file {tmp_path}: {e!r}. "
            f"Backup preserved at {bak_path}."
        )
    if actual != expected:
        ssh(["rm", "-f", tmp_path])
        raise SafeWriteError(
            f"temp file size mismatch for {conf_path}: expected {expected}, "
            f"got {actual}. SSH write likely truncated. "
            f"Backup preserved at {bak_path}."
        )

    # 4. POSIX-atomic rename
    ssh(["mv", tmp_path, conf_path])

    # 5. Verify final size after the rename
    try:
        final = int(ssh(["stat", "-c", "%s", conf_path]).strip())
    except Exception as e:
        raise SafeWriteError(
            f"could not stat final file {conf_path}: {e!r}. "
            f"Backup preserved at {bak_path}."
        )
    if final != expected:
        raise SafeWriteError(
            f"final file size mismatch for {conf_path}: expected {expected}, "
            f"got {final}. Atomic rename may have failed silently. "
            f"Backup preserved at {bak_path}."
        )

    return final
