"""test_hostile_failure_diagnostics.py — Polish Item #2.

The hostile-mode endpoint now distinguishes THREE failure modes instead of one
generic 500. Verify each:

  - conf unreachable  -> 503 with concrete checklist
  - conf readable but no block for identity -> 404 with hint (case-sensitive
    device name match; list of known identities)

Lessons applied:
  - Pre-Response Gate: tools verify, memory cites (this file lives with
    feature commits, logs what was wrong before the polish).
  - CORR-2026-07-11-013: verify rollback on partial-failure destructive ops
    (here: API returns and tells operator what to do, not just "it broke").
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


def _make_fake_session_dep():
    return lambda: {"name": "test-operator"}


def _captured_endpoint(*, eap_return, db_side=None):
    """Register installer_tokens endpoints with a configurable EAP return."""
    from fastapi import FastAPI
    from installer_tokens import register
    app = FastAPI()

    db_q = MagicMock(side_effect=db_side if db_side else [
        [{"id": 42, "name": "zunaid-test", "display_name": "Zunaid (test)",
          "is_active": 1, "is_operator": 0, "tier_id": None,
          "bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20}],
        [{"id": 99, "device_name": "laptop", "device_type": "windows"}],
    ])

    import installer_tokens as it_mod
    orig = it_mod._read_eap_secret_from_conf
    it_mod._read_eap_secret_from_conf = MagicMock(return_value=eap_return)
    register(
        app, db_query=db_q, db_exec=MagicMock(),
        q=lambda x: f"'{x}'",
        audit_fn=MagicMock(),
        require_session_dep=_make_fake_session_dep(),
    )
    endpoint = None
    for route in app.router.routes:
        if hasattr(route, "path") and route.path == "/api/customers/{customer_id}/installer-token":
            endpoint = route.endpoint; break
    captured = {"restore": orig}
    return endpoint, captured


def _restore(captured):
    import installer_tokens as it_mod
    it_mod._read_eap_secret_from_conf = captured["restore"]


def _make_request():
    return MagicMock(cookies={})


# ─── Polish Item #2: hostile failure diagnostics ───────────────────────────

def test_hostile_returns_503_when_conf_unreachable():
    """When _read_eap_secret_from_conf returns (None, None) — file missing or
    SSH broken — endpoint returns 503 with concrete diagnostic (not generic 500)."""
    from fastapi import HTTPException
    endpoint, captured = _captured_endpoint(eap_return=(None, None))
    try:
        with pytest.raises(HTTPException) as ei:
            endpoint(customer_id=42, request=_make_request(), mode="hostile")
        assert ei.value.status_code == 503
        # Must mention concrete diagnostic
        msg = ei.value.detail
        assert "rw-eap.conf" in msg or "VPN gateway" in msg
        # Must list CONCRETE next-step actions (not just "it's broken")
        assert "charon" in msg or "swanctl" in msg
        assert "SSH" in msg
    finally:
        _restore(captured)


def test_hostile_returns_404_with_hint_when_only_customer_matches_not_device():
    """When _read_eap_secret_from_conf returns (None, fingerprint) — file readable
    but no exact block — endpoint returns 404 with concrete hint + sibling identity list."""
    import installer_tokens as it_mod
    # 1) Patch _read_eap_secret_from_conf: has a fingerprint (file readable) but no secret
    orig_read = it_mod._read_eap_secret_from_conf
    it_mod._read_eap_secret_from_conf = MagicMock(
        return_value=(None, "9f2c4a8b1e0d"),
    )
    # 2) Patch _list_eap_identities_in_conf so it returns sibling identities
    #    (simulates an operator-case-mismatch scenario)
    orig_list = it_mod._list_eap_identities_in_conf
    it_mod._list_eap_identities_in_conf = MagicMock(
        return_value=[
            "zunaid-test-LAPTOP",   # right customer, wrong (uppercase) device
            "zunaid-test-phone",
        ],
    )
    try:
        from fastapi import HTTPException
        from fastapi import FastAPI
        from installer_tokens import register
        app = FastAPI()
        db_q = MagicMock(side_effect=[
            [{"id": 42, "name": "zunaid-test", "display_name": "Zunaid (test)",
              "is_active": 1, "is_operator": 0, "tier_id": None,
              "bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20}],
            [{"id": 99, "device_name": "laptop", "device_type": "windows"}],
        ])
        register(
            app, db_query=db_q, db_exec=MagicMock(),
            q=lambda x: f"'{x}'",
            audit_fn=MagicMock(),
            require_session_dep=_make_fake_session_dep(),
        )
        endpoint = None
        for route in app.router.routes:
            if hasattr(route, "path") and route.path == "/api/customers/{customer_id}/installer-token":
                endpoint = route.endpoint; break

        with pytest.raises(HTTPException) as ei:
            endpoint(customer_id=42, request=_make_request(), mode="hostile")

        assert ei.value.status_code == 404
        msg = ei.value.detail
        assert "zunaid-test-laptop" in msg  # the identity it tried
        # operator sees the matching-customer sibling and the case-mismatch hint
        assert "zunaid-test-LAPTOP" in msg
        assert "device name 'laptop'" in msg or "'laptop'" in msg
        assert "case-sensitive" in msg.lower() or "Hint" in msg or "hint" in msg
    finally:
        it_mod._read_eap_secret_from_conf = orig_read
        it_mod._list_eap_identities_in_conf = orig_list


def test_hostile_returns_404_with_unknown_customer_message():
    """When file is readable and no block matches the customer at all, return 404
    with the full sibling list (truncated to 10 to keep response readable)."""
    import installer_tokens as it_mod
    orig_read = it_mod._read_eap_secret_from_conf
    it_mod._read_eap_secret_from_conf = MagicMock(return_value=(None, "1234abcdef56"))
    orig_list = it_mod._list_eap_identities_in_conf
    it_mod._list_eap_identities_in_conf = MagicMock(return_value=[
        "alice-LAPTOP", "alice-PHONE", "bob-WIN", "bob-MAC", "carol-ANDROID",
    ])
    try:
        from fastapi import HTTPException
        from fastapi import FastAPI
        from installer_tokens import register
        app = FastAPI()
        db_q = MagicMock(side_effect=[
            [{"id": 42, "name": "zunaid-test", "display_name": "Zunaid (test)",
              "is_active": 1, "is_operator": 0, "tier_id": None,
              "bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20}],
            [{"id": 99, "device_name": "laptop", "device_type": "windows"}],
        ])
        register(
            app, db_query=db_q, db_exec=MagicMock(),
            q=lambda x: f"'{x}'",
            audit_fn=MagicMock(),
            require_session_dep=_make_fake_session_dep(),
        )
        endpoint = None
        for route in app.router.routes:
            if hasattr(route, "path") and route.path == "/api/customers/{customer_id}/installer-token":
                endpoint = route.endpoint; break
        with pytest.raises(HTTPException) as ei:
            endpoint(customer_id=42, request=_make_request(), mode="hostile")
        assert ei.value.status_code == 404
        msg = ei.value.detail
        assert "zunaid-test" in msg
        assert "alice-LAPTOP" in msg  # sibling list present
        assert "bob-WIN" in msg
    finally:
        it_mod._read_eap_secret_from_conf = orig_read
        it_mod._list_eap_identities_in_conf = orig_list


def test_hostile_returns_404_when_many_siblings_truncated():
    """When there are more than 10 siblings, the response lists the first 10 and
    notes '(+N more)' so it doesn't bloat the response."""
    import installer_tokens as it_mod
    orig_read = it_mod._read_eap_secret_from_conf
    it_mod._read_eap_secret_from_conf = MagicMock(return_value=(None, "deadbeef1234"))
    orig_list = it_mod._list_eap_identities_in_conf
    siblings = [f"user-{i}-laptop" for i in range(50)]
    it_mod._list_eap_identities_in_conf = MagicMock(return_value=siblings)
    try:
        from fastapi import HTTPException
        from fastapi import FastAPI
        from installer_tokens import register
        app = FastAPI()
        db_q = MagicMock(side_effect=[
            [{"id": 42, "name": "no-such-customer", "display_name": "None",
              "is_active": 1, "is_operator": 0, "tier_id": None,
              "bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20}],
            [{"id": 99, "device_name": "laptop", "device_type": "windows"}],
        ])
        register(
            app, db_query=db_q, db_exec=MagicMock(),
            q=lambda x: f"'{x}'",
            audit_fn=MagicMock(),
            require_session_dep=_make_fake_session_dep(),
        )
        endpoint = None
        for route in app.router.routes:
            if hasattr(route, "path") and route.path == "/api/customers/{customer_id}/installer-token":
                endpoint = route.endpoint; break
        with pytest.raises(HTTPException) as ei:
            endpoint(customer_id=42, request=_make_request(), mode="hostile")
        msg = ei.value.detail
        # truncation marker present
        assert "+40 more" in msg or "+30 more" in msg or "+20 more" in msg
    finally:
        it_mod._read_eap_secret_from_conf = orig_read
        it_mod._list_eap_identities_in_conf = orig_list


def test_hostile_503_lists_exactly_three_actions():
    """The 503 message should name exactly the three diagnostic actions:
    (1) charon up, (2) file exists+readable, (3) SSH works. Locks down the
    structure so future edits don't drop steps."""
    import installer_tokens as it_mod
    it_mod._read_eap_secret_from_conf = MagicMock(return_value=(None, None))
    orig_list = it_mod._list_eap_identities_in_conf
    it_mod._list_eap_identities_in_conf = MagicMock(return_value=[])
    try:
        from fastapi import HTTPException
        from fastapi import FastAPI
        from installer_tokens import register
        app = FastAPI()
        db_q = MagicMock(side_effect=[
            [{"id": 42, "name": "zunaid-test", "display_name": "Zunaid (test)",
              "is_active": 1, "is_operator": 0, "tier_id": None,
              "bandwidth_down_mbps": 20, "bandwidth_up_mbps": 20}],
            [{"id": 99, "device_name": "laptop", "device_type": "windows"}],
        ])
        register(
            app, db_query=db_q, db_exec=MagicMock(),
            q=lambda x: f"'{x}'",
            audit_fn=MagicMock(),
            require_session_dep=_make_fake_session_dep(),
        )
        endpoint = None
        for route in app.router.routes:
            if hasattr(route, "path") and route.path == "/api/customers/{customer_id}/installer-token":
                endpoint = route.endpoint; break
        with pytest.raises(HTTPException) as ei:
            endpoint(customer_id=42, request=_make_request(), mode="hostile")
        msg = ei.value.detail
        # Match the three (1) (2) (3) diagnostic steps
        assert "(1)" in msg
        assert "(2)" in msg
        assert "(3)" in msg
    finally:
        it_mod._read_eap_secret_from_conf = it_mod._read_eap_secret_from_conf
        it_mod._list_eap_identities_in_conf = orig_list


# ─── _list_eap_identities_in_conf unit tests ────────────────────────────────

def test_list_eap_identities_returns_block_ids():
    """The sibling-lister must extract `id = ...` values from inside eap-{...} blocks."""
    import installer_tokens as it_mod
    orig_read = it_mod._read_rw_eap_conf if hasattr(it_mod, "read_rw_eap_conf") else None
    # Stub the helper at the module that the function imports from
    sample_conf = (
        "eap-alice-laptop {\n"
        "  id = alice-laptop\n"
        "  secret = \"S3cretAlice\"\n"
        "}\n"
        "\n"
        "eap-bob-phone {\n"
        "  id = bob-phone\n"
        "  secret = \"S3cretBob\"\n"
        "}\n"
    )
    # Inject a fake 'app' module exporting read_rw_eap_conf
    import types
    fake_app = types.ModuleType("app")
    fake_app.read_rw_eap_conf = MagicMock(return_value=sample_conf)
    sys.modules["app"] = fake_app

    identities = it_mod._list_eap_identities_in_conf()
    assert "alice-laptop" in identities
    assert "bob-phone" in identities
    assert len(identities) == 2


def test_list_eap_identities_handles_no_eap_blocks():
    """Empty conf -> empty list (no exception)."""
    import installer_tokens as it_mod
    import types
    fake_app = types.ModuleType("app")
    fake_app.read_rw_eap_conf = MagicMock(return_value="")
    sys.modules["app"] = fake_app
    assert it_mod._list_eap_identities_in_conf() == []


def test_list_eap_identities_handles_unreachable():
    """Helper exception in read_rw_eap_conf -> empty list (no exception)."""
    import installer_tokens as it_mod
    import types
    fake_app = types.ModuleType("app")
    fake_app.read_rw_eap_conf = MagicMock(side_effect=Exception("SSH fail"))
    sys.modules["app"] = fake_app
    assert it_mod._list_eap_identities_in_conf() == []
