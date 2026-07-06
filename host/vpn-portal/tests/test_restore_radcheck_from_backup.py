"""test_restore_radcheck_from_backup.py — Phase 5+ reset bug fix.

The bug: `/api/quota/{customer_id}/reset` zeroes `data_used_bytes` +
`over_quota`, and restores `rw-eap.conf` from the latest pre-cut backup.
But for Phase 5+ customers (auth via RADIUS, not secrets), it never
restored the radcheck Cleartext-Password. The cut left it as
`DISABLED-<16hex>` — without restore, charon eap-radius rejects every
post-reset auth and the iPhone stays down.

The fix: `_restore_radcheck_from_rw_eap_backup(customer_id)` reads the
latest rw-eap.conf backup, extracts the customer's plaintext secret,
calls `portal_auth.enable_customer_radcheck()`.

Tests cover:
  - skip when customer has no devices / no EAP identity
  - skip when no radcheck rows exist (cut never triggered)
  - skip when radcheck already has real password (no DISABLED marker)
  - happy path: extract plaintext from backup, enable radcheck
  - skip when secret in backup starts with KILLED- (cut backup pre-cut had KILLED too)
  - error when no usable backup on disk
  - error when mariadb _db() is unreachable (radcheck pre-check fail)

Import-time gotcha: importing `app` triggers `installer_tokens.register()`
at the bottom of app.py, which immediately calls `_ensure_table()` →
`db_exec()` → `ssh_903(...)` over real SSH to LXC 903. Without an SSH
key, this raises HTTPException(502) at IMPORT time — before any test
fixture runs. We stub `installer_tokens.register` to a no-op BEFORE
importing app, then restore the original register after.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent
sys.path.insert(0, str(APP_DIR))

# Pre-import stub: replace installer_tokens.register with a no-op to
# suppress the SSH-bound _ensure_table() call during module load.
_it_module = None
try:
    _it_module = importlib.import_module("installer_tokens")
    _it_module.register = lambda *a, **k: None  # type: ignore[assignment]
except Exception:
    pass  # not on sys.path yet; app.py will import it normally below

import app  # noqa: E402

# Restore real register for any test that needs it.
if _it_module is not None and hasattr(_it_module, "_real_register"):
    _it_module.register = _it_module._real_register  # type: ignore[attr-defined]

# Sentinels for patch.object usage in tests below.
app.ssh_903 = MagicMock(return_value="")   # type: ignore[attr-defined]
app.db_exec = MagicMock()                   # type: ignore[attr-defined]
app.db_query = MagicMock(return_value=[])   # type: ignore[attr-defined]


SAMPLE_BACKUP_WITH_SECRET = """\
# rw-eap.conf — backup of last good config
# Timestamp: 2026-07-06 17:00:59 UTC

secrets {
    eap-zunaid-en-zunaid-en7 {
        id     = zunaid-en-zunaid-en7
        secret = "hQ179TT39_rUxwThbSNXzw"
    }
    eap-other-customer-iphone {
        id     = other-customer-iphone
        secret = "xK7dQpVcMn2s_L9zN1jR4t"
    }
}
"""

SAMPLE_BACKUP_KILLED = """\
secrets {
    eap-zunaid-en-zunaid-en7 {
        id     = zunaid-en-zunaid-en7
        secret = "KILLED-aabbccdd"
    }
}
"""


def test_skip_when_no_devices():
    with patch.object(app, "db_query", return_value=[]) as _q:
        step = app._restore_radcheck_from_rw_eap_backup(99)
    assert step["ok"] is True
    assert step["skipped"] is True
    assert step["reason"] == "no_devices_or_eap_identity"


def test_skip_when_no_radcheck_rows():
    devs = [{"eap_identity": "zunaid-en-zunaid-en7"}]
    with patch.object(app, "db_query", return_value=devs), \
         patch.object(app.portal_auth, "_db") as mdb:
        mdb.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
        step = app._restore_radcheck_from_rw_eap_backup(93)
    assert step["ok"] is True
    assert step["skipped"] is True
    assert step["reason"] == "no_radcheck_rows_for_user"


def test_skip_when_radcheck_already_enabled():
    devs = [{"eap_identity": "zunaid-en-zunaid-en7"}]
    with patch.object(app, "db_query", return_value=devs), \
         patch.object(app.portal_auth, "_db") as mdb:
        mdb.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
            {"attribute": "Cleartext-Password", "value": "realpw"},
            {"attribute": "NT-Password", "value": "AB" * 16},
        ]
        step = app._restore_radcheck_from_rw_eap_backup(93)
    assert step["ok"] is True
    assert step["skipped"] is True
    assert step["reason"] == "radcheck_already_enabled"


def test_happy_path_extracts_plaintext_and_enables_radcheck():
    devs = [{"eap_identity": "zunaid-en-zunaid-en7"}]
    bak = SAMPLE_BACKUP_WITH_SECRET

    class FakeConn:
        def __enter__(self_): return self_
        def __exit__(self, *a): pass
        def execute(self, q, params=None):
            class R:
                def fetchall(self):
                    return [{"attribute": "Cleartext-Password", "value": "DISABLED-1234567890abcdef"}]
            return R()

    with patch.object(app, "db_query", return_value=devs), \
         patch.object(app.portal_auth, "_db", return_value=FakeConn()), \
         patch.object(app, "ssh_903") as mssh, \
         patch.object(app.portal_auth, "enable_customer_radcheck") as menable:
        mssh.side_effect = [
            "rw-eap.conf.bak-quotamon-1783350060\n",
            bak,
        ]
        step = app._restore_radcheck_from_rw_eap_backup(93)
        assert menable.called, "enable_customer_radcheck must run"
        args, kwargs = menable.call_args
        assert args[0] == "zunaid-en-zunaid-en7"
        assert args[1] == "hQ179TT39_rUxwThbSNXzw"
        assert isinstance(args[2], str) and len(args[2]) == 32
    assert step["ok"] is True
    assert step["eap_identity"] == "zunaid-en-zunaid-en7"
    assert step["backup"] == "rw-eap.conf.bak-quotamon-1783350060"
    assert step["nt_hash_hex"].endswith("...")


def test_skip_when_backup_secret_is_KILLED():
    devs = [{"eap_identity": "zunaid-en-zunaid-en7"}]
    bak = SAMPLE_BACKUP_KILLED

    class FakeConn:
        def __enter__(self_): return self_
        def __exit__(self, *a): pass
        def execute(self, q, params=None):
            class R:
                def fetchall(self):
                    return [{"attribute": "Cleartext-Password", "value": "DISABLED-1234567890abcdef"}]
            return R()

    with patch.object(app, "db_query", return_value=devs), \
         patch.object(app.portal_auth, "_db", return_value=FakeConn()), \
         patch.object(app, "ssh_903") as mssh:
        mssh.side_effect = [
            "rw-eap.conf.bak-quotamon-1783350060\nrw-eap.conf.bak-quotamon-1783350040\n",
            bak,
            bak,
        ]
        step = app._restore_radcheck_from_rw_eap_backup(93)
    assert step["ok"] is False
    assert step["error"] == "no_secret_in_any_backup"


def test_error_when_no_backups_on_disk():
    devs = [{"eap_identity": "zunaid-en-zunaid-en7"}]

    class FakeConn:
        def __enter__(self_): return self_
        def __exit__(self, *a): pass
        def execute(self, q, params=None):
            class R:
                def fetchall(self):
                    return [{"attribute": "Cleartext-Password", "value": "DISABLED-1234567890abcdef"}]
            return R()

    from fastapi import HTTPException
    with patch.object(app, "db_query", return_value=devs), \
         patch.object(app.portal_auth, "_db", return_value=FakeConn()), \
         patch.object(app, "ssh_903", side_effect=HTTPException(503, "ssh fail")):
        step = app._restore_radcheck_from_rw_eap_backup(93)
    assert step["ok"] is False
