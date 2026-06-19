#!/usr/bin/env python3
"""
update_rw_eap_conf.py — Add EAP secret blocks to the host-side
swanctl/conf.d/rw-eap.conf (which is bind-mounted read-only into the
strongSwan container at /etc/swanctl/conf.d/rw-eap.conf).

This is the EAP auth source of truth. The strongSwan `attr-sql` plugin
only stores attributes (pool assignment, virtual IP), NOT auth credentials.
Auth is checked against the `secrets { ... }` block in swanctl.conf.

Usage:
  python3 update_rw_eap_conf.py /path/to/creds_file

Where creds_file format is:
  Username: demo-phone
  Password: E6fkfBK6DvUHkG1jcipJrQ
  ...
"""
import os
import re
import subprocess
import sys
from pathlib import Path

# Read creds
if len(sys.argv) > 1:
    creds_path = Path(sys.argv[1])
    if not creds_path.exists():
        print(f"ERROR: creds file not found: {creds_path}", file=sys.stderr)
        sys.exit(1)
    text = creds_path.read_text()
else:
    text = sys.stdin.read()

# Parse creds
creds = {}
current_user = None
for line in text.splitlines():
    if 'Username:' in line:
        current_user = line.split('Username:')[1].strip()
    elif 'Password:' in line and current_user:
        p = line.split('Password:')[1].strip()
        creds[current_user] = p
        current_user = None

if not creds:
    print("ERROR: no creds parsed from input", file=sys.stderr)
    sys.exit(1)

print(f"Parsed creds for: {list(creds.keys())}")

# Locate the host-side staging file
# This is /home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf on the LXC host
# (where the LXC is, not where we are running)
HOST_CONF = '/home/zunaid/strongswan/swanctl/conf.d/rw-eap.conf'

# We're running this script ON the LXC 903 host (via pct exec 903)
# so the path is direct, not through pct exec
conf = Path(HOST_CONF).read_text()

# Build new secret blocks
new_blocks = []
for username, password in creds.items():
    if f"eap-{username} {{" in conf:
        print(f"  eap-{username}: already in conf, skipping")
        continue
    new_blocks.append(
        f"""  eap-{username} {{
    id     = {username}
    secret = "{password}"
  }}
"""
    )

if not new_blocks:
    print("Nothing to add — all demo creds already in conf")
else:
    # Backup
    bak = f"{HOST_CONF}.bak-pre-demo-{Path('/root/.demo_vpn_creds').stat().st_mtime:.0f}"
    Path(bak).write_text(conf)
    print(f"Backup: {bak}")

    # Insert before the LAST closing `}` (the secrets block end)
    pattern = re.compile(r'^}\s*$', re.MULTILINE)
    matches = list(pattern.finditer(conf))
    if not matches:
        print("ERROR: could not find closing `}` in rw-eap.conf", file=sys.stderr)
        sys.exit(1)
    insertion = matches[-1].start()
    new_conf = conf[:insertion] + ''.join(new_blocks) + conf[insertion:]
    Path(HOST_CONF).write_text(new_conf)
    print(f"Added {len(new_blocks)} secret blocks to {HOST_CONF}")

# Reload charon secrets (this reads from /etc/swanctl which is the bind-mount)
print()
print("=== Reloading charon secrets ===")
reload_proc = subprocess.run(
    ['docker', 'exec', 'strongswan', 'swanctl', '--uri=tcp://127.0.0.1:4502', '--load-creds'],
    capture_output=True, text=True
)
print(reload_proc.stdout)
if reload_proc.returncode != 0:
    print(f"ERROR: reload failed: {reload_proc.stderr}", file=sys.stderr)
    sys.exit(1)

# Verify
print("=== Verify: 'loaded eap secret' lines ===")
verify_proc = subprocess.run(
    ['docker', 'exec', 'strongswan', 'swanctl', '--uri=tcp://127.0.0.1:4502', '--load-creds'],
    capture_output=True, text=True
)
for line in verify_proc.stdout.splitlines():
    if 'loaded eap secret' in line or 'loaded ike secret' in line:
        print(f"  {line}")
