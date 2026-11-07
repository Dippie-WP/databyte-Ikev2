#!/usr/bin/env bash
# seed_demo_creds.sh — Generate NTLM-hashed passwords for demo-phone and
# demo-laptop, write to strongswan users table, and print the creds for the
# operator to hand to the test client.
#
# Uses openssl dgst -md4 -provider legacy (Python 3.13 removed hashlib.md4)
# to compute NTLM = MD4(UTF-16-LE(password)).
#
# Output: prints credentials in three formats (plain / strongSwan swanctl --load-creds /
# iOS mobileconfig-friendly). Also writes them to a file the operator can read
# or delete after the test.
#
# Idempotent: re-runs regenerate new passwords. (Each run produces a new
# secrets.token_urlsafe(16) password.) Use reset_demo.sh to clear data usage.

set -euo pipefail

# --- read DB path + lock the operation ---
DB_PATH="${DB_PATH:-/var/lib/strongswan/ipsec.db}"
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: DB not found at $DB_PATH" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be root (writes to $DB_PATH)" >&2
    exit 1
fi

# --- generate two random passwords + NTLM hashes ---
# Stored on LXC 903 host at /root/.demo_vpn_creds (the LXC's root, not OC host's).
# On the OC host, you can pull it via:
#   ssh root@192.168.10.210 "pct exec 903 -- cat /root/.demo_vpn_creds"
CREDS_FILE="/root/.demo_vpn_creds"
umask 077

# Pass shell vars to Python via env (heredoc doesn't expand them)
export DB_PATH CREDS_FILE
python3 - <<'PY'
import os, secrets, subprocess, sqlite3, sys

db_path = os.environ['DB_PATH']
creds_file = os.environ['CREDS_FILE']

def ntlm_hash(pw: str) -> bytes:
    """NTLM = MD4(UTF-16-LE(password)) — 16 bytes"""
    pw_utf16 = pw.encode('utf-16-le')
    r = subprocess.run(
        ['openssl', 'dgst', '-md4', '-provider', 'legacy', '-binary'],
        input=pw_utf16, capture_output=True, check=True
    )
    return r.stdout

db = sqlite3.connect(db_path)
cur = db.cursor()

creds = {}
for name in ['demo-phone', 'demo-laptop']:
    pw = secrets.token_urlsafe(16)
    h = ntlm_hash(pw)
    cur.execute("UPDATE users SET password = ? WHERE name = ?", (h, name))
    if cur.rowcount != 1:
        print(f"ERROR: {name!r} not found in users table", file=sys.stderr)
        sys.exit(1)
    creds[name] = pw

db.commit()
db.close()

# Write a file the operator can read (mode 0600)
import datetime
ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
with open(creds_file, "w") as f:
    f.write("# Phase 5B demo VPN credentials\n")
    f.write(f"# Generated: {ts}\n")
    f.write("# Server: 102.182.117.43 (vpn.homelab.local)\n")
    f.write("# Connection: rw-eap (EAP-MSCHAPv2, server cert auth)\n")
    f.write("# Tier: 100MB (demo_100mb) — used for 5B.2 + 5B.5 testing\n\n")
    for name, pw in creds.items():
        f.write(f"{name}\n")
        f.write(f"  Username: {name}\n")
        f.write(f"  Password: {pw}\n")
        f.write("\n")
os.chmod(creds_file, 0o600)

# Print to stdout (operator sees the creds)
print("=== demo credentials generated ===")
for name, pw in creds.items():
    print(f"  {name:14} password: {pw}")
print()
print(f"  Saved to: {creds_file} (mode 600)")
print()
print("  Server:    102.182.117.43")
print("  Remote ID: vpn.homelab.local")
print("  Local ID:  <device-name> (e.g. demo-phone)")
print("  Auth:      EAP-MSCHAPv2 (username + password)")
print("  Pool:      rw-pool (10.99.0.0/24)")
PY

echo
echo "=== verify in DB ==="
sqlite3 "$DB_PATH" "SELECT id, name, length(password) AS pw_bytes, substr(hex(password),1,16) AS pw_first16 FROM users WHERE name LIKE 'demo%';"
echo
echo "=== charon sees the change? (will re-read on next auth, no restart needed) ==="
echo "  attr-sql caches config in memory; force re-read with:"
echo "    docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --reload-creds 2>/dev/null || true"
echo "  (actually charon auto-reloads on every EAP-MSCHAPv2 auth attempt)"
