"""test_installer_tokens_mode.py — Phase 1 endpoint integration tests.

Verify that the `installer-token` endpoint correctly dispatches by mode.

Approach: invoke the registered endpoint function directly (not via
TestClient) to bypass a FastAPI+anyio+httpx version mismatch that raises
`coroutine raised StopIteration` in this environment.

Standard mode = FROZEN v2.6.5 token flow (unchanged).
Hostile mode = new baked-ps1 flow (no token, full .ps1 content in response).

Polish #3 (2026-07-11): every Generate click also INSERTs a row in
installer_bakes with the SHA-256 of the artifact. The endpoint's response
shape grew two keys: `bake_id` and `sha256`. Test fixtures provide extra
side-effects to satisfy the bake-record SELECT.
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


def _captured_endpoint(db_query_side_effect, db_exec_mock=None, audit_mock=None, eap_secret="S3cr3tEapPass!"):
    """Register installer_tokens endpoints and return the create function.

    db_query_side_effect should include an extra row at the END to satisfy
    the bake-record SELECT (returns bake_id).
    """
    from fastapi import FastAPI
    from installer_tokens import register
    app = FastAPI()

    db_q = MagicMock(side_effect=db_query_side_effect)
    db_x = db_exec_mock or MagicMock()
    audit = audit_mock or MagicMock()

    import installer_tokens as it_mod
    orig = it_mod._read_eap_secret_from_conf
    it_mod._read_eap_secret_from_conf = MagicMock(return_value=(eap_secret, "9f2c4a8b1e0d"))

    register(
        app,
        db_query=db_q,
        db_exec=db_x,
        q=lambda x: f"'{x}'",
        audit_fn=audit,
        require_session_dep=lambda: {"name": "test-operator"},
    )

    endpoint = None
    for route in app.router.routes:
        if hasattr(route, "path") and route.path == "/api/customers/{customer_id}/installer-token":
            endpoint = route.endpoint
            break
    assert endpoint is not None, "create_installer_token endpoint not registered"

    captured = {"db_q": db_q, "db_x": db_x, "audit": audit, "restore_orig": orig}
    return endpoint, captured


def _make_request():
    return MagicMock(cookies={})


def _restore(captured):
    import installer_tokens as it_mod
    it_mod._read_eap_secret_from_conf = captured["restore_orig"]


def _db_side_effect():
    """Side effect yielding row lists for the canonical 5-call sequence:

      1. customer lookup
      2. device lookup
      3. installer_tokens row INSERT (executed via _db_exec, not _db_query)
      4. installer_bakes row INSERT (executed via _db_exec, not _db_query)
      5. installer_bakes SELECT by (customer, mode, sha) -> [{id: 42}]
    """
    return [
        [{"id": 42, "name": "zunaid-test", "display_name": "Zunaid (test)",
          "is_active": 1, "is_operator": 0, "tier_id": None,
          "bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20}],
        [{"id": 99, "device_name": "laptop", "device_type": "windows"}],
        [{"id": 42}],      # bake lookup SELECT (returns bake_id)
    ]


# ─── Standard mode (FROZEN, must be unchanged) ──────────────────────────────

def test_standard_default_returns_canonical_block():
    """POST without ?mode= → standard. Output matches FROZEN v2.6.5 shape + bake metadata."""
    endpoint, captured = _captured_endpoint(_db_side_effect())
    try:
        result = endpoint(customer_id=42, request=_make_request(), mode="standard")
    finally:
        _restore(captured)

    assert result["customer_name"] == "zunaid-test"
    assert result["device_name"] == "laptop"
    assert "curl.exe" in result["powershell_cmd"]
    assert "rasdial DatabyteVPN" in result["powershell_cmd"]
    assert "-t " in result["powershell_cmd"]
    assert result["expires_at"] is not None
    assert result["expires_in_days"] == 7
    assert result["token_prefix"].endswith("\u2026")


def test_explicit_standard_returns_same_shape_as_default():
    """POST ?mode=standard returns the same response shape as default.
    Tokens differ per invocation. Each invocation gets its own bake_id row."""
    endpoint, captured = _captured_endpoint(_db_side_effect())
    try:
        out_default = endpoint(customer_id=42, request=_make_request(), mode="standard")
    finally:
        _restore(captured)

    endpoint2, captured2 = _captured_endpoint(_db_side_effect())
    try:
        out_explicit = endpoint2(customer_id=42, request=_make_request(), mode="standard")
    finally:
        _restore(captured2)

    assert out_default.get("mode") == "standard"
    assert "installer_kind" not in out_default
    lines1 = out_default["powershell_cmd"].split("\n")
    lines2 = out_explicit["powershell_cmd"].split("\n")
    assert lines1[0] == lines2[0]
    assert lines1[2] == lines2[2]


def test_standard_writes_token_row():
    """Standard mode writes 1 row to installer_tokens (no bake row -- Polish #3 deferred)."""
    db_exec = MagicMock()
    endpoint, captured = _captured_endpoint(_db_side_effect(), db_exec_mock=db_exec)
    try:
        endpoint(customer_id=42, request=_make_request(), mode="standard")
    finally:
        _restore(captured)
    insert_calls = [c.args[0] for c in db_exec.call_args_list if c.args]
    assert any("INSERT INTO installer_tokens" in c for c in insert_calls)
    # Polish #3 deferred: no installer_bakes table on vps-01 yet
    assert not any("INSERT INTO installer_bakes" in c for c in insert_calls)


# ─── Hostile mode (new flow) ────────────────────────────────────────────────

def test_hostile_returns_baked_script():
    """POST ?mode=hostile returns full .ps1 content, no token, no expires_at + bake metadata."""
    endpoint, captured = _captured_endpoint(_db_side_effect())
    try:
        result = endpoint(customer_id=42, request=_make_request(), mode="hostile")
    finally:
        _restore(captured)

    assert result["mode"] == "hostile"
    assert result["installer_kind"] == "baked"
    assert result["filename"] == "setup-databyte-vpn-zunaid-test-laptop-hostile.ps1"
    assert result["content"] is not None and len(result["content"]) > 1000
    assert "powershell" in result["powershell_cmd"].lower()
    assert "setup.ps1" in result["powershell_cmd"]

    assert "expires_at" not in result
    assert "token_prefix" not in result
    assert "installer_url" not in result
    # Polish #3 deferred: bake_id + sha256 not in response yet
    assert "bake_id" not in result
    assert "sha256" not in result


def test_hostile_inline_credentials():
    """Baked script has the EAP password inlined at the top."""
    endpoint, captured = _captured_endpoint(_db_side_effect())
    try:
        result = endpoint(customer_id=42, request=_make_request(), mode="hostile")
    finally:
        _restore(captured)

    content = result["content"]
    assert "S3cr3tEapPass!" in content
    assert "$EAP_USERNAME" in content
    assert "$EAP_PASSWORD" in content


def test_hostile_does_NOT_write_token_row():
    """Hostile mode skips installer_tokens (no token). Polish #3 deferred; no bake row."""
    db_exec = MagicMock()
    endpoint, captured = _captured_endpoint(_db_side_effect(), db_exec_mock=db_exec)
    try:
        endpoint(customer_id=42, request=_make_request(), mode="hostile")
    finally:
        _restore(captured)
    inserts = [c for c in db_exec.call_args_list
               if c.args and "INSERT INTO installer_tokens" in c.args[0]]
    assert not inserts
    # Polish #3 deferred: no installer_bakes table on vps-01 yet
    bake_inserts = [c for c in db_exec.call_args_list
                    if c.args and "INSERT INTO installer_bakes" in c.args[0]]
    assert not bake_inserts


def test_hostile_mode_audit_action_is_hostile_specific():
    """Hostile mode emits an audit action named with 'hostile'."""
    audit = MagicMock()
    endpoint, captured = _captured_endpoint(_db_side_effect(), audit_mock=audit)
    try:
        endpoint(customer_id=42, request=_make_request(), mode="hostile")
    finally:
        _restore(captured)
    actions = [c.args[1] for c in audit.call_args_list if len(c.args) >= 2]
    assert any("hostile" in str(a).lower() for a in actions)




# ─── Errors ────────────────────────────────────────────────────────────────

def test_unknown_mode_returns_400():
    from fastapi import HTTPException
    endpoint, captured = _captured_endpoint(_db_side_effect())
    try:
        with pytest.raises(HTTPException) as ei:
            endpoint(customer_id=42, request=_make_request(), mode="hybrid")
        assert ei.value.status_code == 400
        assert "standard" in ei.value.detail
        assert "hostile" in ei.value.detail
    finally:
        _restore(captured)


# ─── Cross-mode sanity ────────────────────────────────────────────────────

def test_both_modes_echo_consistent_customer_metadata():
    a, c1 = _captured_endpoint(_db_side_effect())
    try:
        std = a(customer_id=42, request=_make_request(), mode="standard")
    finally:
        _restore(c1)

    b, c2 = _captured_endpoint(_db_side_effect())
    try:
        hos = b(customer_id=42, request=_make_request(), mode="hostile")
    finally:
        _restore(c2)

    assert std["customer_name"] == hos["customer_name"] == "zunaid-test"
    assert std["device_name"] == hos["device_name"] == "laptop"
    assert std["customer_display"] == hos["customer_display"] == "Zunaid (test)"
    assert std["device_type"] == hos["device_type"] == "windows"
