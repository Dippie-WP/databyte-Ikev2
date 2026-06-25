"""
test_sa_parser.py — Tests for swanctl --list-sas and --list-pools --leases parsers.

Catches:
- _SA_REMOTE_RE regex matching both old format (no EAP) and new format
  (EAP-MSCHAPv2 injects "EAP: '<eap_id>'" between port and VIP)
- _parse_sas_text extracting VIP from new-format remote line
- _parse_sas_text setting remote_id = eap_id when EAP present (UI display)
- _parse_sas_text falling back to IKE identity (IP) for non-EAP conns
- _parse_pool_leases_text parsing all lease statuses + identities
- _parse_pool_leases_text handling empty input gracefully
- Bug 3 (2026-06-25): parser silently dropped VIP, broke SA enrichment
- Bug 2 (2026-06-25): pool lease parser was missing entirely

The new EAP-aware format appears in strongSwan 6.x output for any conn that
uses EAP auth methods (eap-mschapv2, eap-tls, etc.). The old format (no
EAP: injection) still appears for PSK-only conns (rw-psk fallback).
"""
import pytest


SAMPLE_SAS_NEW_FORMAT = """rw-eap: #29, ESTABLISHED, IKEv2, 50d4bc25d8bde9ee_i f5f1c3d7c1a4eb4c_r*
  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]
  remote '192.168.10.18' @ 102.182.117.43[4500] EAP: 'saalieg-laptop' [10.99.0.2]
  AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
  established 51s ago, rekeying in 77847s, reauth in 80663s
  net: #1, reqid 1, INSTALLED, TUNNEL-in-UDP, ESP:AES_CBC-128/HMAC_SHA2_256_128
    installed 51s ago, rekeying in 3238s, expires in 3909s
    in  c71f618a, 905137 bytes, 3663 packets, 0s ago
    out 636726a8, 7163834 bytes, 7199 packets, 0s ago
    local  0.0.0.0/0
    remote 10.99.0.2/32
"""

SAMPLE_SAS_OLD_FORMAT = """rw-eap: #22, ESTABLISHED, IKEv2, abc12345_i def67890_r*
  local  'myvpn.databyte.co.za' @ 154.65.110.44[4500]
  remote 'demo-phone' @ 105.174.188.166[51234] [10.99.0.5]
  AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
  established 614s ago, rekeying in 79344s, reauth in 78406s
  net: #3, reqid 1, INSTALLED, TUNNEL-in-UDP, ESP:AES_CBC-256/HMAC_SHA2_256_128
    installed 614s ago, rekeying in 2648s, expires in 3346s
    in  cbe261ee, 4199276 bytes, 52155 packets,     0s ago
    out 040b08d2, 128451591 bytes, 105627 packets,     0s ago
    local  0.0.0.0/0
    remote 10.99.0.5/32
"""

SAMPLE_POOL_LEASES = """rw-pool              10.99.0.1                           1 / 1 / 254
  10.99.0.1                      offline  'safwaan-laptop'
  10.99.0.2                      online   'saalieg-laptop'
rw-pool-2            10.100.0.1                          0 / 0 / 254
"""


class TestSASRemoteRegex:
    """Bug 3 — _SA_REMOTE_RE must match the EAP-injected format."""

    def test_old_format_no_eap(self, app_module):
        """Legacy format (pre-EAP-aware) still parses."""
        m = app_module._SA_REMOTE_RE.search(
            "remote 'demo-phone' @ 105.174.188.166[51234] [10.99.0.5]"
        )
        assert m is not None
        assert m.group("id") == "demo-phone"
        assert m.group("ip") == "105.174.188.166"
        assert m.group("port") == "51234"
        assert m.group("vip") == "10.99.0.5"
        assert m.group("eap_id") is None

    def test_new_format_with_eap(self, app_module):
        """strongSwan 6.x EAP format — the bug case."""
        m = app_module._SA_REMOTE_RE.search(
            "remote '192.168.10.18' @ 102.182.117.43[4500] EAP: 'saalieg-laptop' [10.99.0.2]"
        )
        assert m is not None, "BUG 3: regex did not match EAP-injected line"
        assert m.group("id") == "192.168.10.18"
        assert m.group("ip") == "102.182.117.43"
        assert m.group("port") == "4500"
        assert m.group("eap_id") == "saalieg-laptop"
        assert m.group("vip") == "10.99.0.2"

    def test_new_format_no_vip(self, app_module):
        """EAP line without VIP (no pool assignment yet)."""
        m = app_module._SA_REMOTE_RE.search(
            "remote 'foo' @ 1.2.3.4[4500] EAP: 'bar'"
        )
        assert m is not None
        assert m.group("eap_id") == "bar"
        assert m.group("vip") is None

    def test_bare_no_eap_no_vip(self, app_module):
        """Minimal line (defensive — PSK IKE_SA_INIT)."""
        m = app_module._SA_REMOTE_RE.search("remote 'foo' @ 1.2.3.4[4500]")
        assert m is not None
        assert m.group("eap_id") is None
        assert m.group("vip") is None


class TestParseSasText:
    """End-to-end: feed real-looking swanctl --list-sas output, check parsed dicts."""

    def test_new_format_extracts_vip_and_eap_id(self, app_module):
        sas = app_module._parse_sas_text(SAMPLE_SAS_NEW_FORMAT)
        assert len(sas) == 1
        sa = sas[0]
        assert sa["vip"] == "10.99.0.2", "BUG 3: VIP lost when EAP injected"
        assert sa["eap_id"] == "saalieg-laptop"
        # remote_id should be the EAP username (so UI shows saalieg-laptop, not the IP)
        assert sa["remote_id"] == "saalieg-laptop"
        assert sa["remote_ip"] == "102.182.117.43"
        assert sa["local_id"] == "myvpn.databyte.co.za"
        assert sa["state"] == "ESTABLISHED"

    def test_old_format_still_works(self, app_module):
        sas = app_module._parse_sas_text(SAMPLE_SAS_OLD_FORMAT)
        assert len(sas) == 1
        sa = sas[0]
        assert sa["vip"] == "10.99.0.5"
        assert sa["eap_id"] is None
        # No EAP, so remote_id falls back to the IKE identity (the user-provided ID)
        assert sa["remote_id"] == "demo-phone"

    def test_empty_input_returns_empty_list(self, app_module):
        assert app_module._parse_sas_text("") == []
        assert app_module._parse_sas_text("\n\n") == []

    def test_bytes_counters(self, app_module):
        sas = app_module._parse_sas_text(SAMPLE_SAS_NEW_FORMAT)
        sa = sas[0]
        assert sa["bytes_in"] == 905137
        assert sa["bytes_out"] == 7163834
        assert sa["pkts_in"] == 3663
        assert sa["pkts_out"] == 7199

    def test_established_secs(self, app_module):
        sas = app_module._parse_sas_text(SAMPLE_SAS_NEW_FORMAT)
        assert sas[0]["established_secs"] == 51


class TestParsePoolLeases:
    """Bug 2 — pool leases were never parsed; portal showed 0 active sessions."""

    def test_parses_both_leases(self, app_module):
        leases = app_module._parse_pool_leases_text(SAMPLE_POOL_LEASES)
        assert len(leases) == 2
        assert leases[0]["pool"] == "rw-pool"
        assert leases[0]["vip"] == "10.99.0.1"
        assert leases[0]["status"] == "offline"
        assert leases[0]["identity"] == "safwaan-laptop"
        assert leases[0]["online"] is False
        assert leases[1]["vip"] == "10.99.0.2"
        assert leases[1]["status"] == "online"
        assert leases[1]["online"] is True

    def test_handles_pool_with_no_leases(self, app_module):
        """rw-pool-2 has 0 leases — parser should not error."""
        leases = app_module._parse_pool_leases_text(SAMPLE_POOL_LEASES)
        # Only rw-pool has lease lines; rw-pool-2 contributes 0
        assert all(l["pool"] == "rw-pool" for l in leases)

    def test_empty_input(self, app_module):
        assert app_module._parse_pool_leases_text("") == []
        assert app_module._parse_pool_leases_text("\n\n") == []

    def test_unknown_status_skipped(self, app_module):
        """If charon adds a new status keyword we don't recognize, the lease
        is dropped (defensive — we only enumerate online/offline today)."""
        raw = "rw-pool              10.99.0.1                  1 / 1 / 254\n  10.99.0.1   unknown  'x'\n"
        leases = app_module._parse_pool_leases_text(raw)
        # 'unknown' is not in the regex, so the lease is silently skipped.
        # When charon adds a new status, we MUST update _POOL_LEASE_RE.
        assert leases == []


class TestLeasesActiveIntegration:
    """End-to-end: leases_active() joins pool leases with DB devices."""

    def test_leases_active_returns_pool_lease(
        self, app_module, db_path, monkeypatch
    ):
        """Mock swanctl_list_pool_leases to return a known lease; verify the
        join produces a lease dict in the right shape."""
        # Seed a device + customer in the test DB
        import sqlite3
        now = 1700000000
        conn = sqlite3.connect(str(db_path))
        conn.executescript(f"""
            INSERT INTO customers (name, display_name, is_operator, is_active,
                                   data_limit_bytes, tier_id, status, max_devices,
                                   created_at, updated_at, notes)
            VALUES ('saalieg', 'Saalieg', 0, 1, 104857600, NULL, 'active', 1,
                    {now}, {now}, 'seed for test');

            INSERT INTO devices (customer_id, strongswan_user_id, device_name,
                                 is_active, created_at, updated_at)
            VALUES (last_insert_rowid(), 1, 'saalieg-laptop', 1, {now}, {now});
        """)
        conn.commit()
        conn.close()

        # Mock swanctl_list_pool_leases to return a known lease
        def fake_list_pool_leases():
            return [{
                "pool": "rw-pool",
                "vip": "10.99.0.2",
                "status": "online",
                "identity": "saalieg-laptop",
                "online": True,
            }]
        monkeypatch.setattr(
            app_module, "swanctl_list_pool_leases", fake_list_pool_leases
        )

        # Mock SA parser to return empty (no extra enrichment)
        monkeypatch.setattr(app_module, "swanctl_parse_sas", lambda: [])

        leases = app_module.leases_active()
        assert len(leases) == 1
        lease = leases[0]
        assert lease["address"] == "10.99.0.2"
        assert lease["customer_name"] == "saalieg"
        assert lease["device_name"] == "saalieg-laptop"
        assert lease["identity_name"] == "saalieg-laptop"
        assert lease["online"] is True
        assert lease["pool"] == "rw-pool"

    def test_leases_active_empty_when_no_pool_leases(self, app_module, monkeypatch):
        """If charon has no leases, leases_active() returns []. Don't fall
        back to a DB query (which would be empty anyway)."""
        monkeypatch.setattr(app_module, "swanctl_list_pool_leases", lambda: [])
        leases = app_module.leases_active()
        assert leases == []
