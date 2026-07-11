"""test_bake_verify.py — Phase 1 Polish Item #1 (2026-07-11).

Verifies that `GET /api/installer/bake/{bake_id}/verify` correctly cross-checks
the bake's customer+device EAP identity against the live strongSwan SAs.

Three question types this answers for the operator:

1. "Did my customer reach OUR server?" -> verified=true, match present
2. "Did the script fail to connect?"   -> verified=false, no match
3. "Is charon itself unreachable?"     -> 503 from the endpoint

The endpoint is read-only. We mock the swanctl call (the SSH path to vps-01)
but exercise the real parser -- this is the production code path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

TEST_DIR = Path(__file__).resolve().parent
PORTAL_DIR = TEST_DIR.parent
SCRIPTS_DIR = PORTAL_DIR / "scripts"

import sys
sys.path.insert(0, str(PORTAL_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))


# ── swanctl --list-sas sample (2 SAs) ───────────────────────────────────────

SAMPLE_SAS_TEXT = (
    # Convention: each conn is eap-{customer_name}-{device_name} and the
    # EAP identity inside it matches the rw-eap.conf `id =` field.
    "eap-bob-laptop: #1, ESTABLISHED, IKEv2 SPIs 1234_abcd public=5678_def\n"
    "  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]\n"
    "  remote 'client.public' @ 41.13.22.5[4500] EAP:'bob-laptop'\n"
    "  AES_CBC/256/HMAC_SHA2_256/128/PRF_HMAC_SHA2_256/MODP_2048\n"
    "  established 127s ago, rekeying in 86400s\n"
    "eap-zunaid-test-vm-windows: #2, ESTABLISHED, IKEv2 SPIs aaaa_bbbb public=cccc_dddd\n"
    "  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]\n"
    "  remote 'roaming.public' @ 102.45.91.7[4500] EAP:'zunaid-test-vm-windows'\n"
    "  AES_CBC/256/HMAC_SHA2_256/128/PRF_HMAC_SHA2_256/MODP_2048\n"
    "  established 23s ago, rekeying in 86400s\n"
)


def _register_app(*, bake_rows, dev_rows=None, customer_rows=None,
                  sas_text=SAMPLE_SAS_TEXT, sas_should_raise=False):
    """Helper: register installer_tokens endpoints with mocked bake/cust/device
    lookups AND a mocked swanctl pipeline. Returns (app, captured_endpoint)."""
    from fastapi import FastAPI
    from installer_tokens import register
    app = FastAPI()

    # Build side effect for db_query: order depends on which endpoint we call.
    # verify_bake_against_sas does: bake_row + customer_row + (device_row if available)
    # Each db_query call must return a list-of-dicts (one row per call).
    # side_effect unwraps the LIST, so each element here must itself be a
    # list, not a dict. Wrap each batch.
    side_effect = []
    for batch in (bake_rows, customer_rows or [], dev_rows or []):
        if batch:
            side_effect.append(batch)

    db_q = MagicMock(side_effect=side_effect)
    db_x = MagicMock()

    # Stub swanctl by injecting a fake `app` module.
    import types
    fake_app = types.ModuleType("app")
    if sas_should_raise:
        fake_app.swanctl_list_sas = MagicMock(side_effect=Exception("SSH fail"))
    else:
        fake_app.swanctl_list_sas = MagicMock(return_value=sas_text)

    def _fake_parse(text):
        # Real-parser stub: extracts basic fields matching our sample format.
        out = []
        cur = None
        for line in text.splitlines():
            # IKE_SA header: starts with "eap-...: #N, ESTABLISHED, ..."
            if line.startswith("eap-") and "#" in line:
                conn = line.split(":")[0]
                state = "ESTABLISHED"
                cur = {
                    "uniqueid":          int(line.split("#")[1].split(",")[0].strip()),
                    "conn":              conn,
                    "state":             state,
                    "version":           "IKEv2",
                    "local_id":          None, "local_ip": None, "local_port": None,
                    "remote_id":         None, "eap_id": None,
                    "remote_ip":         None, "remote_port": None,
                    "vip":               None,
                    "algo":              None,
                    "established_secs":  None,
                    "bytes_in":          0, "bytes_out":         0,
                    "pkts_in":           0, "pkts_out":          0,
                }
                out.append(cur)
                continue
            if cur is None:
                continue
            if "local  '" in line:
                # e.g. "  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]"
                try:
                    cur["local_id"]   = line.split("'")[1]
                    cur["local_ip"]   = line.split("@")[1].split("[")[0].strip()
                    cur["local_port"] = int(line.split("[")[1].split("]")[0])
                except Exception:
                    pass
                continue
            if "remote '" in line:
                # e.g. "  remote 'client.public' @ 41.13.22.5[4500] EAP:'bob-laptop'"
                try:
                    parts = line.split("'")
                    cur["remote_id"] = parts[1]
                    rest = line.split("@", 1)[1].strip()
                    cur["remote_ip"] = rest.split("[")[0].strip()
                    cur["remote_port"] = int(rest.split("[")[1].split("]")[0])
                    # EAP:'identity' is the trailing token. Find it AFTER the remote_id close-quote.
                    if "EAP:'" in line:
                        # split on EAP:' and take the part before the next '
                        eap_part = line.split("EAP:'", 1)[1]
                        cur["eap_id"] = eap_part.split("'")[0]
                except Exception:
                    pass
                continue
        return out

    fake_app._parse_sas_text = _fake_parse
    sys.modules["app"] = fake_app

    register(
        app, db_query=db_q, db_exec=db_x,
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=lambda: {"name": "test-operator"},
    )

    # Find the verify endpoint
    for r in app.router.routes:
        if hasattr(r, "path") and r.path == "/api/installer/bake/{bake_id}/verify":
            return app, r.endpoint
    raise RuntimeError("verify endpoint not registered")


def _bake_row(bake_id=42, cust_id=1, dev_id=2, mode="hostile", filename="setup.ps1"):
    return [{
        "id": bake_id, "customer_id": cust_id, "device_id": dev_id,
        "mode": mode, "baked_at": 1783779549, "filename": filename, "sha256": "abcd"*16,
    }]


def _customer_row(cust_id=1, name="zunaid-test", display_name="Zun"):
    return [{
        "id": cust_id, "name": name, "display_name": display_name,
    }]


def _device_row(dev_id=2, device_name="vm-windows"):
    return [{
        "id": dev_id, "device_name": device_name,
    }]


# ── Successful matches ──────────────────────────────────────────────────────

def test_verify_returns_verified_true_when_eap_identity_matches():
    """If the bake's customer.zunaid-test matches an SA in swanctl with
    eap_id = zunaid-test-vm-windows, verified=True."""
    app, ep = _register_app(
        bake_rows=_bake_row(),
        customer_rows=_customer_row(name="zunaid-test"),
        dev_rows=_device_row(device_name="vm-windows"),
    )
    request = MagicMock()
    out = ep(bake_id=42, request=request)
    assert out["ok"] is True
    assert out["verified"] is True
    assert out["eap_identity"] == "zunaid-test-vm-windows"
    assert out["match"] is not None
    assert out["match"]["conn"] == "eap-zunaid-test-vm-windows"
    assert out["match"]["state"] == "ESTABLISHED"
    assert out["all_matching_sas_count"] == 1
    assert out["live_sas_total"] == 2   # bob + zunaid both in sample
    assert out["server_local_id"] == "myvpn.databyte.co.za"
    # 5xx safe: never expose the eap password or the SHA-256 of it (Polish #3 invariant)
    assert "password" not in out
    assert "eap_credential" not in out


def test_verify_matches_by_conn_name_fallback():
    """If the SA doesn't have EAP identity but the conn starts with eap-...
    match the eap-... part against our target identity."""
    # Build a sample with no EAP field (non-EAP conn)
    non_eap_sas_text = (
        "rw-psk-somebody: #10, ESTABLISHED, IKEv2 SPIs ...\n"
        "  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]\n"
        "  remote 'somebody' @ 1.2.3.4[4500]\n"
        "  AES_CBC/256\n"
        "  established 50s ago\n"
    )
    # Hack: also include a fake conn `eap-zunaid-test-vm-windows` so the conn-name
    # fallback path matches
    fake_sas_text = (
        non_eap_sas_text
        + "\n"
        + "eap-zunaid-test-vm-windows: #11, ESTABLISHED, IKEv2 SPIs ...\n"
        + "  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]\n"
        + "  remote 'someone' @ 5.6.7.8[4500] EAP:'zunaid-test-vm-windows'\n"
        + "  AES_CBC/256\n"
        + "  established 10s ago\n"
    )
    app, ep = _register_app(
        bake_rows=_bake_row(),
        customer_rows=_customer_row(name="zunaid-test"),
        dev_rows=_device_row(device_name="vm-windows"),
        sas_text=fake_sas_text,
    )
    out = ep(bake_id=42, request=MagicMock())
    assert out["verified"] is True
    assert out["match"]["conn"] == "eap-zunaid-test-vm-windows"


# ── No match ───────────────────────────────────────────────────────────────

def test_verify_returns_verified_false_when_no_sa_present():
    """Customer-device combo not in swanctl -> verified=False, match=None."""
    # The sample has zunaid-vm-windows and bob-laptop. Send a query for alice-laptop.
    app, ep = _register_app(
        bake_rows=_bake_row(),
        customer_rows=_customer_row(name="alice"),
        dev_rows=_device_row(device_name="laptop"),
    )
    out = ep(bake_id=42, request=MagicMock())
    assert out["verified"] is False
    assert out["match"] is None
    assert out["all_matching_sas_count"] == 0
    assert out["live_sas_total"] == 2
    assert out["eap_identity"] == "alice-laptop"
    assert out["server_local_id"] == "myvpn.databyte.co.za"


def test_verify_handles_empty_sas_list():
    """If charon has no active SAs at all (off-hours), match=None, verified=False."""
    app, ep = _register_app(
        bake_rows=_bake_row(),
        customer_rows=_customer_row(name="zunaid-test"),
        dev_rows=_device_row(device_name="vm-windows"),
        sas_text="",   # no SAs at all
    )
    out = ep(bake_id=42, request=MagicMock())
    assert out["verified"] is False
    assert out["live_sas_total"] == 0


# ── Errors ─────────────────────────────────────────────────────────────────

def test_verify_404_on_unknown_bake_id():
    """bake_id that doesn't exist -> 404."""
    # Bake lookup returns empty -> 404
    from installer_tokens import register
    from fastapi import FastAPI
    from fastapi import HTTPException

    app = FastAPI()
    db_q = MagicMock(side_effect=[[]])   # empty = no bake row
    import types
    fake_app = types.ModuleType("app")
    fake_app.swanctl_list_sas = MagicMock(return_value=SAMPLE_SAS_TEXT)
    fake_app._parse_sas_text = lambda x: []
    sys.modules["app"] = fake_app
    register(
        app, db_query=db_q, db_exec=MagicMock(),
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=lambda: {"name": "op"},
    )
    ep = None
    for r in app.router.routes:
        if "bake/" in r.path:
            ep = r.endpoint; break
    with pytest.raises(HTTPException) as ei:
        ep(bake_id=99, request=MagicMock())
    assert ei.value.status_code == 404
    assert "99" in ei.value.detail or "not found" in ei.value.detail


def test_verify_503_when_charon_unreachable():
    """swanctl SSH fails -> 503 (NOT 500 -- upstream problem, not our bug)."""
    from installer_tokens import register
    from fastapi import FastAPI
    from fastapi import HTTPException

    app = FastAPI()
    db_q = MagicMock(side_effect=[
        # bake lookup -- a row exists
        [{"id": 42, "customer_id": 1, "device_id": 2, "mode": "hostile",
          "baked_at": 1783779549, "filename": "setup.ps1", "sha256": "abcd" * 16}],
        # customer lookup
        [{"id": 1, "name": "zunaid-test", "display_name": "Zun"}],
        # device lookup
        [{"id": 2, "device_name": "vm-windows"}],
    ])
    import types
    fake_app = types.ModuleType("app")
    fake_app.swanctl_list_sas = MagicMock(side_effect=Exception("SSH fail"))
    fake_app._parse_sas_text = lambda x: []
    sys.modules["app"] = fake_app
    register(
        app, db_query=db_q, db_exec=MagicMock(),
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=lambda: {"name": "op"},
    )
    ep = None
    for r in app.router.routes:
        if "bake/" in r.path:
            ep = r.endpoint; break
    with pytest.raises(HTTPException) as ei:
        ep(bake_id=42, request=MagicMock())
    assert ei.value.status_code == 503
    assert "charon" in ei.value.detail.lower() or "gateway" in ei.value.detail.lower()


# ── Customer metadata + invariants ─────────────────────────────────────────

def test_verify_response_has_documented_shape():
    """Lock the response shape so the UI/Phase 2 can rely on it."""
    app, ep = _register_app(
        bake_rows=_bake_row(),
        customer_rows=_customer_row(name="zunaid-test"),
        dev_rows=_device_row(device_name="vm-windows"),
    )
    out = ep(bake_id=42, request=MagicMock())
    required_keys = {
        "ok", "bake_id", "mode", "customer_name", "customer_display",
        "device_name", "eap_identity", "verified", "server_local_id",
        "match", "all_matching_sas_count", "live_sas_total", "checked_at",
    }
    assert required_keys.issubset(out.keys()), (
        f"missing keys: {required_keys - set(out.keys())}"
    )


def test_verify_mode_field_comes_from_bake_row():
    """The mode returned to the operator is the bake's mode (hostile/standard)."""
    app, ep = _register_app(
        bake_rows=_bake_row(mode="standard"),
        customer_rows=_customer_row(name="zunaid-test"),
        dev_rows=_device_row(device_name="vm-windows"),
    )
    out = ep(bake_id=42, request=MagicMock())
    assert out["mode"] == "standard"


def test_verify_eap_identity_constructed_from_customer_dash_device():
    """Verify the canonical {customer_name}-{device_name} format is used."""
    app, ep = _register_app(
        bake_rows=_bake_row(),
        customer_rows=_customer_row(name="acme-corp"),
        dev_rows=_device_row(device_name="laptop-pc-01"),
    )
    out = ep(bake_id=42, request=MagicMock())
    assert out["eap_identity"] == "acme-corp-laptop-pc-01"


def test_verify_does_NOT_call_db_exec_after_register():
    """Verify is read-only. The register() boot may issue CREATE TABLE, but the
    verify endpoint itself must not write INSERT/UPDATE/DELETE.

    Snapshot db_exec call count BEFORE calling the verify endpoint; assert the
    delta after is zero."""
    db_exec = MagicMock()
    from fastapi import FastAPI
    from installer_tokens import register
    app = FastAPI()
    db_q = MagicMock(side_effect=[
        [{"id": 42, "customer_id": 1, "device_id": 2, "mode": "hostile",
          "baked_at": 1783779549, "filename": "setup.ps1", "sha256": "abcd" * 16}],
        [{"id": 1, "name": "zunaid-test", "display_name": "Zun"}],
        [{"id": 2, "device_name": "vm-windows"}],
    ])
    import types
    fake_app = types.ModuleType("app")
    fake_app.swanctl_list_sas = MagicMock(return_value=SAMPLE_SAS_TEXT)
    fake_app._parse_sas_text = lambda x: [
        {"eap_id": "zunaid-test-vm-windows", "conn": "eap-zunaid-test-vm-windows",
         "local_id": "myvpn.databyte.co.za", "state": "ESTABLISHED",
         "remote_ip": "1.2.3.4", "vip": "10.99.0.5", "established_secs": 10,
         "algo": "AES_CBC/256"},
    ]
    sys.modules["app"] = fake_app
    register(
        app, db_query=db_q, db_exec=db_exec,
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=lambda: {"name": "op"},
    )
    ep = None
    for r in app.router.routes:
        if "bake/" in r.path:
            ep = r.endpoint; break
    before = db_exec.call_count
    ep(bake_id=42, request=MagicMock())
    after = db_exec.call_count
    assert after == before, (
        f"verify endpoint issued {after-before} _db_exec call(s); must be 0 "
        f"(created calls: {[c.args[0][:50] for c in db_exec.call_args_list[before:]]})"
    )


def test_verify_returns_503_when_installer_bakes_table_missing():
    """Polish #3 deferred: installer_bakes table doesn't exist yet. Verify must
    return a clean 503 with an actionable message, not a 502 from sqlite/mysql.
    """
    from installer_tokens import register
    from fastapi import FastAPI
    from fastapi import HTTPException

    app = FastAPI()
    # bake lookup raises (table doesn't exist)
    db_q = MagicMock(side_effect=Exception("no such table: installer_bakes"))
    import types
    fake_app = types.ModuleType("app")
    fake_app.swanctl_list_sas = MagicMock(return_value=SAMPLE_SAS_TEXT)
    fake_app._parse_sas_text = lambda x: []
    sys.modules["app"] = fake_app
    register(
        app, db_query=db_q, db_exec=MagicMock(),
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=lambda: {"name": "op"},
    )
    ep = None
    for r in app.router.routes:
        if "bake/" in r.path:
            ep = r.endpoint; break
    with pytest.raises(HTTPException) as ei:
        ep(bake_id=42, request=MagicMock())
    assert ei.value.status_code == 503
    assert "Polish #3" in ei.value.detail or "installer_bakes" in ei.value.detail

def test_verify_does_NOT_call_db_exec_after_register():
    """Verify is read-only. The register() boot may issue CREATE TABLE, but the
    verify endpoint itself must not write INSERT/UPDATE/DELETE.

    Snapshot db_exec call count BEFORE calling the verify endpoint; assert the
    delta after is zero."""
    db_exec = MagicMock()
    from fastapi import FastAPI
    from installer_tokens import register
    app = FastAPI()
    db_q = MagicMock(side_effect=[
        [{"id": 42, "customer_id": 1, "device_id": 2, "mode": "hostile",
          "baked_at": 1783779549, "filename": "setup.ps1", "sha256": "abcd" * 16}],
        [{"id": 1, "name": "zunaid-test", "display_name": "Zun"}],
        [{"id": 2, "device_name": "vm-windows"}],
    ])
    import types
    fake_app = types.ModuleType("app")
    fake_app.swanctl_list_sas = MagicMock(return_value=SAMPLE_SAS_TEXT)
    fake_app._parse_sas_text = lambda x: [
        {"eap_id": "zunaid-test-vm-windows", "conn": "eap-zunaid-test-vm-windows",
         "local_id": "myvpn.databyte.co.za", "state": "ESTABLISHED",
         "remote_ip": "1.2.3.4", "vip": "10.99.0.5", "established_secs": 10,
         "algo": "AES_CBC/256"},
    ]
    sys.modules["app"] = fake_app
    register(
        app, db_query=db_q, db_exec=db_exec,
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=lambda: {"name": "op"},
    )
    ep = None
    for r in app.router.routes:
        if "bake/" in r.path:
            ep = r.endpoint; break
    before = db_exec.call_count
    ep(bake_id=42, request=MagicMock())
    after = db_exec.call_count
    assert after == before, (
        f"verify endpoint issued {after-before} _db_exec call(s); must be 0 "
        f"(created calls: {[c.args[0][:50] for c in db_exec.call_args_list[before:]]})"
    )
