#!/usr/bin/env python3
"""
vpn_disconnect.py — RFC 5176 DAE Disconnect-Request sender (cut flow, Phase 5+).

When a customer hits 100% data cap, this module sends a Disconnect-Request
(RADIUS code 40) to charon's eap-radius.dae listener on UDP/127.0.0.1:3799
with `User-Name=<customer-eap-identity>`. charon matches this against active
IKE_SAs and sends IKE DELETE INFORMATIONAL, terminating the session cleanly.

Why DAE (RFC 5176) instead of `swanctl --terminate --ike-id`:
  - Standards-defined for server-side session disconnect. Used by Cisco ISE,
    Aruba ClearPass, Fortinet FortiGate.
  - charon returns Disconnect-ACK if it found and killed the SA, Disconnect-NAK
    if no matching SA (already offline = expected, not error).
  - Removes the regex-parsing-SAs step from the cut path: User-Name match
    handles everything.

Why radclient over pyrad:
  - pyrad 2.5.4 fails on Debian 13's FreeRADIUS 3.2.7 dictionary chain
    (parse error: "Illegal type: vsa" at dictionary.rfc2865:35). Building
    a hand-rolled minimal pyrad dictionary is fragile.
  - `radclient` (freeradius-utils) is already installed at /usr/bin/radclient
    and handles dictionary parsing internally. Used by every RADIUS operator
    for manual CoA testing.
  - This is a thin ~30-line wrapper. If radclient breaks, the test command
    `echo "User-Name=<user>" | radclient -x 127.0.0.1:3799 disconnect <secret>`
    is the canonical diagnostic.

Secret: stored at /root/.strongswan-dae-secret (mode 0600, root:root).
        Must match `charon.plugins.eap-radius.dae.secret` in
        docker/strongswan.d/10-eap-radius.conf. We do NOT reuse the
        RADIUS auth secret — DAE auth uses Message-Authenticator per
        RFC 3579 § 3.2, separate code path.

Caller: quota-monitor._cut_customer() (after radcheck disable, BEFORE
        terminate_customer_sas() which is kept as belt-and-suspenders
        for the case where charon can't see the SA for any reason).
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("vpn-portal.dae")

# RFC 5176 § 2.3 default port. charon listens here; we send CoA here.
DAE_HOST = "127.0.0.1"
DAE_PORT = 3799
RADCLIENT_BIN = "/usr/bin/radclient"
DAE_TIMEOUT_SEC = 5

# Same naming convention as /root/.mariadb-portal-pw (mode 0600 root:root).
DAE_SECRET_FILE = Path("/root/.strongswan-dae-secret")


def _read_secret() -> str | None:
    """Read DAE shared secret from disk. Whitespace-tolerant, comment-aware."""
    if not DAE_SECRET_FILE.exists():
        log.error("DAE secret file missing: %s", DAE_SECRET_FILE)
        return None
    try:
        text = DAE_SECRET_FILE.read_text()
    except OSError as e:
        log.error("reading %s: %s", DAE_SECRET_FILE, e)
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    log.error("No secret line in %s (only comments?)", DAE_SECRET_FILE)
    return None


def send_dae_disconnect(username: str, *, nas_ip: str = "127.0.0.1",
                        timeout: float = DAE_TIMEOUT_SEC) -> str:
    """Send RFC 5176 Disconnect-Request for `username`.

    Returns:
      "ack"   — Disconnect-ACK (code 41): charon killed the SA
      "nak"   — Disconnect-NAK (code 42): no matching SA. NOT an error:
                customer may already be offline, or SA in mid-rekey.
      "error" — radclient missing, secret missing, network failure,
                or unrecognized reply.

    Implementation note: radclient defaults to "expect ACK". For NAK,
    exit code is 0 if `-p <retries>` and the expected-Ack handling is
    acknowledged, but actually radclient prints "Expected Disconnect-ACK
    got Disconnect-NAK" and exits non-zero. We parse the response line
    explicitly to disambiguate ACK vs NAK.
    """
    if not Path(RADCLIENT_BIN).exists():
        log.error("radclient not found at %s", RADCLIENT_BIN)
        return "error"

    secret = _read_secret()
    if not secret:
        return "error"

    # `radclient -x` (debug output) writes two key lines to stdout/stderr:
    #   "Received Disconnect-ACK Id <n> from <ip>:<port> to <ip>:<port> length <n>"
    #   "Received Disconnect-NAK Id <n> from <ip>:<port> to <ip>:<port> length <n>"
    # We check stdout+stderr (radclient writes the response line to stderr).
    cmd = [
        RADCLIENT_BIN, "-x",                            # debug output
        "-r", "1",                                       # 1 retry
        "-t", str(int(timeout)),                         # response_window
        f"{DAE_HOST}:{DAE_PORT}",                        # destination
        "disconnect",                                    # RFC 5176 disconnect
        secret,                                          # shared secret
    ]
    # radclient reads Request attributes from stdin (one per line, comma-separated).
    request = f"User-Name={username},NAS-IP-Address={nas_ip},NAS-Identifier=vpn-prod-01-quota-monitor"

    try:
        proc = subprocess.run(
            cmd, input=request, capture_output=True, text=True, timeout=timeout + 1,
        )
    except subprocess.TimeoutExpired:
        log.error("DAE radclient timed out after %ds", int(timeout + 1))
        return "error"
    except FileNotFoundError:
        log.error("radclient binary missing (looked at %s)", RADCLIENT_BIN)
        return "error"
    except OSError as e:
        log.error("radclient OS error: %s", e)
        return "error"

    # Parse radclient output for the response code line.
    output = proc.stdout + proc.stderr
    if "Received Disconnect-ACK" in output:
        log.info("DAE Disconnect-ACK for user=%s", username)
        return "ack"
    if "Received Disconnect-NAK" in output:
        log.info("DAE Disconnect-NAK for user=%s (no matching SA — likely already offline)",
                 username)
        return "nak"
    # No response captured — could be no SAs (radclient sent but no reply),
    # auth failure (bad Message-Authenticator), or socket-level error.
    log.error("DAE radclient rc=%s — no ACK/NAK captured. Output: %s",
              proc.returncode, output.strip().replace(chr(10), " | ")[:400])
    return "error"


if __name__ == "__main__":
    # CLI mode: python3 vpn_disconnect.py <username>
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) != 2:
        print("Usage: vpn_disconnect.py <eap-username>", file=sys.stderr)
        sys.exit(2)
    result = send_dae_disconnect(sys.argv[1])
    sys.exit(0 if result == "ack" else (1 if result == "nak" else 3))
