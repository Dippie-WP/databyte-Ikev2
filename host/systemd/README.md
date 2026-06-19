# Host-side systemd units

These run on the LXC 903 host (vpn-gateway), NOT inside the strongSwan container.

## Install

After deploying the `quota/` scripts to `/home/zunaid/strongswan/quota/` on the LXC host:

```bash
# Copy units into systemd's search path
sudo cp host/systemd/quota-schema.service /etc/systemd/system/
sudo cp host/systemd/quota-monitor.service /etc/systemd/system/

# Reload systemd to pick up the new units
sudo systemctl daemon-reload

# Enable the schema unit (runs at host boot, idempotent — safe to re-run)
sudo systemctl enable --now quota-schema.service

# Verify
sudo systemctl status quota-schema.service
sudo journalctl -u quota-schema.service

# quota-monitor.service: enable only after quota-monitor.py is built (5B.3)
```

## quota-schema.service (Phase 5B.1)

**What it does:** runs `apply_quota_schema.sh` once at host boot. The script is
idempotent (uses `CREATE TABLE IF NOT EXISTS`), so re-running on a DB that
already has the quota tables is a no-op.

**When to re-run manually:**
- After restoring an older DB backup (e.g., disaster recovery)
- After `rm /var/lib/strongswan/ipsec.db` (DB is recreated empty on next charon start)
- After pulling a fresh strongSwan image and reinitializing

**Manual apply (one-off):**
```bash
ssh zunaid@192.168.10.98
sudo systemctl start quota-schema.service
# or directly:
bash /home/zunaid/strongswan/quota/apply_quota_schema.sh
```

## quota-monitor.service (Phase 5B.3)

**What it does (once quota-monitor.py is built):** long-running Python process
that:
1. Reads nftables byte counters per VIP
2. Joins VIP → strongSwan user → device → customer → tier
3. At 80% threshold: sends Telegram DM to operator + customer
4. At 100% threshold: terminates CHILD_SA via VICI, marks customer `over_quota=1`
5. Periodically (60s) checks for new SAs to re-evaluate

**Dependencies:** `quota-schema.service` (tables exist), `nftables-zun-vpn.service`
(counters in place), `strongswan.service` (VICI socket up).

**Until 5B.3:** the `ExecStart` points to a file that doesn't exist. Don't
`enable` this unit until quota-monitor.py is built.

## Known issue (NOT a 5B.2 deliverable, flagged for follow-up)

The live `/etc/iptables/rules.v4` on LXC 903 does NOT include the
`*mangle` section with the MSS clamp (`-A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260`)
from `host/iptables/rules.v4.template` (5A.7 fix). The template has it;
the live file doesn't. The 5B.2 install script did NOT add it back
(only the *filter FORWARD changes were in scope).

**Why this matters:** 5G PMTUD may be broken without the MSS clamp.
If clients on 5G/CGNAT start seeing TCP timeouts after a TLS handshake,
that's the symptom.

**How to re-apply (one-off, not 5B.2 scope):**
```bash
# On LXC 903 host
sudo iptables-legacy -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1260
sudo iptables-save | sudo tee /etc/iptables/rules.v4 > /dev/null
# Verify: iptables-legacy -t mangle -L FORWARD -nvx
```

**Better:** when the template is next re-deployed (`cp rules.v4.template /etc/iptables/rules.v4`),
this gap closes naturally. Just don't forget the mangle section.
