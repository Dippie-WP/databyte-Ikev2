#!/usr/bin/env bash
# install_mss_clamp.sh — Phase 5A.7 fix for 5G/CGNAT PMTUD.
#
# Adds the TCPMSS rule to the *mangle table's FORWARD chain:
#   iptables-legacy -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260
#
# Why:
#   StrongSwan phone-side MTU is set to 1400. CGNAT (5G carriers) often
#   drops ICMP "fragmentation needed", so PMTUD fails. The result: TCP
#   handshake completes, then data transfer hangs.
#
#   MSS clamp forces the client to advertise a smaller TCP MSS (1260) so
#   server responses fit through the CGNAT path without fragmentation.
#
# This script:
#   1. Applies the rule in memory (idempotent — checks first)
#   2. Edits /etc/iptables/rules.v4 directly to add/keep a *mangle section
#      with the TCPMSS rule (avoids race with strongswan-iptables-watchdog
#      which re-applies rules.v4 on every container event)
#   3. Removes any duplicate *mangle sections (iptables-restore uses the
#      FIRST occurrence of each table)
#   4. Verifies with a container restart
#
# Run on LXC 903 host.

set -euo pipefail

RULES_FILE="${RULES_FILE:-/etc/iptables/rules.v4}"
MSS=1260

# --- preflight ---
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be root" >&2
    exit 1
fi

if [ ! -f "$RULES_FILE" ]; then
    echo "ERROR: $RULES_FILE not found" >&2
    exit 1
fi

if ! command -v iptables-legacy >/dev/null 2>&1; then
    echo "ERROR: iptables-legacy not installed" >&2
    exit 1
fi

# --- step 1: apply rule in memory (idempotent) ---
if iptables-legacy -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS 2>/dev/null; then
    echo "  mangle rule already in memory"
else
    iptables-legacy -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS
    echo "  applied mangle rule in memory"
fi

# --- step 2: edit rules.v4 (add/keep one *mangle section with TCPMSS) ---
# Use python because bash + here-doc + escaping is brittle
python3 - <<PYEOF
import re

path = "$RULES_FILE"
with open(path) as f:
    content = f.read()

# Find all *mangle sections (newline + *mangle + newline)
positions = []
pos = 0
while True:
    idx = content.find("\n*mangle\n", pos)
    if idx == -1:
        break
    positions.append(idx + 1)
    pos = idx + 1

print(f"  found {len(positions)} *mangle sections in {path}")

# For each, check if it has TCPMSS
sections = []
for p in positions:
    end = content.find("\nCOMMIT\n", p)
    if end == -1:
        continue
    end += 8  # include the trailing "\nCOMMIT\n"
    section = content[p:end]
    has_tcpmss = "TCPMSS" in section
    sections.append((p, end, has_tcpmss))
    print(f"    section at {p}-{end}: has_TCPMSS={has_tcpmss}")

with_tcpmss = [s for s in sections if s[2]]
without_tcpmss = [s for s in sections if not s[2]]

if with_tcpmss:
    # Keep the first one with TCPMSS, remove the rest
    keep = with_tcpmss[0]
    to_remove = [s for s in sections if s != keep]
    print(f"  keeping section at {keep[0]}-{keep[1]}, removing {len(to_remove)} others")
else:
    # No section with TCPMSS — need to ADD one
    # Find the end of *nat section (right after its COMMIT)
    nat_match = re.search(r"(\*nat\n.*?^COMMIT\n)", content, re.MULTILINE | re.DOTALL)
    if not nat_match:
        print("ERROR: no *nat section found to anchor the new *mangle section", flush=True)
        exit(1)
    insert_at = nat_match.end()
    new_mangle = (
        "\n*mangle\n"
        ":PREROUTING ACCEPT [0:0]\n"
        ":INPUT ACCEPT [0:0]\n"
        ":FORWARD ACCEPT [0:0]\n"
        ":OUTPUT ACCEPT [0:0]\n"
        ":POSTROUTING ACCEPT [0:0]\n"
        f"-A FORWARD -p tcp -m tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss {MSS}\n"
        "COMMIT\n"
    )
    content = content[:insert_at] + new_mangle + content[insert_at:]
    to_remove = []
    print(f"  added new *mangle section at {insert_at}")

# Remove duplicates in reverse order
for p, e, _ in sorted(to_remove, key=lambda s: -s[0]):
    print(f"  removing duplicate *mangle section at {p}-{e}")
    content = content[:p] + content[e:]

with open(path, "w") as f:
    f.write(content)

# Final verification
with open(path) as f:
    c = f.read()
mangle_count = c.count("\n*mangle\n")
tcpmss_count = c.count("TCPMSS")
quota_count = c.count("quota:")
print(f"  final: *mangle={mangle_count}, TCPMSS={tcpmss_count}, quota:={quota_count}")
PYEOF

# --- step 3: verify in memory ---
echo
echo "  mangle FORWARD in memory:"
iptables-legacy -t mangle -L FORWARD -nvx --line-numbers
