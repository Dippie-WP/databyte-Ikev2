#!/usr/bin/env python3
"""
strongswan_exporter.py — Prometheus exporter for strongSwan IKE_SAs.

Scrapes the strongSwan container via `docker exec strongswan swanctl --list-sas`
every 15s, parses the output, and exposes metrics on http://0.0.0.0:9101/metrics

Metrics:
  strongswan_ikesas_total               — active IKE_SAs (gauge)
  strongswan_childsas_total             — active CHILD_SAs (gauge)
  strongswan_sa_bytes_in_total          — bytes received per SA (counter, labels: conn,remote_id,remote_addr,local_addr,virtual_ip)
  strongswan_sa_bytes_out_total         — bytes sent per SA (counter, same labels)
  strongswan_sa_packets_in_total        — packets in per SA (counter, same labels)
  strongswan_sa_packets_out_total       — packets out per SA (counter, same labels)
  strongswan_sa_uptime_seconds          — seconds since SA established (gauge, same labels)
  strongswan_sa_rekey_seconds           — seconds until next rekey (gauge, same labels)
  strongswan_scrape_errors_total        — counter of scrape failures
  strongswan_scrape_duration_seconds    — time taken for last scrape
  strongswan_loaded_plugins             — gauge (1 if plugin loaded, 0 if not)
  strongswan_charon_uptime_seconds      — charon process uptime (gauge)

Created 2026-06-19 (5A.9 implementation, Zun direction).
"""
import re
import subprocess
import time
from prometheus_client import start_http_server, Gauge, Counter, Info

LISTEN_PORT = 9101
SCRAPE_INTERVAL = 15
CONTAINER = "strongswan"
SWANCTL_URI = "tcp://127.0.0.1:4502"

# Metrics
g_ikesas = Gauge("strongswan_ikesas_total", "Number of active IKE_SAs")
g_childsas = Gauge("strongswan_childsas_total", "Number of active CHILD_SAs")
g_bytes_in = Gauge("strongswan_sa_bytes_in_total",
                   "Bytes received per CHILD_SA",
                   ["conn", "remote_id", "remote_addr", "local_addr", "virtual_ip"])
g_bytes_out = Gauge("strongswan_sa_bytes_out_total",
                    "Bytes sent per CHILD_SA",
                    ["conn", "remote_id", "remote_addr", "local_addr", "virtual_ip"])
g_packets_in = Gauge("strongswan_sa_packets_in_total",
                     "Packets received per CHILD_SA",
                     ["conn", "remote_id", "remote_addr", "local_addr", "virtual_ip"])
g_packets_out = Gauge("strongswan_sa_packets_out_total",
                      "Packets sent per CHILD_SA",
                      ["conn", "remote_id", "remote_addr", "local_addr", "virtual_ip"])
g_uptime = Gauge("strongswan_sa_uptime_seconds",
                 "Seconds since IKE_SA established",
                 ["conn", "remote_id", "remote_addr", "local_addr", "virtual_ip"])
g_rekey = Gauge("strongswan_sa_rekey_seconds",
                "Seconds until next rekey (if scheduled)",
                ["conn", "remote_id", "remote_addr", "local_addr", "virtual_ip"])
c_scrape_errors = Counter("strongswan_scrape_errors_total",
                          "Number of swanctl scrape failures")
g_scrape_duration = Gauge("strongswan_scrape_duration_seconds",
                          "Duration of last swanctl scrape")
g_charon_uptime = Gauge("strongswan_charon_uptime_seconds",
                        "Charon process uptime in seconds")
g_loaded_plugin = Gauge("strongswan_loaded_plugins",
                        "1 if plugin is loaded, 0 if not",
                        ["plugin"])


def run_docker_swanctl(args):
    """Run swanctl in the strongSwan container. Returns stdout or raises."""
    cmd = ["docker", "exec", CONTAINER, "swanctl", f"--uri={SWANCTL_URI}"] + args
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=10).decode("utf-8", errors="replace")


def parse_uptime(stats_output):
    """Parse 'uptime: 92 minutes, since Jun 19 08:20:12 2026' from --stats output."""
    m = re.search(r"uptime:\s+(\d+)\s+(seconds|minutes|hours|days)", stats_output)
    if not m:
        return 0
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}[unit]


def parse_loaded_plugins(stats_output):
    """Parse 'loaded plugins: charon random nonce ...' from --stats output."""
    m = re.search(r"loaded plugins:\s+(.+)", stats_output)
    if not m:
        return []
    return m.group(1).split()


def parse_sas(sas_output):
    """
    Parse swanctl --list-sas output.

    Example:
      rw-eap: #20, ESTABLISHED, IKEv2
        local  'vpn.homelab.local' @ 192.168.10.98[4500]
        remote '192.168.10.18' @ 100.76.235.33[1026] EAP: 'zun-windows' [10.99.0.4]
        AES_CBC-128/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
        net: #1, INSTALLED, TUNNEL-in-UDP, ESP:AES_CBC-128/HMAC_SHA2_256_128
          in  c24d39d9, 237899 bytes, 1229 packets
          out 323951a4, 338600 bytes,  672 packets
    """
    sas = []
    current = None
    for line in sas_output.splitlines():
        line = line.rstrip()
        # New SA header: e.g. "rw-eap: #20, ESTABLISHED, IKEv2"
        m = re.match(r"^([\w-]+):\s+#\d+,\s+(\w+),\s+IKEv2", line)
        if m:
            if current:
                sas.append(current)
            current = {
                "conn": m.group(1),
                "state": m.group(2),
                "remote_id": "",
                "remote_addr": "",
                "local_addr": "",
                "virtual_ip": "",
                "bytes_in": 0,
                "bytes_out": 0,
                "packets_in": 0,
                "packets_out": 0,
                "rekey_in": -1,
                "established": 0,
            }
            continue
        if current is None:
            continue
        # Remote line — two formats depending on auth method:
        #   EAP form:   remote 'X' @ IP[port] EAP: 'ID' [VIP]    (Windows MSCHAPv2)
        #   Plain form: remote 'ID' @ IP[port] [VIP]              (iPhone built-in IKEv2 PSK)
        # Try EAP form first; fall back to plain form for non-EAP SAs.
        m = re.match(r"^\s*remote\s+'\S+'\s+@\s+([\d.]+)\[\d+\]\s+EAP:\s+'([^']+)'(?:\s+\[([\d./]+)\])?", line)
        if m:
            current["remote_addr"] = m.group(1)
            current["remote_id"] = m.group(2)
            if m.group(3):
                current["virtual_ip"] = m.group(3)
            continue
        m = re.match(r"^\s*remote\s+'([^']+)'\s+@\s+([\d.]+)\[\d+\](?:\s+\[([\d./]+)\])?", line)
        if m:
            current["remote_id"] = m.group(1)
            current["remote_addr"] = m.group(2)
            if m.group(3):
                current["virtual_ip"] = m.group(3)
            continue
        # Local line: 'local 'X' @ IP[port]'
        m = re.match(r"^\s*local\s+'\S+'\s+@\s+([\d.]+)\[\d+\]", line)
        if m:
            current["local_addr"] = m.group(1)
            continue
        # Bytes in: 'in  c24d39d9, 237899 bytes, 1229 packets'
        m = re.match(r"^\s*in\s+\w+,\s+(\d+)\s+bytes,\s+(\d+)\s+packets", line)
        if m:
            current["bytes_in"] = int(m.group(1))
            current["packets_in"] = int(m.group(2))
            continue
        # Bytes out: 'out 323951a4, 338600 bytes,  672 packets'
        m = re.match(r"^\s*out\s+\w+,\s+(\d+)\s+bytes,\s+(\d+)\s+packets", line)
        if m:
            current["bytes_out"] = int(m.group(1))
            current["packets_out"] = int(m.group(2))
            continue
        # Rekey: 'scheduling rekeying in 82545s'
        m = re.search(r"scheduling rekeying in (\d+)s", line)
        if m:
            current["rekey_in"] = int(m.group(1))
            continue
        # Established: 'state change: CONNECTING => ESTABLISHED' (look at last seen)
        # We can't easily get a precise "established at" from list-sas, leave as 0.
    if current:
        sas.append(current)
    return sas


def scrape():
    """One scrape cycle. Updates all metrics."""
    t0 = time.monotonic()
    # 1) list-sas
    try:
        sas_output = run_docker_swanctl(["--list-sas"])
        sas = parse_sas(sas_output)
    except subprocess.CalledProcessError as e:
        c_scrape_errors.inc()
        print(f"[{time.strftime('%H:%M:%S')}] swanctl --list-sas failed: {e.output.decode()[:200]}", flush=True)
        g_ikesas.set(0)
        g_childsas.set(0)
        g_scrape_duration.set(time.monotonic() - t0)
        return
    except subprocess.TimeoutExpired:
        c_scrape_errors.inc()
        print(f"[{time.strftime('%H:%M:%S')}] swanctl --list-sas timeout", flush=True)
        g_ikesas.set(0)
        g_childsas.set(0)
        g_scrape_duration.set(time.monotonic() - t0)
        return
    except Exception as e:
        c_scrape_errors.inc()
        print(f"[{time.strftime('%H:%M:%S')}] swanctl --list-sas error: {e}", flush=True)
        g_ikesas.set(0)
        g_childsas.set(0)
        g_scrape_duration.set(time.monotonic() - t0)
        return

    g_ikesas.set(len(sas))
    g_childsas.set(len(sas))  # one CHILD_SA per IKE_SA in our config

    # Track label sets emitted this scrape so we can remove stale ones
    seen_labels = set()
    for sa in sas:
        labels = [
            sa["conn"],
            sa["remote_id"] or "unknown",
            sa["remote_addr"] or "unknown",
            sa["local_addr"] or "unknown",
            sa["virtual_ip"] or "none",
        ]
        labels_tuple = tuple(labels)
        seen_labels.add(labels_tuple)
        g_bytes_in.labels(*labels).set(sa["bytes_in"])
        g_bytes_out.labels(*labels).set(sa["bytes_out"])
        g_packets_in.labels(*labels).set(sa["packets_in"])
        g_packets_out.labels(*labels).set(sa["packets_out"])
        if sa["rekey_in"] >= 0:
            g_rekey.labels(*labels).set(sa["rekey_in"])

    # Remove label sets that didn't appear in this scrape (stale SAs)
    # We use _metrics internal dict to find existing label sets
    for gauge, name in [(g_bytes_in, "bytes_in"), (g_bytes_out, "bytes_out"),
                        (g_packets_in, "packets_in"), (g_packets_out, "packets_out"),
                        (g_rekey, "rekey")]:
        existing = set()
        # Find all label tuples currently in the gauge
        if hasattr(gauge, '_metrics'):
            for label_values in gauge._metrics.keys():
                # _metrics keys are the label values as a tuple
                existing.add(tuple(label_values))
        # Remove ones that weren't seen
        for label_tuple in (existing - seen_labels):
            try:
                gauge.remove(*label_tuple)
            except KeyError:
                pass

    # 2) stats (uptime + plugins)
    try:
        stats_output = run_docker_swanctl(["--stats"])
        g_charon_uptime.set(parse_uptime(stats_output))
        plugins = parse_loaded_plugins(stats_output)
        # Mark each known plugin 0 first, then 1 for loaded
        known_plugins = [
            "charon", "random", "nonce", "x509", "constraints", "pubkey", "pem",
            "openssl", "ml", "sqlite", "attr-sql", "kernel-netlink", "resolve",
            "socket-default", "vici", "updown", "eap-identity", "eap-md5",
            "eap-mschapv2", "eap-dynamic", "eap-radius", "eap-tls", "counters",
        ]
        for p in known_plugins:
            g_loaded_plugin.labels(p).set(1 if p in plugins else 0)
    except Exception as e:
        # stats is best-effort; don't bump scrape error counter
        print(f"[{time.strftime('%H:%M:%S')}] swanctl --stats error: {e}", flush=True)

    g_scrape_duration.set(time.monotonic() - t0)
    print(f"[{time.strftime('%H:%M:%S')}] scraped: {len(sas)} IKE_SAs, {time.monotonic()-t0:.2f}s", flush=True)


def main():
    print(f"strongswan_exporter starting on 0.0.0.0:{LISTEN_PORT}", flush=True)
    start_http_server(LISTEN_PORT)
    while True:
        scrape()
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
