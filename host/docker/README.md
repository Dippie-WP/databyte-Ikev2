# host/docker/

Docker daemon configuration for the VPN gateway host (LXC 903).

## daemon.json — v1.2.1 reboot fix

```json
{
  "iptables": false,
  "ip-forward": false,
  "bridge": "none"
}
```

### Why this is needed

On cold reboot, dockerd failed to start because its default bridge
network init triggered python-nftables to insert rules into firewalld
chains that weren't ready:

```
[ERROR] Error creating network "bridge" with driver "bridge":
Could not process rule: No such file or directory
```

Result: docker.service restart-loops for ~4 minutes, which means no
strongSwan container, which means no VPN. On the *next* cold reboot
it eventually settles, but you don't want 4 minutes of downtime on
every reboot.

### Why disabling iptables / bridge is safe

The strongSwan container is the only thing on this LXC, and it is run
with `network_mode: host` (see `host/strongswan/docker-compose.yml`).
It uses the host's network namespace directly. Docker's iptables NAT
rules, default bridge, and IP forwarding management are all
unnecessary — and they're what was racing with firewalld.

IP forwarding is set at the LXC host level via
`/etc/sysctl.d/99-strongswan.conf` and persists in iptables via
`netfilter-persistent` (rules loaded from `/etc/iptables/rules.v4`).

### Install

```bash
sudo cp host/docker/daemon.json /etc/docker/daemon.json
sudo systemctl restart docker
```

Or as part of the strongSwan install script.

### Verified

- 2026-06-20 10:26: cold reboot of LXC 903 with this daemon.json
  - docker up at +10s (was 4 min)
  - container charon bound UDP 500/4500 immediately
  - iPhone reconnected in 6 seconds
