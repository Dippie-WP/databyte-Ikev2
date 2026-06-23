#!/usr/bin/env python3
"""Generate an Argon2id hash for the operator admin password.

Usage:
    python3 gen-argon2-hash.py 'your-admin-password'
    # or interactive:
    python3 gen-argon2-hash.py

Output goes to /etc/vpn-portal.env as ADMIN_PASS_HASH=...

Parameters per OWASP 2026 Password Storage Cheat Sheet:
  - memory_cost: 19 MiB
  - time_cost: 2 iterations
  - parallelism: 1
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import portal_auth


def main():
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        import getpass
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("ERROR: passwords don't match", file=sys.stderr)
            sys.exit(1)

    if len(password) < 12:
        print("WARNING: password is <12 chars. Recommend >=16 for production.",
              file=sys.stderr)

    h = portal_auth.hash_operator_password(password)
    print(f"Argon2id hash ({len(h)} chars):")
    print(h)
    print()
    print("Add to /etc/vpn-portal.env as:")
    print(f"ADMIN_PASS_HASH={h}")


if __name__ == "__main__":
    main()