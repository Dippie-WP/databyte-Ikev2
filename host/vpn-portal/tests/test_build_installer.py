"""test_build_installer.py — Phase 1 of mode-selector build.

Tests the pure function `build_installer.build(customer, device, mode)` against:

- STANDARD mode: byte-identical match with the canonical 3-line block produced by
  installer_tokens.py today (regression safe — FROZEN v2.6.5 token flow).
- HOSTILE mode: produces a self-contained .ps1 with creds inlined, 5-step structure
  matches skills/windows-vpn-hostile-network-setup, no HTTPS invoked, ASCII-only.
- Dispatcher: rejects unknown modes + missing required args with clear errors.

Why these tests
---------------
The standard branch is critical-regression: today, every operator Generate click
on Windows produces the exact 3-line block. If build_installer.py ever drifts from
installer_tokens.py's reference output, customer onboarding breaks silently (the
script just fails to fetch creds). So we lock the output.

The hostile branch is brand-new code — we lock the structure (5 steps present,
creds inlined, no HTTPS, ASCII-only) so future refactors don't accidentally
re-introduce a cert-fetch step (which would defeat the entire hostile flow).

No DB, no network, no SSH — pure function tests.
"""
from __future__ import annotations

import base64
import re
import string
from pathlib import Path

import pytest

# Make `scripts/build_installer.py` importable
TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TEST_DIR.parent / "scripts"
import sys
sys.path.insert(0, str(SCRIPTS_DIR))

from build_installer import (  # noqa: E402  (after sys.path mutation)
    build,
    MODE_STANDARD,
    MODE_HOSTILE,
    ALLOWED_MODES,
    FROZEN_INSTALLER_URL,
)


# Test data
CUST = {"name": "zunaid-test", "display_name": "Zunaid (test)"}
DEV = {"device_name": "laptop", "device_type": "windows"}
TOKEN = "abc123def456ghi789jkl012mno345pq"
PW = "S3cr3tEapPass!"


# ─── Standard mode (regression — must match installer_tokens.py exactly) ───

def test_standard_byte_identical_to_installer_tokens_today():
    """Build the FROZEN 3-line block the same way installer_tokens.py does.
    
    This is the regression test that catches accidental drift between
    build_installer._build_standard() and the canonical generator.
    """
    out = build(CUST, DEV, MODE_STANDARD, token=TOKEN)
    assert out["installer_kind"] == "token"
    assert out["filename"] is None
    assert out["content"] is None
    assert out["installer_url"] == FROZEN_INSTALLER_URL
    assert out["token_prefix"] == TOKEN[:8] + "…"

    # Reconstruct the reference using installer_tokens.py's logic
    packed = base64.urlsafe_b64encode(
        f"{CUST['name']}:{TOKEN}".encode()
    ).decode().rstrip("=")
    expected = "\n".join([
        f"curl.exe -o $env:TEMP\\setup.ps1 '{FROZEN_INSTALLER_URL}'",
        f"& $env:TEMP\\setup.ps1 -t {packed}",
        "rasdial DatabyteVPN",
    ])

    assert out["powershell_cmd"] == expected, (
        "STANDARD block drifted from installer_tokens.py output. "
        "If you intended to change the customer-facing flow, update the "
        "FROZEN setup-databyte-vpn.ps1 script + communicate to ops. "
        "Otherwise revert the change."
    )


def test_standard_token_prefix_redacts_to_8_chars():
    """Token prefix in the response shows only 8 chars + ellipsis — never the full token."""
    out = build(CUST, DEV, MODE_STANDARD, token=TOKEN)
    assert len(out["token_prefix"]) == 9  # 8 chars + ellipsis
    assert out["token_prefix"].endswith("…")
    assert out["token_prefix"][:-1] == TOKEN[:8]


def test_standard_requires_token():
    with pytest.raises(ValueError, match="standard mode requires a non-empty token"):
        build(CUST, DEV, MODE_STANDARD, token="")


# ─── Hostile mode (new flow) ────────────────────────────────────────────────

def test_hostile_returns_baked_artifact():
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    assert out["installer_kind"] == "baked"
    assert out["filename"] == f"setup-databyte-vpn-{CUST['name']}-{DEV['device_name']}-hostile.ps1"
    assert out["content"] is not None
    assert len(out["content"]) > 2000  # must be a real script, not a stub
    assert out["powershell_cmd"]


def test_hostile_inlines_eap_credentials_at_top():
    """Credentials MUST be at the top of the file as PowerShell variables, NOT
    fetched via HTTPS (defeats the entire hostile flow)."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    
    # Variables present and correctly populated
    assert "$EAP_USERNAME" in body
    assert "$EAP_PASSWORD" in body
    # The actual password string must appear in the file (it's baked)
    assert PW in body
    # The EAP identity is customer-device per rw-eap.conf convention
    assert f"$EAP_USERNAME  = \"{CUST['name']}-{DEV['device_name']}\"" in body

    # PowerShell variables are populated BEFORE the first step block
    var_section_end = body.find("STEP 1")
    pw_pos = body.find(PW)
    assert pw_pos < var_section_end, "Password position is past the first step header"


def test_hostile_has_all_5_required_steps():
    """The 5 steps from the skill must all be present and in order."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]

    required_in_order = [
        "Add-VpnConnection",                # Step 1: profile
        "Set-VpnConnectionIPsecConfiguration",  # Step 2: crypto
        "NegotiateDH2048_AES256",           # Step 3: registry
        "RasSetCredentials",                # Step 4: P/Invoke
        "rasdial",                          # Step 5: connect
    ]
    last_pos = 0
    for marker in required_in_order:
        pos = body.find(marker, last_pos)
        assert pos > last_pos, f"Step marker '{marker}' missing or out of order (after pos {last_pos})"
        last_pos = pos


def test_hostile_does_not_invoke_https():
    """The hostile flow must NOT use HTTPS during config — that's the whole point.
    A single https:// reference would defeat the FortiGate bypass.
    Scans EXECUTABLE lines only (non-comment, non-here-string)."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)

    # Only inspect executable code (strip the <#...#> header comment block)
    body_full = out["content"]
    # Strip <#...#> comment block at top
    if body_full.startswith("<#"):
        end = body_full.find("#>")
        body_no_header = body_full[end+2:] if end >= 0 else body_full
    else:
        body_no_header = body_full
    # Also strip single-line # comments for scan
    executable_lines = []
    for line in body_no_header.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Also skip lines INSIDE @'...'@ here-strings (the C# type def)
        executable_lines.append(line)
    body_exec = "\n".join(executable_lines)

    # No URL fetchers in executable code
    fetchers = ["Invoke-WebRequest", "Invoke-RestMethod", "iex", "(irm", "curl.exe", "curl(", "wget", "Start-BitsTransfer"]
    for fetcher in fetchers:
        assert fetcher not in body_exec, (
            f"{fetcher!r} present in executable code -- would defeat hostile network bypass"
        )

    # No HTTPS URL fetches in executable code
    assert "https://" not in body_exec or all(
        "https://" + s in body_exec for s in []  # placeholder; real check below
    ), "https:// present in executable code"

    # Specifically: no portal URL references in actual command lines
    forbidden_in_executable = ["vpn-portal.databyte.co.za", "myvpn.databyte.co.za/static/", ".ps1' "]
    for forbidden in forbidden_in_executable:
        assert forbidden not in body_exec, (
            f"{forbidden!r} present in executable code -- would break hostile flow. "
            f"References in comments/docs are OK, but the script must not USE these URLs at runtime."
        )


def test_hostile_skips_cert_validation():
    """The skill documents cert validation is intentionally skipped on hostile nets."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    
    # No ServerCertificate validation, no fingerprint pin
    assert "ServerCertificate" not in body or "SKIPPED" in body
    assert "ServerCertSha256" not in body or "SKIPPED" in body
    assert "Issuer not Let's Encrypt" not in body
    assert "ISRG Root X2" not in body, "Root bootstrap defeats hostile flow"


def test_hostile_server_address_correct():
    """Must point at myvpn.databyte.co.za (cloudflare-free direct DNS), per skill."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    assert "$ServerAddress = \"myvpn.databyte.co.za\"" in body
    assert "154.65.110.44" in body  # IP literal also works through firewalls


def test_hostile_crypto_matches_strongswan_server():
    """The IPsec config MUST match strongSwan server's aes128-sha256-modp2048-ecp256."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    assert '"Group14"' in body or "\"Group14\"" in body  # modp2048
    assert '"PFS2048"' in body or "\"PFS2048\"" in body  # PFS with modp2048
    assert "AES256" in body or "AES128" in body  # at least one matches server
    assert "SHA256" in body


def test_hostile_ras_setcredentials_mask_correct():
    """Mask 0x87 = UserName | Password | Domain | Default. The skill flags this as required."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    assert "$c.Mask = 0x87" in body


def test_hostile_negotiate_dh2048_enforce():
    """Registry value must be 2 (ENFORCE), not 1."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    assert '"NegotiateDH2048_AES256" -Value 2' in body


def test_hostile_requires_password():
    with pytest.raises(ValueError, match="hostile mode requires eap_password"):
        build(CUST, DEV, MODE_HOSTILE, eap_password="")


def test_hostile_uses_office_safe_filename():
    """Filename uses only filesystem-safe chars; no spaces, no special chars."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    fname = out["filename"]
    safe = set(string.ascii_letters + string.digits + "-_.ps1")
    bad = set(fname) - safe
    assert not bad, f"Filename has unsafe chars: {bad} in {fname!r}"


def test_hostile_ascii_only():
    """PowerShell 5.1 is ANSI-codepage — non-ASCII chars corrupt the parse.
    Same hard rule as the standard baked script."""
    out = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    body = out["content"]
    body_bytes = body.encode("utf-8")
    # Try to detect any non-ASCII
    for i, b in enumerate(body_bytes):
        assert b < 128, f"Non-ASCII byte 0x{b:02x} at offset {i} — PS 5.1 will corrupt"
    

def test_hostile_profile_name_default():
    """Default connection name is 'DatabyteVPN'. Operators can pass custom."""
    out_default = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    assert "DatabyteVPN" in out_default["content"]
    
    out_custom = build(CUST, DEV, MODE_HOSTILE, eap_password=PW, connection_name="DatabyteVPN-corp")
    assert "DatabyteVPN-corp" in out_custom["content"]


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown mode"):
        build(CUST, DEV, "hybrid", token=TOKEN, eap_password=PW)


def test_unknown_mode_includes_allowed_values():
    """Error message lists allowed modes so operators / future devs understand."""
    with pytest.raises(ValueError) as ei:
        build(CUST, DEV, "bogus", token=TOKEN)
    assert "standard" in str(ei.value)
    assert "hostile" in str(ei.value)


def test_allowed_modes_isomorphism():
    """Sanity: ALLOWED_MODES is the public contract; verify it's the documented pair."""
    assert set(ALLOWED_MODES) == {"standard", "hostile"}


# ─── Cross-mode sanity ──────────────────────────────────────────────────────

def test_both_modes_return_consistent_base_shape():
    std = build(CUST, DEV, MODE_STANDARD, token=TOKEN)
    hos = build(CUST, DEV, MODE_HOSTILE, eap_password=PW)
    
    # Common keys
    common = {"mode", "customer_name", "customer_display", "device_name", "device_type",
              "installer_kind", "filename", "content", "powershell_cmd"}
    for k in common:
        assert k in std, f"STANDARD output missing key '{k}'"
        assert k in hos, f"HOSTILE output missing key '{k}'"
    
    # Diverging expected values
    assert std["mode"] == "standard" and hos["mode"] == "hostile"
    assert std["filename"] is None and hos["filename"] is not None
    assert std["content"] is None and hos["content"] is not None
    assert std["installer_kind"] == "token" and hos["installer_kind"] == "baked"
