#!/usr/bin/env python3
"""
rotate-vpn-credentials.py — Rotate EAP credentials for a VPN user.

Generates a new password, updates:
  1. SQLite users table on VPS (charon auth source of truth)
  2. /etc/swanctl/conf.d/rw-eap.conf on VPS (charon secrets file)
  3. audit_log entry

Reloads charon via swanctl --load-creds (non-disruptive, no SA drops).
Prints Windows PS1 commands to update client profile.

USAGE:
    python3 rotate-vpn-credentials.py --user zun-windows-laptop --dry-run
    python3 rotate-vpn-credentials.py --user zun-windows-laptop --confirm

PASSWORDS:
    16 random bytes from secrets.token_bytes(16), base64url-encoded without
    padding → 22-char ASCII secret. Format matches the existing entries in
    rw-eap.conf (e.g. "4le5hACpKjgYWpMoANLNdQ").

DB SCHEMA (verified 2026-06-24):
    users.password = 16 raw bytes (NOT base64 string). VPS stores token_bytes(16).
    rw-eap.conf secret = base64.urlsafe_b64encode(token_bytes(16)).rstrip(b'=')
    LXC 903 DB is OUT OF SYNC (3 days stale) — not authoritative for charon.

CHANGELOG:
    2026-06-24 v1.0 — Initial version. Discovered need: Windows client was
    sending old EAP identity 'test-win-5g-laptop' but server's charon only
    knows 'zun-windows-laptop'. Rotation + Windows-side update fixes both
    the username desync AND gives a fresh password to start clean.
"""
import argparse
import base64
import secrets
import subprocess
import sys
from datetime import datetime, timezone, timedelta

SAST = timezone(timedelta(hours=2))
VPS = "vpn-prod-01"


def now_sast() -> str:
    return datetime.now(SAST).strftime("%Y-%m-%d %H:%M:%S SAST")


def ssh_run(cmd: str, *, check: bool = True, stdin: str | None = None) -> str:
    """Run a command on VPS via SSH. Returns stdout. If check, exit on non-zero."""
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", VPS, cmd],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and r.returncode != 0:
        sys.stderr.write(f"SSH FAIL on `{cmd}`:\n  stdout={r.stdout!r}\n  stderr={r.stderr!r}\n")
        sys.exit(1)
    return r.stdout.strip()


def gen_password() -> tuple[bytes, str]:
    """16 raw bytes + base64url 22-char form."""
    raw = secrets.token_bytes(16)
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return raw, b64


def get_user_id(user_name: str) -> int:
    """Look up users.id by name in VPS DB."""
    script = f'''
import sqlite3
con = sqlite3.connect("/var/lib/strongswan/ipsec.db")
row = con.execute("SELECT id FROM users WHERE name = ?", ("{user_name}",)).fetchone()
if row is None:
    raise SystemExit(f"USER_NOT_FOUND: {user_name}")
print(row[0])
'''
    out = ssh_run('sudo /opt/vpn-portal/.venv/bin/python3', stdin=script)
    return int(out)


def update_db_password(user_id: int, raw: bytes, dry_run: bool) -> None:
    """UPDATE users SET password = ? WHERE id = ? (parameterized)."""
    hex_bytes = raw.hex()
    if dry_run:
        print(f"[DRY-RUN] DB: UPDATE users SET password = X'{hex_bytes}' WHERE id={user_id}")
        return
    script = f'''
import sqlite3
con = sqlite3.connect("/var/lib/strongswan/ipsec.db")
c = con.cursor()
c.execute("UPDATE users SET password = ? WHERE id = ?", (bytes.fromhex("{hex_bytes}"), {user_id}))
con.commit()
print("rows_updated:", c.rowcount)
'''
    ssh_run('sudo /opt/vpn-portal/.venv/bin/python3', stdin=script)
    print(f"[OK] DB: users.id={user_id} password rotated ({len(raw)} bytes)")


def update_secrets_file(user_name: str, secret_b64: str, dry_run: bool) -> None:
    """Edit /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf in place
    (this is the HOST path that bind-mounts into the strongswan container at
    /etc/swanctl/conf.d/rw-eap.conf). Replace the secret line inside the
    eap-{user_name} block."""
    if dry_run:
        print(f"[DRY-RUN] rw-eap.conf: eap-{user_name} secret = \"{secret_b64}\"")
        return
    script = f'''
import sys
path = "/opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf"
with open(path) as f:
    lines = f.readlines()

in_block = False
found = False
for i, line in enumerate(lines):
    s = line.strip()
    if s.startswith("eap-{user_name}"):
        in_block = True
        continue
    if in_block:
        if s.startswith("}}"):
            in_block = False
            continue
        if "secret" in s and "=" in s:
            lines[i] = f'    secret = "{secret_b64}"\\n'
            found = True
            break

if not found:
    sys.stderr.write("SECRET_BLOCK_NOT_FOUND: {user_name}\\n")
    sys.exit(1)

# Backup before write
import shutil
shutil.copy2(path, path + ".bak-" + str(int(__import__("time").time())))

with open(path, "w") as f:
    f.writelines(lines)
print("OK")
'''
    ssh_run('sudo python3', stdin=script)
    print(f"[OK] rw-eap.conf: eap-{user_name} secret rotated")


def reload_charon(dry_run: bool) -> None:
    """Non-disruptive credential reload."""
    if dry_run:
        print("[DRY-RUN] charon: would run swanctl --load-creds")
        return
    out = ssh_run('sudo docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds 2>&1')
    print(f"[OK] charon reloaded: {out!r}")


def verify_load(user_name: str) -> None:
    """Confirm charon loaded the new key."""
    out = ssh_run(
        'sudo grep "loaded EAP shared key" /var/lib/docker/overlay2/939ee38d91b1a3ee20d39b069b8b4cc7f5522b1a86f21a0584a404c3a0832e2d/merged/var/log/charon-log 2>&1 | tail -3'
    )
    print("[VERIFY] last 3 EAP key loads in charon log:")
    for line in out.splitlines():
        print(f"  {line}")


def audit_log(user_id: int, user_name: str) -> None:
    """Write an audit row (parameterized)."""
    script = f'''
import sqlite3, json, time
con = sqlite3.connect("/var/lib/strongswan/ipsec.db")
c = con.cursor()
payload = json.dumps({{"user_name": "{user_name}", "reason": "silent_rename_fix", "rotated_by": "ops/rotate-vpn-credentials.py"}})
c.execute(
    "INSERT INTO audit_log (actor, action, target_type, target_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    ("operator", "rotate_credentials", "user", {user_id}, payload, int(time.time()))
)
con.commit()
print("audit_id:", c.lastrowid)
'''
    ssh_run('sudo /opt/vpn-portal/.venv/bin/python3', stdin=script)
    print(f"[OK] audit_log written for user_id={user_id}")


def main() -> None:
    p = argparse.ArgumentParser(description="Rotate VPN EAP credentials.")
    p.add_argument("--user", required=True, help="EAP username to rotate")
    p.add_argument("--dry-run", action="store_true", help="Show what would change, change nothing")
    p.add_argument("--confirm", action="store_true", help="Actually perform the rotation")
    args = p.parse_args()

    if not args.dry_run and not args.confirm:
        sys.stderr.write("ERROR: pass --dry-run OR --confirm\n")
        sys.exit(2)
    if args.dry_run and args.confirm:
        sys.stderr.write("ERROR: --dry-run and --confirm are mutually exclusive\n")
        sys.exit(2)

    print(f"=== Rotating credentials for `{args.user}` @ {now_sast()} ===")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'CONFIRM (LIVE)'}")

    raw, b64 = gen_password()
    print(f"Generated password: {b64}  (raw {len(raw)} bytes = {raw.hex()})")

    user_id = get_user_id(args.user)
    print(f"User ID: {user_id}")

    update_db_password(user_id, raw, args.dry_run)
    update_secrets_file(args.user, b64, args.dry_run)
    audit_log(user_id, args.user) if not args.dry_run else None
    reload_charon(args.dry_run)
    if not args.dry_run:
        verify_load(args.user)

    if args.dry_run:
        print("\n[DRY-RUN COMPLETE] — no changes made. Re-run with --confirm to apply.")
    else:
        print()
        print("=" * 60)
        print("ROTATION COMPLETE")
        print("=" * 60)
        print(f"  Username: {args.user}")
        print(f"  Password: {b64}")
        print()
        print("Windows PowerShell (run as Admin on the laptop):")
        print()
        print(f'  Set-VpnConnectionUsername -Name "Databyte-VPN" -UserName "{args.user}"')
        print()
        print("Then reconnect the VPN. Windows will prompt for the password.")
        print(f"Enter this password: {b64}")
        print()
        print("(Or run the full installer again from the operator portal to get")
        print(" a fresh profile with everything pre-configured.)")


if __name__ == "__main__":
    main()